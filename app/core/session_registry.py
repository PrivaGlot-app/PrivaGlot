"""
Session registry — 按 student_id 的会话表 + 每用户 single-flight
（设计决定：同一学生同时只允许一个活跃会话）。

"同一节课、同一份进度"的另一半保证：任何时刻、任何通道，一个学生只允许
有一个活着的 ConversationKernel，否则两份对话历史会把同一份学生档案写花
（进度/评估的读改写互相踩）。两层互斥：

  - 进程内：_entries 表。谁先 register 谁持有，后来者拿到
    StudentSessionBusy（通道负责把这句话翻译给用户）。
  - 跨进程：data/session_locks/<student_id>.lock 上的 OS 文件锁。both 部署
    是 web 与 feishu 两个进程（bootstrap.ps1 / install_linux.sh 均如此），
    内存表互相看不见；OS 文件锁在进程崩溃时自动释放，这正是选它而不是
    文件占位标记的原因。

single-flight：同一学生的多条消息并发到达（两个网页标签页、网页与飞书
同时开口）时按 FIFO 排队，turn() 是每学生一把的 asyncio.Lock，按事件
循环惰性创建（本进程内 web/feishu 各一个循环，学生不会混用两个循环）。

军规自查：本模块属于 core，但只认识 student_id 与 kernel.state，不认识
任何平台。第三、四个通道接入时这里的 diff 必须为零。
"""
import asyncio
import contextlib
import os

from .. import config


def _validated_student_id(student_id: str) -> str:
    """student_id 会成为文件锁的文件名（data/session_locks/<id>.lock）。

    student.py 的 id 规则：ASCII 净化词（zhang_yan）或原始中文名（小明），
    所以这里不能按 ASCII 白名单卡 —— 只拒绝路径注入与控制字符，
    中文名等合法 id 原样放行。
    """
    sid = (student_id or "").strip()
    if (not sid or len(sid) > 100 or "/" in sid or "\\" in sid
            or sid in (".", "..") or any(ord(c) < 32 for c in sid)):
        raise ValueError(f"bad student_id: {student_id!r}")
    return sid


class StudentSessionBusy(Exception):
    """该学生在本进程或另一个进程已有进行中的课。"""

    def __init__(self, student_id: str):
        self.student_id = student_id
        super().__init__(f"student {student_id} already has an active session")


class _OsFileLock:
    """跨进程咨询锁。try_acquire 非阻塞，拿不到就干净地返回 False ——
    调用方得到的是"该学生正忙"的明确信号，而不是一次无声的挂起。"""

    def __init__(self, path):
        self._path = path
        self._fh = None

    def try_acquire(self) -> bool:
        if self._fh is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "a+b")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh = fh
            return True
        except (OSError, ValueError):
            fh.close()
            return False

    def release(self):
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass  # 进程退出时 OS 无论如何都会释放
        finally:
            fh.close()  # Windows 上句柄不关，锁文件就删不掉


class _SessionEntry:
    def __init__(self, kernel, file_lock: _OsFileLock):
        self.kernel = kernel
        self.file_lock = file_lock


class SessionRegistry:
    """一个进程一张表。web / feishu / REPL 各自的进程里各有一份，
    跨进程的一致性由文件锁保证。"""

    def __init__(self, lock_dir=None):
        self._lock_dir = lock_dir or config.SESSION_LOCKS_DIR
        self._entries: dict[str, _SessionEntry] = {}
        self._turn_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}

    # ---- ownership ----

    def register(self, student_id: str, kernel) -> None:
        """为 kernel 占住这个学生。busy 时抛 StudentSessionBusy，通道负责
        提示；ended/failed 的残留条目在这里清掉，让新课立刻能开。"""
        safe_id = _validated_student_id(student_id)
        entry = self._entries.get(student_id)
        if entry is not None and entry.kernel is not kernel:
            if entry.kernel.state in ("fresh", "active"):
                raise StudentSessionBusy(student_id)
            self._drop(student_id)
        if entry is not None and entry.kernel is kernel:
            return  # 同一个 kernel 重复 register：什么也不做，别二次拿锁
        file_lock = _OsFileLock(self._lock_dir / f"{safe_id}.lock")
        if not file_lock.try_acquire():
            raise StudentSessionBusy(student_id)
        self._entries[student_id] = _SessionEntry(kernel, file_lock)

    def release(self, student_id: str, kernel) -> None:
        """只放得掉自己名下的会话； kernel 无害。"""
        entry = self._entries.get(student_id)
        if entry is not None and entry.kernel is kernel:
            self._drop(student_id)

    def _drop(self, student_id: str):
        entry = self._entries.pop(student_id, None)
        if entry is not None:
            entry.file_lock.release()

    def active_kernel(self, student_id: str):
        entry = self._entries.get(student_id)
        if entry is not None and entry.kernel.state in ("fresh", "active"):
            return entry.kernel
        return None

    # ---- single-flight ----

    @contextlib.asynccontextmanager
    async def turn(self, student_id: str):
        """包住一次完整的"说话→回复"流水线：同学生的并发请求在这里排队。
        Lock 按事件循环惰性创建并绑定 —— 换了循环（理论上不会发生）就重建。"""
        loop = asyncio.get_running_loop()
        bound = self._turn_locks.get(student_id)
        if bound is None or bound[0] is not loop:
            bound = (loop, asyncio.Lock())
            self._turn_locks[student_id] = bound
        async with bound[1]:
            yield


# 进程级单例。通道 import 它；测试里整个换掉（SessionRegistry(tmp)）。
registry = SessionRegistry()
