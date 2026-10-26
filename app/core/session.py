"""
Session context — conversation state only, no curriculum mechanics, no
transport. Moved verbatim from app/websocket.py in the v4 kernel refactor
(the class was already transport-free; it now lives where it belongs).
"""
from datetime import datetime

from .. import config
from ..course_engine import course_engine
from ..matcher import SessionMatcher
from ..logging_config import log as logger


class SessionContext:
    """Per-connection state: mode, teacher, conversation history, vocab stats."""

    def __init__(self, student_profile_obj, teacher=None, style: str = None):
        self.student_profile = student_profile_obj
        self.conversation_history = []
        self.session_started_at = datetime.now()
        self.session_id = self.session_started_at.strftime("%Y%m%d_%H%M%S")

        # #48 effective-duration accounting: the session's duration is the sum
        # of gaps between student turns (each gap capped — see kernel's
        # _tick_interaction), NOT the start→end wall-clock span. Seeding
        # last_active_at with the start time lets the warm-up before the first
        # turn count too, within the cap.
        self.last_active_at = self.session_started_at
        self.effective_seconds = 0.0

        # Who teaches this session. The store holds every teacher; the session
        # binds one, exactly like the unit binding — never global state.
        self.teacher = teacher
        self.style = style or (teacher.style if teacher else config.DEFAULT_STYLE)

        # Session mode: "lesson" (unit-guided chat) or "free" (pure chat)
        self.mode = "lesson"
        self.session_ended = False

        # Local vocabulary statistics (display/report only — drives nothing)
        self.session_matcher: SessionMatcher | None = None

        # The student's living material for the bound unit (the lesson
        # evaluator's last carry list). None = first session or stale
        # material: the full unit teaches.
        self.material: dict | None = None

        # Unit binding (lesson mode)
        self.textbook: str | None = None
        self.unit_id: str | None = None

    def bind_unit(self, textbook: str, unit_id: str):
        self.textbook = textbook
        self.unit_id = unit_id
        logger.info(f"[SESSION] Bound to: {textbook}/{unit_id}")

    def bind_teacher(self, teacher, style: str = None):
        self.teacher = teacher
        self.style = style if style in config.STYLE_PRESETS else teacher.style
        logger.info(f"[SESSION] Teacher: {teacher.name} (voice={teacher.voice}, "
                    f"style={self.style})")

    def unit_vocabulary(self) -> list[str]:
        if self.material:
            return course_engine.material_vocabulary(
                self.textbook or "", self.unit_id or "", self.material)
        unit = course_engine.get_unit(self.textbook or "", self.unit_id or "")
        return list(unit.get("vocabulary", [])) if unit else []

    def init_matcher(self):
        """Initialize the vocabulary statistics tracker for this unit."""
        vocab = self.unit_vocabulary()
        self.session_matcher = SessionMatcher(required_vocab=vocab)
        logger.info(f"[MATCHER] Initialized: {len(vocab)} vocab")

    def unit_title(self) -> str:
        """Resolve this session's own unit binding — never global state."""
        if not self.textbook or not self.unit_id:
            return ""
        unit = course_engine.get_unit(self.textbook, self.unit_id)
        return (unit.get("title", "") or self.unit_id) if unit else ""
