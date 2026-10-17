"""
TTS Module - Xiaomi MiMo TTS (Cloud only, no Edge fallback)
"""
import base64
import struct
from openai import OpenAI
from . import config
from .logging_config import log as logger

# MiMo TTS is generative: the assistant message is a prefix it may continue,
# which once produced a runaway monologue on Chinese-quoted text (问题清单 #6).
# The user message is the doc-sanctioned place for performance direction —
# pin a verbatim read in the teacher persona.
_READ_VERBATIM = (
    "Read the assistant message out loud, word for word, in the voice of a "
    "warm and friendly teacher talking to a child. Do not add, repeat, or "
    "continue beyond the given text."
)


def _style_instruction(style_id: str) -> str:
    """The TTS half of the delivery-style setting.

    MiMo exposes no numeric speed parameter — a style tier is a
    natural-language cue appended AFTER the verbatim constraint, so that
    constraint keeps reading first (问题清单 #6). Returns "" for an unknown
    or absent style: the plain verbatim read is the safe default.
    """
    preset = config.STYLE_PRESETS.get(style_id or "")
    return f" {preset['tts']}" if preset else ""


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


class MiMoTTSEngine:
    def __init__(self):
        self.client = None
        self._initialized = False
        # 记下建 client 时用的是哪把 Key：用户在网页里换 Key 之后要能跟着换
        self._key_used = None
        self._base_used = None

    def _init_client(self):
        if (self._initialized
                and self._key_used == config.TTS_API_KEY
                and self._base_used == config.TTS_BASE_URL):
            return
        if not config.TTS_API_KEY:
            raise ValueError("MIMO_API_KEY not configured")
        self.client = OpenAI(api_key=config.TTS_API_KEY, base_url=config.TTS_BASE_URL)
        self._key_used = config.TTS_API_KEY
        self._base_used = config.TTS_BASE_URL
        self._initialized = True

    def synthesize(self, text: str, voice: str = None, style: str = None) -> bytes:
        """Synthesize text to WAV audio.

        `style` is a STYLE_PRESETS key controlling delivery (pace/tone). It is
        injected as a natural-language cue in the user message — the only
        style channel MiMo offers — never as a tag inside the assistant text,
        which is the prefix the generative model may continue.
        """
        self._init_client()
        if voice is None:
            from .teacher import teacher_store
            voice = teacher_store.default_teacher().voice

        instruction = _READ_VERBATIM + _style_instruction(style)
        messages = [
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": text},
        ]
        completion = self.client.chat.completions.create(
            model=config.TTS_MODEL,
            messages=messages,
            audio={"format": "wav", "voice": voice}
        )

        audio_data = completion.choices[0].message.audio
        if audio_data and audio_data.data:
            audio_bytes = base64.b64decode(audio_data.data)
            logger.info(f"[TTS] {len(text)} chars, voice={voice}, style={style} → "
                        f"{len(audio_bytes)} bytes "
                        f"(~{_wav_duration_seconds(audio_bytes)}s audio)")
            return audio_bytes
        raise Exception("No audio data in response")
