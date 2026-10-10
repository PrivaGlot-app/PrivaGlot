"""
教材管理对话流（平台无关）—— 纯文本指令 + 分步向导，聊天里完成教材 CRUD。

设计约束（2026-09-21 产品共识）：
  - 录入协议是纯文本指令语法，任何聊天平台都能原样实现；飞书菜单/按钮
    只是可选加速，永远不是依赖。
  - 核心契约只接受结构化教材：校验复用 course_engine.validate_book_dict，
    校验不过绝不落盘 —— 永不产生脏数据。项目不做"理解原始教材"的解析：
    课本照片永远交给用户自携的外部 AI（豆包/Kimi），机器人只接收按提取
    提示词（course_engine.EXTRACT_PROMPT）整理好的回复，解析也只认
    [词汇]/[句式]/[语法] 三段标记（course_engine.parse_ai_sections，
    #22 2026-10-08）。
  - 每次写入前逐条回显确认（聊天等价于网页编辑器的实时预览+报错区）。

本模块不 import 任何平台 SDK：store 通过构造注入（生产用 CourseEngineStore
适配 course_engine，测试用内存假件），handle() 吃一条文字、吐若干条回复。

用法：
    flow = TextbookFlow(CourseEngineStore())
    for reply in flow.handle("建教材"):
        send(reply)
"""
import re

# 指令帮助（管理模式内的命令面）。平台壳层（如飞书通道）负责模式外的
# 通用指令（上课/进度等），这里的帮助只覆盖管理模式。
MANAGEMENT_HELP = (
    "📖 教材管理（发送文字指令）\n"
    "· 看教材 —— 列出全部教材\n"
    "· 看单元 序号 —— 看某本书的单元列表\n"
    "· 看内容 书序号 单元序号 —— 看单元完整内容\n"
    "· 建教材 —— 分步创建一本新教材\n"
    "· 提示词 —— 拿「拍照 → 外部 AI 整理」的提取提示词\n"
    "· 粘贴 —— （向导内容步骤）把 AI 整理好的回复整段贴入\n"
    "· 加单元 书序号 [单元标题] —— 给已有教材添加单元\n"
    "· 改单元 书序号 单元序号 —— 覆盖式修改单元的标题/词汇/句式/语法\n"
    "· 改书名 书序号 新书名\n"
    "· 删单元 书序号 单元序号 / 删教材 书序号\n"
    "· 退出 —— 回到上课")


class CourseEngineStore:
    """把 course_engine 适配成 TextbookFlow 需要的窄接口。

    只暴露流程真正用到的方法；id/ unit_id/ format 的生成、文件命名、
    catalog 重扫全部留在 course_engine 里，这里不做二次实现。
    """

    def __init__(self, engine=None):
        from .course_engine import course_engine
        self._engine = engine or course_engine

    def list_books(self) -> list[dict]:
        books = []
        for textbook_id in self._engine.get_textbook_names():
            tb = self._engine.get_textbook(textbook_id) or {}
            books.append({
                "id": textbook_id,
                "title": self._engine.book_title(textbook_id) or textbook_id,
                "source": tb.get("source", "user"),
                "units": len(tb.get("units") or []),
            })
        return books

    def get_book(self, book_id: str) -> dict | None:
        return self._engine.get_user_book(book_id)

    def read_book(self, book_id: str) -> dict | None:
        """任何教材（含内置示例）的只读视图——「看单元/看内容」用。
        catalog 里 units 是字典（unit_id 为键），这里归一成与 get_user_book
        相同的列表形状，向导的渲染逻辑不用分叉。"""
        entry = self._engine.get_textbook(book_id)
        if not entry:
            return None
        units = entry.get("units") or {}
        unit_list = [dict(u, unit_id=uid) for uid, u in units.items()] \
            if isinstance(units, dict) else [dict(u) for u in units]
        meta = entry.get("meta") or {}
        return {"id": book_id,
                "title": str(meta.get("title") or book_id),
                "source": entry.get("source", "user"),
                "units": unit_list}

    def save_new_book(self, title: str, units: list[dict]) -> str:
        return self._engine.save_new_book(title, units)["id"]

    def update_book(self, book_id: str, title: str, units: list[dict]) -> bool:
        return self._engine.update_book(book_id, title, units) is not None

    def delete_book(self, book_id: str) -> bool:
        return self._engine.delete_book(book_id)

    def validate(self, title: str, units: list[dict]) -> list[str]:
        # 校验是纯函数（course_engine.validate_book_dict，网页端同一份），
        # 不需要引擎实例——曾误写成 self._engine.validate_book_dict 导致
        # 真机 AttributeError（引擎类上没有该方法；假件按错误假设实现了
        # 同名方法所以离线测试全绿，2026-09-22 第七轮实测暴露）。
        from .course_engine import validate_book_dict
        return validate_book_dict({"title": title, "units": units})


class TextbookFlow:
    """一个聊天用户一份实例（挂在通道的会话对象上）。handle() 返回回复列表。

    状态机只有两层：命令态（_state=None）和一种未完成的操作（向导进行中 /
    等待确认）。没有嵌套状态，出错时用「取消」一键回到命令态。
    """

    def __init__(self, store: CourseEngineStore):
        self._store = store
        self._state: dict | None = None

    # ------------------------------------------------------------------
    # entry
    # ------------------------------------------------------------------

    def welcome(self) -> list[str]:
        """进入管理模式的第一屏：书单 + 提示。"""
        return [_MANAGE_BANNER, *self._book_list_lines(), MANAGEMENT_HELP]

    def handle(self, text: str) -> list[str]:
        text = (text or "").strip()
        if not text:
            return []
        if self._state is not None:
            return self._handle_in_op(text)
        return self._handle_command(text.lower(), text)

    def book_list_lines(self) -> list[str]:
        """书单文本行（通道进管理模式时拼进菜单卡正文；纯文字模式下
        「看教材」指令照常输出同样内容——协议不变）。"""
        return self._book_list_lines()

    def step_hint(self) -> dict | None:
        """当前步骤的只读元数据，供通道把「此刻需要你做选择」的时刻渲染成
        卡片（GUI 等效）。文字协议不受影响：没有通道读它时，纯文字交互
        照旧完整；本模块也不依赖任何调用方读它。

        None = 命令态（无进行中的操作）。否则：
          expect=confirm    —— 等「确认/取消」（含无 step 键的纯确认操作）
          expect=words      —— 等多行内容（词汇清单/整段覆盖内容；带 label
                               元数据，通道渲染一键粘贴卡，#22 二轮——
                               文字打字与聊天「粘贴」照常完整）
          expect=paste_confirm —— 一键粘贴解析完成，等「确认/重贴/取消」
          expect=skip       —— 句式/语法步骤，可「跳过」
          expect=form       —— 单行文本输入（附 label/placeholder；
                               extra=done 表示也可以「完成」收尾）
          expect=edit_field —— 改单元的字段选择（附 label）
        """
        st = self._state
        if st is None:
            return None
        if st.get("paste_result") is not None:
            # 一键粘贴解析完成待确认：独立卡片身份（#22 三轮）——此前
            # 复用 words 导致粘贴卡重渲染，与「回复确认」文本自相矛盾
            return {"expect": "paste_confirm"}
        if st["op"] == "rename" and st.get("step") == "new_title":
            # #25 #12：选择器卡按钮带出「改书名 N」（新书名是必选自由文本，
            # 按钮带不全）→ 两步式小向导，第二步递新书名表单卡
            return {"expect": "form",
                    "label": f"把「{st['book_title']}」改名为什么？",
                    "placeholder": "如：英语人教版四年级上册"}
        step = st.get("step") or ""
        if step.startswith("confirm") or not step:
            return {"expect": "confirm"}
        if st["op"] == "edit_unit":
            if step == "field":
                return {"expect": "edit_field",
                        "label": f"改「{st['book_title']}」的单元——改哪一项？"}
            if step == "edit_title":
                return {"expect": "form", "label": "新单元标题（覆盖旧标题）",
                        "placeholder": "如 U3"}
            # vocabulary_input / patterns_input / grammar_input：整段覆盖
            # 的新内容，粘贴整段回复只取当前字段
            name = _FIELD_NAMES[st["step"].replace("_input", "")]
            return {"expect": "words",
                    "label": f"改「{st['unit_title']}」的{name}（整段覆盖）"}
        if step == "vocab":
            if st["op"] == "new_book":
                unit_title = (st["units"][-1].get("title") or "") \
                    if st["units"] else ""
            else:
                unit_title = st["unit"].get("title") or ""
            return {"expect": "words",
                    "label": f"单元「{unit_title}」内容（词汇/句式/语法）"}
        if step in ("patterns", "grammar"):
            return {"expect": "skip"}
        if step == "next_unit":
            return {"expect": "form", "extra": "done",
                    "label": "继续加下一个单元——发标题，或点「完成」收尾",
                    "placeholder": "如 U2"}
        if step in ("title", "unit_title"):
            if st["op"] == "new_book" and step == "title":
                return {"expect": "form", "label": "给新教材起个名字",
                        "placeholder": "如：英语人教版四年级上册"}
            return {"expect": "form", "label": "单元标题",
                    "placeholder": "如 U3"}
        return None

    # ------------------------------------------------------------------
    # state: idle command mode
    # ------------------------------------------------------------------

    def _handle_command(self, cmd: str, raw: str) -> list[str]:
        if cmd in ("看教材", "教材列表", "列表"):
            return [*self._book_list_lines(),
                    "看单元：看单元 序号；看内容：看内容 书序号 单元序号"]

        if cmd.startswith("看单元"):
            return self._cmd_list_units(cmd)
        if cmd.startswith("看内容"):
            return self._cmd_show_unit(cmd)

        if cmd in ("建教材", "新建教材", "创建教材"):
            self._state = {"op": "new_book", "step": "title",
                           "title": "", "units": []}
            return ["给新教材起个名字（如：英语人教版四年级上册）："]

        if cmd.startswith("加单元"):
            return self._cmd_add_unit(cmd, raw)
        if cmd.startswith("改单元"):
            return self._cmd_edit_unit(cmd, raw)
        if cmd.startswith("改书名"):
            return self._cmd_rename(cmd, raw)
        if cmd.startswith("删单元"):
            return self._cmd_delete_unit(cmd)
        if cmd.startswith("删教材"):
            return self._cmd_delete_book(cmd)

        if cmd in ("提示词", "提取提示词"):
            return self._prompt_replies(in_wizard=False)
        if cmd in ("粘贴", "贴入"):
            return ["「粘贴」在教材向导的内容步骤用：发「建教材」（或"
                    "「加单元 / 改单元 …」）进入向导，到词汇步骤再发「粘贴」，"
                    "把 AI 按提示词整理的回复整段贴进来。发「提示词」可先拿"
                    "提取提示词。"]

        if cmd in ("帮助", "指令", "help"):
            return [MANAGEMENT_HELP]
        return ["没看懂这条指令。" + MANAGEMENT_HELP]

    def _handle_in_op(self, text: str) -> list[str]:
        stripped = text.strip()
        low = stripped.lower()
        if self._state.get("paste"):
            # 粘贴态有自己的「取消」语义：只退出粘贴、回到刚才的输入步骤，
            # 不放弃整个向导（#22 2026-10-08）。
            if low in ("取消", "算了", "cancel"):
                self._state.pop("paste", None)
                self._state.pop("paste_result", None)
                return ["已退出粘贴，回到手动输入。"] + self._current_step_prompt()
            return self._step_paste(stripped, low)
        if low in ("取消", "算了", "cancel"):
            self._state = None
            return ["已取消。可以继续发指令，或发「退出」回到上课。"]
        if low in ("提示词", "提取提示词"):
            # 向导中途拿提示词：纯信息，不动状态（内容步骤发「粘贴」才是入口）
            return self._prompt_replies(in_wizard=True)
        if low == "粘贴" or low.startswith("粘贴\n"):
            if self._state.get("step") in _PASTE_CAPABLE_STEPS:
                self._state["paste"] = True
                # 「粘贴」与整段内容同条发送 = 原子一键粘贴（飞书卡片提交
                # 的等价文字路径）：一次完成进粘贴态+解析，不出中间等待文案
                content = stripped[2:].strip()
                if content:
                    return self._step_paste(content, content.lower())
                return [_PASTE_WAIT_TEXT]
            return ["当前步骤不用粘贴，直接按提示输入即可。"]
        op = self._state["op"]
        if op == "new_book":
            if self._state["step"] == "confirm_new":
                return self._confirm_branch(low)
            return self._step_new_book(stripped, low)
        if op == "add_unit":
            if self._state["step"] == "confirm_add":
                return self._confirm_branch(low)
            return self._step_add_unit(stripped, low)
        if op == "edit_unit":
            if self._state["step"] == "confirm_edit":
                return self._confirm_branch(low)
            return self._step_edit_unit(stripped, low)
        if op == "rename" and self._state.get("step") == "new_title":
            return self._step_rename_title(stripped, low)
        # 纯确认态（删单元/删教材/改书名）
        return self._confirm_branch(low)

    def _confirm_branch(self, low: str) -> list[str]:
        if low in ("确认", "确定", "ok", "yes"):
            return self._commit_pending()
        return ["回复「确认」执行，或「取消」放弃。"]

    # ------------------------------------------------------------------
    # 一键粘贴（#22 2026-10-08）：把网页端「从 AI 提取结果粘贴」搬进对话。
    # 只服务内容录入步（_PASTE_CAPABLE_STEPS）：创建流整段贴入、各段归各
    # 字段并跳过已满步骤；改单元流只取当前字段、走既有覆盖确认。
    # ------------------------------------------------------------------

    def paste_active(self) -> bool:
        """通道语音护栏用：是否处于「等待粘贴 / 解析待确认」态。"""
        return bool(self._state and self._state.get("paste"))

    def _prompt_replies(self, in_wizard: bool) -> list[str]:
        """「提示词」指令。提取提示词单独成一条消息——聊天里长按即可复制，
        是网页端「复制提取提示词」按钮的等价物。不改向导状态。"""
        from .course_engine import EXTRACT_PROMPT
        tail = ("拿到 AI 回复后，回到这里发「粘贴」，把回复整段贴进来。"
                if in_wizard else
                "拿到 AI 回复后，发「建教材」（或「加单元 / 改单元 …」）进入"
                "向导，在内容步骤发「粘贴」，把回复整段贴进来。")
        return ["📋 教材提取提示词——长按复制下面整段，连同课本照片发给"
                "豆包 / Kimi / ChatGPT：",
                EXTRACT_PROMPT,
                tail]

    def _step_paste(self, text: str, low: str) -> list[str]:
        from .course_engine import parse_ai_sections
        st = self._state
        if low in ("提示词", "提取提示词"):
            return self._prompt_replies(in_wizard=True) + [
                "（仍在粘贴等待中，拿到回复直接整段贴进来即可。）"]
        if low in ("粘贴", "贴入"):
            if st.get("paste_result") is not None:
                # 「重贴」按钮的等价指令：清掉上次解析，回到粘贴等待
                # （expect 复原为 words，粘贴卡重新递上）
                st.pop("paste_result", None)
                return [_PASTE_WAIT_TEXT]
            return ["已经在等粘贴了——把 AI 的回复整段发过来即可。"]
        if st.get("paste_result") is not None:
            # 解析完成待确认：确认采用；其余输入一律视为重新解析
            # （无「重贴」指令，重发即重试——2026-10-08 拍板）
            if low in ("确认", "确定", "ok", "yes"):
                return self._paste_commit()
            parsed = parse_ai_sections(text)
            if not parsed["found"]:
                return ["这段里没有 [词汇]/[句式]/[语法] 分段标记——重发整段"
                        "回复重新解析，或「确认」采用上一次解析，或「取消」"
                        "退出粘贴。"]
            st["paste_result"] = parsed
            return self._paste_summary(parsed)
        parsed = parse_ai_sections(text)
        if not parsed["found"]:
            return ["没有找到 [词汇]/[句式]/[语法] 分段标记——请确认粘贴的是"
                    "按提取提示词整理的完整回复（发「提示词」可再拿一次）。"
                    "重发或「取消」退出粘贴。"]
        missing = self._paste_missing_current_field(parsed)
        if missing:
            return [missing]
        st["paste_result"] = parsed
        return self._paste_summary(parsed)

    def _paste_missing_current_field(self, parsed: dict) -> str:
        """当前步骤要的那个字段在解析结果里是空的：留在粘贴态让用户重发，
        不进确认。创建流空段=后面的步骤还要用；改单元流绝不把「整段覆盖
        成空」当默认语义——清空必须走显式的「清空」。"""
        st = self._state
        if st["op"] == "edit_unit":
            field = st["step"].replace("_input", "")
            if parsed[field]:
                return ""
            name = _FIELD_NAMES[field]
            return (f"解析到了内容，但里面没有{name}段（该段可能为空）。要"
                    f"清空{name}，请「取消」退出粘贴后发「清空」；重发完整"
                    "回复可再解析。")
        field = {"vocab": "vocabulary", "patterns": "patterns",
                 "grammar": "grammar"}[st["step"]]
        if parsed[field]:
            return ""
        name = _FIELD_NAMES[field]
        return (f"解析到了内容，但里面没有{name}段（该段可能为空）。当前"
                f"步骤正等着{name}——重发完整回复，或「取消」退出粘贴。")

    def _paste_summary(self, parsed: dict) -> list[str]:
        st = self._state
        lines = [f"✓ 解析完成：词汇 {len(parsed['vocabulary'])} 条、"
                 f"句式 {len(parsed['patterns'])} 条、"
                 f"语法 {len(parsed['grammar'])} 条。"]
        cjk = re.compile(r"[\u4e00-\u9fff]")
        dirty = [_FIELD_NAMES[key]
                 for key in ("vocabulary", "patterns", "grammar")
                 if any(cjk.search(x) for x in parsed[key])]
        if dirty:
            lines.append(f"⚠️ {'、'.join(dirty)}里检测到中文——这是英文口语"
                         "陪练，建议重发删掉中文的版本再采用。")
        if st["op"] == "edit_unit":
            lines.append(f"采用后只把{_FIELD_NAMES[st['field']]}段填入"
                         f"「{st['unit_title']}」（覆盖原内容）。")
        lines.append("回复「确认」采用；重发整段回复可重新解析；「取消」退出粘贴。")
        return lines

    def _paste_commit(self) -> list[str]:
        st = self._state
        parsed = st.pop("paste_result")
        st.pop("paste", None)
        if st["op"] == "edit_unit":
            field = st["step"].replace("_input", "")
            value = parsed[field]
            if not value:  # 理论上已被 _paste_missing_current_field 拦下
                st["paste"] = True
                return [f"解析结果里没有{_FIELD_NAMES[field]}段。重发整段"
                        "回复可重试，或「取消」退出粘贴。"]
            # 与手动录入同一套分隔/去重（创建流同款，见 _paste_commit_creation）
            joined = "\n".join(value)
            st["new_value"] = _split_words(joined) if field == "vocabulary" \
                else _split_lines(joined)
            return self._confirm_edit()
        return self._paste_commit_creation(parsed)

    def _paste_commit_creation(self, parsed: dict) -> list[str]:
        st = self._state
        unit = st["units"][-1] if st["op"] == "new_book" else st["unit"]
        order = (("vocab", "vocabulary"), ("patterns", "patterns"),
                 ("grammar", "grammar"))
        start = next(i for i, (step, _) in enumerate(order)
                     if step == st["step"])
        # 与手动录入同一套分隔/去重语义（网页端粘贴按行直填，这里沿用
        # 向导既有的逗号分隔与去重，AI 一行多词时也能拆开）
        for step, field in order[start:]:
            if parsed[field]:
                joined = "\n".join(parsed[field])
                unit[field] = _split_words(joined) if field == "vocabulary" \
                    else _split_lines(joined)
        next_step = None
        for step, field in order[start:]:
            if not unit[field]:
                next_step = step
                break
        counts = "、".join(
            f"{_FIELD_NAMES[field]} {len(unit[field])} 条"
            for _, field in order[start:] if parsed[field])
        head = f"✓ 已采用：{counts}。"
        if next_step == "patterns":
            st["step"] = "patterns"
            return [head, "句式（一行一句，格式：问题 - 回答）。"
                          "没有就发「跳过」："]
        if next_step == "grammar":
            st["step"] = "grammar"
            return [head, "语法点（一行一条，纯英文描述）。没有就发「跳过」："]
        summary = (f"单元「{unit['title']}」已记录："
                   f"{len(unit['vocabulary'])} 词、{len(unit['patterns'])} 句式、"
                   f"{len(unit['grammar'])} 语法点。")
        if st["op"] == "new_book":
            st["step"] = "next_unit"
            return [head, summary, "继续发下一个单元的标题，或发「完成」收尾："]
        return self._ask_add_confirm(head)

    def _current_step_prompt(self) -> list[str]:
        """退出粘贴后重放当前内容步骤的提示，用户接着手动输入。"""
        st = self._state
        op, step = st["op"], st.get("step")
        if op in ("new_book", "add_unit"):
            if step == "vocab":
                unit = st["units"][-1] if op == "new_book" else st["unit"]
                return [f"继续录单元「{unit['title']}」的词汇（逗号、顿号或"
                        "换行分隔，也可再发「粘贴」）："]
            if step == "patterns":
                return ["句式（一行一句，格式：问题 - 回答）。没有就发"
                        "「跳过」，或再发「粘贴」："]
            if step == "grammar":
                return ["语法点（一行一条，纯英文描述）。没有就发「跳过」"
                        "，或再发「粘贴」："]
        if op == "edit_unit" and step and step.endswith("_input"):
            name = _FIELD_NAMES[step.replace("_input", "")]
            return [f"请发送完整的新{name}（整段覆盖，也可再发「粘贴」整段"
                    "贴入），清空则发「清空」："]
        return []

    def _ask_add_confirm(self, head: str | None = None) -> list[str]:
        """加单元的落盘确认：校验走 store.validate（与网页端同一份规则），
        不过则放弃本次添加（与手动流同语义）。head 用于粘贴采用时先报
        「已采用」再接确认。"""
        st = self._state
        unit = st["unit"]
        book = self._store.get_book(st["book_id"])
        merged = list((book or {}).get("units") or []) + [unit]
        problems = self._store.validate(
            (book or {}).get("title") or st["book_title"], merged)
        if problems:
            self._state = None
            lead = [head] if head else []
            return [*lead, "⚠️ 校验没有通过，本次添加已放弃。先改这几处：",
                    *problems,
                    "稍后发「加单元 …」可以重新来。"]
        st["step"] = "confirm_add"
        lead = [head] if head else []
        return [*lead,
                f"即将给「{st['book_title']}」添加单元「{unit['title']}」："
                f"{len(unit['vocabulary'])} 词、{len(unit['patterns'])} 句式、"
                f"{len(unit['grammar'])} 语法点。",
                "回复「确认」保存，或「取消」放弃。"]

    # ------------------------------------------------------------------
    # wizard: new book
    # ------------------------------------------------------------------

    def _step_new_book(self, text: str, low: str) -> list[str]:
        st = self._state
        if st["step"] == "title":
            st["title"] = text
            st["step"] = "unit_title"
            return [f"书名记为「{text}」。",
                    "第一个单元的标题（如：U2 或 Unit 1 My Friends）："]
        if st["step"] == "unit_title":
            st["units"].append({"title": text, "vocabulary": [],
                                "patterns": [], "grammar": []})
            st["step"] = "vocab"
            return [f"单元「{text}」开始。请把这一单元的词汇发给我："
                    "用逗号、顿号或换行分隔，词组（如 short hair）没关系，"
                    "直接打字发，别用语音（英文语音识别不可靠）。",
                    "提示：也可拍课本页让豆包 / Kimi 按提示词整理（发「提示词」"
                    "拿提示词），然后发「粘贴」把回复整段贴入。"]
        if st["step"] == "vocab":
            words = _split_words(text)
            if not words:
                return ["没有解析出词汇，请用逗号或换行分隔再发一次："]
            st["units"][-1]["vocabulary"] = words
            st["step"] = "patterns"
            return [f"已记 {len(words)} 个词：{'、'.join(words[:8])}"
                    + ("…" if len(words) > 8 else ""),
                    "句式（一行一句，格式：问题 - 回答）。没有就发「跳过」，"
                    "也可发「粘贴」整段贴入："]
        if st["step"] == "patterns":
            st["units"][-1]["patterns"] = [] if low == "跳过" else _split_lines(text)
            st["step"] = "grammar"
            return ["语法点（一行一条，纯英文描述，如：Modal verb 'can'）。"
                    "没有就发「跳过」，也可发「粘贴」整段贴入："]
        if st["step"] == "grammar":
            st["units"][-1]["grammar"] = [] if low == "跳过" else _split_lines(text)
            unit = st["units"][-1]
            st["step"] = "next_unit"
            return [f"单元「{unit['title']}」已记录：{len(unit['vocabulary'])} 词、"
                    f"{len(unit['patterns'])} 句式、{len(unit['grammar'])} 语法点。",
                    "继续发下一个单元的标题，或发「完成」收尾："]
        if st["step"] == "next_unit":
            if low in ("完成", "结束", "保存", "ok", "done"):
                return self._ask_save_new_book()
            st["units"].append({"title": text, "vocabulary": [],
                                "patterns": [], "grammar": []})
            st["step"] = "vocab"
            return [f"单元「{text}」开始。请发这一单元的词汇"
                    "（逗号、顿号或换行分隔；也可发「粘贴」整段贴入）："]
        return self._bug("new_book")

    def _ask_save_new_book(self) -> list[str]:
        st = self._state
        problems = self._store.validate(st["title"], st["units"])
        if problems:
            # 向导里的内容一旦记下没法回改（聊天没有光标），校验不过只能
            # 取消重来 —— 把问题一次性说清楚，避免用户在死循环里打转。
            self._state = None
            return ["⚠️ 校验没有通过，本次创建已放弃。先改这几处：",
                    *problems,
                    "发「建教材」可以重新来一遍。"]
        st["step"] = "confirm_new"
        total_words = sum(len(u["vocabulary"]) for u in st["units"])
        return [f"即将保存教材「{st['title']}」：{len(st['units'])} 个单元、"
                f"共 {total_words} 个词。",
                "回复「确认」保存，或「取消」放弃。"]

    def _commit_pending(self) -> list[str]:
        st = self._state
        op = st["op"]
        self._state = None
        if op == "new_book":
            book_id = self._store.save_new_book(st["title"], st["units"])
            return [f"✅ 已保存：教材「{st['title']}」（{len(st['units'])} 个单元，"
                    f"id: {book_id}）。学生发「上课」即可开始。"]
        if op == "add_unit":
            book = self._store.get_book(st["book_id"])
            if book is None:
                return ["⚠️ 教材不见了（可能刚被删除），操作没有执行。"]
            book["units"].append(st["unit"])
            if not self._store.update_book(st["book_id"],
                                           book.get("title", ""), book["units"]):
                return ["⚠️ 保存失败（这本教材可能不可编辑）。"]
            return [f"✅ 已把单元「{st['unit']['title']}」加进"
                    f"「{book.get('title')}」。"]
        if op == "del_unit":
            book = self._store.get_book(st["book_id"])
            if book is None or not (0 <= st["unit_index"] < len(book["units"])):
                return ["⚠️ 教材或单元已不存在，操作没有执行。"]
            removed = book["units"].pop(st["unit_index"])
            self._store.update_book(st["book_id"], book.get("title", ""),
                                    book["units"])
            return [f"✅ 已删除单元「{removed.get('title') or removed.get('unit_id')}」。"]
        if op == "del_book":
            title = st["title"]
            if not self._store.delete_book(st["book_id"]):
                return ["⚠️ 删除失败（教材不存在，或它不是可编辑的自建教材）。"]
            return [f"✅ 已删除教材「{title}」。学生的进度记录保留（休眠）。"]
        if op == "edit_unit":
            book = self._store.get_book(st["book_id"])
            if book is None or not (0 <= st["unit_index"]
                                    < len(book.get("units") or [])):
                return ["⚠️ 教材或单元已不存在，操作没有执行。"]
            unit = book["units"][st["unit_index"]]
            unit[st["field"]] = st["new_value"]
            if not self._store.update_book(st["book_id"],
                                           book.get("title", ""),
                                           book["units"]):
                return ["⚠️ 保存失败（这本教材可能不可编辑）。"]
            if st["field"] == "title":
                return [f"✅ 标题已改为「{st['new_value']}」。"]
            unit_name = unit.get("title") or unit.get("unit_id") or ""
            return [f"✅ 已更新单元「{unit_name}」的"
                    f"{_FIELD_NAMES[st['field']]}（现 {len(st['new_value'])} 条）。"]
        if op == "rename":
            book = self._store.get_book(st["book_id"])
            if book is None:
                return ["⚠️ 教材不见了，操作没有执行。"]
            self._store.update_book(st["book_id"], st["new_title"], book["units"])
            return [f"✅ 书名已改为「{st['new_title']}」。"]
        return self._bug(op)

    # ------------------------------------------------------------------
    # wizard: add unit to existing book
    # ------------------------------------------------------------------

    def _cmd_add_unit(self, cmd: str, raw: str) -> list[str]:
        parts = raw.split(None, 2)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            return ["用法：加单元 书序号 [单元标题]。先发「看教材」查序号。"]
        book, err = self._pick_book(int(parts[1]), for_mutation="修改")
        if err:
            return [err]
        unit = {"title": (parts[2].strip() if len(parts) > 2 else ""),
                "vocabulary": [], "patterns": [], "grammar": []}
        self._state = {"op": "add_unit", "book_id": book["id"],
                       "book_title": book["title"], "unit": unit,
                       "step": "title" if not unit["title"] else "vocab"}
        if not unit["title"]:
            return [f"给「{book['title']}」添加单元。单元标题（如 U3）："]
        return [f"单元「{unit['title']}」开始。请发词汇"
                "（逗号、顿号或换行分隔；也可发「粘贴」整段贴入）："]

    def _step_add_unit(self, text: str, low: str) -> list[str]:
        st = self._state
        unit = st["unit"]
        if st["step"] == "title":
            unit["title"] = text
            st["step"] = "vocab"
            return [f"单元「{text}」开始。请发词汇（逗号、顿号或换行分隔；"
                    "也可发「粘贴」整段贴入）："]
        if st["step"] == "vocab":
            words = _split_words(text)
            if not words:
                return ["没有解析出词汇，请用逗号或换行分隔再发一次："]
            unit["vocabulary"] = words
            st["step"] = "patterns"
            return [f"已记 {len(words)} 个词。句式（一行一句，问题 - 回答），"
                    "没有就发「跳过」，也可发「粘贴」整段贴入："]
        if st["step"] == "patterns":
            unit["patterns"] = [] if low == "跳过" else _split_lines(text)
            st["step"] = "grammar"
            return ["语法点（一行一条），没有就发「跳过」，也可发「粘贴」"
                    "整段贴入："]
        if st["step"] == "grammar":
            unit["grammar"] = [] if low == "跳过" else _split_lines(text)
            return self._ask_add_confirm()
        return self._bug("add_unit")

    # ------------------------------------------------------------------
    # wizard: edit unit content (overwrite semantics, 2026-09-22 用户裁定)
    # ------------------------------------------------------------------

    def _cmd_edit_unit(self, cmd: str, raw: str) -> list[str]:
        parts = cmd.split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            return ["用法：改单元 书序号 单元序号。先发「看教材」查书序号，"
                    "「看单元 书序号」查单元序号。"]
        book, err = self._pick_book(int(parts[1]), for_mutation="修改")
        if err:
            return [err]
        if book["source"] != "user":
            return [f"「{book['title']}」是内置示例教材，不可修改。"]
        idx = int(parts[2]) - 1
        units = book.get("units") or []
        if not (0 <= idx < len(units)):
            return [f"单元序号超出范围（1-{len(units)}）。"]
        unit = units[idx]
        self._state = {"op": "edit_unit", "book_id": book["id"],
                       "book_title": book["title"], "unit_index": idx,
                       "unit_title": unit.get("title") or unit.get("unit_id") or "",
                       "step": "field"}
        return [f"要改「{book['title']}」的单元"
                f"「{self._state['unit_title']}」的哪一项？"
                "回复「标题」/「词汇」/「句式」/「语法」。"]

    def _step_edit_unit(self, text: str, low: str) -> list[str]:
        st = self._state
        if st["step"] == "field":
            field = _EDIT_FIELD_ALIASES.get(low.replace(" ", ""))
            if field is None:
                return ["要改哪一项？回复「标题」/「词汇」/「句式」/「语法」，"
                        "或「取消」。"]
            st["field"] = field
            book = self._store.get_book(st["book_id"])
            if book is None or not (0 <= st["unit_index"]
                                    < len(book.get("units") or [])):
                self._state = None
                return ["⚠️ 教材或单元已不存在，操作已复位。"]
            unit = book["units"][st["unit_index"]]
            st["unit_title"] = unit.get("title") or unit.get("unit_id") or ""
            if field == "title":
                st["step"] = "edit_title"
                return [f"当前标题：「{st['unit_title']}」。"
                        "请直接发送新标题（覆盖旧标题）："]
            current = unit.get(field) or []
            shown = "、".join(current) if current else "（空）"
            if field != "vocabulary":
                shown = "\n".join(f"· {x}" for x in current) if current else "（空）"
            st["step"] = f"{field}_input"
            name = _FIELD_NAMES[field]
            sep = "逗号、顿号或换行分隔" if field == "vocabulary" else "一行一条"
            return [f"「{st['unit_title']}」当前的{name}：", shown,
                    f"⚠️ 整段覆盖：请发送完整的新{name}——要保留的原内容也要"
                    f"包含在内（加一条=多发，删一条=少发），{sep}。清空则发"
                    f"「清空」；也可发「粘贴」贴入 AI 整理的整段回复"
                    f"（只取{name}段）："]
        if st["step"] == "edit_title":
            if low == "清空":
                return ["标题不能为空。请发送新标题，或「取消」放弃："]
            st["new_value"] = text
            return self._confirm_edit()
        if st["step"] in ("vocabulary_input", "patterns_input",
                          "grammar_input"):
            field = st["step"].replace("_input", "")
            if low == "清空":
                st["new_value"] = []
            else:
                parsed = _split_words(text) if field == "vocabulary" \
                    else _split_lines(text)
                if not parsed:
                    return ["没有解析出内容，请重新发送完整列表"
                            "（清空则发「清空」）："]
                st["new_value"] = parsed
            return self._confirm_edit()
        return self._bug("edit_unit")

    def _confirm_edit(self) -> list[str]:
        """把改后的整本书喂给与网页端同一份校验；不过则留在当前输入步骤
        原地重试（覆盖式编辑的用户可能手滑删了要保留的内容，绝不能静默
        放弃，也不能打回命令态让用户从头再来）。"""
        st = self._state
        book = self._store.get_book(st["book_id"])
        if book is None or not (0 <= st["unit_index"]
                                < len(book.get("units") or [])):
            self._state = None
            return ["⚠️ 教材或单元已不存在，操作已复位。"]
        name = _FIELD_NAMES[st["field"]]
        units = [dict(u) for u in book["units"]]
        units[st["unit_index"]] = {**units[st["unit_index"]],
                                   st["field"]: st["new_value"]}
        problems = self._store.validate(
            book.get("title") or st["book_title"], units)
        if problems:
            return [f"⚠️ 校验没有通过，新{name}暂未生效。重新发送完整的"
                    f"{name}即可继续（或「取消」放弃本次修改）：",
                    *problems]
        st["step"] = "confirm_edit"
        if st["field"] == "title":
            summary = (f"即将把单元「{st['unit_title']}」的标题改为"
                       f"「{st['new_value']}」。")
        else:
            old = book["units"][st["unit_index"]].get(st["field"]) or []
            summary = (f"即将覆盖单元「{st['unit_title']}」的{name}："
                       f"{len(old)} 条 → {len(st['new_value'])} 条。")
        return [summary, "回复「确认」保存，或「取消」放弃。"]

    # ------------------------------------------------------------------
    # simple mutations with confirm
    # ------------------------------------------------------------------

    def _cmd_rename(self, cmd: str, raw: str) -> list[str]:
        parts = raw.split(None, 2)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            return ["用法：改书名 书序号 新书名（只发「改书名 书序号」也行，"
                    "会一步步问你新书名）"]
        book, err = self._pick_book(int(parts[1]), for_mutation="修改")
        if err:
            return [err]
        if book["source"] != "user":
            return [f"「{book['title']}」是内置示例教材，不可修改。"]
        if len(parts) < 3:
            # #25 #12：只有书序号（选择器卡按钮的等价指令）→ 两步式小向导，
            # 第二步递新书名表单卡；一次性「改书名 N 新名」协议原样保留
            self._state = {"op": "rename", "book_id": book["id"],
                           "book_title": book["title"], "step": "new_title"}
            return [f"给「{book['title']}」改名。请直接发送新书名："]
        new_title = parts[2].strip()
        self._state = {"op": "rename", "book_id": book["id"],
                       "new_title": new_title}
        return [f"把「{book['title']}」改名为「{new_title}」？回复「确认」执行。"]

    def _step_rename_title(self, text: str, low: str) -> list[str]:
        """改书名两步式的输入步：收新书名，落既有确认态（_commit 的 rename
        分支共用）。空标题不进确认。"""
        st = self._state
        if not text.strip() or low == "清空":
            return ["书名不能为空。请直接发送新书名，或「取消」放弃："]
        st["new_title"] = text
        st.pop("step", None)
        return [f"把「{st['book_title']}」改名为「{text}」？"
                "回复「确认」执行，或「取消」。"]

    def _cmd_delete_unit(self, cmd: str) -> list[str]:
        parts = cmd.split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            return ["用法：删单元 书序号 单元序号。先发「看单元 书序号」查序号。"]
        book, err = self._pick_book(int(parts[1]), for_mutation="删除")
        if err:
            return [err]
        if book["source"] != "user":
            return [f"「{book['title']}」是内置示例教材，不可修改。"]
        idx = int(parts[2]) - 1
        units = book.get("units") or []
        if not (0 <= idx < len(units)):
            return [f"单元序号超出范围（1-{len(units)}）。"]
        unit = units[idx]
        self._state = {"op": "del_unit", "book_id": book["id"],
                       "unit_index": idx}
        return [f"删除「{book['title']}」的单元"
                f"「{unit.get('title') or unit.get('unit_id')}」？"
                "回复「确认」执行，或「取消」。"]

    def _cmd_delete_book(self, cmd: str) -> list[str]:
        parts = cmd.split()
        if len(parts) != 2 or not parts[1].isdigit():
            return ["用法：删教材 书序号。先发「看教材」查序号。"]
        book, err = self._pick_book(int(parts[1]), for_mutation="删除")
        if err:
            return [err]
        if book["source"] != "user":
            return [f"「{book['title']}」是内置示例教材，不可删除。"]
        self._state = {"op": "del_book", "book_id": book["id"],
                       "title": book["title"]}
        return [f"删除教材「{book['title']}」（{book['units']} 个单元）？"
                "学生的进度会保留（休眠）。回复「确认」执行，或「取消」。"]

    # ------------------------------------------------------------------
    # read-only commands
    # ------------------------------------------------------------------

    def _cmd_list_units(self, cmd: str) -> list[str]:
        parts = cmd.split()
        if len(parts) != 2 or not parts[1].isdigit():
            return ["用法：看单元 书序号。先发「看教材」查序号。"]
        book, err = self._pick_book(int(parts[1]))
        if err:
            return [err]
        lines = [f"「{book['title']}」的单元："]
        for i, unit in enumerate(book.get("units") or [], 1):
            lines.append(f"{i}. {unit.get('title') or unit.get('unit_id')}"
                         f"（{len(unit.get('vocabulary') or [])} 词、"
                         f"{len(unit.get('patterns') or [])} 句式）")
        return ["\n".join(lines)]

    def _cmd_show_unit(self, cmd: str) -> list[str]:
        parts = cmd.split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            return ["用法：看内容 书序号 单元序号"]
        book, err = self._pick_book(int(parts[1]))
        if err:
            return [err]
        units = book.get("units") or []
        idx = int(parts[2]) - 1
        if not (0 <= idx < len(units)):
            return [f"单元序号超出范围（1-{len(units)}）。"]
        return ["\n".join(self._unit_content_lines(book, idx))]

    def unit_content(self, book_idx: int, unit_idx: int) -> list[str] | None:
        """「看内容 书序号 单元序号」的数据面（展示卡用，#22 三轮 P3）：
        返回与文字指令同一份的展示行；书读不出/序号越界返回 None
        （调用方回落文字流，错误文案仍由文字指令路径给出）。"""
        book, err = self._pick_book(book_idx)
        if err:
            return None
        units = book.get("units") or []
        if not (0 <= unit_idx - 1 < len(units)):
            return None
        return self._unit_content_lines(book, unit_idx - 1)

    @staticmethod
    def _unit_content_lines(book: dict, idx: int) -> list[str]:
        unit = book["units"][idx]
        lines = [f"「{book['title']}」· {unit.get('title') or unit.get('unit_id')}"]
        vocab = unit.get("vocabulary") or []
        lines.append("词汇：" + ("、".join(vocab) if vocab else "（空）"))
        patterns = unit.get("patterns") or []
        if patterns:
            lines.append("句式：")
            lines.extend(f"· {p}" for p in patterns)
        grammar = unit.get("grammar") or []
        if grammar:
            lines.append("语法：" + "；".join(grammar))
        return lines

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _book_list_lines(self) -> list[str]:
        books = self._store.list_books()
        if not books:
            return ["还没有任何教材。发「建教材」创建第一本。"]
        lines = ["现有教材："]
        for i, b in enumerate(books, 1):
            tag = "内置示例" if b["source"] != "user" else "自建"
            lines.append(f"{i}. {b['title']}（{tag} · {b['units']} 个单元）")
        return ["\n".join(lines)]

    def _pick_book(self, index_1based: int,
                   for_mutation: str | None = None) -> tuple[dict | None, str]:
        """按 1-based 序号取书。先在书单里定位，再取完整内容 —— 书单条目
        里的 units 只是数量，单元操作需要完整列表。

        for_mutation=None：只读（看单元/看内容），任何书都给——内置示例
        也能看（read_book 只读视图）。传动词（"修改"/"删除"）则为写操作：
        预设书在书单层就拒绝。不提前拦的话 get_book 对非自建书返回 None，
        用户只会看到"读不出来"（2026-09-22 改单元轮发现；2026-09-23 收窄
        为只拦写操作——上一版把只读命令也拦了，看不了示例书）。"""
        books = self._store.list_books()
        if not (1 <= index_1based <= len(books)):
            return None, f"书序号超出范围（1-{len(books)}）。先发「看教材」查序号。"
        entry = books[index_1based - 1]
        if for_mutation and entry.get("source", "user") != "user":
            return None, f"「{entry['title']}」是内置示例教材，不可{for_mutation}。"
        book = self._store.get_book(entry["id"]) if for_mutation \
            else self._store.read_book(entry["id"])
        if book is None:
            return None, "这本书读不出来了（可能刚被删除）。"
        book.setdefault("source", entry.get("source", "user"))
        book.setdefault("title", entry.get("title", entry["id"]))
        return book, ""

    @staticmethod
    def _bug(where: str) -> list[str]:
        # 状态机的每个 step 都应该覆盖到；走到这里说明有分支漏了，
        # 宁可自曝也不要把用户卡死在一个回不了头的状态里。
        return [f"（内部状态异常：{where}）已复位，请重新发指令。"
                "如果反复出现，请把这条截图发给项目作者。"]


def _split_words(text: str) -> list[str]:
    """词汇按 逗号/顿号/分号/换行 分隔。空格不是分隔符：short hair、
    tall and strong 这类词组靠它活下来。"""
    parts = re.split(r"[，,、;；\n]+", text or "")
    seen, out = set(), []
    for part in parts:
        part = part.strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return out


def _split_lines(text: str) -> list[str]:
    """句式/语法按行分隔；去重保序。"""
    seen, out = set(), []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and line.lower() not in seen:
            seen.add(line.lower())
            out.append(line)
    return out


_MANAGE_BANNER = "📖 教材管理模式（输入不影响学生上课；发「退出」返回）"

# 一键粘贴（#22 2026-10-08）：允许发「粘贴」的向导步骤 = 全部内容录入步
# （建书/加单元的 词汇/句式/语法 + 改单元的三个 *_input）。
_PASTE_CAPABLE_STEPS = {"vocab", "patterns", "grammar",
                        "vocabulary_input", "patterns_input", "grammar_input"}
_PASTE_WAIT_TEXT = ("请把 AI 按提示词整理的回复整段贴进来"
                    "（[词汇]/[句式]/[语法] 三段一次全贴）。还没拿提示词就发"
                    "「提示词」；发「取消」退出粘贴、回到手动输入。")

# 改单元：回复别名 → 单元字段名
_EDIT_FIELD_ALIASES = {
    "标题": "title", "题目": "title", "单元标题": "title",
    "词汇": "vocabulary", "单词": "vocabulary", "词": "vocabulary",
    "句式": "patterns", "句型": "patterns",
    "语法": "grammar", "语法点": "grammar",
}
_FIELD_NAMES = {"title": "标题", "vocabulary": "词汇",
                "patterns": "句式", "grammar": "语法"}
