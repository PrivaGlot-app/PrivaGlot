"""
Course Engine v4 — user-authored curriculum.

Two scan roots, deliberately separate — but only one of them is the user's:

    app/bundled_curriculum/   随应用分发的示例教材（PUBLIC）。更新时整体覆盖，
                              用户永远不需要（也不应该）往这里放东西。
    data/curriculum/          用户自己的教材（USER）。网页端「我的教材」读写
                              这里；git pull / 重新解压永远碰不到它。

同名（id 相同）时用户教材覆盖捆绑教材，所以用户可以做出自己的版本替换掉
内置示例。两个渠道都允许不存在；目录平铺，一本教材 = 一个 JSON 文件：

    {
      "format": "privaglot-textbook/1",
      "id": "renjiao-pep-4a",       # 稳定标识，也是学生进度记录的键
      "title": "人教版四年级上册",     # 选课页上显示的名字
      "units": [ { ...unit... } ]
    }

单元在数组里的次序就是上课次序。id 独立于文件名，所以文件可以随便改名、
随便挪位置，学生的练习记录不会丢。文件的 id / unit_id 由「我的教材」生成器
自动填写，用户不需要知道它们的存在。

单元文件只承载三个知识列表（patterns / grammar 允许为空，词汇表是唯一
硬性内容）：

    {
      "unit_id": "unit_1",            # 系统生成
      "title": "Unit 1 Helping at home",  # 展示用，可空
      "vocabulary": ["doctor", ...],  # required, list[str]
      "patterns": ["What's your mother's job? - She's a doctor.", ...],
      "grammar": ["Modal verb 'can' ...", ...]
    }

There are no lessons, tasks, dialogue state graphs, or rubrics. The engine
only scans, validates, catalogs, and renders the unit content for the LLM
prompt. Whether a session "passes" is not a concept anymore.

What a RETURNING student practices is decided after each session by the
lesson evaluator (app/evaluator.py): an AI-maintained subset of the unit's
items ("living material"), stored per student in the profile and rendered
by build_unit_content_block when present. Unit files stay static — the
living material belongs to the student, never to the textbook.

"""
import os
import json
import hashlib
import re
from pathlib import Path
from datetime import datetime

from . import config
from .logging_config import log as logger

FORMAT_ID = "privaglot-textbook/1"

# 学段/年级/出版社等归类字段已随 v4 移除：真实用户手里只有一两本书，
# 排序交给「最近用过 + 标题」。读取旧文件时这些字段被静默忽略。
META_KEYS = ("title",)


def unit_item_text(unit: dict, item_id: str) -> str:
    """Resolve a stable item id ("v0" / "p2" / "g1") to the item's text.

    The lesson evaluator addresses unit items by these ids so its decisions
    stay anchored to the original unit across generations — re-typed prose
    would drift, a reference cannot. Returns "" for unknown/out-of-range ids.
    """
    match = re.fullmatch(r"([vpg])(\d+)", item_id or "")
    if not match:
        return ""
    key = {"v": "vocabulary", "p": "patterns", "g": "grammar"}[match.group(1)]
    items = unit.get(key) or []
    index = int(match.group(2))
    return items[index] if index < len(items) else ""


def slugify_book_id(title: str) -> str:
    """从教材标题生成文件级 id（ascii 小写 + 连字符）。

    中文等非 ascii 标题 slug 出来是空的，退回到时间戳 id —— id 只被系统
    使用，从不展示给用户，丑一点无所谓，稳定和唯一才是它的职责。
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    if not slug:
        slug = f"textbook-{datetime.now():%Y%m%d-%H%M%S}"
    return slug[:64]


# ---------------------------------------------------------------------------
# 一键粘贴录入（#22，2026-10-08）：提取提示词与分段解析的唯一事实源。
# 网页端 static/index.html 的 EXTRACT_PROMPT / parseAISections 与本节逐字符
# 同源，由 tests/kernel/test_textbook_paste.py 的测试锁看住——改任何一边都
# 必须同步另一边，测试会拦下漂移（UPDATE_COMMAND↔README 同款手法）。
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = (
    "请阅读我拍的照片，把这一个单元的内容整理成下面三段，按顺序输出：\n"
    "\n"
    "[词汇]\n"
    "本单元要学的单词和短语，一行一个\n"
    "\n"
    "[句式]\n"
    "本单元出现的问答句型和核心句子，一行一句，问答写成 \"问句 - 答句\"\n"
    "\n"
    "[语法]\n"
    "本单元讲的语法规则，用英文写，每条后面跟一个例子\n"
    "\n"
    "只要这三段，不要中文翻译，不要讲解，不要表格。某一段没有内容就留空。"
)

# 段标记别名 → 教材字段（与网页端 ALIAS 一致）
_EXTRACT_SECTION_ALIASES = {
    "词汇": "vocabulary", "单词": "vocabulary", "生词": "vocabulary",
    "句式": "patterns", "句型": "patterns", "句子": "patterns",
    "语法": "grammar", "语法点": "grammar",
}
_EXTRACT_MARKER_RE = re.compile(
    r"^[\s>#*]*[\[【(（]?(词汇|单词|生词|句式|句型|句子|语法|语法点)"
    r"[\]】)）]?\s*[:：]?\s*$")
_LIST_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)、])\s+")
_MD_BOLD_RE = re.compile(r"^\*+|\*+$")


def parse_ai_sections(text: str) -> dict:
    """把按 EXTRACT_PROMPT 整理的 AI 回复解析成三段（网页端 parseAISections
    的 Python 镜像，逐字符同源）。标记写法尽量宽容：[词汇] / 【词汇】 /
    词汇： / **语法** / 1. 句式 都认；行首列表符号与 markdown 加粗剥掉。

    返回 {"vocabulary": [...], "patterns": [...], "grammar": [...],
    "found": 命中的段标记数}；found 为 0 表示没有可识别的标记（调用方据此
    让用户重发）。中文内容照原样保留——过滤是调用方的职责，网页端同样
    只警告不过滤。
    """
    res: dict = {"vocabulary": [], "patterns": [], "grammar": [], "found": 0}
    current = None
    for raw_line in re.split(r"\r?\n", text or ""):
        marker = _EXTRACT_MARKER_RE.match(raw_line)
        if marker:
            current = res[_EXTRACT_SECTION_ALIASES[marker.group(1)]]
            res["found"] += 1
            continue
        if current is None:
            continue
        line = _LIST_BULLET_RE.sub("", raw_line)
        line = _MD_BOLD_RE.sub("", line).strip()
        if line:
            current.append(line)
    return res


def validate_book_dict(data: dict) -> list[str]:
    """校验用户在「我的教材」里组装出的教材对象，返回中文问题列表（空 = 通过）。

    与扫描期的 _validate_unit 分工：那边是英文、最小化、给日志看；这边面向
    用户，把「为什么保存不了」用人话说清楚。规则只有内容性的：
    书名必填、至少一个单元、词汇表非空、三个列表只能是英文且不重复。
    patterns / grammar 允许为空。
    """
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["教材数据必须是一个 JSON 对象"]

    title = str(data.get("title") or "").strip()
    if not title:
        problems.append("教材名称不能为空")

    units = data.get("units")
    if not isinstance(units, list) or not units:
        problems.append("至少要有一个单元")
        return problems

    seen_unit_ids = set()
    for index, unit in enumerate(units):
        tag = f"第 {index + 1} 个单元"
        if not isinstance(unit, dict):
            problems.append(f"{tag}：内容不是有效的单元数据")
            continue

        unit_id = str(unit.get("unit_id") or "").strip()
        if unit_id:
            if unit_id in seen_unit_ids:
                problems.append(f"{tag}：unit_id {unit_id!r} 重复（进度会互相覆盖）")
            seen_unit_ids.add(unit_id)
            if re.search(r"[\s/\\]", unit_id):
                problems.append(f"{tag}：unit_id {unit_id!r} 不能含空格或斜杠")

        # 单元标题可空（展示时退回 unit_id），但给了就必须是字符串。
        if not isinstance(unit.get("title", ""), str):
            problems.append(f"{tag}：单元标题必须是字符串")

        problems.extend(_check_unit_lists(unit, tag))
    return problems


def _check_unit_lists(unit: dict, tag: str) -> list[str]:
    """三个知识列表的内容检查（中文字符 / 空串 / 重复 / 类型）。"""
    problems: list[str] = []
    cjk = re.compile(r"[\u4e00-\u9fff]")
    for key, label in (("vocabulary", "词汇"), ("patterns", "句式"),
                       ("grammar", "语法")):
        value = unit.get(key)
        if value is None:
            if key == "vocabulary":
                problems.append(f"{tag}：词汇表不能为空")
            continue
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            problems.append(f"{tag}：{label}必须是文本列表")
            continue
        if key == "vocabulary" and not value:
            problems.append(f"{tag}：词汇表不能为空")
            continue
        empty = [x for x in value if not x.strip()]
        if empty:
            problems.append(f"{tag}：{label}里有 {len(empty)} 个空行")
        chinese = [x for x in value if cjk.search(x)]
        if chinese:
            sample = chinese[0][:30]
            problems.append(f"{tag}：{label}里有 {len(chinese)} 条含中文"
                            f"（{sample!r}）—— 这是英文口语陪练，只填英文")
        if key in ("vocabulary", "patterns"):
            seen, dup = set(), 0
            for x in value:
                if x in seen:
                    dup += 1
                seen.add(x)
            if dup:
                problems.append(f"{tag}：{label}里有 {dup} 个重复项"
                                "（词汇重复会让课后报告算错）")
    return problems


class CourseEngine:
    """Scans, validates, and serves unit content from textbook files."""

    def __init__(self):
        # bundled 在应用包内，随仓库分发、更新时整体覆盖；user 在 data/ 下，
        # 永不被更新碰。用户视角只有一个教材目录：data/curriculum/。
        self.bundled_dir = Path(__file__).parent / "bundled_curriculum"
        self.user_dir = config.DATA_DIR / "curriculum"

        # Catalog: { textbook_id: {source, meta, path, units} }
        # Stateless by design — sessions bind to a unit explicitly via
        # SessionContext, so concurrent sessions can never leak into each other.
        self._catalog: dict[str, dict] = {}


    @staticmethod
    def _is_hidden(path: Path, base: Path) -> bool:
        return any(part.startswith("_") for part in path.relative_to(base).parts)

    # ------------------------------------------------------------------
    # Catalog scanning
    # ------------------------------------------------------------------

    def load(self):
        """扫描两个渠道，索引每一本教材的每一个有效单元。"""
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self._catalog.clear()

        # 顺序即优先级：先捆绑后用户，同名（id）时用户覆盖捆绑。
        for source, base in (("bundled", self.bundled_dir),
                             ("user", self.user_dir)):
            if not base.exists():
                continue
            for path in sorted(base.rglob("*.json")):
                if self._is_hidden(path, base):
                    continue
                self._index_book_file(path, source)

        logger.info(f"Catalog loaded: {len(self._catalog)} textbook(s), "
                    f"{sum(len(t['units']) for t in self._catalog.values())} unit(s) total")

    def _index_book_file(self, path: Path, source: str):
        """把一本教材文件读进 catalog。

        教材名取文件里的 id（没有就退回文件名），因为 id 是学生进度记录的
        键：文件可以随便改名、随便挪位置，练习记录都不会丢。
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            # 最常见的原因是 AI 的输出被截断 —— 这条日志就是给那种情况留的
            logger.error(f"[{path.name}] 教材文件 JSON 解析失败：{e}")
            return
        except Exception as e:
            logger.error(f"[{path.name}] 教材文件读取失败：{e}")
            return

        if not isinstance(data, dict) or not isinstance(data.get("units"), list):
            # 不是教材文件（可能是别的配置），安静跳过
            return

        textbook_id = str(data.get("id") or path.stem).strip()
        units = {}
        for index, unit in enumerate(data["units"]):
            if not isinstance(unit, dict):
                logger.error(f"[{textbook_id}] 第 {index + 1} 个单元不是 JSON 对象，已跳过")
                continue
            errors = _validate_unit(unit)
            if errors:
                if "legacy" in errors:
                    continue
                for e in errors:
                    logger.error(f"[{textbook_id}] 第 {index + 1} 个单元：{e}")
                continue
            unit_id = unit.get("unit_id") or f"unit_{index + 1}"
            units[unit_id] = unit

        if textbook_id in self._catalog:
            old = self._catalog[textbook_id]
            if old["source"] == "user" and source == "bundled":
                return  # 用户的同名教材优先，捆绑副本让位
            logger.info(f"[{source}] {textbook_id} 覆盖了之前的同名教材")

        meta = {k: data[k] for k in META_KEYS if isinstance(data.get(k), str) and data[k].strip()}
        meta.setdefault("title", textbook_id)
        self._catalog[textbook_id] = {
            "source": source,
            "meta": meta,
            "path": str(path),
            "units": units,
        }

    # ------------------------------------------------------------------
    # Catalog queries (for API / frontend)
    # ------------------------------------------------------------------

    def get_textbook_names(self) -> list[str]:
        """教材 id 列表，按展示标题排序（最近使用排序在 API 层做，那里才有学生）。"""
        return sorted(self._catalog, key=self._book_sort_key)

    def _book_sort_key(self, textbook_id: str) -> tuple:
        meta = (self._catalog.get(textbook_id) or {}).get("meta") or {}
        return (str(meta.get("title") or "").lower(), textbook_id)

    def book_title(self, textbook_id: str) -> str:
        meta = (self._catalog.get(textbook_id) or {}).get("meta") or {}
        return str(meta.get("title") or "").strip() or textbook_id

    def get_textbook(self, textbook_id: str) -> dict | None:
        return self._catalog.get(textbook_id)

    def get_unit(self, textbook_id: str, unit_id: str) -> dict | None:
        tb = self._catalog.get(textbook_id)
        if not tb:
            return None
        return tb["units"].get(unit_id)

    def list_units_summary(self, textbook_id: str) -> list[dict]:
        tb = self._catalog.get(textbook_id)
        if not tb:
            return []
        return [
            {
                "unit_id": unit_id,
                "title": data.get("title", ""),
                "word_count": len(data.get("vocabulary", [])),
            }
            for unit_id, data in tb["units"].items()
        ]

    def unit_fingerprint(self, textbook_id: str, unit_id: str) -> str:
        """Stable hash of a unit's three content lists.

        The living material references items by position, so an edited unit
        (items added / removed / reordered) invalidates it: the caller
        compares fingerprints and discards stale material instead of teaching
        against pointers into a unit that no longer exists.
        """
        unit = self.get_unit(textbook_id, unit_id)
        if not unit:
            return ""
        payload = json.dumps([unit.get("vocabulary") or [],
                              unit.get("patterns") or [],
                              unit.get("grammar") or []], ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def material_vocabulary(self, textbook_id: str, unit_id: str,
                            material: dict) -> list[str]:
        """The vocabulary texts a living material carries (matcher targets)."""
        unit = self.get_unit(textbook_id, unit_id)
        if not unit:
            return []
        out = []
        for entry in material.get("carry") or []:
            item_id = entry.get("id") if isinstance(entry, dict) else ""
            if isinstance(item_id, str) and item_id.startswith("v"):
                text = unit_item_text(unit, item_id)
                if text:
                    out.append(text)
        return out

    # ------------------------------------------------------------------
    # Saving user-authored textbooks (「我的教材」生成器)
    # ------------------------------------------------------------------

    def save_new_book(self, title: str, units: list[dict]) -> dict:
        """把生成器组装的教材写成用户目录下的 JSON 文件。

        id / unit_id / format 在这里生成，用户永远不用碰。写完即重扫 catalog，
        选课页立刻可见。同名文件靠 id 去重保证不会覆盖任何已有教材。
        """
        title = str(title or "").strip()
        base_id = slugify_book_id(title)
        book_id = base_id
        counter = 2
        while book_id in self._catalog or (self.user_dir / f"{book_id}.json").exists():
            book_id = f"{base_id}-{counter}"
            counter += 1

        clean_units = []
        for index, unit in enumerate(units):
            clean_units.append({
                "unit_id": str(unit.get("unit_id") or f"unit_{index + 1}"),
                "title": str(unit.get("title") or "").strip(),
                "vocabulary": [w for w in unit.get("vocabulary", []) if str(w).strip()],
                "patterns": [p for p in unit.get("patterns", []) if str(p).strip()],
                "grammar": [g for g in unit.get("grammar", []) if str(g).strip()],
            })

        book = {
            "format": FORMAT_ID,
            "id": book_id,
            "title": title,
            "units": clean_units,
        }
        self.user_dir.mkdir(parents=True, exist_ok=True)
        target = self.user_dir / f"{book_id}.json"
        target.write_text(json.dumps(book, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        logger.info(f"Saved new textbook: {target.name} "
                    f"({len(clean_units)} unit(s))")
        self.load()
        return {"id": book_id, "title": title, "path": str(target)}

    # ------------------------------------------------------------------
    # Editing & deleting user-authored textbooks
    # ------------------------------------------------------------------

    def _user_book_path(self, textbook_id: str) -> Path | None:
        """这本教材的文件路径，前提是它是可编辑的用户教材；否则 None。

        双重防护：id 必须在 catalog 里且 source 为 user，解析后的真实路径
        必须落在用户目录内（防路径穿越，虽然 URL 路由本身已不含斜杠）。
        """
        entry = self._catalog.get(textbook_id)
        if not entry or entry.get("source") != "user":
            return None
        path = Path(entry.get("path") or "")
        try:
            path.resolve().relative_to(self.user_dir.resolve())
        except ValueError:
            return None
        return path

    def get_user_book(self, textbook_id: str) -> dict | None:
        """读回一本用户教材的完整内容（编辑器预填用）。捆绑教材不可编辑。"""
        path = self._user_book_path(textbook_id)
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            logger.error(f"[{textbook_id}] 教材读取失败：{e}")
            return None
        return data if isinstance(data, dict) else None

    def update_book(self, textbook_id: str, title: str, units: list[dict]) -> dict | None:
        """把编辑器提交的内容写回原文件。返回 None 表示教材不可编辑。

        身份绝不改变：教材 id 不变，已有单元的 unit_id 原样保留（进度键），
        只有新增单元领取新号（现有最大编号 +1）。单元顺序即提交时的数组
        顺序，unit_id 跟着内容走，重排不影响进度。
        """
        path = self._user_book_path(textbook_id)
        if path is None:
            return None

        taken: set[str] = set()
        max_n = 0
        for unit in units:
            uid = str(unit.get("unit_id") or "").strip()
            if uid:
                taken.add(uid)
                m = re.fullmatch(r"unit_(\d+)", uid)
                if m:
                    max_n = max(max_n, int(m.group(1)))

        clean_units = []
        next_n = max_n + 1
        for unit in units:
            uid = str(unit.get("unit_id") or "").strip()
            if not uid:
                while f"unit_{next_n}" in taken:
                    next_n += 1
                uid = f"unit_{next_n}"
                taken.add(uid)
                next_n += 1
            clean_units.append({
                "unit_id": uid,
                "title": str(unit.get("title") or "").strip(),
                "vocabulary": [w for w in unit.get("vocabulary", []) if str(w).strip()],
                "patterns": [p for p in unit.get("patterns", []) if str(p).strip()],
                "grammar": [g for g in unit.get("grammar", []) if str(g).strip()],
            })

        title = str(title or "").strip()
        book = {
            "format": FORMAT_ID,
            "id": textbook_id,
            "title": title,
            "units": clean_units,
        }
        path.write_text(json.dumps(book, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        logger.info(f"Updated textbook: {path.name} ({len(clean_units)} unit(s))")
        self.load()
        return {"id": textbook_id, "title": title}

    def delete_book(self, textbook_id: str) -> bool:
        """删除一本用户教材文件并重扫 catalog。

        只删教材本身：学生进度记录留在档案里（休眠键，无害），重新放入
        同 id 的教材文件进度会自动恢复。捆绑教材不可删除。
        """
        path = self._user_book_path(textbook_id)
        if path is None:
            return False
        try:
            path.unlink()
        except OSError as e:
            logger.error(f"Failed to delete {path.name}: {e}")
            return False
        logger.info(f"Deleted textbook: {path.name}")
        self.load()
        return True

    # ------------------------------------------------------------------
    # LLM prompt rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _render_unit_items(lines: list, unit: dict, with_focus: bool = False,
                           material: dict | None = None):
        """Push the three knowledge lists into `lines`.

        Living-material mode (with_focus) renders ONLY the carried items,
        one per line, attaching each item's "why" as a parenthesized focus
        cue for the teacher; full-unit mode keeps the original compact
        rendering.
        """
        carried: set[str] | None = None
        whys: dict[str, str] = {}
        if with_focus and material:
            carried = set()
            for entry in material.get("carry") or []:
                if not isinstance(entry, dict):
                    continue
                item_id = str(entry.get("id") or "")
                carried.add(item_id)
                if entry.get("why"):
                    whys[item_id] = str(entry["why"]).strip()

        vocab = unit.get("vocabulary") or []
        if vocab:
            if with_focus:
                shown = [(i, w) for i, w in enumerate(vocab) if f"v{i}" in carried]
                if shown:
                    lines.append("Vocabulary:")
                    for i, word in shown:
                        why = whys.get(f"v{i}")
                        lines.append(f"- {word}"
                                     + (f" (focus: {why})" if why else ""))
            else:
                lines.append("Vocabulary: " + ", ".join(vocab))
        for key, label, prefix in (("patterns", "Sentence patterns:", "p"),
                                   ("grammar", "Grammar focus:", "g")):
            items = unit.get(key) or []
            if not items:
                continue
            shown = [(i, item) for i, item in enumerate(items)
                     if carried is None or f"{prefix}{i}" in carried]
            if not shown:
                continue
            lines.extend(["", label])
            for i, item in shown:
                bullet = f'- "{item}"' if key == "patterns" else f"- {item}"
                if with_focus:
                    why = whys.get(f"{prefix}{i}")
                    bullet += f" (focus: {why})" if why else ""
                lines.append(bullet)

    def build_unit_content_block(self, textbook: str, unit_id: str,
                                 material: dict | None = None) -> str:
        """Render a unit's knowledge lists as the prompt's [UNIT] block.

        With a living material (the lesson evaluator's carry list) only those
        items render, framed as a continuing session; an empty carry list
        renders a consolidation chat. Without one the full unit renders as
        before. The block carries its own teaching guidance so the base
        system prompt stays content-agnostic. Empty lists render as nothing —
        a vocabulary-only unit is legitimate.
        """
        unit = self.get_unit(textbook, unit_id)
        if not unit:
            return ""

        title = unit.get("title", "") or unit.get("unit_id", "")
        lines = []

        if material is None:
            lines.extend([
                f"[UNIT MATERIAL — {title}]",
                "Today's textbook unit. Guide the conversation so the student "
                "practices this material:",
                "",
            ])
            self._render_unit_items(lines, unit)
        elif material.get("carry"):
            lines.extend([
                f"[UNIT MATERIAL — {title}] (continuing session)",
                "This student has practiced this unit before. Today's material "
                "is ONLY the items below — the rest have been practiced well "
                "in earlier sessions and are resting:",
                "",
            ])
            self._render_unit_items(lines, unit, with_focus=True,
                                    material=material)
        else:
            lines.extend([
                f"[UNIT MATERIAL — {title}] (consolidation session)",
                "Every item of this unit was judged well practiced in earlier "
                "sessions, so there is no fixed material today. Keep the "
                "student talking freely within this unit's topics; revisit "
                "anything that sounds shaky.",
                "",
            ])

        note = str(material.get("note") or "").strip() if material else ""
        if note:
            lines.extend([
                "",
                "[TEACHER'S NOTE — from the last session's evaluation]",
                note,
            ])

        if material is None or material.get("carry"):
            lines.extend([
                "",
                "Teach through conversation: ask questions whose ANSWERS naturally "
                "use the vocabulary and patterns above. Do not quiz definitions "
                "(never ask what a word means), and do not say the target sentence "
                "yourself — elicit it from the student. When the student makes an "
                "error with this material, gently correct it. If the chat drifts "
                "away from these topics, steer it back. Once the material has been "
                "well practiced, keep chatting freely within the unit's topics. "
                "In your first reply, naturally bring up this unit's topic and "
                "ask the first question about it.",
            ])
        return "\n".join(lines)


def _validate_unit(data: dict) -> list[str]:
    """Validate the three-key unit format. Returns a list of problems.

    A single "legacy" entry means the file is an old lesson-based unit JSON,
    which the caller should skip silently rather than treat as broken data.
    patterns / grammar may be empty or absent; vocabulary is the only
    mandatory content.
    """
    if not isinstance(data, dict):
        return ["unit file must be a JSON object"]
    if "lessons" in data or "tasks" in data:
        return ["legacy"]

    problems = []
    vocab = data.get("vocabulary")
    if vocab is None:
        problems.append("missing required key 'vocabulary'")
    elif not isinstance(vocab, list) or not all(isinstance(x, str) for x in vocab):
        problems.append("'vocabulary' must be a list of strings")
    elif not vocab:
        problems.append("'vocabulary' must not be empty")

    for key in ("patterns", "grammar"):
        val = data.get(key)
        if val is None:
            continue
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            problems.append(f"'{key}' must be a list of strings")
    return problems


# Global instance
course_engine = CourseEngine()
