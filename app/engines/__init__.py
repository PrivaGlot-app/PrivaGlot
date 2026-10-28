"""引擎积木层 —— 把「厂商档案」装配成内核可插的「端口」。

分发器（llm_engine 等）是内核实际拿到的对象：每次调用现读 .env 与运行期
配置（vendors.llm_selection）解析当前厂商，把活儿转给对应协议族积木。
现读不缓存——网页里换 Key、改 LLM_VENDOR 都即时生效（旧单例哲学延续）。

Phase 1（#15 读法B，2026-10-21）装配了 LLM；Phase 2（同日）迁入 TTS
（openai_speech / mimo_generative_chat 两族）；ASR 仍走 app/asr.py 旧
单例，Phase 3 迁入（core/ 里的白名单引用同步清零，见 tests/kernel 的
tripwire）。

积木规则：
  - contracts.py 是内核唯一允许的引擎依赖；
  - engines/<协议族>.py 不出现厂商名，差异全部读档案；
  - 新厂商 = app/vendors/ 加一份档案，注册进 vendors._REGISTRY 即用。
"""
from .. import vendors
from .contracts import ASRError, ASRPort, LLMPort, TTSPort, TTSError
from .asr_mimo_audio_chat import MimoAudioChatASR
from .asr_openai_transcriptions import OpenAITranscriptionsASR
from .llm_openai_chat import OpenAIChatLLM
from .tts_mimo_generative import MimoGenerativeTTS
from .tts_openai_speech import OpenAISpeechTTS


class LLMDispatcher(LLMPort):
    """按 .env 现场解析当前 LLM 厂商/模型并转发给协议积木。"""

    @property
    def api_key(self) -> str:
        return vendors.llm_selection()["api_key"]

    async def generate_stream(
        self,
        user_message: str,
        history: list = None,
        system_prompt: str = None,
        max_tokens: int = None,
        temperature: float = 0.5,
    ):
        engine = OpenAIChatLLM(vendors.llm_selection())
        async for token in engine.generate_stream(
            user_message, history=history, system_prompt=system_prompt,
            max_tokens=max_tokens, temperature=temperature,
        ):
            yield token

    async def generate(
        self,
        user_message: str,
        history: list = None,
        system_prompt: str = None,
        max_tokens: int = None,
        temperature: float = 0.5,
    ) -> str:
        full = ""
        async for token in self.generate_stream(
            user_message, history=history, system_prompt=system_prompt,
            max_tokens=max_tokens, temperature=temperature,
        ):
            full += token
        return full


llm_engine = LLMDispatcher()


_TTS_FAMILIES = {
    "openai_speech": OpenAISpeechTTS,
    "mimo_generative_chat": MimoGenerativeTTS,
}


class TTSDispatcher(TTSPort):
    """按 .env 现场解析当前 TTS 厂商/模型并转发给协议积木。失败一律
    TTSError（人话），由内核转错误事件——2026-10-21 裁定：不降级不换厂商。"""

    def synthesize(self, text: str, voice: str = None, style: str = None) -> bytes:
        sel = vendors.tts_selection()
        family = (sel.get("section") or {}).get("protocol")
        family_cls = _TTS_FAMILIES.get(family)
        if family_cls is None:
            raise TTSError(f"⚠️ 语音模型档案协议未知（{family or '缺失'}），"
                           "请把这条信息发给项目作者。")
        return family_cls(sel).synthesize(text, voice=voice, style=style)


tts_engine = TTSDispatcher()


_ASR_FAMILIES = {
    "openai_transcriptions": OpenAITranscriptionsASR,
    "mimo_audio_chat": MimoAudioChatASR,
}


def _wav_seconds_from_b64(audio_base64: str):
    """WAV 头里的时长（秒）；非 WAV 或解析失败返回 None（交由上游处置）。"""
    import base64 as _b64
    import struct as _struct
    try:
        raw = _b64.b64decode(audio_base64)
    except Exception:
        return None
    if len(raw) >= 44 and raw[:4] == b"RIFF":
        try:
            byte_rate = _struct.unpack("<I", raw[28:32])[0]
            if byte_rate > 0:
                return (len(raw) - 44) / byte_rate
        except Exception:
            pass
    return None


class ASRDispatcher(ASRPort):
    """按 .env 现场解析当前 ASR 厂商/模型并转发给协议积木。

    时长守卫在分发器统一执行：档案声明 limits.max_audio_seconds 的厂商
    （智谱 30 秒硬限）在请求发出前人话报错——比上游 1214 报错更可懂，
    也省一次上传。
    """

    def transcribe(self, audio_base64: str, mime_type: str = "audio/wav",
                   language: str = "auto", hotwords=None) -> str:
        sel = vendors.asr_selection()
        sec = sel["section"]
        limit = (sec.get("limits") or {}).get("max_audio_seconds")
        seconds = _wav_seconds_from_b64(audio_base64)
        if seconds and limit and seconds > float(limit):
            raise ASRError(
                f"⚠️ 这句语音约 {int(seconds + 0.5)} 秒，超过当前语音识别"
                f"模型的上限（{int(limit)} 秒）。请说完一句再松开发送，"
                "或直接打字发文字。")
        family = sec.get("protocol")
        family_cls = _ASR_FAMILIES.get(family)
        if family_cls is None:
            raise ASRError(f"⚠️ 语音识别档案协议未知（{family or '缺失'}），"
                           "请把这条信息发给项目作者。")
        cap = int((sec.get("hotwords") or {}).get("max", 0) or 0)
        words = [w for w in (x.strip() for x in (hotwords or [])) if w][:cap] or None
        return family_cls(sel).transcribe(audio_base64, mime_type,
                                          language, hotwords=words)


asr_engine = ASRDispatcher()
