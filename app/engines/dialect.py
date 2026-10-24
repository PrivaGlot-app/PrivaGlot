"""错误方言渲染（TTS 积木共用）：HTTP 状态 + 响应体 → 档案话术。

键形如 "HTTP码:body码"（智谱 429:1113）或 "HTTP码"；话术键值 → 档案
verify 段模板。LLM 侧的 render_error（llm_openai_chat.py）语义相同，
Phase 1 已有测试锁定，留待后续统一到此处。
"""
import json

DEFAULT_MODEL_MISSING = ("对话模型不存在或已下线（404）。"
                         "请把 PrivaGlot 升级到最新版本后再试。")
GENERIC_TTS = ("⚠️ 语音合成失败，本句没能读出来。请稍后再试；若持续失败，"
               "请检查语音模型（网页端在模型配置页，飞书端发「配置」）。")


def error_key(status: int, body_text: str) -> str:
    code = ""
    try:
        code = str((json.loads(body_text).get("error") or {}).get("code") or "")
    except (ValueError, AttributeError):
        pass
    return f"{status}:{code}" if code else str(status)


def render_dialect(sel: dict, status, body_text: str) -> str:
    """把上游错误翻成档案 verify 话术。status 可为 None（连接层/SDK 异常
    拿不到状态码时）——此时走无网络/通用话术。"""
    verify = (sel.get("profile") or {}).get("verify", {})
    errors = (sel.get("section") or {}).get("errors", {})
    if status is None:
        return "⚠️ " + verify.get(
            "no_network", "连不上接口，请检查网络或代理设置。")
    key = error_key(status, body_text)
    canon = errors.get(key) or errors.get(str(status))
    if status == 401:
        return f"⚠️ {verify.get('bad_key', 'API Key 无效（401）。')}"
    if canon == "no_quota":
        return f"⚠️ {verify.get('no_quota', f'账户余额不足（{status}）。')}"
    if canon == "forbidden":
        return f"⚠️ {verify.get('forbidden', '这把 Key 没有调用权限（403）。')}"
    if canon == "model_missing":
        return f"⚠️ {verify.get('model_missing', DEFAULT_MODEL_MISSING)}"
    if canon == "invalid_request":
        return f"⚠️ 语音合成请求被拒绝（{status}）：{body_text[:200]}"
    if status >= 500:
        return "⚠️ " + verify.get("server_error", "服务端错误（{code}）。").format(code=status)
    return GENERIC_TTS
