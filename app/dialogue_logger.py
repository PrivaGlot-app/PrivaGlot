"""
Dialogue Logger - Persists every student-teacher turn as a permanent teaching archive.

Always on (no env switch): this is a teaching record, not a debug tool.

Output: data/sessions/{student_name}/{session_id}_{unit_id}.jsonl
Each line is one turn: what the student said (ASR raw + cleaned) and what
the teacher replied.

Timestamp format matches prompt_logger exactly (ISO, second precision) so the
two logs can be aligned for diagnosis: "what did I send the AI that made it
reply that way" ↔ "what actually happened in the conversation".
"""
import re
import json
from datetime import datetime
from . import config
from .logging_config import log as logger


def _safe_name(name: str) -> str:
    """Sanitize a student name for use as a directory name."""
    # Strip characters illegal in Windows paths
    safe = re.sub(r'[\\/:*?"<>|]', '_', name or "unknown").strip()
    return safe or "unknown"


class DialogueLogger:
    def __init__(self):
        # data/sessions already created by config.py at import time
        pass

    def _filepath(self, student_name: str, session_id: str, unit_id: str) -> object:
        student_dir = config.SESSIONS_DIR / _safe_name(student_name)
        student_dir.mkdir(parents=True, exist_ok=True)
        unit_part = _safe_name(unit_id) if unit_id else "no_unit"
        return student_dir / f"{session_id}_{unit_part}.jsonl"

    def log_turn(self, student_name: str, session_id: str, unit_id: str,
                 turn_number: int, student_text: str, asr_raw: str,
                 teacher_text: str):
        """Append one dialogue turn to the student's session file."""
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "turn": turn_number,
            "student": student_text,
            "asr_raw": asr_raw,
            "teacher": teacher_text,
        }

        filepath = self._filepath(student_name, session_id, unit_id)
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"DialogueLogger failed: {e}")


dialogue_logger = DialogueLogger()
