"""Vendor profiles —— 供应商知识的单一事实源。

「这家云厂商叫什么、Key 长什么样、走哪条通道、怎么注册拿 Key、
验证失败时怎么说人话」全部住在 app/vendors/<厂商>.py 的 PROFILE 里。
消费方（网页引导层 /api/setup/status、飞书 Key 引导卡、verify_key 的
错误文案、config 的通道路由、README/图解的同步脚本）一律从档案渲染，
不许自己再硬编码一份 —— 改厂商活动/文案只动档案，改完跑
`python setup/sync_guidance.py` 把静态文档一并刷新。

.env 里 `VENDOR_ID=` 选择激活哪家（默认 mimo）。新增厂商 = 新增一个
模块 + 注册进 _REGISTRY。#15 多厂商拆分（2026-10-21 起推进）：档案 v2
开始按模态承载「说明书」（llm/asr/tts 段：协议族/quirks/错误方言），
`llm_selection()` 是 LLM 模态的运行期解析器，供 app/engines/ 分发器
现读现用；配置面 Phase 1 为 .env-only（读法B：不填回落默认厂商）。
"""
import os

from . import mimo, zhipu

_REGISTRY = {
    mimo.PROFILE["id"]: mimo.PROFILE,
    zhipu.PROFILE["id"]: zhipu.PROFILE,
}

# .env 里没有 VENDOR_ID 时（绝大多数部署）走 mimo。
# 自己 load_dotenv 而不是依赖 app.config：sync_guidance 脚本与测试
# 会单独 import 本包，不该被迫先拉起整个应用配置。
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), ".env"), override=False)
except ImportError:
    pass


def active() -> dict:
    """当前生效的供应商档案。未知 VENDOR_ID 一律回落 mimo（fail-closed：
    宁可继续用已知可服务的厂商，也不让空档案把引导层打穿）。"""
    vid = (os.getenv("VENDOR_ID") or "").strip() or "mimo"
    return _REGISTRY.get(vid) or mimo.PROFILE


def _strip_foreign_model(model: str, modality: str, vendor_id: str) -> str:
    """他厂已知模型名残留不跨厂发送（如 .env 里给小米默认配的
    LLM_MODEL=mimo-v2.6-flash 在 LLM_VENDOR=zhipu 时必须忽略——
    2026-10-21 冒烟实测：原样发出去即 1211）。指向任何厂商都不认识的
    全新模型名时原样返回（逃生门：厂商上新模型快过档案更新）。"""
    if not model:
        return ""
    known_elsewhere = any(
        entry.get("id") == model
        for vid_other, prof_other in _REGISTRY.items()
        if vid_other != vendor_id
        for entry in (prof_other.get(modality) or {}).get("models", []))
    return "" if known_elsewhere else model


def _selection_for(modality: str, vendor_env: str) -> dict:
    """llm/tts 共用的模态解析骨架（现读现用，每次调用重建）。

    读法B（#15，2026-10-21 裁定）：<模态>_VENDOR 未填 = 默认厂商（过渡期
    mimo，存量部署零感知）；未知值回落默认厂商（与 active() 同哲学）。
    <模态>_MODEL / <模态>_BASE_URL 是跨厂商逃生门；MIMO_* 旧键只对小米
    生效（键名冻结，见 mimo.py）。
    """
    vid = (os.getenv(vendor_env) or "").strip()
    prof = _REGISTRY.get(vid) if vid else None
    if prof is None:
        prof = active()
    sec = prof.get(modality) or {}
    key = (os.getenv(sec.get("key_env", "")) or "").strip()
    model = _strip_foreign_model((os.getenv(f"{modality.upper()}_MODEL") or "").strip(),
                                 modality, prof["id"])
    if prof["id"] == "mimo":
        model = model or (os.getenv(f"MIMO_{modality.upper()}_MODEL") or "").strip()
    if not model:
        models = sec.get("models") or []
        model = models[0]["id"] if models else ""
    override = (os.getenv(f"{modality.upper()}_BASE_URL") or "").strip()
    if override:
        base = override.rstrip("/")
    elif sec.get("base_url"):
        base = sec["base_url"].rstrip("/")
    else:
        # 无固定地址 = 小米形态：手填 MIMO_BASE_URL 优先，否则 Key 前缀路由
        base = ((os.getenv("MIMO_BASE_URL") or "").strip()
                or base_url_for_key(key))
    return {"vendor_id": prof["id"], "profile": prof, "section": sec,
            "api_key": key, "base_url": base, "model": model}


def llm_selection() -> dict:
    """当前 LLM 模态的解析结果（现读现用，每次调用重建）。"""
    return _selection_for("llm", "LLM_VENDOR")


def tts_selection() -> dict:
    """当前 TTS 模态的解析结果（现读现用，每次调用重建）。"""
    return _selection_for("tts", "TTS_VENDOR")


def asr_selection() -> dict:
    """当前 ASR 模态的解析结果（现读现用，每次调用重建）。"""
    return _selection_for("asr", "ASR_VENDOR")


def channels() -> list:
    """通道表，第一条是默认通道（Key 前缀认不出时也归它）。"""
    return active()["channels"]


def base_url_for_key(key: str) -> str:
    """Key 前缀即通道（如 sk- 按量付费 / tp- Token Plan）。"""
    key = (key or "").strip()
    for ch in channels():
        if key.startswith(ch["prefix"]):
            return ch["base_url"]
    return channels()[0]["base_url"]


def default_base_url() -> str:
    return channels()[0]["base_url"]


def prefix_labels() -> str:
    """「sk-（按量付费）或 tp-（Token Plan）」——给验证文案拼前缀提示用。"""
    chs = channels()
    parts = [f"{ch['prefix']}（{ch['label']}）" for ch in chs]
    if len(parts) > 1:
        return " 或 ".join(parts[:-1]) + " 或 " + parts[-1]
    return parts[0] if parts else ""
