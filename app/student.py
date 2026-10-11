"""
Student Profile Module - Load, save, and update student profiles.

The profile has two decoupled halves:
  - course_progress: the lesson-side footprint. Practice counts per unit
    plus the living material the lesson evaluator maintains (per-item carry
    list + 0-100 progress); the per-turn dialogue jsonl is the session's
    real archive.
  - free_chat + interests: the free-mode teacher's memory. Written at the end
    of every free conversation and injected into the next one, so the teacher
    remembers what the student likes and what they talked about.
"""
import json
import re
from pathlib import Path
from datetime import datetime
from . import config

# Raw facts only — the LLM infers the student's level from age + background;
# we never freeze a derived level label (学段/CEFR) into the profile.
LANGUAGE_BACKGROUNDS = ("中文", "海外华人家庭")


class StudentProfile:
    def __init__(self, profile_path: Path):
        self.profile_path = profile_path
        self._data = None

    def load(self) -> dict:
        if not self.profile_path.exists():
            raise FileNotFoundError(f"Profile not found: {self.profile_path}")
        with open(self.profile_path, "r", encoding="utf-8") as f:
            self._data = json.load(f)
        return self._data

    def save(self, data: dict = None):
        if data is not None:
            self._data = data
        if self._data is None:
            raise ValueError("No data to save")
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.profile_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    @property
    def data(self) -> dict:
        if self._data is None:
            self.load()
        return self._data

    @property
    def student_name(self) -> str:
        return self.data.get("student_info", {}).get("name", "Student")

    @property
    def age(self) -> int:
        return self.data.get("student_info", {}).get("age", 10)

    @property
    def interests(self) -> list:
        return self.data.get("student_info", {}).get("interests", [])

    @property
    def recent_chats(self) -> list:
        """Free-conversation memory: [{date, topics, summary}], newest last."""
        return self.data.get("free_chat", {}).get("recent_chats", [])

    @property
    def course_progress(self) -> dict:
        """Unit-granularity progress keyed by 'textbook/unit_id'."""
        return self.data.get("course_progress", {})

    def get_unit_progress(self, textbook: str, unit_id: str) -> dict:
        """Get progress for a specific unit. Returns empty dict if not started."""
        key = f"{textbook}/{unit_id}"
        return self.course_progress.get(key, {})

    def get_unit_status(self, textbook: str, unit_id: str) -> str:
        """Return display status for a unit: "practiced" or "not_started"."""
        unit_prog = self.get_unit_progress(textbook, unit_id)
        if not unit_prog:
            return "not_started"
        if unit_prog.get("sessions", 0) > 0:
            return "practiced"
        return "not_started"

    def get_all_textbook_statuses(self, textbooks_units: dict) -> dict:
        """
        Given textbooks_units = { "课本名": [unit_id, ...], ... },
        return { "课本名/unit_id": status_string, ... } for all units.
        """
        result = {}
        for textbook, unit_ids in textbooks_units.items():
            for unit_id in unit_ids:
                result[f"{textbook}/{unit_id}"] = self.get_unit_status(textbook, unit_id)
        return result

    def record_unit_practice(self, textbook: str, unit_id: str,
                             duration_minutes: float = 0.0, total_turns: int = 0):
        """Record one practice session on a unit (no completion concept)."""
        key = f"{textbook}/{unit_id}"
        cp = self.data.setdefault("course_progress", {})
        unit_prog = cp.setdefault(key, {"sessions": 0, "total_minutes": 0.0,
                                        "total_turns": 0, "last_practiced": ""})
        unit_prog["sessions"] = unit_prog.get("sessions", 0) + 1
        unit_prog["total_minutes"] = round(
            unit_prog.get("total_minutes", 0.0) + duration_minutes, 1)
        unit_prog["total_turns"] = unit_prog.get("total_turns", 0) + total_turns
        unit_prog["last_practiced"] = datetime.now().isoformat()
        self.save()

    # ---- Living material: the lesson evaluator's per-student subset ----

    def get_unit_material(self, textbook: str, unit_id: str,
                          fingerprint: str) -> dict | None:
        """The AI-maintained material for this unit, if it is still valid.

        Material references unit items by position, so it is only returned
        when the stored fingerprint still matches the unit's current content
        (an edited unit invalidates it). Returns None when absent or stale —
        the caller decides what a stale entry means.
        """
        unit_prog = self.get_unit_progress(textbook, unit_id)
        material = unit_prog.get("material")
        if not isinstance(material, dict):
            return None
        if unit_prog.get("material_fingerprint") != fingerprint:
            return None
        return material

    def get_unit_progress_number(self, textbook: str, unit_id: str) -> int | None:
        """The evaluator's 0-100 progress for this unit, None if never evaluated."""
        progress = self.get_unit_progress(textbook, unit_id).get("progress")
        return progress if isinstance(progress, int) else None

    def save_unit_evaluation(self, textbook: str, unit_id: str,
                             progress: int, material: dict, fingerprint: str):
        """Persist one evaluator verdict (progress number + living material).

        Sits inside the same course_progress entry as the practice counts:
        one key per textbook/unit, everything the lesson side writes.
        """
        key = f"{textbook}/{unit_id}"
        cp = self.data.setdefault("course_progress", {})
        unit_prog = cp.setdefault(key, {"sessions": 0, "total_minutes": 0.0,
                                        "total_turns": 0, "last_practiced": ""})
        unit_prog["progress"] = progress
        unit_prog["material"] = material
        unit_prog["material_fingerprint"] = fingerprint
        unit_prog["material_updated"] = datetime.now().isoformat()
        unit_prog["material_generation"] = unit_prog.get("material_generation", 0) + 1
        self.save()

    def clear_unit_material(self, textbook: str, unit_id: str):
        """Discard a stale material (unit content was edited). Progress number
        goes with it — it described content that no longer exists."""
        unit_prog = self.get_unit_progress(textbook, unit_id)
        if not unit_prog:
            return
        for field in ("material", "material_fingerprint", "material_updated",
                      "material_generation", "progress"):
            unit_prog.pop(field, None)
        self.save()

    # ---- Free Mode: the teacher's memory ----

    def update_from_free_session(self, analysis: dict):
        """Record a finished free conversation in the profile.

        The free mode's entire profile footprint: merge new interests,
        append a recent-chats entry (date/topics/summary), bump the chat
        counter. Caps: 8 interests, 5 recent chats (newest kept).
        """
        si = self.data.setdefault("student_info", {})
        current_interests = si.get("interests", [])
        seen = {i.lower() for i in current_interests}
        for interest in analysis.get("new_interests", []):
            if interest.lower() not in seen:
                current_interests.append(interest)
                seen.add(interest.lower())
        si["interests"] = current_interests[-8:]

        free_chat = self.data.setdefault("free_chat", {})
        chats = free_chat.setdefault("recent_chats", [])
        chats.append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "topics": analysis.get("topics_discussed", []),
            "summary": analysis.get("summary", ""),
        })
        free_chat["recent_chats"] = chats[-5:]

        meta = self.data.setdefault("session_meta", {})
        meta["total_sessions"] = meta.get("total_sessions", 0) + 1
        meta["last_session_date"] = datetime.now().strftime("%Y-%m-%d")
        self.save()


class StudentStore:
    def __init__(self, students_dir: Path = None):
        self.students_dir = students_dir or (config.DATA_DIR / "students")
        self.students_dir.mkdir(parents=True, exist_ok=True)

    def list_students(self) -> list:
        students = []
        for path in sorted(self.students_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                si = data.get("student_info", {})
                meta = data.get("session_meta", {})
                students.append({
                    "id": path.stem,
                    "name": si.get("name", path.stem),
                    "age": si.get("age", ""),
                    "total_sessions": meta.get("total_sessions", 0),
                    "last_session_date": meta.get("last_session_date", ""),
                })
            except Exception as e:
                print(f"[StudentStore] Failed to read {path.name}: {e}")
        students.sort(key=lambda s: s["name"].lower())
        return students

    def get_profile(self, student_id: str) -> StudentProfile:
        """Resolve a student_id to a profile.  Tries the raw id first (supports
        unicode / Chinese filenames), then falls back to ASCII-sanitized form."""
        if not student_id:
            raise ValueError("Empty student id")
        # Try the original id directly (handles Chinese names like "亨利.json")
        direct = self.students_dir / f"{student_id}.json"
        if direct.exists():
            return StudentProfile(profile_path=direct)
        # Fallback: ASCII-sanitized form (handles "lily" → "lily.json")
        safe = re.sub(r"[^a-z0-9_-]+", "_", student_id.lower())
        if not safe:
            raise ValueError(f"Student not found: {student_id}")
        return StudentProfile(profile_path=self.students_dir / f"{safe}.json")

    def create_student(self, name: str, age: int = 8,
                       language_background: str = "中文",
                       interests: list = None) -> StudentProfile:
        # Sanitize: keep ASCII alphanumeric + underscores; for pure-Chinese names
        # the result is empty, so fall back to the original (unicode-safe on modern OS).
        base = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
        if not base:
            # Pure non-ASCII name (e.g. Chinese) — use the original as-is
            base = name.strip()
        sid = base
        counter = 2
        while (self.students_dir / f"{sid}.json").exists():
            sid = f"{base}_{counter}"
            counter += 1

        if language_background not in LANGUAGE_BACKGROUNDS:
            language_background = "中文"

        data = {
            "student_info": {
                "name": name.strip(),
                "age": age,
                "language_background": language_background,
                "interests": interests or [],
            },
            "course_progress": {},
            "free_chat": {"recent_chats": []},
            "session_meta": {"total_sessions": 0, "last_session_date": ""},
        }
        profile = StudentProfile(profile_path=self.students_dir / f"{sid}.json")
        profile.save(data)
        print(f"[StudentStore] Created student {name} ({sid})")
        return profile


# Global store instance
student_store = StudentStore()
