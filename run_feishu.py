"""
PrivaGlot Feishu channel entry point.

    python run_feishu.py             # 启动机器人（长连接，无需公网端口）
    python run_feishu.py --selftest  # 部署自检：ffmpeg / SDK / 凭据 / 网络

Requires FEISHU_APP_ID / FEISHU_APP_SECRET in .env — setup steps in
docs/飞书接入.md. The web UI stays on run.py; both share data/.
"""
import sys

from app import config
from app.course_engine import course_engine


def selftest() -> int:
    """部署自检已统一到 setup/check_env.py（飞书模式 = 环境检查 + 凭据 +
    真实探活），这里只是换个入口调它，保证两边说法永不打架。"""
    import subprocess
    from pathlib import Path
    checker = Path(__file__).resolve().parent / "setup" / "check_env.py"
    proc = subprocess.run(
        [sys.executable, str(checker), "--mode", "feishu", "--with-api"])
    return proc.returncode


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    # the web app loads the course catalog in its FastAPI startup event;
    # the bot has no FastAPI, so load it here
    course_engine.load()
    from app.channels.feishu.channel import FeishuChannel
    channel = FeishuChannel()
    channel.start()  # blocks forever (reconnecting long connection)
    return 0


if __name__ == "__main__":
    sys.exit(main())
