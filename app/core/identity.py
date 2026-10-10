"""
Identity map — 通道侧 platform_id → student_id 的绑定存储
（设计决定：身份映射是通道的责任）。

通道拥有这个映射（飞书的 open_id、将来的 TG chat_id → student_id），内核
只认 student_id。本类是其中可复用的存储半边：一个 platform_id →
student_id 的 JSON 文件，读改写语义与现有 data/feishu_bindings.json 完全
兼容（同构格式，老数据无缝迁移）。首次接触的建档（问平台要名字、建档案）
留在通道里 —— 那需要平台 API，不是存储的事。
"""
import json
from pathlib import Path

from ..logging_config import log as logger


class IdentityMap:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.error(f"[IDENTITY] Failed to read {self.path.name}: {e}")
            return {}

    def get(self, platform_id: str) -> str | None:
        return self._data.get(platform_id)

    def bound_ids(self) -> set:
        """已绑定的 platform_id 集合（通道判断"首次接触"用）。"""
        return set(self._data)

    def bind(self, platform_id: str, student_id: str) -> None:
        """绑定并持久化。重复绑定同一个 id 是常规操作（覆盖写回）。"""
        self._data[platform_id] = student_id
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            logger.error(f"[IDENTITY] Failed to save {self.path.name}: {e}")
