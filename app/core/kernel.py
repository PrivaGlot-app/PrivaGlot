"""
ConversationKernel — the headless heart of PrivaGlot.

Knows how a lesson runs: orchestration, greeting, the ASR→LLM→TTS turn
pipeline, vocabulary statistics, end-of-lesson evaluation, continuity
material, and disconnect finalization. Knows nothing about transports:
every public method is an async generator of contract events (see
events.py), and channels translate those into whatever their platform
speaks. Web channel: app/channels/web_ws.py · Feishu: app/channels/feishu/.

State machine: fresh → (load_student) → begin → active → end_session →
ended. begin() may fail the kernel (failed) — the channel decides what a
failed handshake means for its transport (web closes the socket).

Behavioral parity note: this module is a faithful extraction of the v3
websocket handler — same event order, same error messages, same fallback
policies. The only intended deltas: audio conversion runs off the event
loop, and submit_text() adds a text-entrance path for chat channels.
"""
import asyncio
import base64
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from ..course_engine import course_engine
from ..dialogue_logger import dialogue_logger
from ..engines import ASRError, TTSError, asr_engine, llm_engine, tts_engine
from ..evaluator import evaluate_lesson
from ..llm import (build_lesson_prompt, build_free_prompt,
                   build_free_opening_prompt, build_lesson_opening_prompt)
from ..logging_config import log as logger
from ..matcher import SessionMatcher
from ..prompt_logger import prompt_logger
from ..student import student_store
from ..teacher import teacher_store
from .. import config
from . import events
from .codecs import to_wav_16k
from .session import SessionContext

tts_executor = ThreadPoolExecutor(max_workers=2)


class StudentNotFoundError(Exception):
    pass


def log(module: str, msg: str):
    logger.info(f"[{module}] {msg}")


# ---------------------------------------------------------------------------
# Text cleaning utilities
# ---------------------------------------------------------------------------

def clean_asr_output(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<think>.*', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()


def clean_for_tts(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'[😀-🙏🌀-🗿🚀-🛿]+', '', text)
    return re.sub(r'\s+', ' ', text).strip()


# ---------------------------------------------------------------------------
# Visual-reference guard
# ---------------------------------------------------------------------------

VISUAL_REFERENCE_PATTERN = re.compile(
    r"\b(photo|photograph|picture|image)\b", re.IGNORECASE)


def _contains_visual_reference(text: str) -> bool:
    """True when a teacher line references picture-based content.

    The lesson is voice-only (unit block guidance); this deterministic check
    powers the one-shot regeneration guard in the turn pipeline so a
    picture-referencing draft never reaches the student unchallenged.
    """
    return bool(VISUAL_REFERENCE_PATTERN.search(text or ""))


# One-shot correction appended when a draft references pictures — shared by
# the turn pipeline and the lesson-mode greeting generator.
VISUAL_CORRECTION = (
    "\n\n[CORRECTION] Your previous draft referenced pictures, "
    "photos, or images. Rewrite the same teaching move for a "
    "voice-only lesson without any visual words.")


OPENING_USER_MESSAGE = "(Session starting — output your opening line now.)"


# ---------------------------------------------------------------------------
# Language guard (#46: students can talk the teacher out of the target
# language; the prompt's discipline rules are the first line of defense and
# this deterministic check is the second)
# ---------------------------------------------------------------------------

_SCRIPT_COUNT_CACHE = {}


def _script_counts(text: str) -> dict:
    """Letter counts per script name, from config.SCRIPT_RANGES."""
    counts = {}
    for name, cls in config.SCRIPT_RANGES.items():
        rx = _SCRIPT_COUNT_CACHE.get(name)
        if rx is None:
            rx = re.compile(f"[{cls}]")
            _SCRIPT_COUNT_CACHE[name] = rx
        n = len(rx.findall(text or ""))
        if n:
            counts[name] = n
    return counts


def _reply_off_target(text: str) -> bool:
    """True when the reply's dominant script is not the target language's.

    Language-agnostic by design (multi-language constitution): which scripts
    are legal comes from config.LANGUAGE_SCRIPTS, so a future Japanese or
    Spanish tutor is one table row, not new guard code. A short scaffold-
    language gloss never dominates a healthy reply, so mixed text with a
    target-language backbone passes; too few letters to judge passes too
    (fail-open — the guard must never block the class).
    """
    allowed = config.LANGUAGE_SCRIPTS.get(config.TARGET_LANGUAGE)
    if not allowed:
        return False
    counts = _script_counts(text)
    if not counts:
        return False
    top = max(counts, key=counts.get)
    if top in allowed:
        return False
    total = sum(counts.values())
    return counts[top] >= 4 and counts[top] / total >= 0.6


# One-shot correction appended when a reply drifts out of the target
# language — shared by the turn pipeline and the greeting generator.
LANGUAGE_CORRECTION = (
    f"\n\n[LANGUAGE GUARD] Your previous reply was mostly NOT in "
    f"{config.TARGET_LANGUAGE}. This is a {config.TARGET_LANGUAGE} practice "
    f"session: say that same reply again, in {config.TARGET_LANGUAGE} only. "
    f"Keep the warmth and the meaning, use very simple words, and end with "
    f"your question as usual. If the student cannot understand, simplify "
    f"your {config.TARGET_LANGUAGE} — never switch languages. At most one "
    f"short {config.SCAFFOLD_LANGUAGE} clause may appear; the backbone must "
    f"be {config.TARGET_LANGUAGE}.")


# ---------------------------------------------------------------------------
# Lesson finalization
# ---------------------------------------------------------------------------

def _is_real_session(ctx: SessionContext) -> bool:
    """Return True if the session qualifies as a real conversation.

    Empty sessions (0 student turns and <1 minute) are not sessions — they are
    failed connections, accidental starts, or user walked away before speaking.
    """
    turns = len([m for m in ctx.conversation_history if m.get("role") == "user"])
    duration_min = (datetime.now() - ctx.session_started_at).total_seconds() / 60
    if turns == 0 and duration_min < 1.0:
        log("SESSION", f"Empty session: {turns} turns, {duration_min:.1f}min — skipping")
        return False
    return True


def finalize_lesson(ctx: SessionContext) -> bool:
    """Record unit practice for a finished lesson. Returns False for empty sessions.

    The lesson mode's only profile write is course_progress (the ✅ status);
    the per-turn dialogue jsonl is the session's real archive. Both the normal
    end and the disconnect path go through here, so the two can't diverge.
    """
    if not (ctx.textbook and ctx.unit_id):
        return False
    if not _is_real_session(ctx):
        return False

    duration = (datetime.now() - ctx.session_started_at).total_seconds() / 60
    turns = len([m for m in ctx.conversation_history if m.get("role") == "user"])
    try:
        ctx.student_profile.record_unit_practice(ctx.textbook, ctx.unit_id,
                                                 duration, turns)
        log("SESSION", f"Lesson finalized: {ctx.textbook}/{ctx.unit_id}, "
                       f"{duration:.1f}min, {turns} turns")
    except Exception as e:
        log("SESSION", f"Failed to record unit practice: {e}")
        return False
    return True


async def _evaluate_on_disconnect(ctx: SessionContext):
    """Best-effort evaluation after an unexpected disconnect.

    The caller (channel) is gone, so the outcome can only be logged — there
    is no one left to alert. Per the agreed failure policy nothing is re-run
    later: an evaluation that fails here is simply lost.
    """
    try:
        result = await evaluate_lesson(ctx)
        fingerprint = course_engine.unit_fingerprint(ctx.textbook, ctx.unit_id)
        ctx.student_profile.save_unit_evaluation(
            ctx.textbook, ctx.unit_id, result["progress"],
            {"carry": result["carry"], "note": result["note"]}, fingerprint)
        log("SESSION", f"Disconnect evaluation saved: progress={result['progress']}, "
                       f"carry={len(result['carry'])}")
    except Exception as e:
        log("SESSION", f"Disconnect evaluation failed (progress not updated): {e}")


# ---------------------------------------------------------------------------
# Free Mode session analysis
# ---------------------------------------------------------------------------

async def analyze_free_session(conversation_history: list, ctx: SessionContext) -> dict:
    """Use LLM to analyze a free conversation session — extract interests and weaknesses."""
    dialogue = [
        f"{'Student' if m['role'] == 'user' else 'Teacher'}: {m['content']}"
        for m in conversation_history if m.get("role") in ("user", "assistant")
    ]
    dialogue_text = "\n".join(dialogue)

    current_interests = ", ".join(ctx.student_profile.interests) or "unknown"
    log("FREE_ANALYZE", f"Analyzing {len(dialogue)} messages, current interests: {current_interests}")

    prompt = f"""Analyze this free English conversation between a teacher and a student.

SESSION LOG:
{dialogue_text}

Student's known interests: {current_interests}

Your task:
1. Identify topics discussed in this session
2. Find any NEW interests the student mentioned (not already in their known interests)
3. Write a brief summary in Chinese

Return ONLY valid JSON:
{{
  "topics_discussed": ["topic1", "topic2"],
  "new_interests": ["new interest only if clearly expressed"],
  "summary": "1-2 sentence summary in Chinese describing how the conversation went"
}}"""

    try:
        log("FREE_ANALYZE", f"Calling LLM...")
        t0 = time.time()
        response = await llm_engine.generate(
            prompt,
            system_prompt="You are an English teaching session analyst. Return ONLY valid JSON.",
            max_tokens=2000,
        )
        log("FREE_ANALYZE", f"LLM responded in {time.time()-t0:.1f}s, {len(response)} chars")
        # The analysis call is invisible to the per-turn logger, so record it
        # here too (turn_number=0 marks the post-session analysis).
        prompt_logger.log_turn(
            session_id=ctx.session_id,
            turn_number=0,
            system_prompt="You are an English teaching session analyst. Return ONLY valid JSON.",
            history=[],
            user_input=prompt,
            response=response,
            metadata={
                "kind": "free_session_analysis",
                "student": ctx.student_profile.student_name,
                "mode": "free",
            },
        )
        start = response.find("{")
        end = response.rfind("}")
        if start != -1 and end != -1:
            result = json.loads(response[start:end + 1])
            log("FREE_ANALYZE", f"Parsed OK: {result.get('topics_discussed', [])}")
            return result
        else:
            log("FREE_ANALYZE", f"No JSON found: {response[:200]}")
    except Exception as e:
        log("FREE_ANALYZE", f"Analysis failed: {e}")

    return {
        "topics_discussed": [],
        "new_interests": [],
        "summary": "自由对话完成（分析失败）",
    }


async def _finish_free_session(ctx: SessionContext) -> dict:
    """Analyze a free session, update the profile, and return the summary payload."""
    duration = round((datetime.now() - ctx.session_started_at).total_seconds() / 60, 1)
    turns = len([m for m in ctx.conversation_history if m.get("role") == "user"])

    # Empty session (accidental start / instant disconnect): nothing to
    # analyze — don't burn an LLM call on silence.
    if not _is_real_session(ctx):
        log("FREE_ANALYZE", "Empty free session — skipping analysis")
        return {
            "type": events.EVENT_FREE_SESSION_COMPLETE,
            "message": "Free conversation ended!",
            "topics": [],
            "new_interests": [],
            "summary": "",
            "total_turns": turns,
            "duration_minutes": duration,
        }

    try:
        analysis = await asyncio.wait_for(
            analyze_free_session(ctx.conversation_history, ctx),
            timeout=45.0,
        )
    except (asyncio.TimeoutError, Exception) as e:
        log("FREE_ANALYZE", f"Analysis failed: {e}")
        analysis = {
            "topics_discussed": [],
            "new_interests": [],
            "summary": "自由对话完成",
        }

    try:
        ctx.student_profile.update_from_free_session(analysis)
    except Exception as e:
        log("FREE_ANALYZE", f"Profile update failed: {e}")

    return {
        "type": events.EVENT_FREE_SESSION_COMPLETE,
        "message": "Free conversation ended!",
        "topics": analysis.get("topics_discussed", []),
        "new_interests": analysis.get("new_interests", []),
        "summary": analysis.get("summary", ""),
        "total_turns": turns,
        "duration_minutes": duration,
    }


# ---------------------------------------------------------------------------
# The kernel
# ---------------------------------------------------------------------------

class ConversationKernel:
    """One student's live lesson. Channels own the lifecycle: construct →
    load_student → begin → submit_audio/submit_text×N → end_session (or
    close() on disconnect)."""

    def __init__(self):
        self.student_id: str = ""
        self.ctx: SessionContext | None = None
        # fresh → active → ended; fresh/active → failed on handshake errors
        self.state = "fresh"

    # ---- lifecycle ----

    def load_student(self, student_id: str) -> dict:
        """Load the profile and resolve this student's teacher.

        Returns the student_info event payload; raises StudentNotFoundError
        for an unknown id. Anything else (e.g. no teacher profiles at all)
        propagates to the channel — the kernel cannot teach without one.
        """
        try:
            profile = student_store.get_profile(student_id)
            profile.load()
        except Exception:
            raise StudentNotFoundError(student_id)

        teacher, style_id, self._teacher_notice = \
            teacher_store.resolve_for_student(student_id)
        self.student_id = student_id
        self.ctx = SessionContext(profile, teacher, style_id)
        return events.student_info(profile.student_name, teacher.name)

    async def begin(self, textbook: str = "", unit_id: str = "",
                    mode: str = "lesson", teacher_id: str = "",
                    style: str = ""):
        """Start the session and yield setup events plus the greeting."""
        ctx = self.ctx
        if self.state != "fresh" or ctx is None:
            yield events.error("Session has already been started.")
            return

        if mode == "free":
            # === FREE CONVERSATION MODE (fully decoupled from the curriculum) ===
            ctx.mode = "free"
            log("CONN", "Starting FREE CONVERSATION mode")
        elif textbook and unit_id:
            # Bind a specific unit to this session only
            if not course_engine.get_unit(textbook, unit_id):
                self.state = "failed"
                yield events.error("Unit not found")
                return
            ctx.bind_unit(textbook, unit_id)
            # 续学：载入这个学生在这个单元上的 AI 维护材料。教材内容被
            # 编辑过（指纹不符）就作废，重新从全集学起。
            fingerprint = course_engine.unit_fingerprint(textbook, unit_id)
            stored = ctx.student_profile.get_unit_progress(textbook, unit_id)
            if isinstance(stored.get("material"), dict):
                if stored.get("material_fingerprint") == fingerprint:
                    ctx.material = stored["material"]
                    log("SESSION", f"Living material loaded: "
                                   f"carry={len(ctx.material.get('carry') or [])}")
                else:
                    ctx.student_profile.clear_unit_material(textbook, unit_id)
                    log("SESSION", "Unit content changed since the last evaluation — "
                                   "material discarded, teaching the full unit")
        else:
            self.state = "failed"
            yield events.error("No unit selected")
            return

        # The channel carries its teacher/style pick on begin. Persist it so
        # the next session starts the same way — that is the whole point of
        # the memory. Switching teacher resets the style to that teacher's
        # own default (the store enforces it).
        if teacher_id:
            teacher_store.remember_choice(self.student_id, teacher_id, style)
            resolved_t, resolved_s, self._teacher_notice = \
                teacher_store.resolve_for_student(self.student_id)
            ctx.bind_teacher(resolved_t, resolved_s)

        # Common post-setup
        yield events.session_started()

        # 教师目录跨厂商回落提示（#15：切语音厂商须重选老师）。error 事件
        # 不进 TTS、只作文字提示。
        notice = getattr(self, "_teacher_notice", "")
        if notice:
            yield events.error(notice)

        if ctx.mode == "lesson":
            ctx.init_matcher()
            yield events.lesson_targets(ctx.unit_vocabulary())

        yield events.session_info(
            textbook=ctx.textbook or "",
            unit_id=ctx.unit_id or "",
            unit_title=ctx.unit_title() or ("Free Conversation" if ctx.mode == "free" else ""),
        )

        # Greeting. The name intro is fixed and goes out immediately (text +
        # audio) so the student hears the teacher without waiting; the rest is
        # LLM-improvised for both modes and generated in parallel with the
        # intro audio. Both fall back to their old fixed lines when no API key
        # is set or the call fails.
        profile = ctx.student_profile
        teacher_name = ctx.teacher.name
        intro = f"Hi {profile.student_name}! I'm {teacher_name}."
        if ctx.mode == "free":
            fallback_question = ("So tell me — what's new with "
                                 f"{profile.interests[-1]}?"
                                 if profile.interests else
                                 "What do you want to talk about today?")
            system_prompt = build_free_opening_prompt(
                ctx.student_profile, ctx.teacher, ctx.style)
        else:
            unit_block = ""
            if ctx.textbook and ctx.unit_id:
                unit_block = course_engine.build_unit_content_block(
                    ctx.textbook, ctx.unit_id, ctx.material)
            fallback_question = (
                f"Today we're talking about {ctx.unit_title()}. Are you ready?"
                if ctx.unit_title() else "Are you ready?")
            system_prompt = build_lesson_opening_prompt(
                ctx.student_profile, unit_block, ctx.teacher, ctx.style)
        opening_task = asyncio.create_task(
            _opening_question(ctx, system_prompt, fallback_question, ctx.mode))

        log("PIPE", f"Greeting intro: '{intro}'")
        yield events.llm_token(intro)
        ev = await self._tts_audio_event(tts_engine, intro)
        if ev:
            yield ev

        question = await opening_task

        greeting = f"{intro} {question}"
        log("PIPE", f"Greeting: '{greeting}'")
        # Leading space joins the question onto the intro in the same bubble.
        yield events.llm_token(f" {question}")
        yield events.llm_done()
        ev = await self._tts_audio_event(tts_engine, question)
        if ev:
            yield ev
        ctx.conversation_history.append({"role": "assistant", "content": greeting})

        self.state = "active"

    # ---- turns ----

    async def submit_audio(self, audio_bytes: bytes, ext: str = ".webm"):
        """One spoken turn: normalize → ASR → respond."""
        if self.state != "active":
            yield events.error(
                "Session has ended." if self.state == "ended"
                else "Session has not started.")
            return

        t0 = time.time()
        ctx = self.ctx

        # Step 1: normalize to WAV
        log("PIPE", f"Step 1: Converting audio ({len(audio_bytes)} bytes)...")
        try:
            wav_bytes = await to_wav_16k(audio_bytes, ext)
            wav_b64 = base64.b64encode(wav_bytes).decode("utf-8")
            log("PIPE", f"Step 1 OK: WAV {len(wav_bytes)} bytes ({time.time()-t0:.1f}s)")
        except Exception as e:
            log("PIPE", f"Step 1 FAILED: {e}")
            # to_wav_16k raises RuntimeError with text meant for the user;
            # anything else stays generic so we never leak a traceback into
            # the chat area.
            detail = str(e) if isinstance(e, RuntimeError) else "录音转换失败，详情见服务端日志。"
            yield events.error(f"🎤 {detail}")
            return

        # Step 2: ASR
        log("PIPE", f"Step 2: ASR...")
        try:
            # 课中把单元词汇喂给支持热词的识别模型（#15 Phase 3：智谱
            # hotwords ≤100），缓解形似词误转（#3 观察项）。free 模式无词表。
            try:
                hotwords = list(ctx.unit_vocabulary()) if ctx.mode == "lesson" else []
            except Exception:
                hotwords = []
            try:
                transcription = asr_engine.transcribe(
                    wav_b64, "audio/wav", "auto", hotwords=hotwords)
            except ASRError as e:
                log("PIPE", f"Step 2 FAILED: {e}")
                yield events.error(str(e))
                return
            raw = transcription
            transcription = clean_asr_output(transcription)
            if raw != transcription:
                log("PIPE", f"  Cleaned: '{raw[:50]}' → '{transcription}'")
            log("PIPE", f"Step 2 OK: '{transcription}' ({time.time()-t0:.1f}s)")

            if not transcription.strip():
                repeat = "Sorry, I didn't hear that clearly. Can you say it again?"
                yield events.llm_token(repeat)
                yield events.llm_done()
                ev = await self._tts_audio_event(tts_engine, repeat)
                if ev:
                    yield ev
                return

            yield events.transcription(transcription)

            # Local vocabulary statistics (lesson mode only, display-only)
            if ctx.mode == "lesson" and ctx.session_matcher:
                hit_result = ctx.session_matcher.check_turn(transcription)
                yield events.vocab_hit(
                    turn=ctx.session_matcher.total_turns,
                    student_text=transcription,
                    vocab_hits=hit_result["turn_vocab_hits"],
                    cumulative_vocab_hits=hit_result["cumulative_vocab_hits"],
                    cumulative_vocab_missed=hit_result["cumulative_vocab_missed"],
                    accuracy=hit_result["accuracy"],
                )
                log("MATCHER", f"Turn {ctx.session_matcher.total_turns}: "
                    f"vocab={hit_result['turn_vocab_hits']}")

        except Exception as e:
            log("PIPE", f"Step 2 FAILED: {e}")
            yield events.error(f"ASR error: {str(e)[:50]}")
            return

        async for ev in self._respond(transcription, asr_raw=raw):
            yield ev

    async def submit_text(self, text: str):
        """One typed turn (chat channels): the text stands in for the
        transcript and goes straight to the teacher pipeline."""
        if self.state != "active":
            yield events.error(
                "Session has ended." if self.state == "ended"
                else "Session has not started.")
            return

        transcription = clean_asr_output(text or "").strip()
        if not transcription:
            return
        ctx = self.ctx
        yield events.transcription(transcription)
        if ctx.mode == "lesson" and ctx.session_matcher:
            hit_result = ctx.session_matcher.check_turn(transcription)
            yield events.vocab_hit(
                turn=ctx.session_matcher.total_turns,
                student_text=transcription,
                vocab_hits=hit_result["turn_vocab_hits"],
                cumulative_vocab_hits=hit_result["cumulative_vocab_hits"],
                cumulative_vocab_missed=hit_result["cumulative_vocab_missed"],
                accuracy=hit_result["accuracy"],
            )
        async for ev in self._respond(transcription, asr_raw=""):
            yield ev

    async def _respond(self, transcription: str, asr_raw: str = ""):
        """Steps 3–5 of the pipeline: prompt build, streaming LLM (+ visual
        guard), per-sentence TTS, history and archive writes."""
        ctx = self.ctx

        # Step 3: Build LLM prompt
        try:
            if ctx.mode == "free":
                system_prompt = build_free_prompt(
                    ctx.student_profile, ctx.teacher, ctx.style)
            else:
                unit_block = ""
                if ctx.textbook and ctx.unit_id:
                    unit_block = course_engine.build_unit_content_block(
                        ctx.textbook, ctx.unit_id, ctx.material)
                system_prompt = build_lesson_prompt(
                    ctx.student_profile, unit_block, ctx.teacher, ctx.style)
                # Lightweight vocabulary reminder: words not yet used this session.
                # Pure guidance for the teacher — never a completion condition.
                if ctx.session_matcher:
                    used = set(ctx.session_matcher.cumulative_vocab_hits)
                    missing = [w for w in ctx.unit_vocabulary() if w not in used]
                    if missing:
                        system_prompt += (
                            "\n\n[WORDS NOT USED YET] Weave some of these into your "
                            "questions naturally — do not quiz them directly: "
                            + ", ".join(missing))
            log("PIPE", f"Step 3: Prompt built ({len(system_prompt)} chars)")
        except Exception as e:
            log("PIPE", f"Step 3 FAILED: {e}")
            system_prompt = None

        # Step 4: Streaming LLM + TTS
        log("PIPE", f"Step 4: LLM stream (transcription: '{transcription[:50]}')...")
        try:
            full_response = ""
            token_count = 0
            max_tokens = config.LLM_MAX_TOKENS_PER_TURN

            stream = llm_engine.generate_stream(
                transcription, ctx.conversation_history, system_prompt=system_prompt
            )
            async for token in stream:
                full_response += token
                token_count += 1
                if token_count <= 5 or token_count % 10 == 0:
                    log("PIPE", f"  → token #{token_count}: {repr(token)}")
                yield events.llm_token(token)

                if token_count >= max_tokens:
                    log("PIPE", f"LLM hit token limit ({max_tokens}) — truncating")
                    break

            # Visual-reference guard: regenerate once when the draft references
            # pictures. The original response is kept when the retry also
            # violates or the API fails — the guard never blocks the class.
            if (ctx.mode == "lesson"
                    and _contains_visual_reference(full_response)):
                retry_prompt = (system_prompt or "") + VISUAL_CORRECTION
                corrected = (await llm_engine.generate(
                    transcription, ctx.conversation_history,
                    system_prompt=retry_prompt,
                    max_tokens=config.LLM_MAX_TOKENS_PER_TURN,
                ) or "").strip()
                if (corrected and "⚠️" not in corrected
                        and not _contains_visual_reference(corrected)):
                    log("PIPE", "Step 4b: visual-reference guard rewrote response")
                    yield events.llm_cleaned(corrected)
                    full_response = corrected
                else:
                    log("PIPE",
                        "Step 4b: visual-reference guard retry failed — keeping "
                        "original response")

            # Language guard (#46): regenerate once when the reply's dominant
            # script left the target language (e.g. the student talked the
            # teacher into the scaffold language). One retry, never blocks
            # the class; a valid retry is adopted even if it still misses
            # the target — no loops (2026-10-23 ruling). The rewrite lands
            # before TTS and the history write, so a drifted reply never
            # reaches the student's ears or poisons the context.
            if ("⚠️" not in full_response
                    and _reply_off_target(full_response)):
                retry_prompt = (system_prompt or "") + LANGUAGE_CORRECTION
                corrected = (await llm_engine.generate(
                    transcription, ctx.conversation_history,
                    system_prompt=retry_prompt,
                    max_tokens=config.LLM_MAX_TOKENS_PER_TURN,
                ) or "").strip()
                if corrected and "⚠️" not in corrected:
                    log("PIPE", "Step 4c: language guard rewrote response")
                    yield events.llm_cleaned(corrected)
                    full_response = corrected
                else:
                    log("PIPE",
                        "Step 4c: language guard retry failed — keeping "
                        "original response")

            yield events.llm_done()
            log("PIPE", f"Step 4: LLM done ({token_count} tokens, {len(full_response)} chars)")
            log("PIPE", f"  → full response: '{full_response}'")

            # Debug prompt logging (only when DEBUG_PROMPTS=1)
            prompt_logger.log_turn(
                session_id=ctx.session_id,
                turn_number=len([m for m in ctx.conversation_history if m.get("role") == "user"]) + 1,
                system_prompt=system_prompt,
                history=ctx.conversation_history,
                user_input=transcription,
                response=full_response,
                metadata={
                    "student": ctx.student_profile.student_name,
                    "unit": f"{ctx.textbook}/{ctx.unit_id}" if ctx.textbook else "",
                    "mode": ctx.mode,
                },
            )

            # Step 5: TTS sentence by sentence
            sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', full_response) if s.strip()]
            for sentence in sentences:
                ev = await self._tts_audio_event(tts_engine, sentence)
                if ev:
                    yield ev
                    if ev.get("type") == events.EVENT_ERROR:
                        # TTS 故障直接报错（2026-10-21 裁定）：停止本回合剩余朗读，
                        # 不降级为纯文字、不静默跳句
                        break
            log("PIPE", f"Step 5 OK: {len(sentences)} sentences")

            # Record in conversation history
            ctx.conversation_history.append({"role": "user", "content": transcription})
            ctx.conversation_history.append({"role": "assistant", "content": full_response})

            # Persist this turn as a permanent teaching archive
            dialogue_logger.log_turn(
                student_name=ctx.student_profile.student_name,
                session_id=ctx.session_id,
                unit_id=ctx.unit_id or ("free" if ctx.mode == "free" else ""),
                turn_number=len([m for m in ctx.conversation_history if m.get("role") == "user"]),
                student_text=transcription,
                asr_raw=asr_raw,
                teacher_text=full_response,
            )

        except Exception as e:
            log("PIPE", f"Step 4/5 FAILED: {e}")
            yield events.error(f"LLM error: {str(e)[:50]}")

    async def _tts_audio_event(self, tts, text: str):
        """TTS → tts_audio event, in the session's voice and delivery style.
        文本清洗后为空 = 本回合没有语音内容 → None（网页端 5 秒后展开原文，
        #45 行为保留）；合成失败 = 直接报错（2026-10-21 裁定，推翻 #24 的
        静默降级为文字）——话术已由引擎方言层渲染成人话。"""
        clean = clean_for_tts(text)
        if not clean:
            return None
        try:
            loop = asyncio.get_event_loop()
            audio_bytes = await loop.run_in_executor(
                tts_executor, tts.synthesize, clean,
                self.ctx.teacher.voice if self.ctx.teacher else None,
                self.ctx.style)
            return events.tts_audio(audio_bytes)
        except TTSError as e:
            log("TTS", f"Failed: {e}")
            return events.error(str(e))
        except Exception as e:
            log("TTS", f"Failed: {e}")
            return events.error("⚠️ 语音合成失败，本句没能读出来。请稍后再试；"
                                "若持续失败，请检查语音模型（网页端在模型"
                                "配置页，飞书端发「配置」）。")

    # ---- ending ----

    async def end_session(self):
        """Close the session and yield the summary/evaluation events."""
        ctx = self.ctx
        if ctx is None or self.state != "active":
            if self.state == "ended":
                log("SESSION", "session_ended was already True — skipping")
            return

        log("SESSION", f"=== END_SESSION START === mode={ctx.mode}")
        self.state = "ended"
        ctx.session_ended = True

        if ctx.mode == "free":
            summary_msg = await _finish_free_session(ctx)
            log("SESSION", f"Sending completion: {summary_msg.get('type')}")
            yield summary_msg
            log("SESSION", "=== END_SESSION DONE ===")
            return

        # Lesson mode: the local vocabulary report goes out first, computed
        # from hit statistics — no LLM verdict gates it. The evaluator runs
        # afterwards and its verdict arrives as a separate message.
        if ctx.session_matcher:
            report = ctx.session_matcher.get_final_report()
        else:
            report = {"vocabulary_used": [], "vocabulary_missed": [],
                      "accuracy": 0.0}
        finalized = finalize_lesson(ctx)

        duration = round((datetime.now() - ctx.session_started_at).total_seconds() / 60, 1)
        turns = len([m for m in ctx.conversation_history if m.get("role") == "user"])
        # 只要 finalize 判定为真实课程就评估——学一轮也算学，学生随时退出
        # 都该在选课页看到一个 AI 给出的进度
        will_evaluate = finalized
        summary_msg = events.lesson_complete(
            message="Lesson ended!",
            accuracy=report.get("accuracy", 0.0),
            vocab_used=report.get("vocabulary_used", []),
            vocab_missed=report.get("vocabulary_missed", []),
            total_turns=turns,
            duration_minutes=duration,
            eval_="pending" if will_evaluate else "none",
        )
        log("SESSION", f"Sending completion: {summary_msg.get('type')}")
        yield summary_msg

        if summary_msg.get("eval") == "pending":
            # The instant report reached the student first; the evaluation
            # lands whenever it lands. Save order matters: the profile is
            # written BEFORE the result message, so a channel that vanishes
            # mid-eval still keeps its progress. On final failure the user is
            # alerted loudly and this lesson's progress update is simply lost.
            try:
                result = await evaluate_lesson(ctx)
            except Exception as e:
                # 不只是评估的"业务失败"（重试耗尽/额度问题）——任何崩溃都必须
                # 变成大声警告，而不是把连接无声杀掉
                log("SESSION", f"Lesson evaluation failed: {e}")
                yield events.lesson_eval_failed(
                    "本课的进度评估未能完成，这次的学习进度没有保存。"
                    "课程本身已完成；请检查网络与 API 余额后正常继续，"
                    "下次课会从上一次保存的进度开始。")
                log("SESSION", "=== END_SESSION DONE ===")
                return

            fingerprint = course_engine.unit_fingerprint(ctx.textbook, ctx.unit_id)
            try:
                ctx.student_profile.save_unit_evaluation(
                    ctx.textbook, ctx.unit_id, result["progress"],
                    {"carry": result["carry"], "note": result["note"]}, fingerprint)
            except Exception as e:
                log("SESSION", f"Failed to save the evaluation: {e}")
                yield events.lesson_eval_failed(
                    "进度评估完成，但写入学生档案失败，请检查磁盘与权限。")
                log("SESSION", "=== END_SESSION DONE ===")
                return

            log("SESSION", f"Lesson evaluation saved: progress={result['progress']}, "
                           f"carry={len(result['carry'])}")
            yield events.lesson_eval_result(
                progress=result["progress"],
                note=result["note"],
                carry_count=len(result["carry"]),
            )
        log("SESSION", "=== END_SESSION DONE ===")

    async def close(self):
        """The channel is going away without an end_session (tab closed,
        process killed). Best-effort finalize + evaluation; outcomes can only
        be logged — no one is left to alert."""
        ctx = self.ctx
        if not (ctx and ctx.conversation_history and not ctx.session_ended):
            return
        if ctx.mode == "free":
            try:
                await _finish_free_session(ctx)
                log("SESSION", "Free mode disconnect: analyzed and saved")
            except Exception as e:
                log("SESSION", f"Free mode disconnect analysis failed: {e}")
            return
        finalized = finalize_lesson(ctx)
        # 断线课也评估（孩子关网页是最常见的下课方式）。结果只能落日志——
        # 这是"不补跑"决策的直接后果。空课（finalize=False）不评估。
        if finalized and ctx.mode == "lesson" and ctx.textbook and ctx.unit_id:
            try:
                await _evaluate_on_disconnect(ctx)
            except Exception as e:
                log("SESSION", f"Disconnect evaluation could not start: {e}")


async def _opening_question(ctx: SessionContext, system_prompt: str,
                            fallback: str, mode: str) -> str:
    """LLM-improvised greeting question (shared by free and lesson mode).

    The fixed name intro is already on its way to the student while this
    runs; only the question half of the greeting is generated. Falls back to
    the fixed line when no API key is set, the call fails, or the model
    returns nothing usable — the greeting must never block the session.
    """
    if not llm_engine.api_key:
        return fallback
    try:
        text = (await llm_engine.generate(
            OPENING_USER_MESSAGE,
            system_prompt=system_prompt,
            max_tokens=100,  # an opening line is 1-3 short sentences
        ) or "").strip()
    except Exception as e:
        log("LLM", f"{mode} opening generation failed: {e}")
        return fallback
    if not text or "⚠️" in text:
        log("LLM", f"{mode} opening unusable — using fallback greeting")
        return fallback
    # Lesson material may describe pictures; the class is voice-only, so run
    # the same one-shot correction as the turn pipeline and fall back if the
    # rewrite still mentions them.
    if mode == "lesson" and _contains_visual_reference(text):
        retry_prompt = system_prompt + VISUAL_CORRECTION
        try:
            corrected = (await llm_engine.generate(
                OPENING_USER_MESSAGE,
                system_prompt=retry_prompt,
                max_tokens=100,
            ) or "").strip()
        except Exception as e:
            log("LLM", f"{mode} opening rewrite failed: {e}")
            corrected = ""
        if (corrected and "⚠️" not in corrected
                and not _contains_visual_reference(corrected)):
            text = corrected
            system_prompt = retry_prompt
        else:
            log("LLM", f"{mode} opening still references visuals — fallback")
            return fallback
    # #46: the improvised opening must speak the target language too. One
    # retry; a retry that still misses (or fails) falls back to the fixed
    # greeting — the session must never open in the wrong language.
    if _reply_off_target(text):
        retry_prompt = system_prompt + LANGUAGE_CORRECTION
        try:
            corrected = (await llm_engine.generate(
                OPENING_USER_MESSAGE,
                system_prompt=retry_prompt,
                max_tokens=100,
            ) or "").strip()
        except Exception as e:
            log("LLM", f"{mode} opening language rewrite failed: {e}")
            corrected = ""
        if (corrected and "⚠️" not in corrected
                and not _reply_off_target(corrected)):
            text = corrected
            system_prompt = retry_prompt
        else:
            log("LLM", f"{mode} opening off-target — fallback greeting")
            return fallback
    # Models occasionally wrap the line in quotes despite the instruction.
    if len(text) >= 2 and text[0] in "\"'“”" and text[-1] in "\"'“”":
        text = text[1:-1].strip()
    if not text:
        return fallback
    prompt_logger.log_turn(
        session_id=ctx.session_id,
        turn_number=0,  # greeting — before the first real turn
        system_prompt=system_prompt,
        history=[],
        user_input=f"({mode} opening generation)",
        response=text,
        metadata={"student": ctx.student_profile.student_name, "mode": mode},
    )
    return text
