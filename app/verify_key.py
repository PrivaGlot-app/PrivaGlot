"""
PrivaGlot —— API Key 自检

回答一个问题：这把 key 现在能不能真的把课上起来？

做两次最小调用（合计花费远不到 1 分钱）：
  1) 对话：发一个 "hi"，只要 1 个 token —— 验证 key、余额、模型名、BASE_URL
  2) 语音合成：合成一个单词 —— 验证这个通道确实提供 TTS，音色名也一并验了
不做 ASR：它需要一段真实音频，代价和复杂度都不划算。对话和 TTS 都通，
ASR 出问题的概率很低，真出问题也会在第一次录音时暴露。

网页里填 Key（app/main.py 的 /api/setup/key）和环境自检的
setup/check_env.py --with-api 共用这一份，免得两边的说法不一致。
"""
from dataclasses import dataclass, field

import httpx

from . import config, vendors

# 给调用方（网页接口 / 命令行）判断用，别拿它当 HTTP 状态码
OK = "ok"
BAD_KEY = "bad_key"
NO_QUOTA = "no_quota"
NO_NETWORK = "no_network"
OTHER = "other"


@dataclass
class CheckResult:
    code: str
    message: str = ""
    detail: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.code == OK


def classify(response: httpx.Response, profile: dict = None) -> CheckResult:
    """把 HTTP 错误翻译成非技术用户能照着做的一句话。话术来自供应商档案
    （app/vendors/<厂商>.py 的 verify 段），这里只做分发；profile 缺省 =
    当前激活厂商（旧单厂商探活路径）。"""
    v = (profile or vendors.active())["verify"]
    code = response.status_code
    if code == 401:
        return CheckResult(BAD_KEY, v["bad_key"])
    if code == 402:
        return CheckResult(NO_QUOTA, v["no_quota"])
    if code == 403:
        return CheckResult(NO_QUOTA, v["forbidden"])
    if code == 404:
        return CheckResult(OTHER, "接口或模型不存在（404）。多半是 BASE_URL 和 Key 不是同一条通道；"
                                 "也可能是默认对话模型已下线 —— 请把 PrivaGlot 升级到最新版本，"
                                 "或在 .env 里用 LLM_MODEL 指定可用模型。")
    if code == 429:
        return CheckResult(OTHER, "请求太频繁被限流（429）。等一分钟再试。")
    if code >= 500:
        return CheckResult(OTHER, v["server_error"].format(code=code))
    return CheckResult(OTHER, f"请求失败（{code}）：{response.text[:200]}")


async def post_once(client: httpx.AsyncClient, url: str, key: str, payload: dict,
                    profile: dict = None):
    """POST 一次。返回 (错误结果 or None, 响应 or None)。"""
    try:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
    except httpx.HTTPError as exc:
        no_network = (profile or vendors.active())["verify"]["no_network"]
        return CheckResult(NO_NETWORK, no_network.format(err=type(exc).__name__)), None
    if response.status_code >= 400:
        return classify(response, profile), None
    return None, response


async def verify_api_key(
    key: str,
    base_url: str = "",
    llm_model: str = "",
    tts_model: str = "",
    voice: str = "Dean",
) -> CheckResult:
    """真调两次接口验证这把 Key。不写任何文件，落盘由调用方决定。"""
    key = (key or "").strip()
    base = (base_url or config.base_url_for_key(key)).rstrip("/")
    llm_model = llm_model or config.LLM_MODEL
    tts_model = tts_model or config.TTS_MODEL
    detail = [f"BASE_URL：{base}"]

    if not key:
        return CheckResult(BAD_KEY, "没有填入 API Key。")

    # Key 前缀和通道必须一致，否则后面每个请求都会 401/404，而且错误信息看不懂。
    v = vendors.active()
    prefixes = tuple(ch["prefix"] for ch in v["channels"])
    expected = config.base_url_for_key(key)
    if key.startswith(prefixes):
        if base != expected:
            detail.append(f"注意：{key[:3]} 开头的 Key 通常要配 {expected}，"
                          f"当前填的是 {base}。")
    else:
        detail.append(v["verify"]["prefix_hint"].format(
            prefixes=vendors.prefix_labels()))

    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1) 对话
        failure, response = await post_once(client, f"{base}/chat/completions", key, {
            "model": llm_model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_completion_tokens": 1,
            "stream": False,
        })
        if failure:
            return CheckResult(failure.code, failure.message, detail)
        usage = {}
        try:
            usage = response.json().get("usage") or {}
        except ValueError:
            pass
        detail.append(f"对话模型 {llm_model} 可用"
                      f"（本次用量 {usage.get('total_tokens', '?')} tokens）")

        # 2) 语音合成
        failure, response = await post_once(client, f"{base}/chat/completions", key, {
            "model": tts_model,
            "messages": [
                {"role": "user", "content": "Read the assistant message out loud, word for word."},
                {"role": "assistant", "content": "Hi"},
            ],
            "audio": {"format": "wav", "voice": voice},
        })
        if failure:
            return CheckResult(failure.code, failure.message, detail)
        try:
            audio = response.json()["choices"][0]["message"]["audio"]["data"]
        except (ValueError, KeyError, IndexError, TypeError):
            return CheckResult(OTHER, "语音合成返回里没有音频数据，请把这条信息发给项目作者。", detail)
        detail.append(f"语音合成 {tts_model} / 音色 {voice} 可用"
                      f"（返回 {len(audio)} 字符音频）")

    return CheckResult(OK, "API Key 可用。", detail)


# ---------------------------------------------------------------------------
# 模态化探活（#15 配置页期）：逐模态独立探测「候选配置」。
# 与 verify_api_key（旧单厂商一把 Key 探两枪）并存：配置页保存时对三个
# 模态各强测 3 次（2026-10-21 用户裁定：三次全过才算成功），全过才应用。
# payload 刻意最小化、不带厂商专有参数——探的是「这把 Key + 这个模型在
# 这条通道上活着」，不是功能全集。话术按候选厂商档案渲染（不是当前激活
# 厂商）——探智谱的新配置要说智谱的话。
# ---------------------------------------------------------------------------

def _profile_for(vendor_id: str) -> dict:
    return vendors._REGISTRY.get(vendor_id) or vendors.active()


def _silence_wav_b64(seconds: float = 1.0, rate: int = 16000) -> str:
    """1 秒 16k 静音 wav——ASR 探活只验「通道+模型活着」（200），不验词。"""
    import base64 as _b64
    import struct as _struct
    frames = b"\x00\x00" * int(rate * seconds)
    hdr = (b"RIFF" + _struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
           + _struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
           + b"data" + _struct.pack("<I", len(frames)))
    return _b64.b64encode(hdr + frames).decode()


async def probe_llm(key: str, base: str, model: str, vendor_id: str) -> CheckResult:
    profile = _profile_for(vendor_id)
    async with httpx.AsyncClient(timeout=60.0) as client:
        failure, _ = await post_once(
            client, f"{base.rstrip('/')}/chat/completions", key,
            {"model": model,
             "messages": [{"role": "user", "content": "hi"}],
             "stream": False}, profile)
        if failure:
            return failure
    return CheckResult(OK, f"思考模型 LLM {model} 可用")


async def probe_tts(key: str, base: str, model: str, vendor_id: str) -> CheckResult:
    profile = _profile_for(vendor_id)
    sec = (vendors._REGISTRY.get(vendor_id) or {}).get("tts") or {}
    voice = sec.get("default_voice", "")
    base = base.rstrip("/")
    async with httpx.AsyncClient(timeout=120.0) as client:
        if sec.get("protocol") == "openai_speech":
            failure, response = await post_once(
                client, f"{base}/audio/speech", key,
                {"model": model, "input": "Hi", "voice": voice,
                 "response_format": "wav"}, profile)
            if failure:
                return failure
            if response.content[:4] != b"RIFF":
                return CheckResult(OTHER, "语音合成返回里没有音频数据，请把这条信息发给项目作者。")
        else:  # mimo_generative_chat：chat 形状 + audio 参数
            failure, response = await post_once(
                client, f"{base}/chat/completions", key,
                {"model": model,
                 "messages": [
                     {"role": "user",
                      "content": "Read the assistant message out loud, word for word."},
                     {"role": "assistant", "content": "Hi"}],
                 "audio": {"format": "wav", "voice": voice}}, profile)
            if failure:
                return failure
            try:
                response.json()["choices"][0]["message"]["audio"]["data"]
            except (ValueError, KeyError, IndexError, TypeError):
                return CheckResult(OTHER, "语音合成返回里没有音频数据，请把这条信息发给项目作者。")
    return CheckResult(OK, f"语音合成 TTS {model} / 音色 {voice} 可用")


async def probe_asr(key: str, base: str, model: str, vendor_id: str) -> CheckResult:
    profile = _profile_for(vendor_id)
    sec = (vendors._REGISTRY.get(vendor_id) or {}).get("asr") or {}
    base = base.rstrip("/")
    audio_b64 = _silence_wav_b64()
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            if sec.get("protocol") == "openai_transcriptions":
                import base64 as _b64
                response = await client.post(
                    f"{base}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    files={"model": (None, model),
                           "file": ("probe.wav", _b64.b64decode(audio_b64),
                                    "audio/wav")})
            else:  # mimo_audio_chat：audio-in-chat（asr_options 是顶层键）
                response = await client.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": [
                              {"type": "input_audio",
                               "input_audio": {"data": f"data:audio/wav;base64,{audio_b64}"}}]}],
                          "asr_options": {"language": "auto"}})
        except httpx.HTTPError as exc:
            return CheckResult(NO_NETWORK, profile["verify"]["no_network"].format(
                err=type(exc).__name__))
        if response.status_code >= 400:
            return classify(response, profile)
    return CheckResult(OK, f"语音识别 ASR {model} 可用")
