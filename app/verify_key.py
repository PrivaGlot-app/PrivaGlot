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


def classify(response: httpx.Response) -> CheckResult:
    """把 HTTP 错误翻译成非技术用户能照着做的一句话。话术来自供应商档案
    （app/vendors/mimo.py 的 verify 段），这里只做分发。"""
    v = vendors.active()["verify"]
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


async def post_once(client: httpx.AsyncClient, url: str, key: str, payload: dict):
    """POST 一次。返回 (错误结果 or None, 响应 or None)。"""
    try:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
    except httpx.HTTPError as exc:
        no_network = vendors.active()["verify"]["no_network"]
        return CheckResult(NO_NETWORK, no_network.format(err=type(exc).__name__)), None
    if response.status_code >= 400:
        return classify(response), None
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
