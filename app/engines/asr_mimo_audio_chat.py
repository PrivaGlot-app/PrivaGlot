"""协议积木：mimo_audio_chat 族的 ASR 引擎（自 app/asr.py 迁入，#15 Phase 3）。

audio-in-chat 协议：chat 形状 + input_audio 内容（data URL）+
asr_options.language。小米无热词参数（官方文档 asr_options 仅 language），
hotwords 入参被忽略——热词反哺只在声明了 hotwords 上限的厂商生效。
官方文档无单次时长上限（格式限 mp3/wav）；失败抛 ASRError（人话）。
"""
from openai import OpenAI

from .contracts import ASRError, ASRPort
from .dialect import render_dialect


class MimoAudioChatASR(ASRPort):
    """mimo_audio_chat 族引擎。一次调用 = 一份 selection。"""

    def __init__(self, selection: dict):
        self._sel = selection
        self.client = None
        self._key_used = None
        self._base_used = None

    def _init_client(self):
        sel = self._sel
        if (self.client is not None
                and self._key_used == sel["api_key"]
                and self._base_used == sel["base_url"]):
            return
        if not sel["api_key"]:
            raise ASRError(sel["section"]["no_key_hint"])
        self.client = OpenAI(api_key=sel["api_key"], base_url=sel["base_url"])
        self._key_used = sel["api_key"]
        self._base_used = sel["base_url"]

    def transcribe(self, audio_base64: str, mime_type: str = "audio/wav",
                   language: str = "auto", hotwords=None) -> str:
        sel = self._sel
        self._init_client()
        data_url = f"data:{mime_type};base64,{audio_base64}"
        try:
            completion = self.client.chat.completions.create(
                model=sel["model"],
                messages=[{
                    "role": "user",
                    "content": [{"type": "input_audio",
                                 "input_audio": {"data": data_url}}],
                }],
                extra_body={"asr_options": {"language": language}},
            )
        except Exception as e:
            status = getattr(e, "status_code", None)
            raise ASRError(render_dialect(sel, status, str(e))) from e
        return completion.choices[0].message.content or ""
