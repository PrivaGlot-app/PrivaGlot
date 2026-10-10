"""
LLM Module - Language Model integration using MiMo API (OpenAI compatible)
"""
import httpx
import json
from typing import AsyncGenerator
from . import config
from .teacher import teacher_store


def _style_line(style_id: str) -> str:
    """The delivery cue written into the prompt — the LLM half of the style
    setting. TTS receives its own half; see tts.py. Both halves are needed:
    a style that only reaches one of them makes the wording and the reading
    fight each other."""
    preset = config.STYLE_PRESETS.get(style_id or "")
    if preset is None:
        preset = config.STYLE_PRESETS[config.DEFAULT_STYLE]
    return preset["llm"]


def _student_fields(student_profile_obj, teacher=None, style_id: str = None) -> dict:
    """Extract the shared prompt placeholders from a student profile."""
    try:
        si = student_profile_obj.data.get("student_info", {})
    except Exception:
        si = {}
    if teacher is None:
        teacher = teacher_store.default_teacher()
    return {
        "teacher_name": teacher.name,
        "style_line": _style_line(style_id or teacher.style),
        "student_name": si.get("name", "Student"),
        "student_age": si.get("age", 10),
        "language_background": si.get("language_background", "中文"),
    }


def build_lesson_prompt(student_profile_obj, unit_block: str,
                        teacher=None, style_id: str = None) -> str:
    """Lesson mode: base teaching rules + the bound unit's material block."""
    return config.SYSTEM_PROMPT_TEMPLATE.format(
        unit_block=(unit_block + "\n" if unit_block else ""),
        **_student_fields(student_profile_obj, teacher, style_id),
    )


def build_memory_block(student_profile_obj) -> str:
    """Render the free-mode memory block: interests + recent chat topics.

    This is what lets the free-mode teacher "remember" the student across
    sessions. Returns "" when the profile has nothing memorable yet.
    """
    try:
        interests = list(student_profile_obj.interests)
        recent = list(student_profile_obj.recent_chats)
    except Exception:
        return ""

    lines = []
    if interests:
        lines.append("Things the student likes (drive the chat with these): "
                     + ", ".join(interests))
    if recent:
        lines.append("Topics you already talked about together (do not just "
                     "repeat them — build on them or move on):")
        for chat in recent[-3:]:
            topics = ", ".join(chat.get("topics", [])) or "general chat"
            lines.append(f"- {chat.get('date', '')}: {topics} — {chat.get('summary', '')}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def build_free_prompt(student_profile_obj, teacher=None,
                      style_id: str = None) -> str:
    """Free mode: companion persona + the student's memory block, no unit material."""
    memory_block = build_memory_block(student_profile_obj)
    return config.FREE_SYSTEM_PROMPT_TEMPLATE.format(
        memory_block=(memory_block + "\n" if memory_block else ""),
        **_student_fields(student_profile_obj, teacher, style_id),
    )


FREE_OPENING_TASK = """

[OPENING TASK]
The session is starting. The fixed intro ("Hi <student's name>! I'm <your name>.") was already said — do NOT repeat it. Output ONLY the opening line that comes next: one question, at most two short sentences, that starts today's chat. Use the memory block above: pick something the student likes, or build on a recent chat topic from a fresh angle instead of repeating it. If the memory block is empty, warmly ask what is on their mind today. Plain spoken English, no markdown, no surrounding quotation marks, no explanations.
"""


def build_free_opening_prompt(student_profile_obj, teacher=None,
                              style_id: str = None) -> str:
    """Free-mode greeting: the companion persona + the opening-line task.

    Only the question after the fixed name intro is generated — the intro is
    hardcoded and already sent/spoken while this call is in flight.
    """
    return (build_free_prompt(student_profile_obj, teacher, style_id)
            + FREE_OPENING_TASK)


LESSON_OPENING_TASK = """

[OPENING TASK]
The session is starting. The fixed intro ("Hi <student's name>! I'm <your name>.") was already said — do NOT repeat it. Output ONLY the opening line that comes next: at most three short sentences that welcome the student into today's lesson — hook them with the topic from the course content above, maybe with a quick question about their own life — and end with a brief, natural check-in that they are ready to begin, phrased in your own words rather than a fixed formula. Plain spoken English, no markdown, no surrounding quotation marks, no explanations.
"""


def build_lesson_opening_prompt(student_profile_obj, unit_block: str = "",
                                teacher=None, style_id: str = None) -> str:
    """Lesson-mode greeting: teaching persona + unit material + opening task.

    Same split as free mode: the fixed name intro is sent immediately, the
    LLM writes everything after it, ending in a ready-check question.
    """
    return (build_lesson_prompt(student_profile_obj, unit_block,
                                teacher, style_id)
            + LESSON_OPENING_TASK)


class LLMEngine:
    # 这个实例是 import 时就建好的单例，而 Key 可能在服务跑起来之后才由用户
    # 在网页里填上（见 app/main.py 的 /api/setup/key）。所以三个值都现读 config，
    # 一旦缓存进对象属性，用户在网页里填完 Key 也得重启服务才生效。
    @property
    def api_key(self) -> str:
        return (config.LLM_API_KEY or "").strip()

    @property
    def base_url(self) -> str:
        return config.LLM_BASE_URL

    @property
    def model(self) -> str:
        return config.LLM_MODEL

    async def generate_stream(
        self,
        user_message: str,
        history: list = None,
        system_prompt: str = None,
        max_tokens: int = None,
        temperature: float = 0.5,
    ) -> AsyncGenerator[str, None]:
        if not self.api_key:
            yield "⚠️ 还没有配置小米 API Key。请在浏览器里打开 http://localhost:8000 填写。"
            return

        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "thinking": {"type": "disabled"},
            "max_completion_tokens": max_tokens or 500,
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                                if "choices" in chunk and len(chunk["choices"]) > 0:
                                    delta = chunk["choices"][0].get("delta", {})
                                    content = delta.get("content")
                                    if content is not None:
                                        yield content
                            except json.JSONDecodeError:
                                continue

        except httpx.HTTPStatusError as e:
            # HTTPStatusError 是 HTTPError 的子类，必须先接。404 单列：
            # 默认对话模型被厂商下线时（如 mimo-v2.5 于 2026-10-21 下线），
            # 学生在聊天里看到的不能再是一串 URL 报错。
            if e.response.status_code == 404:
                yield "⚠️ 对话模型不存在或已下线（404）。请把 PrivaGlot 升级到最新版本后再试。"
            else:
                yield f"⚠️ LLM API error: {str(e)}"
        except httpx.HTTPError as e:
            yield f"⚠️ LLM API error: {str(e)}"
        except Exception as e:
            yield f"⚠️ Unexpected error: {str(e)}"

    async def generate(self, user_message: str, history: list = None,
                       system_prompt: str = None, max_tokens: int = None,
                       temperature: float = 0.5) -> str:
        full_response = ""
        async for token in self.generate_stream(
            user_message, history, system_prompt=system_prompt, max_tokens=max_tokens,
            temperature=temperature,
        ):
            full_response += token
        return full_response


llm_engine = LLMEngine()
