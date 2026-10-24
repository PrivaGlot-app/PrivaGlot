"""Teacher Profile Module — load, list, and remember teacher personas.

v2 多厂商（#15，2026-10-21 用户裁定「教师目录随 TTS 厂商走」）：切换
语音厂商须重选老师，跨厂商不可见他厂角色。目录形态：

  app/bundled_teachers/{vendor}/<name>.json   预置（随代码 COPY 走，
                                              data/ 被空挂载盖住也在）
  data/teachers/{vendor}/<name>.json          自建（按厂商子目录）
  data/teachers/<name>.json                   旧形态扁平文件（视为 mimo）

教师 id = 「{vendor}:{name}」命名空间（mimo:dean / zhipu:jam）。裸 id
（存量 _active.json、旧前端缓存）按 mimo 优先回落解析——旧绑定不丢；
绑定指向的老师不在当前 TTS 厂商目录时，回落当前厂商默认老师并给出
重选提示（resolve_for_student 第三返回值），已存的选择不覆盖——切回
原厂商后原老师自然恢复。

Lookup merges bundled + data/：同名（同厂商）时 data/ 覆盖 bundled。
老师的名字服务端说了算：前端只能选，不能改名。
"""
import json
import os
import threading
from pathlib import Path
from . import config, vendors

# Preset personas live next to the code so that a bind mount over data/ can
# never hide them (an empty mount used to crash every lesson).
BUNDLED_TEACHERS_DIR = Path(__file__).parent / "bundled_teachers"


def _active_tts_vendor() -> str:
    """当前 TTS 厂商 id（读法B 过渡期：TTS_VENDOR 未填 = 默认厂商）。"""
    return vendors.tts_selection()["vendor_id"]


def _vendor_voices(vendor: str) -> tuple:
    """该厂商的合法音色表；档案缺失时退回旧 MiMo 白名单（防御性）。"""
    sec = (vendors._REGISTRY.get(vendor) or {}).get("tts") or {}
    return tuple(sec.get("voices") or ()) or tuple(config.ALLOWED_VOICES)


class TeacherProfile:
    def __init__(self, profile_path: Path, vendor: str = "mimo"):
        self.profile_path = profile_path
        self._vendor = vendor
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
    def vendor(self) -> str:
        """这位老师属于哪个 TTS 厂商目录。"""
        return self._vendor

    @property
    def id(self) -> str:
        """命名空间 id（mimo:dean / zhipu:jam）——存档与按钮 payload 都用它。"""
        return f"{self._vendor}:{self.profile_path.stem}"

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
        """该厂商音色表内的预置音色 id；表外值回落厂商默认音色。"""
        default = ((vendors._REGISTRY.get(self._vendor) or {}).get("tts")
                   or {}).get("default_voice", config.DEFAULT_VOICE)
        voice = self.data.get("tts_config", {}).get("voice", default)
        return voice if voice in _vendor_voices(self._vendor) else default

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
            "vendor": self._vendor,
        }


class TeacherStore:
    """Reads the teacher directories and remembers each student's choice."""

    def __init__(self, teachers_dir: Path = None, bundled_dir: Path = None):
        self.teachers_dir = teachers_dir or config.TEACHERS_DIR
        self.teachers_dir.mkdir(parents=True, exist_ok=True)
        self.bundled_dir = bundled_dir or BUNDLED_TEACHERS_DIR
        self.active_path = self.teachers_dir / "_active.json"
        self._lock = threading.Lock()

    # ---- teacher profiles ----

    def _dir_vendor(self, path: Path) -> str:
        """子目录名即厂商（bundled_teachers/zhipu/x.json → zhipu）；
        不是已知厂商的目录按 JSON 字段兜底，最终回落 mimo。"""
        parent = path.parent.name
        if parent != path.parent.parent.name and parent in vendors._REGISTRY:
            return parent
        return ""

    def _profile_paths(self) -> dict:
        """{canonical_id: (path, vendor)}，bundled 先铺、data/ 覆盖。

        三个来源：bundled 与 data 的厂商子目录，外加 data 的旧形态扁平
        文件（视为 mimo）。下划线开头 = 运行态文件，永不进列表。
        """
        found = {}  # (vendor, stem) -> (path, vendor)
        bases = (
            (self.bundled_dir, False),
            (self.teachers_dir, True),
        )
        for base, allow_flat in bases:
            if not base:
                continue
            base = Path(base)
            candidates = []
            if base.exists():
                candidates.extend(base.glob("*/*.json"))
                candidates.extend(base.glob("*.json"))
            for path in sorted(candidates):
                if path.name.startswith("_"):
                    continue
                vendor = self._dir_vendor(path)
                if not vendor:
                    if path.parent == base and allow_flat:
                        vendor = "mimo"  # 旧形态扁平文件
                    else:
                        continue
                try:
                    declared = (json.loads(path.read_text(encoding="utf-8"))
                                .get("tts_config", {}).get("vendor", ""))
                except Exception:
                    declared = ""
                found[(declared or vendor, path.stem)] = (path, declared or vendor)
        return {f"{vendor}:{stem}": (path, vendor)
                for (vendor, stem), (path, vendor) in sorted(found.items())}

    def list_teachers(self, vendor: str = None) -> list:
        """教师目录。vendor=None = 当前 TTS 厂商（产品语义：跨厂商不可见，
        GLM 下选不到 Dean）；vendor="all" = 全量（管理/测试用）。"""
        want = _active_tts_vendor() if vendor is None else vendor
        teachers = []
        for canonical, (path, prof_vendor) in self._profile_paths().items():
            if want != "all" and prof_vendor != want:
                continue
            try:
                teachers.append(TeacherProfile(profile_path=path,
                                               vendor=prof_vendor).summary())
            except Exception as e:
                print(f"[TeacherStore] Failed to read {path.name}: {e}")
        teachers.sort(key=lambda t: t["name"].lower())
        return teachers

    def _canonical(self, teacher_id: str) -> str:
        """裸 id（存量绑定/旧缓存）→ 命名空间 id：mimo 优先，其次当前
        TTS 厂商，最后全表唯一名。带冒号的 id 原样返回。"""
        tid = (teacher_id or "").strip()
        if not tid:
            return tid
        if ":" in tid:
            return tid
        paths = self._profile_paths()
        for candidate in (f"mimo:{tid}",
                          f"{_active_tts_vendor()}:{tid}"):
            if candidate in paths:
                return candidate
        matches = [c for c in paths if c.endswith(f":{tid}")]
        return matches[0] if len(matches) == 1 else tid

    def get_teacher(self, teacher_id: str) -> TeacherProfile:
        if not teacher_id:
            raise ValueError("Empty teacher id")
        entry = self._profile_paths().get(self._canonical(teacher_id))
        if entry is None:
            raise FileNotFoundError(f"Teacher not found: {teacher_id}")
        path, vendor = entry
        return TeacherProfile(profile_path=path, vendor=vendor)

    def default_teacher_id(self, vendor: str = None) -> str:
        """The configured default (该厂商目录内)，falling back when missing.

        优先级：_active.json 的 default_teacher（旧裸 id 兼容解析）→
        该厂商档案的 default_teacher → config.DEFAULT_TEACHER_ID → 目录里
        第一个。
        """
        want = vendor or _active_tts_vendor()
        configured = self._read_active().get("default_teacher", "")
        paths = self._profile_paths()
        candidates = [self._canonical(configured) if configured else "",
                      self._canonical(str((vendors._REGISTRY.get(want)
                                           or {}).get("tts", {})
                                         .get("default_teacher", "")
                                         or "")),
                      self._canonical(config.DEFAULT_TEACHER_ID)]
        for candidate in candidates:
            if candidate and candidate in paths:
                _, prof_vendor = paths[candidate]
                if prof_vendor == want:
                    return candidate
        in_vendor = [c for c, (_, v) in paths.items() if v == want]
        if in_vendor:
            return sorted(in_vendor)[0]
        raise FileNotFoundError(
            f"No teacher profiles found for vendor '{want}' in "
            f"{self.teachers_dir} or {self.bundled_dir}")

    def default_teacher(self, vendor: str = None) -> TeacherProfile:
        return self.get_teacher(self.default_teacher_id(vendor))

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
        """Return (TeacherProfile, style_id, notice) for this student's next
        session.

        A remembered teacher whose file was deleted is a dangling reference —
        the student falls back to the default teacher. A remembered teacher
        from ANOTHER TTS vendor falls back to the current vendor's default and
        returns a re-select notice（2026-10-21 裁定：切厂商须重选老师，回落
        但不覆盖已存选择——切回原厂商后原老师自然恢复）.
        """
        entry = self._read_active().get("student_choices", {}).get(student_id) or {}
        remembered_id = self._canonical(entry.get("teacher") or "")

        teacher = None
        if remembered_id:
            try:
                teacher = self.get_teacher(remembered_id)
            except Exception:
                print(f"[TeacherStore] Dangling teacher reference "
                      f"'{remembered_id}' for {student_id} — using default")
        notice = ""
        active_vendor = _active_tts_vendor()
        if teacher is not None and teacher.vendor != active_vendor:
            print(f"[TeacherStore] Teacher '{remembered_id}' belongs to "
                  f"{teacher.vendor}, active TTS vendor is {active_vendor} "
                  f"— falling back to default + re-select notice")
            notice = (f"⚠️ 语音厂商已切到 {active_vendor}，你之前的老师 "
                      f"「{teacher.name}」不在新厂商目录里，本次先用默认老师。"
                      "请在「选老师」里重新挑一位。")
            teacher = None
        if teacher is None:
            teacher = self.default_teacher(active_vendor)

        # The style override belongs to the teacher it was chosen for, so it
        # only applies while that exact teacher is still the one teaching.
        style = entry.get("style")
        if style not in config.STYLE_PRESETS or remembered_id != teacher.id:
            style = teacher.style
        return teacher, style, notice

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
                and previous_teacher in (None, "", teacher.id)
            )
            resolved_style = style if style_survives else teacher.style

            active["default_teacher"] = active.get(
                "default_teacher", config.DEFAULT_TEACHER_ID)
            choices[student_id] = {
                "teacher": teacher.id,
                "style": resolved_style,
            }
            try:
                self._write_active(active)
                print(f"[TeacherStore] {student_id} → "
                      f"{teacher.id} / {resolved_style}")
            except Exception as e:
                print(f"[TeacherStore] Failed to save choice: {e}")


# Global store instance
teacher_store = TeacherStore()
