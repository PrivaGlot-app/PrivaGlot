"""
Web channel — the original WebSocket transport, now a thin kernel consumer.

The wire protocol is unchanged since v3: the same event types (16 since the
#52 translation event, 2026-10-25), the same
order, the same error messages. The
channel only translates: browser messages in → kernel calls, kernel events
out → WebSocket JSON frames. Everything lesson-related lives in
app/core/kernel.py.
"""
import asyncio
import base64

from fastapi import WebSocket, WebSocketDisconnect

from ..core import events
from ..core.kernel import ConversationKernel
from ..core.session_registry import StudentSessionBusy, registry
from ..logging_config import log as logger


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"[CONN] Connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"[CONN] Disconnected. Total: {len(self.active_connections)}")


manager = ConnectionManager()


def log(module: str, msg: str):
    logger.info(f"[{module}] {msg}")


async def _send(websocket: WebSocket, ev: dict):
    await websocket.send_json(ev)


async def handle_websocket(websocket: WebSocket):
    """Main WebSocket handler."""
    await manager.connect(websocket)

    kernel: ConversationKernel | None = None
    student_id = ""
    try:
        # Wait for student selection
        init_msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)
        student_id = init_msg.get("student_id", "")
        kernel = ConversationKernel()

        if not student_id:
            await _send(websocket, events.error("No student_id"))
            await websocket.close()
            return

        # Load student profile (and resolve this student's teacher)
        try:
            student_info_ev = kernel.load_student(student_id)
        except Exception:
            await _send(websocket, events.error(f"Student '{student_id}' not found"))
            await websocket.close()
            return
        # 会话注册表：一个学生同时只能有一节进行中的课 —— 无论从哪个通道
        # 进来（本进程内存表 + 跨进程文件锁，见 core/session_registry.py）。
        try:
            registry.register(student_id, kernel)
        except StudentSessionBusy:
            await _send(websocket, events.error(
                "该学生已经在别的窗口或通道上课，一个学生同时只能有一节课。"
                "请先在另一边「下课」，再回到这里开始。"))
            await websocket.close()
            return
        await _send(websocket, student_info_ev)

        # Wait for begin_session from frontend (lesson carries textbook/unit)
        log("CONN", "Waiting for begin_session...")
        begin_msg = await asyncio.wait_for(websocket.receive_json(), timeout=60)
        if begin_msg.get("type") != "begin_session":
            await _send(websocket, events.error("Expected begin_session"))
            await websocket.close()
            return

        async for ev in kernel.begin(
            textbook=begin_msg.get("textbook", ""),
            unit_id=begin_msg.get("unit_id", ""),
            mode=begin_msg.get("mode", "lesson"),  # "lesson" or "free"
            teacher_id=begin_msg.get("teacher_id", ""),
            style=begin_msg.get("style", ""),
        ):
            await _send(websocket, ev)
        if kernel.state == "failed":
            await websocket.close()
            return

        # Main loop — anything that is not audio/end_session (e.g. the
        # frontend's 30s 'ping') is silently ignored, same as v3. 每一轮
        # 转写→回复的整条流水线在 per-student 的 single-flight 闸内跑：
        # 同学生从别的连接/通道并发发来的消息在这里按序排队。
        while True:
            msg = await websocket.receive_json()

            if msg.get("type") == "audio":
                audio_data = base64.b64decode(msg.get("data", ""))
                async with registry.turn(student_id):
                    async for ev in kernel.submit_audio(audio_data):
                        await _send(websocket, ev)

            elif msg.get("type") == "end_session":
                async with registry.turn(student_id):
                    async for ev in kernel.end_session():
                        await _send(websocket, ev)
                break

    except WebSocketDisconnect:
        log("CONN", "Client disconnected unexpectedly")
    except asyncio.TimeoutError:
        log("CONN", "Connection timeout")
    except Exception as e:
        log("CONN", f"Error: {e}")
    finally:
        if kernel is not None:
            try:
                # 先 close（断线结算/评估仍在写档案），后 release（释放
                # 会话占用）——顺序反了会让新课和结算并发写同一份进度。
                await kernel.close()
            except Exception as e:
                log("SESSION", f"Disconnect save failed: {e}")
            finally:
                registry.release(student_id, kernel)
        manager.disconnect(websocket)
