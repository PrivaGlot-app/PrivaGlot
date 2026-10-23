"""协议积木：openai_transcriptions 族的 ASR 引擎。

覆盖「multipart 文件上传 /audio/transcriptions」形状的厂商（智谱
glm-asr-2512 等）。档案 asr 段契约（ASR 侧键）：
  languages[]     声明支持的语言参数（当前引擎不发 language 字段，
                  各家默认 auto）
  limits          {"max_audio_seconds": 30} —— 时长守卫在分发器统一执行
  hotwords        {"max": 100} —— 热词表上限；逗号分隔字符串实测可用，
                  教材词汇反哺 #3（转写噪声观察项）
失败一律抛 ASRError（人话已按档案方言渲染）——与 TTS 同裁定：
不换厂商、不静默降级。
"""
import base64

import httpx

from .contracts import ASRError, ASRPort
from .dialect import render_dialect

_EXT_BY_MIME = {"audio/wav": ".wav", "audio/wave": ".wav",
                "audio/mpeg": ".mp3", "audio/mp3": ".mp3"}


class OpenAITranscriptionsASR(ASRPort):
    """openai_transcriptions 族引擎。一次调用 = 一份 selection。"""

    def __init__(self, selection: dict, transport: httpx.BaseTransport = None):
        self._sel = selection
        self._transport = transport  # 测试注入 MockTransport 用

    def transcribe(self, audio_base64: str, mime_type: str = "audio/wav",
                   language: str = "auto", hotwords=None) -> str:
        sel = self._sel
        sec = sel["section"]
        if not sel["api_key"]:
            raise ASRError(sec["no_key_hint"])
        audio_bytes = base64.b64decode(audio_base64)
        ext = _EXT_BY_MIME.get((mime_type or "").lower(), ".wav")
        files = {
            "model": (None, sel["model"]),
            "file": (f"audio{ext}", audio_bytes, mime_type or "audio/wav"),
        }
        if hotwords:
            cap = int((sec.get("hotwords") or {}).get("max", 0) or 0)
            words = [w for w in (x.strip() for x in hotwords) if w]
            if cap:
                words = words[:cap]
            if words:
                files["hotwords"] = (None, ",".join(words))
        try:
            with httpx.Client(timeout=120.0, transport=self._transport) as client:
                response = client.post(
                    f"{sel['base_url']}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {sel['api_key']}"},
                    files=files,
                )
        except httpx.HTTPError as e:
            raise ASRError(render_dialect(sel, None, type(e).__name__)) from e
        if response.status_code >= 400:
            raise ASRError(render_dialect(
                sel, response.status_code, response.text[:400]))
        try:
            text = (response.json() or {}).get("text", "")
        except ValueError:
            raise ASRError("⚠️ 语音识别返回了无法解析的内容。请稍后再试；"
                           "若持续失败，请换一个语音识别模型（网页端在模型"
                           "配置页，飞书端发「配置」）。")
        return text or ""
