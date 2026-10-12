"""
Teacher Profile Module — load, list, and remember teacher personas.

Preset profiles ship with the CODE, in app/bundled_teachers/ — one per MiMo
preset English voice (dean / chloe / milo / mia). Plain configuration: the TTS
voice plus that voice's default delivery style. A teacher's name IS the voice
name, so no separate persona name is invented. No personal data, so they are
tracked and ride along with `COPY app`: that keeps them reachable even when
data/ is bind-mounted over (an empty mount used to hide them and crash every
lesson with "No teacher profiles found").

data/teachers/ holds runtime state only:

  _active.json   Which teacher each student picked last. Its keys are student
                 ids, so it is gitignored. The loader skips every file whose
                 name starts with "_", keeping it out of the list.

Lookup merges both directories: bundled presets first, then data/teachers/ —
whatever you drop in yourself wins on the same id.

A teacher's name is authoritative on the server side: the frontend can list
teachers and pick one, but it can never rename one.
"""
import json
import os
import threading
from pathlib import Path
from . import config

# Preset personas live next to the code so that a bind mount over data/ can
# never hide them (an empty mount used to crash every lesson).
BUNDLED_TEACHERS_DIR = Path(__file__).parent / "bundled_teachers"


class TeacherProfile:
    def __init__(self, profile_path: Path):
        self.profile_path = profile_path
        self._data = None

    def load(self) -> dict:
        if not self.profile_path.exists():
            raise FileNotFoundError(f"Teacher profile not found: {self.profile_path}")
        with open(self.profile_path, "r", encoding="utf-8") as f:
            self._data = json.load(f)
        return self._data

    @property
    def data(self) -> dict:
        if self._data is None:
            self.load()
        return self._data

    @property
    def id(self) -> str:
        return self.profile_path.stem

    @property
    def name(self) -> str:
        """The preset voice name.

        A profile may carry an explicit display_name, but identity is the voice,
        so an absent value falls back to it rather than inventing a name.
        """
        configured = self.data.get("teacher_profile", {}).get("display_name")
        return configured or self.voice

    @property
    def voice(self) -> str:
        """Preset voice id, validated against the allow-list."""
        voice = self.data.get("tts_config", {}).get("voice", config.DEFAULT_VOICE)
        return voice if voice in config.ALLOWED_VOICES else config.DEFAULT_VOICE

    @property
    def style(self) -> str:
        """This teacher's default delivery style, validated against the presets."""
        style = self.data.get("tts_config", {}).get("style", config.DEFAULT_STYLE)
        return style if style in config.STYLE_PRESETS else config.DEFAULT_STYLE

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "voice": self.voice,
            "style": self.style,
        }


class TeacherStore:
    """Reads the teacher directory and remembers each student's choice."""

    def __init__(self, teachers_dir: Path = None, bundled_dir: Path = None):
        self.teachers_dir = teachers_dir or config.TEACHERS_DIR
        self.teachers_dir.mkdir(parents=True, exist_ok=True)
        self.bundled_dir = bundled_dir or BUNDLED_TEACHERS_DIR
        self.active_path = self.teachers_dir / "_active.json"
        self._lock = threading.Lock()

    # ---- teacher profiles ----

    def _profile_paths(self) -> dict:
        """{teacher_id: path} spanning the bundled presets and data/teachers/.

        Bundled presets are laid in first and data/teachers/ overwrites them by
        id, so whatever you put in yourself wins. Reading both is what keeps a
        fresh container alive: an empty data/ bind mount hides nothing here
        because the presets live next to the code in app/bundled_teachers/.
        """
        paths = {}
        for base in (self.bundled_dir, self.teachers_dir):
            if not base:
                continue
            for path in sorted(Path(base).glob("*.json")):
                if path.name.startswith("_"):
                    continue
                paths[path.stem] = path
        return paths

    def list_teachers(self) -> list:
        """Every teacher profile, ignoring runtime/underscore-prefixed files."""
        teachers = []
        for path in self._profile_paths().values():
            try:
                teachers.append(TeacherProfile(profile_path=path).summary())
            except Exception as e:
                print(f"[TeacherStore] Failed to read {path.name}: {e}")
        teachers.sort(key=lambda t: t["name"].lower())
        return teachers

    def get_teacher(self, teacher_id: str) -> TeacherProfile:
        if not teacher_id:
            raise ValueError("Empty teacher id")
        path = self._profile_paths().get(teacher_id)
        if path is None:
            raise FileNotFoundError(f"Teacher not found: {teacher_id}")
        return TeacherProfile(profile_path=path)

    def default_teacher_id(self) -> str:
        """The configured default, falling back when that file is missing."""
        configured = self._read_active().get("default_teacher", "")
        paths = self._profile_paths()
        for candidate in (configured, config.DEFAULT_TEACHER_ID):
            if candidate and candidate in paths:
                return candidate
        teachers = self.list_teachers()
        if teachers:
            return teachers[0]["id"]
        raise FileNotFoundError(
            f"No teacher profiles found in {self.teachers_dir} "
            f"or {self.bundled_dir}")

    def default_teacher(self) -> TeacherProfile:
        return self.get_teacher(self.default_teacher_id())

    # ---- per-student memory ----

    def _read_active(self) -> dict:
        try:
            data = json.loads(self.active_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[TeacherStore] Failed to read {self.active_path.name}: {e}")
        return {}

    def _write_active(self, data: dict):
        """Write the runtime file atomically — a torn file would lose every
        student's remembered choice at once."""
        tmp_path = self.active_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.active_path)

    def resolve_for_student(self, student_id: str) -> tuple:
        """Return (TeacherProfile, style_id) for this student's next session.

        A remembered teacher whose file was deleted is a dangling reference —
        the student falls back to the default teacher.
        """
        entry = self._read_active().get("student_choices", {}).get(student_id) or {}
        remembered_id = entry.get("teacher") or ""

        teacher = None
        if remembered_id:
            try:
                teacher = self.get_teacher(remembered_id)
            except Exception:
                print(f"[TeacherStore] Dangling teacher reference "
                      f"'{remembered_id}' for {student_id} — using default")
        if teacher is None:
            teacher = self.default_teacher()

        # The style override belongs to the teacher it was chosen for, so it
        # only applies while that exact teacher is still the one teaching.
        style = entry.get("style")
        if style not in config.STYLE_PRESETS or remembered_id != teacher.id:
            style = teacher.style
        return teacher, style

    def remember_choice(self, student_id: str, teacher_id: str,
                        style: str = None):
        """Persist one student's pick so the next session starts the same way.

        An explicit style is honored while the student stays with the same
        teacher. Switching to a *different* teacher resets the style to that
        teacher's own default — the override was made for the previous teacher,
        so it does not follow the student across teachers.
        """
        if not student_id:
            return
        try:
            teacher = self.get_teacher(teacher_id)
        except Exception:
            return

        with self._lock:
            active = self._read_active()
            choices = active.setdefault("student_choices", {})
            previous_teacher = (choices.get(student_id) or {}).get("teacher")
            # A student with no stored pick yet has not switched away from
            # anything, so an explicit style on the first save is honored.
            style_survives = (
                style in config.STYLE_PRESETS
                and previous_teacher in (None, "", teacher_id)
            )
            resolved_style = style if style_survives else teacher.style

            active["default_teacher"] = active.get(
                "default_teacher", config.DEFAULT_TEACHER_ID)
            choices[student_id] = {
                "teacher": teacher_id,
                "style": resolved_style,
            }
            try:
                self._write_active(active)
                print(f"[TeacherStore] {student_id} → "
                      f"{teacher_id} / {resolved_style}")
            except Exception as e:
                print(f"[TeacherStore] Failed to save choice: {e}")


# Global store instance
teacher_store = TeacherStore()
