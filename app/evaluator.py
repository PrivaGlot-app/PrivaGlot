"""
Lesson Evaluator — the AI that maintains each student's living material.

After a lesson ends, one LLM call judges the transcript against the full
unit and decides what the NEXT session on this unit practices: for every
unit item, "carry" (part of the next material) or "drop" (rests this time).
Plus a 0-100 progress number and a parent-facing Chinese note. The verdict
is stored in the student profile and bound into the next session's prompt
by CourseEngine.build_unit_content_block.

Division of labor (agreed design):
  - The LLM owns the teaching judgment, as a black box. It may re-carry
    items the student already used correctly, drop items it deems solid,
    and re-open dropped ones later — "练过的，下次可能还出现；没练过的，
    下次肯定出现". Local code never judges content.
  - Local code validates format only: parseable JSON, item ids that
    reference the real unit, progress clamped to 0-100. Two mechanical
    guarantees back the owner's own rules:
      * items the evaluator failed to mention default to "carry" — omission
        can never silently shrink the material;
      * a vocabulary item that was in this session's material but never
        showed up in the student's own speech (matcher stats) is
        force-carried.
  - Failure policy: up to 3 attempts; auth/quota errors fail fast without
    retry; on final failure the caller alerts the user and the lesson's
    progress update is lost — no fallback state is invented, nothing is
    re-run later.

Item ids ("v0" / "p2" / "g1") come from course_engine.unit_item_text and
stay anchored to the original unit, so decisions remain addressable across
generations instead of drifting through re-typed prose.
"""
import asyncio
import json
import re

from .course_engine import course_engine, unit_item_text
from .llm import llm_engine
from .logging_config import log as logger

MAX_ATTEMPTS = 3
MAX_TOKENS = 3000
TEMPERATURE = 0.2

# 401/403 = key problem, 402 = out of balance — retrying an unpaid API
# achieves nothing, so these fail fast into the caller's alert path.
_AUTH_ERROR_MARKERS = ("401", "402", "403")

EVALUATOR_SYSTEM_PROMPT = (
    "You are the course evaluator of an English tutoring system for children. "
    "You judge what a student should practice next. Return ONLY valid JSON."
)

_ITEM_ID_RE = re.compile(r"[vpg]\d+")


class EvaluationError(Exception):
    """All retries spent without a format-valid evaluation."""


class _Invalid(Exception):
    """One reply failed format validation — worth one more attempt."""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _render_unit_with_ids(unit: dict) -> str:
    """Every item of the unit with its stable id, for the evaluator to judge."""
    lines = []
    for key, label, prefix in (("vocabulary", "Vocabulary", "v"),
                               ("patterns", "Sentence patterns", "p"),
                               ("grammar", "Grammar focus", "g")):
        items = unit.get(key) or []
        if not items:
            continue
        lines.append(f"{label}:")
        for i, item in enumerate(items):
            lines.append(f'  {prefix}{i}: "{item}"' if key == "patterns"
                         else f"  {prefix}{i}: {item}")
    return "\n".join(lines)


def _render_current_state(material: dict | None, prev_progress) -> str:
    if material and material.get("carry"):
        lines = ["This session's material (from the previous evaluation):"]
        for entry in material["carry"]:
            item_id = str(entry.get("id") or "")
            why = str(entry.get("why") or "").strip()
            lines.append(f"  {item_id}" + (f" (focus: {why})" if why else ""))
        note = str(material.get("note") or "").strip()
        if note:
            lines.append(f"  Teacher's note: {note}")
    elif material:
        lines = ["This was a consolidation session — every item had been judged "
                 "well practiced, so no fixed material was assigned."]
    else:
        lines = ["This was the student's FIRST session on this unit — the "
                 "material was the full unit."]
    if isinstance(prev_progress, int):
        lines.append(f"Last recorded progress: {prev_progress}/100.")
    else:
        lines.append("There is no recorded progress yet (first evaluation).")
    return "\n".join(lines)


def _render_transcript(history: list) -> str:
    return "\n".join(
        f"{'Student' if m.get('role') == 'user' else 'Teacher'}: {m.get('content', '')}"
        for m in history if m.get("role") in ("user", "assistant"))


def build_evaluation_prompt(student_name: str, student_age: int, unit: dict,
                            material: dict | None, prev_progress,
                            missed_vocab: list[str],
                            transcript: str) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for the evaluation call."""
    prev_line = (f"Last recorded progress: {prev_progress}/100."
                 if isinstance(prev_progress, int)
                 else "There is no recorded progress yet (first evaluation).")
    missed_line = "" if not missed_vocab else (
        "\nVocabulary the student NEVER used in their own words this session "
        "(these must carry):\n  " + ", ".join(missed_vocab))
    user_prompt = f"""A one-on-one spoken English lesson just ended for {student_name}, age {student_age}.

[FULL UNIT — every item, with its stable id]
{_render_unit_with_ids(unit)}

[CURRENT STATE]
{_render_current_state(material, prev_progress)}{missed_line}

[SESSION LOG]
{transcript}

Decide the material for the student's NEXT session on this unit:
1. For EVERY item in the full unit, output "carry" (part of the next session's material) or "drop" (it rests this time). Output every item — an unmentioned item is kept by default, but explicit decisions are better.
2. An item the student has not yet used correctly in their own speech must be "carry".
3. Items the student used well may be "drop" — but you may also "carry" a well-used item when you judge it still needs consolidation. That is your teaching judgment; there is no fixed rule. Items dropped in earlier sessions may be re-carried when worthwhile.
4. "why" (one short English phrase) explains what to practice or watch out for a carried item; the next session's teacher will see it.
5. "progress" is an integer 0-100: your holistic judgment of how much of this unit the student has internalized. {prev_line} Move it up or down as today's evidence justifies.
6. "note" is 1-2 sentences in CHINESE for the parent: how today went and what the next session focuses on.

Return ONLY valid JSON exactly in this shape:
{{"progress": 0, "items": [{{"id": "v0", "action": "carry", "why": "..."}}, {{"id": "p1", "action": "drop"}}], "note": "..."}}"""
    return EVALUATOR_SYSTEM_PROMPT, user_prompt


# ---------------------------------------------------------------------------
# Format-only validation
# ---------------------------------------------------------------------------

def _all_ids(unit: dict) -> list[str]:
    ids = []
    for prefix, key in (("v", "vocabulary"), ("p", "patterns"), ("g", "grammar")):
        ids.extend(f"{prefix}{i}" for i in range(len(unit.get(key) or [])))
    return ids


def _extract_json(raw: str) -> dict:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise _Invalid("no JSON object found in the reply")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise _Invalid(f"JSON parse error: {e}")
    if not isinstance(data, dict):
        raise _Invalid("reply is not a JSON object")
    return data


def _parse_progress(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Invalid("'progress' must be a number")
    if isinstance(value, float) and not value.is_integer():
        raise _Invalid("'progress' must be an integer")
    return max(0, min(100, int(value)))


def parse_and_validate(raw: str, unit: dict,
                       force_carry_ids: list[str] | None = None) -> dict:
    """Format-only validation of an evaluator reply. Raises _Invalid.

    Content is never judged here — the LLM owns it. Structure rules:
    every id must reference the real unit; action is carry/drop; missing
    items default to carry (omission can never shrink the material);
    progress is clamped to 0-100. Force-carried ids (never-spoken vocab)
    are merged in last, whatever the LLM decided.
    """
    data = _extract_json(raw)
    progress = _parse_progress(data.get("progress"))

    items = data.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise _Invalid("'items' must be a list")

    carry: list[dict] = []
    decided: set[str] = set()
    for entry in items:
        if not isinstance(entry, dict):
            raise _Invalid("every item must be a JSON object")
        item_id = str(entry.get("id") or "").strip()
        if not _ITEM_ID_RE.fullmatch(item_id):
            raise _Invalid(f"unknown item id: {item_id!r}")
        if unit_item_text(unit, item_id) == "":
            raise _Invalid(f"item id out of range: {item_id!r}")
        if item_id in decided:
            raise _Invalid(f"duplicate item id: {item_id!r}")
        decided.add(item_id)
        action = str(entry.get("action") or "").strip().lower()
        if action not in ("carry", "drop"):
            raise _Invalid(f"item {item_id}: action must be 'carry' or 'drop', "
                           f"got {action!r}")
        if action == "carry":
            why = str(entry.get("why") or "").strip()
            carry.append({"id": item_id, **({"why": why} if why else {})})

    # 遗漏即保留：评估器没提到的条目默认 carry，材料只会因明确判断而缩小
    for item_id in _all_ids(unit):
        if item_id not in decided:
            carry.append({"id": item_id})

    # 没练过的（本课材料内、整节课没说出口的词汇）按规则强制保留
    for fid in force_carry_ids or []:
        existing = next((c for c in carry if c["id"] == fid), None)
        if existing is None:
            carry.append({"id": fid,
                          "why": "never spoken by the student this session "
                                 "(kept by rule)"})
        elif "why" not in existing:
            existing["why"] = ("never spoken by the student this session "
                               "(kept by rule)")

    order = {item_id: n for n, item_id in enumerate(_all_ids(unit))}
    carry.sort(key=lambda c: order.get(c["id"], 1 << 30))
    note = str(data.get("note") or "").strip()
    return {"progress": progress, "carry": carry, "note": note}


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

async def evaluate_lesson(ctx) -> dict:
    """Run the post-lesson evaluation for a bound lesson session.

    Retries transient failures (network hiccups, malformed output) up to
    MAX_ATTEMPTS; auth/quota errors raise immediately. Raises
    EvaluationError after the final failure — alerting the user is the
    caller's job, and per the agreed policy nothing is re-run later.
    """
    unit = course_engine.get_unit(ctx.textbook, ctx.unit_id)
    if not unit:
        raise EvaluationError(f"unit not found: {ctx.textbook}/{ctx.unit_id}")
    profile = ctx.student_profile

    # 本课材料里整节课没说出口的词汇 → 强制保留（owner 的硬规则：
    # "没练过的，下次肯定出现"；句式/语法没有本地信号，只能靠提示词）
    hits = ctx.session_matcher.cumulative_vocab_hits if ctx.session_matcher else set()
    today_vocab = set(ctx.unit_vocabulary())
    vocab = unit.get("vocabulary") or []
    missed = [w for w in vocab if w in today_vocab and w not in hits]
    force_ids = [f"v{i}" for i, w in enumerate(vocab) if w in missed]

    system, base_user = build_evaluation_prompt(
        profile.student_name, profile.age, unit, ctx.material,
        profile.get_unit_progress(ctx.textbook, ctx.unit_id).get("progress"),
        missed, _render_transcript(ctx.conversation_history))

    user_prompt = base_user
    last_problem = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            user_prompt = (base_user + f"\n\nYour previous reply was rejected: "
                           f"{last_problem} Return ONLY valid JSON in the "
                           "required shape.")
            await asyncio.sleep(2 * (attempt - 1))
        reply = await llm_engine.generate(
            user_prompt, system_prompt=system, max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE)
        if reply.startswith("⚠️"):
            last_problem = reply.strip()
            if any(marker in reply for marker in _AUTH_ERROR_MARKERS):
                raise EvaluationError(f"API error (no retry): {last_problem}")
            logger.error(f"[EVALUATOR] attempt {attempt}: API error: "
                         f"{last_problem[:200]}")
            continue
        try:
            result = parse_and_validate(reply, unit, force_ids)
            logger.info(f"[EVALUATOR] attempt {attempt}: progress={result['progress']}, "
                        f"carry={len(result['carry'])}, note={result['note'][:40]!r}")
            return result
        except _Invalid as e:
            last_problem = str(e)
            logger.error(f"[EVALUATOR] attempt {attempt}: invalid output: {e}")
    raise EvaluationError(last_problem or "all attempts failed")
