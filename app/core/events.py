"""
Kernel event contract — the vocabulary every channel speaks.

The kernel never touches a transport: it yields plain, JSON-ready dicts and
each channel decides what they become (a WebSocket frame, a Feishu voice
message, a plain log line, ...). The schema below is the wire protocol the
web frontend has been speaking since v3 — extracted, not invented.

Rules of change (frozen contract):
  - event "type" values and existing fields are frozen;
  - new events may be added, existing events may gain OPTIONAL fields;
  - a channel may ignore any event it cannot render — capability
    degradation is the channel's decision, never the kernel's.

The web channel forwards these dicts 1:1 as WebSocket JSON messages; the
frontend's switch statement (static/index.html handleServerMessage) is the
reference consumer.
"""

EVENT_STUDENT_INFO = "student_info"
EVENT_SESSION_STARTED = "session_started"
EVENT_LESSON_TARGETS = "lesson_targets"
EVENT_SESSION_INFO = "session_info"
EVENT_TRANSCRIPTION = "transcription"
EVENT_VOCAB_HIT = "vocab_hit"
EVENT_LLM_TOKEN = "llm_token"
EVENT_LLM_CLEANED = "llm_cleaned"
EVENT_LLM_DONE = "llm_done"
EVENT_TTS_AUDIO = "tts_audio"
EVENT_LESSON_COMPLETE = "lesson_complete"
EVENT_LESSON_EVAL_RESULT = "lesson_eval_result"
EVENT_LESSON_EVAL_FAILED = "lesson_eval_failed"
EVENT_FREE_SESSION_COMPLETE = "free_session_complete"
EVENT_ERROR = "error"


def student_info(name: str, teacher_name: str) -> dict:
    return {"type": EVENT_STUDENT_INFO, "name": name, "teacher_name": teacher_name}


def session_started() -> dict:
    return {"type": EVENT_SESSION_STARTED}


def lesson_targets(vocabulary: list) -> dict:
    return {"type": EVENT_LESSON_TARGETS, "vocabulary": vocabulary}


def session_info(textbook: str, unit_id: str, unit_title: str) -> dict:
    return {"type": EVENT_SESSION_INFO, "textbook": textbook,
            "unit_id": unit_id, "unit_title": unit_title}


def transcription(text: str) -> dict:
    return {"type": EVENT_TRANSCRIPTION, "text": text}


def vocab_hit(turn: int, student_text: str, vocab_hits: list,
              cumulative_vocab_hits: list, cumulative_vocab_missed: list,
              accuracy: float) -> dict:
    return {
        "type": EVENT_VOCAB_HIT,
        "turn": turn,
        "student_text": student_text,
        "vocab_hits": vocab_hits,
        "cumulative_vocab_hits": cumulative_vocab_hits,
        "cumulative_vocab_missed": cumulative_vocab_missed,
        "accuracy": accuracy,
    }


def llm_token(text: str) -> dict:
    return {"type": EVENT_LLM_TOKEN, "text": text}


def llm_cleaned(text: str) -> dict:
    return {"type": EVENT_LLM_CLEANED, "text": text}


def llm_done() -> dict:
    return {"type": EVENT_LLM_DONE}


def tts_audio(wav_bytes: bytes) -> dict:
    import base64
    return {"type": EVENT_TTS_AUDIO,
            "data": base64.b64encode(wav_bytes).decode("utf-8")}


def lesson_complete(message: str, accuracy: float, vocab_used: list,
                    vocab_missed: list, total_turns: int,
                    duration_minutes: float, eval_: str) -> dict:
    return {
        "type": EVENT_LESSON_COMPLETE,
        "message": message,
        "accuracy": accuracy,
        "vocab_used": vocab_used,
        "vocab_missed": vocab_missed,
        "total_turns": total_turns,
        "duration_minutes": duration_minutes,
        "eval": eval_,
    }


def lesson_eval_result(progress: int, note: str, carry_count: int) -> dict:
    return {"type": EVENT_LESSON_EVAL_RESULT, "progress": progress,
            "note": note, "carry_count": carry_count}


def lesson_eval_failed(message: str) -> dict:
    return {"type": EVENT_LESSON_EVAL_FAILED, "message": message}


def error(message: str) -> dict:
    return {"type": EVENT_ERROR, "message": message}
