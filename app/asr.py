"""
ASR Module - Xiaomi MiMo ASR (Cloud only, no local whisper)
"""
from openai import OpenAI
from . import config


class MiMoASREngine:
    def __init__(self):
        self.client = None
        self._initialized = False
        # 记下建 client 时用的是哪把 Key：用户在网页里换 Key 之后要能跟着换
        self._key_used = None
        self._base_used = None

    def _init_client(self):
        if (self._initialized
                and self._key_used == config.ASR_API_KEY
                and self._base_used == config.ASR_BASE_URL):
            return
        if not config.ASR_API_KEY:
            raise ValueError("MIMO_API_KEY not configured")
        self.client = OpenAI(api_key=config.ASR_API_KEY, base_url=config.ASR_BASE_URL)
        self._key_used = config.ASR_API_KEY
        self._base_used = config.ASR_BASE_URL
        self._initialized = True

    def transcribe(self, audio_base64: str, mime_type: str = "audio/wav", language: str = "auto") -> str:
        self._init_client()
        data_url = f"data:{mime_type};base64,{audio_base64}"
        completion = self.client.chat.completions.create(
            model=config.ASR_MODEL,
            messages=[{
                "role": "user",
                "content": [{"type": "input_audio", "input_audio": {"data": data_url}}]
            }],
            extra_body={"asr_options": {"language": language}}
        )
        return completion.choices[0].message.content


mimo_asr_engine = MiMoASREngine()
