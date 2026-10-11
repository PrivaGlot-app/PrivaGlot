"""
PrivaGlot 文本 REPL 入口 —— 无头内核的根形态验收。

    python run_repl.py

脱离网页与飞书，在终端里打字跑通一节课：选学生 → 选课 → 对话 → 下课。
与 run.py / run_feishu.py 共享同一份 data/，同一批学生档案和进度。
"""
import asyncio
import sys

from app.course_engine import course_engine


def main() -> int:
    # the web app loads the course catalog in its FastAPI startup event;
    # the REPL has no FastAPI, so load it here
    course_engine.load()
    from app.channels.repl import ReplChannel
    return asyncio.run(ReplChannel().run())


if __name__ == "__main__":
    sys.exit(main())
