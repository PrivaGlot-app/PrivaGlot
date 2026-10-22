"""协议积木：openai_speech 族的 TTS 引擎（/audio/speech 形状的真合成器）。

覆盖智谱 glm-tts 及同形状厂商（请求 = model/input/voice/speed/
response_format，非流式返回二进制 WAV，24kHz）。档案 tts 段契约：
  style_render {"mode": "speed", "gentle": 0.85, ...}  风格三档→语速参数
  limits       {"max_input_chars": 1024}  超限人话报错（不静默截断，
               2026-10-21 裁定：不降级不掩盖）
  billing      "tokens" | "characters"  备查（话术与配置页展示用）

失败一律抛 TTSError（人话已按档案方言渲染）——2026-10-21 裁定：
TTS 故障直接报错，不换厂商、不降级为文字。
"""
import base64
import struct

import httpx

from ..logging_config import log as logger
from .contracts import TTSPort, TTSError
from .dialect import GENERIC_TTS, render_dialect


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


def _parse_wav(wav_bytes: bytes):
    """(prefix, fmt_body, data_body)——标准 chunk 走查（头部偏移不假定 44）。"""
    if wav_bytes[:4] != b"RIFF":
        return None
    pos = 12
    fmt = data = None
    while pos + 8 <= len(wav_bytes):
        cid = wav_bytes[pos:pos + 4]
        size = struct.unpack("<I", wav_bytes[pos + 4:pos + 8])[0]
        body = wav_bytes[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            data = body
            break
        pos += 8 + size + (size % 2)
    if fmt is None or data is None:
        return None
    return wav_bytes[:pos], fmt, data


def _trim_leading_preamble(wav: bytes) -> bytes:
    """智谱语音每段合成自带确定性引导音：四声 333Hz 纯音＋静音（实测恒定
    ~1.76s，听感为「嘟嘟」两声，2026-10-21 探针定谳——payload 无参数可关）。
    内容感知裁剪：跳过开头的纯音/静音窗，从首个「持续语音」窗保留。
    判据＝波峰系数 crest（纯音≈√2，语音≫2），连续 3 个语音窗才算起点；
    2 秒内找不到起点或格式非 16bit → 原样返回（宁可保留嘟嘟不误剪语音）。"""
    parsed = _parse_wav(wav)
    if parsed is None:
        return wav
    prefix, fmt, data = parsed
    if struct.unpack("<H", fmt[14:16])[0] != 16:
        return wav
    rate = struct.unpack("<I", fmt[4:8])[0]
    channels = struct.unpack("<H", fmt[2:4])[0] or 1
    frame_bytes = channels * 2
    win = max(1, rate // 100) * frame_bytes          # 10ms 窗
    total = len(data) // frame_bytes
    win_frames = win // frame_bytes
    scan_windows = min(total // win_frames, 300)     # 上限 3.0s（引导音实测 ~2.06s 处才出语音）
    onset_window = None
    run = 0
    for w in range(scan_windows):
        seg = data[w * win:(w + 1) * win]
        samples = struct.unpack("<" + "h" * (len(seg) // 2), seg)
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5 if samples else 0
        peak = max((abs(s) for s in samples), default=0)
        if rms < 100:
            run = 0
            continue
        crest = peak / rms if rms else 0
        if crest < 1.8:
            run = 0        # 纯音（引导音特征）
            continue
        run += 1           # 语音样窗
        if run >= 3:
            onset_window = w - 2
            break
    if onset_window is None:
        return wav
    cut = onset_window * win
    if cut <= 0:
        return wav
    kept = data[cut:]
    # prefix 含 RIFF 头＋WAVE＋data 之前的全部 chunk（不含 data 块头）
    body = b"data" + struct.pack("<I", len(kept)) + kept
    out = prefix + body
    out = out[:4] + struct.pack("<I", len(out) - 8) + out[8:]
    return out


class OpenAISpeechTTS(TTSPort):
    """openai_speech 族引擎。一次调用 = 一份 selection。"""

    def __init__(self, selection: dict, transport: httpx.BaseTransport = None):
        self._sel = selection
        self._transport = transport  # 测试注入 MockTransport 用

    def synthesize(self, text: str, voice: str = None, style: str = None) -> bytes:
        sel = self._sel
        sec = sel["section"]
        if not sel["api_key"]:
            raise TTSError(sec["no_key_hint"])
        limit = (sec.get("limits") or {}).get("max_input_chars")
        if limit and len(text) > int(limit):
            raise TTSError(
                f"⚠️ 本句 {len(text)} 字符，超出该语音模型单次上限（{limit}）。"
                "请稍后再试；若反复出现请把这条信息发给项目作者。")
        render = sec.get("style_render") or {}
        speed = (float(render.get(style, 1.0))
                 if render.get("mode") == "speed" else 1.0)
        voice = voice or sec.get("default_voice") or ""
        payload = {
            "model": sel["model"],
            "input": text,
            "voice": voice,
            "speed": speed,
            "response_format": "wav",
        }
        try:
            with httpx.Client(timeout=120.0, transport=self._transport) as client:
                response = client.post(
                    f"{sel['base_url']}/audio/speech",
                    headers={"Authorization": f"Bearer {sel['api_key']}",
                             "Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.HTTPError as e:
            raise TTSError(render_dialect(sel, None, type(e).__name__)) from e
        if response.status_code >= 400:
            raise TTSError(render_dialect(
                sel, response.status_code, response.text[:400]))
        raw = response.content
        if raw[:4] != b"RIFF" and response.headers.get(
                "content-type", "").startswith("application/json"):
            try:
                raw = base64.b64decode(response.json().get("data", ""))
            except Exception:
                pass
        if not raw or raw[:4] != b"RIFF":
            raise TTSError("⚠️ 语音合成返回了无法识别的音频。请稍后再试；"
                           "若持续失败，请换一个语音模型（网页端在模型配置页，"
                           "飞书端发「配置」）。")
        raw = _trim_leading_preamble(raw)
        logger.info(f"[TTS] {len(text)} chars, voice={voice}, style={style}, "
                    f"speed={speed} → {len(raw)} bytes "
                    f"(~{_wav_duration_seconds(raw)}s audio)")
        return raw
