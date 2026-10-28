"""
PrivaGlot - Entry Point
"""
import os

import uvicorn
from app import config

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=config.SERVER_HOST,
        port=config.SERVER_PORT,
        # 热重载默认关：reloader 是独立进程，任何 .py 变动都会重启服务、
        # 杀掉所有进行中的连接（#43）。开发要热重载时设 PRIVAGLOT_RELOAD=1。
        reload=os.environ.get("PRIVAGLOT_RELOAD") == "1",
    )
