"""
Text REPL channel — 无头内核的"根形态"试金石。

ConversationKernel + 几十行终端代码，必须脱离任何浏览器、任何平台跑通
一整节课：选学生 → 选教材/单元（或自由对话）→ 打字上课 → 下课看报告。
跑法：python run_repl.py

没配 API Key 也能跑：问候语走内核的固定兜底，LLM 轮次以 error 事件浮出
—— 编排本身（事件序列、词汇统计、下课结算）原样运转，正好用来在不动真
云的情况下做内核冒烟。
"""
import asyncio

from ..core import events
from ..core.kernel import ConversationKernel
from ..core.session_registry import StudentSessionBusy, registry
from ..course_engine import course_engine
from ..student import student_store


class ReplChannel:
    """stdin/stdout adapter: kernel events become log lines, typed lines
    become kernel calls. input_fn/print_fn are injectable so tests can run
    a scripted lesson offline."""

    def __init__(self, input_fn=input, print_fn=print):
        self._input = input_fn
        self._print = print_fn
        self._text_buf: list[str] = []

    # ---- session flow ----

    async def run(self) -> int:
        p = self._print
        p("PrivaGlot 文本 REPL —— 无头内核试金石（没有浏览器，没有平台）")
        student_id = self._pick_student()
        if not student_id:
            return 1
        while True:
            kernel = ConversationKernel()
            kernel.load_student(student_id)
            try:
                registry.register(student_id, kernel)
            except StudentSessionBusy:
                p("⚠️ 这个学生已经有进行中的课（别的窗口/通道/进程），"
                  "一个学生同时只能上一节课。")
                return 1
            try:
                begin_kwargs = self._pick_lesson()
                if begin_kwargs is None:
                    return 0
                async for ev in kernel.begin(**begin_kwargs):
                    self._render(ev)
                while kernel.state == "active":
                    line = await self._readline()
                    if line is None or line.strip().lower() in ("/quit", "/exit"):
                        await self._end(kernel)
                        return 0
                    text = line.strip()
                    if text in ("/end", "下课"):
                        await self._end(kernel)
                        break
                    if not text:
                        continue
                    async with registry.turn(student_id):
                        async for ev in kernel.submit_text(text):
                            self._render(ev)
                p("（这节课结束了。再选一节继续，或输入 q 退出。）")
            finally:
                registry.release(student_id, kernel)

    async def _readline(self, prompt: str = "\nyou> ") -> str | None:
        try:
            return await asyncio.to_thread(self._input, prompt)
        except (EOFError, KeyboardInterrupt):
            return None

    async def _end(self, kernel: ConversationKernel):
        async for ev in kernel.end_session():
            self._render(ev)

    # ---- menus ----

    def _pick_student(self) -> str:
        p = self._print
        students = student_store.list_students()
        if not students:
            p("还没有学生档案 —— 先在网页端创建一个，再来跑 REPL。")
            return ""
        p("\n学生：")
        for i, s in enumerate(students, 1):
            p(f"  {i}. {s['name']} ({s.get('age', '?')}岁)  id={s['id']}")
        raw = self._input("选择学生（序号）: ").strip()
        try:
            pick = students[int(raw) - 1]
        except (ValueError, IndexError):
            p("没有这个序号，退出。")
            return ""
        return pick["id"]

    def _pick_lesson(self) -> dict | None:
        p = self._print
        books = course_engine.get_textbook_names()
        p("\n教材：")
        for i, book in enumerate(books, 1):
            p(f"  {i}. {course_engine.book_title(book)} ({book})")
        raw = self._input("选择教材（序号，回车=自由对话，q=退出）: ").strip()
        if raw.lower() in ("q", "quit", "/quit"):
            return None
        if not raw:
            return {"mode": "free"}
        try:
            book = books[int(raw) - 1]
        except (ValueError, IndexError):
            p("没有这个序号，回到选课。")
            return self._pick_lesson()
        units = course_engine.list_units_summary(book)
        p(f"\n{course_engine.book_title(book)} 的单元：")
        for i, u in enumerate(units, 1):
            p(f"  {i}. [{u['unit_id']}] {u.get('title') or u['unit_id']}"
              f"（{u.get('word_count', 0)} 词）")
        raw = self._input("选择单元（序号）: ").strip()
        try:
            unit = units[int(raw) - 1]
        except (ValueError, IndexError):
            p("没有这个序号，回到选课。")
            return self._pick_lesson()
        return {"textbook": book, "unit_id": unit["unit_id"], "mode": "lesson"}

    # ---- rendering ----

    def _render(self, ev: dict):
        """Kernel event → terminal line. llm tokens are buffered and printed
        as one teacher line at llm_done, like the web channel's bubble."""
        p = self._print
        t = ev.get("type")
        if t == events.EVENT_LLM_TOKEN:
            self._text_buf.append(ev.get("text", ""))
        elif t == events.EVENT_LLM_CLEANED:
            self._text_buf = [ev.get("text", "")]
        elif t == events.EVENT_LLM_DONE:
            text = "".join(self._text_buf)
            self._text_buf = []
            if text:
                p(f"\nteacher> {text}")
        elif t == events.EVENT_TTS_AUDIO:
            import base64
            size = len(base64.b64decode(ev.get("data", "")))
            p(f"   [TTS] {size} bytes 的语音已生成（终端放不了，靠想象）")
        elif t == events.EVENT_TRANSCRIPTION:
            p(f"   [heard] {ev.get('text', '')}")
        elif t == events.EVENT_VOCAB_HIT:
            hits = ev.get("vocab_hits") or []
            if hits:
                p(f"   [vocab] 本轮命中：{'、'.join(hits)}"
                  f"（累计 {ev.get('accuracy', 0):.0%}）")
        elif t == events.EVENT_LESSON_TARGETS:
            p(f"   [targets] {'、'.join(ev.get('vocabulary') or [])}")
        elif t == events.EVENT_SESSION_INFO:
            where = (f"{ev.get('textbook')}/{ev.get('unit_title') or ev.get('unit_id')}"
                     if ev.get("textbook") else "自由对话")
            p(f"   [session] {where}")
        elif t == events.EVENT_LESSON_COMPLETE:
            p("\n📋 本课结束！"
              f"turns={ev.get('total_turns', 0)}"
              f"  duration={ev.get('duration_minutes', 0)}min"
              f"  accuracy={ev.get('accuracy', 0.0):.0%}")
        elif t == events.EVENT_LESSON_EVAL_RESULT:
            p(f"📈 进度评估：{ev.get('progress', 0)}% — {ev.get('note', '')}")
        elif t == events.EVENT_LESSON_EVAL_FAILED:
            p(f"⚠️ {ev.get('message', '')}")
        elif t == events.EVENT_FREE_SESSION_COMPLETE:
            p(f"\n💬 自由对话结束！turns={ev.get('total_turns', 0)}")
            if ev.get("summary"):
                p(f"   {ev.get('summary')}")
        elif t == events.EVENT_ERROR:
            p(f"⚠️ {ev.get('message', '')}")
        # student_info / session_started：不需要在终端单独成行
