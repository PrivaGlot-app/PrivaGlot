"""
Feishu Open Platform REST client — only what the channel needs:
tenant token, voice download, opus upload, audio/text send, user name.

Deliberately plain httpx (already a project dependency) with explicit
error text: the lark-oapi SDK is used ONLY for its WebSocket long
connection (event subscription), where its reconnect logic earns its
keep. Token caching is process-local and refreshed 5 minutes early.
"""
import json
import threading
import time

import httpx

from ... import config
from ...logging_config import log as logger


class FeishuAPIError(Exception):
    """A Feishu OpenAPI call failed (non-zero code or bad HTTP status)."""


class FeishuClient:
    def __init__(self, app_id: str = None, app_secret: str = None,
                 domain: str = None):
        self.app_id = (app_id or config.FEISHU_APP_ID).strip()
        self.app_secret = (app_secret or config.FEISHU_APP_SECRET).strip()
        self.domain = (domain or config.FEISHU_DOMAIN).rstrip("/")
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------

    def tenant_access_token(self) -> str:
        with self._token_lock:
            if self._token and time.time() < self._token_expires_at - 300:
                return self._token
            resp = httpx.post(
                f"{self.domain}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=15)
            data = resp.json()
            if data.get("code") != 0:
                raise FeishuAPIError(
                    f"获取 tenant_access_token 失败: {data.get('msg')}")
            self._token = data["tenant_access_token"]
            self._token_expires_at = time.time() + int(data.get("expire", 7200))
            return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.tenant_access_token()}"}

    @staticmethod
    def _unwrap(data: dict, what: str):
        if data.get("code") != 0:
            raise FeishuAPIError(f"{what} 失败: {data.get('msg')}")
        return data.get("data") or {}

    # ------------------------------------------------------------------
    # im resources
    # ------------------------------------------------------------------

    def download_message_resource(self, message_id: str, file_key: str) -> bytes:
        """Download a message's file (voice notes are opus)."""
        resp = httpx.get(
            f"{self.domain}/open-apis/im/v1/messages/{message_id}"
            f"/resources/{file_key}",
            params={"type": "file"}, headers=self._headers(), timeout=60)
        if resp.status_code != 200:
            raise FeishuAPIError(
                f"下载语音失败: HTTP {resp.status_code} {resp.text[:120]}")
        return resp.content

    def upload_opus(self, opus_bytes: bytes, duration_ms: int) -> str:
        """Upload a voice clip; returns its file_key. `duration` (ms) is
        required for opus files — the client shows it on the voice bar."""
        resp = httpx.post(
            f"{self.domain}/open-apis/im/v1/files",
            headers=self._headers(),
            data={"file_type": "opus", "file_name": "teacher.opus",
                  "duration": str(max(1, int(duration_ms)))},
            files={"file": ("teacher.opus", opus_bytes, "audio/opus")},
            timeout=60)
        data = self._unwrap(resp.json(), "上传语音")
        file_key = data.get("file_key")
        if not file_key:
            raise FeishuAPIError("上传语音失败: 响应中没有 file_key")
        return file_key

    def upload_image(self, image_bytes: bytes,
                     filename: str = "guide.jpg") -> str:
        """Upload an image for sending; returns its image_key (im/v1/images,
        image_type=message)——原生图片消息与卡片 img 组件都只收 image_key。
        即点即传不缓存：image_key 绑定在当前应用名下，重扫部署换应用后
        旧 key 必失效，缓存反而要写失效逻辑（#37，2026-10-14 拍板）。"""
        resp = httpx.post(
            f"{self.domain}/open-apis/im/v1/images",
            headers=self._headers(),
            data={"image_type": "message"},
            files={"image": (filename, image_bytes, "image/jpeg")},
            timeout=60)
        data = self._unwrap(resp.json(), "上传图片")
        image_key = data.get("image_key")
        if not image_key:
            raise FeishuAPIError("上传图片失败: 响应中没有 image_key")
        return image_key

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------

    def _send_message(self, open_id: str, msg_type: str, content: dict):
        resp = httpx.post(
            f"{self.domain}/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            headers=self._headers(),
            json={"receive_id": open_id, "msg_type": msg_type,
                  "content": json.dumps(content, ensure_ascii=False)},
            timeout=30)
        self._unwrap(resp.json(), f"发送{msg_type}消息")

    def send_audio(self, open_id: str, file_key: str):
        self._send_message(open_id, "audio", {"file_key": file_key})

    def send_image(self, open_id: str, image_key: str):
        self._send_message(open_id, "image", {"image_key": image_key})

    def send_text(self, open_id: str, text: str):
        self._send_message(open_id, "text", {"text": text})

    def send_card(self, open_id: str, card: dict):
        """发送交互卡片（v1 / JSON 2.0 均为同一 msg_type=interactive）。
        按钮点击回调走长连接的 card.action.trigger（见 channel._on_card_action）。"""
        self._send_message(open_id, "interactive", card)

    # ------------------------------------------------------------------
    # message reactions (#23 等待焦虑：给学生的消息挂「输入中」回应)
    # ------------------------------------------------------------------

    def create_reaction(self, message_id: str, emoji_type: str) -> str:
        """给一条消息添加表情回应，返回 reaction_id（删除时要用）。
        失败抛 FeishuAPIError——调用方决定是否在意（打字标记按尽力而为
        处理，静默降级）。"""
        resp = httpx.post(
            f"{self.domain}/open-apis/im/v1/messages/{message_id}/reactions",
            headers=self._headers(),
            json={"reaction_type": {"emoji_type": emoji_type}},
            timeout=15)
        data = self._unwrap(resp.json(), "添加表情回应")
        return str(data.get("reaction_id") or "")

    def delete_reaction(self, message_id: str, reaction_id: str):
        """删除自己添加的表情回应（只能删自己加的）。"""
        resp = httpx.delete(
            f"{self.domain}/open-apis/im/v1/messages/{message_id}"
            f"/reactions/{reaction_id}",
            headers=self._headers(), timeout=15)
        self._unwrap(resp.json(), "删除表情回应")

    # ------------------------------------------------------------------
    # contacts (naming on first contact only; failure is non-fatal)
    # ------------------------------------------------------------------

    def get_user_name(self, open_id: str) -> str:
        try:
            resp = httpx.get(
                f"{self.domain}/open-apis/contact/v3/users/{open_id}",
                params={"user_id_type": "open_id"},
                headers=self._headers(), timeout=15)
            data = self._unwrap(resp.json(), "查询用户名")
            return str((data.get("user") or {}).get("name") or "")
        except Exception as e:
            logger.error(f"[FEISHU] get_user_name: {e}")
            return ""
