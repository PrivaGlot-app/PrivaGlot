"""协议积木：openai_chat 族的 LLM 引擎。

覆盖「OpenAI 兼容 chat/completions」形状的全部厂商（小米 MiMo、智谱 GLM、
DeepSeek、通义…）。厂商差异一律来自档案声明（app/vendors/<厂商>.py 的
llm 段），本文件不许出现任何厂商名——加厂商 = 加档案，不改这里。

档案 llm 段契约（缺项按注释里的默认行为）：
  protocol          固定 "openai_chat"
  key_env           该厂商 API Key 的环境变量名
  base_url          固定地址；None = Key 前缀通道路由（小米 sk-/tp- 形态）
  models[]          可选模型，id 严格等于真实调用的模型名；每模型带 quirks：
    thinking          "disabled_param" = 载荷带 thinking:{"type":"disabled"}；
                      "omit" = 绝不发该键（智谱 5.x 强制思考模型，发了 1210）
    max_tokens_param  "max_tokens"（标准）或 "max_completion_tokens"（小米）
    min_max_tokens    max_tokens 下限：思考型模型思考也烧 token，预算低于
                      下限会被思考吃光、content 返回空（实测 5.3 116 token 起）
  transient_retry   瞬时拥塞的窄豁免重试（2026-10-21 用户裁定，仅此一种）：
    codes[]           响应体里的错误码子串（如 "1305" = 免费档容量满）
    max_attempts      总尝试次数上限（1 = 不重试）
    backoff_s         两次尝试的间隔秒数
  errors            错误方言表："HTTP码" 或 "HTTP码:body码" → 话术键
  no_key_hint       未配 Key 时给学生看的一句话
"""
import asyncio
import json

import httpx

from .contracts import LLMPort

# 与旧 llm.py 时代逐字一致的 404 话术（默认小米路径零变化纪律）；
# 档案 verify.model_missing 可按厂商覆盖。
DEFAULT_MODEL_MISSING = ("对话模型不存在或已下线（404）。"
                         "请把 PrivaGlot 升级到最新版本后再试。")
TRANSIENT_EXHAUSTED = ("⚠️ 模型繁忙（{code}），自动重试 {attempts} 次仍失败。"
                       "请稍后再试一次，或换一个可用模型（网页端在模型配置页，"
                       "飞书端发「配置」）。")


def _model_entry(sel: dict) -> dict:
    """sel["model"] 对应的档案条目；LLM_MODEL 逃生门指向表外模型时返回 {}。"""
    for entry in (sel.get("section") or {}).get("models", []):
        if entry.get("id") == sel.get("model"):
            return entry
    return {}


def build_payload(sel: dict, user_message: str, history: list = None,
                  system_prompt: str = None, max_tokens: int = None,
                  temperature: float = 0.5, stream: bool = True) -> dict:
    """请求载荷。厂商/模型差异全部在这一处按档案 quirks 落地。"""
    quirks = _model_entry(sel).get("quirks", {})
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    payload = {
        "model": sel["model"],
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
    }
    if quirks.get("thinking") == "disabled_param":
        payload["thinking"] = {"type": "disabled"}
    # "omit"：绝不发 thinking 键
    cap = max_tokens if max_tokens is not None else 500
    floor = int(quirks.get("min_max_tokens") or 0)
    payload[quirks.get("max_tokens_param", "max_tokens")] = max(cap, floor)
    return payload


def _error_key(status: int, body_text: str) -> str:
    """错误方言键：优先 "HTTP码:body码"（智谱 429:1113 / 429:1305），
    解析不出 body 码则退 "HTTP码"。"""
    code = ""
    try:
        code = str((json.loads(body_text).get("error") or {}).get("code") or "")
    except (ValueError, AttributeError):
        pass
    return f"{status}:{code}" if code else str(status)


def render_error(sel: dict, status: int, body_text: str) -> str:
    """把上游错误翻成档案话术。这是错误唯一的出口——按 2026-10-21 裁定，
    不换厂商、不降级，把原因讲清并指到配置入口（网页端模型配置页 /
    飞书端「配置」卡）。"""
    verify = (sel.get("profile") or {}).get("verify", {})
    errors = (sel.get("section") or {}).get("errors", {})
    key = _error_key(status, body_text)
    canon = errors.get(key) or errors.get(str(status))
    if status == 401:
        return f"⚠️ {verify.get('bad_key', 'API Key 无效（401）。')}"
    if canon == "no_quota":
        return f"⚠️ {verify.get('no_quota', '账户余额不足（{s}）。'.format(s=status))}"
    if canon == "forbidden":
        return f"⚠️ {verify.get('forbidden', '这把 Key 没有调用权限（403）。')}"
    if canon == "model_missing":
        return f"⚠️ {verify.get('model_missing', DEFAULT_MODEL_MISSING)}"
    if canon == "invalid_request":
        return f"⚠️ 请求被拒绝（{status}）：{body_text[:200]}"
    if status >= 500:
        return "⚠️ " + verify.get("server_error", "服务端错误（{code}）。").format(code=status)
    return f"⚠️ LLM API error: {status} {body_text[:200]}"


class _UpstreamError(Exception):
    """带状态码与响应体的上游 HTTP 错误（与连接层错误分流处理）。"""

    def __init__(self, status: int, body: str):
        super().__init__(f"upstream {status}")
        self.status = status
        self.body = body


class OpenAIChatLLM(LLMPort):
    """openai_chat 族引擎。一次调用 = 一份 selection（厂商解析结果）。"""

    def __init__(self, selection: dict, transport: httpx.BaseTransport = None):
        self._sel = selection
        self._transport = transport  # 测试注入 MockTransport 用

    @property
    def api_key(self) -> str:
        return self._sel["api_key"]

    async def generate_stream(
        self,
        user_message: str,
        history: list = None,
        system_prompt: str = None,
        max_tokens: int = None,
        temperature: float = 0.5,
    ):
        sel = self._sel
        if not sel["api_key"]:
            yield sel["section"]["no_key_hint"]
            return

        retry = sel["section"].get("transient_retry") or {}
        transient_codes = tuple(retry.get("codes", []))
        max_attempts = max(1, int(retry.get("max_attempts", 1)))
        backoff = float(retry.get("backoff_s", 3))
        payload = build_payload(sel, user_message, history, system_prompt,
                                max_tokens, temperature, stream=True)
        headers = {"Authorization": f"Bearer {sel['api_key']}",
                   "Content-Type": "application/json"}

        attempt = 0
        while True:
            attempt += 1
            started = False  # 已吐出首个内容块后不再重试（避免重复输出）
            try:
                async with httpx.AsyncClient(timeout=60.0,
                                             transport=self._transport) as client:
                    async with client.stream(
                        "POST",
                        f"{sel['base_url']}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as response:
                        if response.status_code >= 400:
                            body = (await response.aread()).decode("utf-8", "replace")
                            raise _UpstreamError(response.status_code, body)
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                data = line[6:]
                                if data == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data)
                                    if "choices" in chunk and len(chunk["choices"]) > 0:
                                        content = chunk["choices"][0].get("delta", {}).get("content")
                                        if content is not None:
                                            started = True
                                            yield content
                                except json.JSONDecodeError:
                                    continue
                return
            except _UpstreamError as e:
                if (not started and attempt < max_attempts
                        and any(c in e.body for c in transient_codes)):
                    await asyncio.sleep(backoff)
                    continue
                if any(c in e.body for c in transient_codes):
                    yield TRANSIENT_EXHAUSTED.format(
                        code=_error_key(e.status, e.body).split(":")[-1],
                        attempts=max_attempts)
                else:
                    yield render_error(sel, e.status, e.body)
                return
            except httpx.HTTPError as e:
                # 连接层瞬时失败（超时/断连）同样只做窄豁免重试
                if not started and attempt < max_attempts:
                    await asyncio.sleep(backoff)
                    continue
                yield ("⚠️ " + sel["profile"].get("verify", {}).get(
                    "no_network", "连不上接口（{err}）。").format(err=type(e).__name__))
                return

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
