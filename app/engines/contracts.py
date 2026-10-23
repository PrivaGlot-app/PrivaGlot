"""模态端口契约 —— 内核（app/core/）认识的三只「插座」。

军规（#15，2026-10-21 立法）：内核只许 import 本模块来获得引擎能力，
不许 import 任何厂商档案（app/vendors/）或协议积木（engines/ 具体实现）；
反向亦然——积木不 import 内核。厂商知识全部住在档案里，运行期由
engines/__init__.py 的分发器把「档案声明」装配成「端口实现」。

接口形状与 tests/kernel 的打桩引擎逐字对齐（FakeLLMEngine 等）：
改这里必须同步改打桩，反之亦然。分发器遵守本契约，打桩对象走鸭子
类型——契约是文档与法定形状，不做运行期 isinstance 拦截。

Phase 1 只启用了 LLMPort；ASRPort / TTSPort 先立法，随 Phase 2/3
把 app/asr.py、app/tts.py 的旧单例迁进来。
"""
import abc
from typing import AsyncGenerator


class TTSError(RuntimeError):
    """语音合成的带话术失败。message 已是给学生/家长看的人话
    （含原因与「换一个可用模型」指引——网页端指模型配置页，飞书端指
    「配置」卡），通道层原样展示即可。
    2026-10-21 裁定：TTS 故障直接报错——不换厂商、不降级为文字。"""


class ASRError(RuntimeError):
    """语音识别的带话术失败（契约同 TTSError）：超时长上限、无 Key、
    上游错误方言等，message 直接可展示。"""


class LLMPort(abc.ABC):
    """对话端口：内核唯一的「大脑」插座。"""

    @property
    @abc.abstractmethod
    def api_key(self) -> str:
        """当前 Key（现读语义：运行期换 Key 立即可见）。"""

    @abc.abstractmethod
    def generate_stream(
        self,
        user_message: str,
        history: list = None,
        system_prompt: str = None,
        max_tokens: int = None,
        temperature: float = 0.5,
    ) -> AsyncGenerator[str, None]:
        """流式生成一句回复；失败时以一条 ⚠️ 人话文本收尾，不抛异常。"""

    @abc.abstractmethod
    async def generate(
        self,
        user_message: str,
        history: list = None,
        system_prompt: str = None,
        max_tokens: int = None,
        temperature: float = 0.5,
    ) -> str:
        """一次性生成（generate_stream 的聚合）。"""


class ASRPort(abc.ABC):
    """识别端口：内核唯一的「耳朵」插座（Phase 3 启用）。"""

    @abc.abstractmethod
    def transcribe(self, audio_base64: str, mime_type: str = "audio/wav",
                   language: str = "auto") -> str:
        """音频 base64 → 转写文本（同步接口，与现役 MiMo ASR 一致）。"""


class TTSPort(abc.ABC):
    """合成端口：内核唯一的「嗓子」插座（Phase 2 启用）。"""

    @abc.abstractmethod
    def synthesize(self, text: str, voice: str = None,
                   style: str = None) -> bytes:
        """文本 → WAV 音频字节（同步接口，与现役 MiMo TTS 一致）。"""
