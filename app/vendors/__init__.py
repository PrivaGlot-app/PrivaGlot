"""Vendor profiles —— 供应商知识的单一事实源。

「这家云厂商叫什么、Key 长什么样、走哪条通道、怎么注册拿 Key、
验证失败时怎么说人话」全部住在 app/vendors/<厂商>.py 的 PROFILE 里。
消费方（网页引导层 /api/setup/status、飞书 Key 引导卡、verify_key 的
错误文案、config 的通道路由、README/图解的同步脚本）一律从档案渲染，
不许自己再硬编码一份 —— 改厂商活动/文案只动档案，改完跑
`python setup/sync_guidance.py` 把静态文档一并刷新。

.env 里 `VENDOR_ID=` 选择激活哪家（默认 mimo）。新增厂商 = 新增一个
模块 + 注册进 _REGISTRY。三模态各自拆不同厂商的远期议题见
docs/问题清单.md #15 —— 现在一把 Key 驱动 ASR/LLM/TTS 是档案内的事实，
不是代码假设。
"""
import os

from . import mimo

_REGISTRY = {
    mimo.PROFILE["id"]: mimo.PROFILE,
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
