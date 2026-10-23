"""
LLM Module - prompt building (the engine lives in app/engines/, #15).

本模块只保留「说什么」——课程/自由对话的系统提示词与开场任务；「怎么调
模型」（协议、厂商、quirks）在 app/engines/ 的协议积木里，内核经
engines.llm_engine 分发器使用。教师音色/风格只在这里贡献 prompt 半边
（TTS 半边在 engines 迁入前仍见 app/tts.py）。
"""
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
