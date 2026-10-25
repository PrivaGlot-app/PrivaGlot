"""协议积木：mimo_generative_chat 族的 TTS 引擎（自 app/tts.py 迁入，#15 Phase 2）。

生成式朗读：chat 形状（user=朗读指令 / assistant=要念的文本）+ audio 参数
返回 base64 WAV。风格三档 = 自然语言提示拼进指令（小米无语速旋钮的
历史形态，档案 style_render.mode == "cue" 声明）。

失败一律抛 TTSError（人话已按档案方言渲染）——不再静默吞掉，
2026-10-21 裁定：TTS 故障直接报错，不降级为文字。
"""
import base64
import struct

from openai import OpenAI

from .. import config
from ..logging_config import log as logger
from .contracts import TTSPort, TTSError

# 生成式模型的朗读约束：assistant 消息是它可能续写的前缀（MiMo TTS 幻觉
# 续写 #6 的输入侧对策——用户消息是官方许可的表演指令位）。
_READ_VERBATIM = (
    "Read the assistant message out loud, word for word, in the voice of a "
    "warm and friendly teacher talking to a child. Do not add, repeat, or "
    "continue beyond the given text."
)


def build_instruction(style_render: dict, style: str) -> str:
    """朗读指令 = 逐字约束（+cue 模式下的风格提示，置于约束之后，
    保证「先念原文」的优先级——#6 教训）。"""
    instruction = _READ_VERBATIM
    if (style_render or {}).get("mode") == "cue":
        preset = config.STYLE_PRESETS.get(style or "")
        if preset:
            instruction += f" {preset['tts']}"
    return instruction


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    """Duration of a WAV buffer, from its header; falls back to 24 kHz/16-bit."""
    try:
        if len(wav_bytes) >= 44 and wav_bytes[:4] == b"RIFF":
            byte_rate = struct.unpack("<I", wav_bytes[28:32])[0]
            if byte_rate > 0:
                return round((len(wav_bytes) - 44) / byte_rate, 1)
    except Exception:
        pass
    return round(len(wav_bytes) / (24000 * 2), 1)


class MimoGenerativeTTS(TTSPort):
    """mimo_generative_chat 族引擎。一次调用 = 一份 selection。"""

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
            raise TTSError(sel["section"]["no_key_hint"])
        self.client = OpenAI(api_key=sel["api_key"], base_url=sel["base_url"])
        self._key_used = sel["api_key"]
        self._base_used = sel["base_url"]

    def synthesize(self, text: str, voice: str = None, style: str = None) -> bytes:
        sel = self._sel
        sec = sel["section"]
        self._init_client()
        voice = voice or sec.get("default_voice") or ""
        instruction = build_instruction(sec.get("style_render"), style)
        messages = [
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": text},
        ]
        try:
            completion = self.client.chat.completions.create(
                model=sel["model"],
                messages=messages,
                audio={"format": "wav", "voice": voice},
            )
        except Exception as e:
            status = getattr(e, "status_code", None)
            from .dialect import render_dialect
            raise TTSError(render_dialect(sel, status, str(e))) from e
        audio = completion.choices[0].message.audio
        if audio and audio.data:
            audio_bytes = base64.b64decode(audio.data)
            logger.info(f"[TTS] {len(text)} chars, voice={voice}, style={style} → "
                        f"{len(audio_bytes)} bytes "
                        f"(~{_wav_duration_seconds(audio_bytes)}s audio)")
            return audio_bytes
        raise TTSError("⚠️ 语音合成返回里没有音频数据，请把这条信息发给项目作者。")
