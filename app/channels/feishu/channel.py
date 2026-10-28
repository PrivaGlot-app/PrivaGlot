"""
PrivaGlot Feishu channel — voice-message tutoring over the Feishu app.

Architecture: this channel only translates. Feishu
voice/text messages come in via the SDK's WebSocket long connection (no
public callback URL, no HTTPS, no ICP filing), get turned into kernel
calls, and kernel events come out as chat messages — the teacher's reply
as a text bubble plus ONE voice bar for the whole turn.

Commands are parsed here (channel's job) and become kernel intents; the
student identity mapping (open_id → student profile) is also channel
business. The kernel never learns that Feishu exists.

Besides the lesson loop there is a MANAGEMENT plane, all admin-gated by
FEISHU_ADMIN_OPEN_ID (the QR deployer is enrolled automatically): 状态 /
换Key / 教材管理 (a platform-agnostic wizard in app/textbook_flow.py that
drives course_engine CRUD — no cloud docs, no LLM parsing, per the
2026-09 product consensus). Bot-menu taps (application.bot.menu_v6) and
CARD button taps (card.action.trigger, long-connection EVENT frames) are
translated into the same text commands — buttons are always shortcuts,
never dependencies; the text grammar remains the only contract.
Interactive cards: student picker (首聊引导/「学生」), panel (「面板」),
lesson report, textbook-management menu / wizard forms / skip /
confirmations, and form cards (换Key / 新建学生 — inputs travel in the
card callback, NOT in chat history).

GUI parity is the product law (user decision 2026-09-22, superseding the
old "speak to start class" premise): the web UI starts a lesson ONLY via
its 「开始上课」 button, so does the chat — cold-start voice AND text
never summon the teacher (they guide to the student picker / panel
instead); mid-lesson voice and text are the student's spoken input.
Identity is a per-account "current student" pointer (= the web student
picker's selection): no auto-created profiles, no binding concept —
学生卡 lists every profile to switch to, 「新建学生」 opens the profile
form card, same fields as the web form.
"""
import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path

from ... import config, vendors, version_check
from ...core import events
from ...core.identity import IdentityMap
from ...core.kernel import ConversationKernel
from ...core.session_registry import StudentSessionBusy, registry
from ...logging_config import log as logger
from ...textbook_flow import CourseEngineStore, TextbookFlow
from .client import FeishuAPIError, FeishuClient

# 教材目录走 course_engine（与网页端同一份实现）。挂在模块名下是为了让
# 测试可以像换内核一样换掉它。
from ...course_engine import course_engine

# Feishu voice bars are documented without an explicit hard limit, but
# community testing reports playback problems beyond ~60s; keep chunks at
# 55s with margin.
VOICE_MAX_SECONDS = 55.0

# 「输入中」表情回应的 emoji_type（飞书《表情文案说明》原文拼写，混合
# 大小写）。#23 等待焦虑：学生的语音/课中文字收到即挂上，回复发出后删除。
TYPING_EMOJI = "Typing"

_WELCOME = (
    "你好！我是 PrivaGlot 的 AI 英语外教。先在下面的卡片里选一位学生"
    "（或「➕ 新建学生」建档），然后点「上课」开始课程——网页端也是这样："
    "选学生、点开始，老师才会开口。也可以发文字：「上课」开始课程模式，"
    "「自由对话」随便聊，「下课」结束本次课，「进度」查看学习进度。"
    "发「帮助」看全部指令。")

# 冷启动（没有进行中的课）收到的消息不召唤老师——GUI 对齐（2026-09-22
# 原则裁定，取代旧"开口即上课"）：开课的主动权在学生，面板按钮 /
# 「上课」/「自由对话」才开课。语音和文字在课中同样是口语输入。
_COLD_TEXT_HINT = (
    "现在没有进行中的课。点下面主页卡上的「单元」开始上课，"
    "或「自由对话」闲聊；发「学生」可切换或新建学生。")

_COLD_NO_STUDENT_HINT = (
    "还没有选学生。在下面卡片里选一位（或新建）再开始；"
    "发「帮助」看全部指令。")

_HELP_TEXT = (
    "📖 指令一览\n"
    "· 上课 —— 从上次练的单元继续；发「选课」挑教材单元\n"
    "· 选课 / 上课 序号 / 上课 序号-单元序号 —— 选课卡或指定开课\n"
    "· 自由对话 / 下课 / 进度\n"
    "· 学生 / 切换 学生名 —— 选学生、切换学生（网页端选择器的等价；"
    "「绑定 学生名」同样可用）\n"
    "· 新建学生 姓名,年龄,兴趣 —— 建档；无参数递表单卡\n"
    "· 编辑学生 学生名 —— 改年龄/母语（旧词「改学生」同收；无参数递"
    "选择卡；下次开课生效）\n"
    "· 选老师 / 设老师 老师名 / 设风格 风格 —— 网页端高级设置的等价\n"
    "· 教材管理 —— 建教材 / 加单元 / 改书名 / 改单元 / 删单元 / 删教材；"
    "「提示词 / 粘贴 / 图文说明」—— 拿提取提示词 / 整段贴入 / 看操作图解\n"
    "· 状态 / 配置 / 换Key —— 体检 / 模型配置（选厂商、配 Key、逐项选模型；"
    "发「配置」进配置卡，Key 走卡片提交不进聊天记录）\n"
    "· 版本 / 检查更新 —— 看当前版本；有新版时给一条可整段复制的升级命令\n"
    "· 面板 / 主页 —— 打开选课主页（点按钮=发指令）\n"
    "· 帮助 —— 本列表")

# 项目外部联系渠道（#30 立项 / #33 收敛，2026-10-12 用户裁定）：开发者
# 个人的飞书「添加联系人」分享链接（add_contact 名片页——非组织成员邀请、
# 非 API 凭据，能力上限=打开"向开发者发好友申请"；2026-10-17 差分实测
# 有效，重生成入口在飞书客户端）。#30 时代的纯文本素材与
# 「联系开发者/反馈」文字指令随裁定移除，联系入口唯一化为主页两态按钮
# 直跳（1.0 卡 multi_url：点击客户端直接开链接，不发回调、不依赖服务端
# 在线）——#30 当时「不做 open_url 按钮」的决定就此反转。
_CONTACT_URL = (
    "https://www.feishu.cn/invitation/page/add_contact/"
    "?token=092v8e0a-1c78-4c44-8e0c-d7e2f2890e73&unique_id=JU-1OgFE0TgMlgp1SwZqVQ==")

# 教材提取操作说明图（#37，2026-10-15 用户定稿素材）：随镜像内置
# （Dockerfile COPY app 整目录带走），竖版「拍照 → 提取 → 粘贴」五步
# 图解。发图必先把文件上传飞书换 image_key（原生图片消息与卡片 img
# 均只收 image_key，无外链捷径）；策略=即点即传，见 _send_guide_image。
_GUIDE_IMAGE = (Path(__file__).resolve().parents[2]
                / "assets" / "extract_guide.jpg")

# 教材管理命令态词汇（含别名）。向导进行中收到这些 = 用户想回菜单，不是
# 在回答向导——先自动「取消」再执行（第六轮实测教训：表单卡发送失败后
# 的重试点击曾把「建教材」当成书名、「改书名」当成单元名）。
_MGMT_COMMANDS = ("看教材", "教材列表", "列表", "看单元", "看内容",
                  "建教材", "新建教材", "创建教材", "加单元", "改单元",
                  "改书名", "删单元", "删教材", "帮助", "指令", "help")

# #51：确认下课卡的重复请求防抖（秒）——孩子连点课堂操作面板的「⏹ 下课」
# 不刷屏；toast「已收到」已给即时反馈，防抖窗内静默忽略。
_END_CONFIRM_DEBOUNCE = 5.0


def _button(label: str, command: str, style: str = "default") -> dict:
    """卡片按钮：value.text = 等价的文字指令，点击回调翻译后进同一指令通道。"""
    return {"tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": style,
            "value": {"text": command}}


def _link_button(label: str, url: str, style: str = "default") -> dict:
    """链接型按钮（#33）：点击客户端直接打开 url，不回调——服务端收不到
    点击，按钮在服务端宕机时依旧可用。"""
    return {"tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": style,
            "multi_url": {"url": url}}


def _report_card() -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "接下来？"},
                   "template": "green"},
        "elements": [
            {"tag": "action", "actions": [
                _button("🎓 再来一课", "上课", "primary"),
                _button("📚 选课", "选课"),
                _button("💬 自由对话", "自由对话"),
            ]},
            {"tag": "action", "actions": [
                _button("📈 查看进度", "进度"),
                _button("👥 切换学生", "学生"),
            ]},
            {"tag": "note", "elements": [
                {"tag": "plain_text",
                 "content": "按钮等同于回复文字指令。"}]},
        ],
    }


def _reply_card(file_keys: list[str], transcript: str) -> dict:
    """回复卡（#24，2.0）：语音为主、原文折叠——每段音频一个 audio 组件，
    默认收起的折叠面板装 LLM 全文（2026-10-11 裁定：纠错回合不豁免折叠；
    低版本客户端不写降级逻辑，默认用户是最新版）。
    项目首张非表单 2.0 卡（audio/collapsible_panel 均为 2.0 专属）；
    含 audio 组件的卡片必须 enable_forward=false（平台硬性要求，漏配=
    发送失败）。file_key 来自 upload_opus（卡片 audio 组件仅收 OPUS+
    duration，与原生语音条同一上传管道）。
    body 必须是 {"elements": [...]} 对象——裸数组会被整卡拒收
    ErrCode 200621（failed to unmarshal for CardBody, type:
    []interface{}），2026-10-11 真机抓漏定谳；面板四字段
    （collapsible_panel/expanded/header.title/elements）已经官方
    文档与多源示例核对。
    #51 课堂操作面板：默认收起=视觉零增量；两层点击（展开→点钮）+
    确认卡天然防儿童误触。恒加——回复卡只在 active 会话回合产生，
    无需状态判断。面板内嵌 button 为项目首用形状（官方文档明确
    collapsible_panel 可嵌 button；button 在 2.0 卡内已有表单卡取消钮
    先例），真机最小验证卡前置。"""
    elements = [{"tag": "audio", "file_key": k} for k in file_keys]
    if transcript:
        elements.append({
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "🔊 听不懂？点此查看原文",
                }},
            "elements": [{"tag": "markdown", "content": transcript}],
        })
    elements.append({
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "⏸ 课堂操作",
            }},
        "elements": [_btn20("⏹ 下课", "下课确认", "danger")],
    })
    return {
        "schema": "2.0",
        "config": {"enable_forward": False},
        "body": {"elements": elements},
    }


def _version_card() -> dict:
    """升级完成卡（v1）：升级重启后管理员手里要有一张能点的卡（拓扑不留
    死端）。回滚（新→旧）同样发这张——方向由前面的文字消息交代，卡只给
    出口。仍用 v1：纯按钮卡无 2.0 专属组件，无需升级（非表单 2.0 卡的
    现例只有回复卡 `_reply_card`，audio/折叠面板均为 2.0 专属）。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "✅ 已更新"},
                   "template": "green"},
        "elements": [
            {"tag": "note", "elements": [
                {"tag": "plain_text",
                 "content": "这是升级后机器人的第一次打招呼，课程与数据都在。"}]},
            {"tag": "action", "actions": [
                _button("📚 主页", "主页"),
                _button("🩺 状态", "状态"),
            ]},
        ],
    }


def _live_lesson_card(note: str = "") -> dict:
    """课中控制卡（GUI：课里常驻的 ⏹ 结束课程 / 面板）。"""
    elements = [
        {"tag": "action", "actions": [
            _button("⏹ 下课", "下课", "danger"),
            _button("📈 进度", "进度"),
            _button("👥 切换学生", "学生"),
        ]},
        {"tag": "note", "elements": [
            {"tag": "plain_text",
             "content": note or "课中：发语音或文字 = 口语输入；"
                                "点「下课」结束并生成报告。"}]},
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "🎓 上课中"},
                   "template": "green"},
        "elements": elements,
    }


def _targets_card(vocabulary: list) -> dict:
    """本课目标卡（GUI：🎯 本课目标面板的开课快照）。"""
    words = "、".join((vocabulary or [])[:40]) or "（本课无目标词汇）"
    more = len(vocabulary or []) - 40
    if more > 0:
        words += f" …等 {len(vocabulary)} 个"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "🎯 本课目标"},
                   "template": "turquoise"},
        "elements": [
            {"tag": "div",
             "text": {"tag": "lark_md", "content": words}},
            {"tag": "note", "elements": [
                {"tag": "plain_text",
                 "content": "课中说出来会记入覆盖率；下课报告里看命中情况。"}]},
        ],
    }


_CONFIRM_CARD = {
    "config": {"wide_screen_mode": True},
    "elements": [
        {"tag": "action", "actions": [
            _button("✅ 确认", "确认", "primary"),
            _button("取消", "取消"),
        ]},
        {"tag": "note", "elements": [
            {"tag": "plain_text",
             "content": "按钮等同于回复「确认」/「取消」。"}]},
    ],
}


def _end_confirm_card(stats: dict) -> dict:
    """确认下课卡（#51）：回复卡课堂操作面板「⏹ 下课」的承接。确认按钮 =
    既有「下课」指令（等价指令体系零新语义）。单钮裁定——讨论稿的
    「继续上课」按钮会撞既有「继续上课」指令的结算重开语义（点了把课
    重置），出局；不理这张卡继续聊天 = 课原样继续，零状态边。v1 形状
    （纯按钮卡无 2.0 专属组件，非表单 2.0 卡的现例仍只有回复卡）。"""
    minutes = stats.get("duration_minutes", 0)
    turns = stats.get("total_turns", 0)
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "🏁 确认下课"},
                   "template": "orange"},
        "elements": [
            {"tag": "markdown", "content":
                f"要下课了吗？这节课约 {minutes} 分钟、聊了 {turns} 个回合。"},
            {"tag": "action", "actions": [
                _button("✅ 确认下课", "下课", "danger")]},
            {"tag": "note", "elements": [
                {"tag": "plain_text",
                 "content": "不想下课就不用理这张卡，继续发语音或文字即可；"
                            "点确认后会生成本课报告。"}]},
        ],
    }


# 三模态显示标签（#15④，与网页端配置页同款措辞）
_MODALITY_LABELS = {"asr": "语音识别ASR", "llm": "思考模型LLM",
                    "tts": "语音合成TTS"}

# 冷启动配置向导的固定顺序（N-A6 三步；用户 2026-10-22 拍板的卡片交互）
_CFG_WIZARD_ORDER = ("asr", "llm", "tts")


def _disabled_button(label: str, tips: str) -> dict:
    """禁用态按钮（#15④）：官方字段 disabled + disabled_tips（JSON 1.0
    按钮组件即支持，2026-10-22 官方文档核对；移动端点按灰钮的表现真机
    首击定谳，回落方案=按 Key 有无过滤列表）。不带 value=无回调可发；
    服务端 handler 仍校验厂商 Key（旧客户端忽略禁用态的双保险）。"""
    return {"tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": "default",
            "disabled": True,
            "disabled_tips": {"tag": "plain_text", "content": tips}}


def _vendor_key(vendor_id: str) -> str:
    """该厂商当前已配置的 Key（档案声明的 key_env 现读；无该厂商档案
    或未配置返回空串）。"""
    prof = vendors._REGISTRY.get(vendor_id) or {}
    for mod in ("llm", "tts", "asr"):
        env = (prof.get(mod) or {}).get("key_env")
        if env:
            return (os.environ.get(env) or "").strip()
    return ""


def _vendor_pick_card(mode: str = "cold") -> dict:
    """N-A4 厂商选择卡（#15④）：冷启动向导第一问（用哪家 AI）与 Key 管理
    入口（配哪家的 Key）共用一张卡，title/正文随模式切换。按钮 =
    「选厂商 <id>」，已配 Key 的厂商标 ✓。厂商列表来自注册表（新厂商 =
    档案一份，本卡自动多一枚按钮，零改动）。"""
    if mode == "key":
        title, intro = "🔑 Key 管理——选择厂商", (
            "配置/更换哪家的 API Key？Key 走卡片提交，"
            "**不会留在聊天记录里**（保存前轻探活一次，坏 Key 不落盘）。")
    else:
        title, intro = "🏷 选择 AI 厂商", (
            "这台 PrivaGlot 的模型配置还不完整。先选一家 AI 厂商并填入它的 "
            "API Key，然后逐项挑选语音识别 / 思考模型 / 语音合成的模型——"
            "全程跟着推荐项（⭐）点也可以。")
    rows, row = [], []
    for vid, prof in vendors._REGISTRY.items():
        has = bool(_vendor_key(vid))
        row.append(_button(f"{'✓ ' if has else ''}{prof['display_name']}",
                           f"选厂商 {vid}", "primary" if has else "default"))
        if len(row) == 3:
            rows.append({"tag": "action", "actions": row})
            row = []
    if row:
        rows.append({"tag": "action", "actions": row})
    if mode == "key":
        rows.append({"tag": "action", "actions": [
            _button("↩️ 返回", "配置", "primary")]})
    rows.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": "按钮等同于回复对应文字指令。"}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title},
                   "template": "orange"},
        "elements": [{"tag": "div",
                      "text": {"tag": "lark_md", "content": intro}},
                     *rows],
    }


def _key_form_card(vendor_id: str = "") -> dict:
    """N-A5 Key 表单卡 v2（#15④ 参数化）：按指定厂商档案渲染注册指引
    （单一事实源不变），已有 Key 显掩码、输入留空 = 沿用已保存 Key。
    提交走 __save_key（回调携带厂商）→ 轻探活一次，坏 Key 不落盘。
    旧单厂商卡（N-A1，渲染 active()）退役——它有三缺陷：机器跑智谱却
    渲染小米档案 / 智谱 Key 配不进 / 换 Key 假成功（只写 MIMO_API_KEY）。

    2.0 表单三要素缺一不可——schema:"2.0" + body.elements、input 包在
    form 容器里、提交按钮用 form_action_type:"submit"。v1 写法（顶层
    elements + action_type:"form_submit"）会被整卡拒收 ErrCode 11310
    （2026-09-22 沙箱实测；换Key 卡自 36d6452 起潜伏同一 bug）。
    提交回调仍走 card.action.trigger：form_value 按输入框 name 回流、
    behaviors 的 value 原样带回——翻译层 _extract_card_action 不变。
    """
    v = vendors._REGISTRY.get(vendor_id) if vendor_id else None
    v = v or vendors.active()
    g = v["guide"]
    existing = _vendor_key(v["id"])
    lines = [
        g["intro"],
        "",
        g["preamble"],
        "",
        *(f"{i}. {step}" for i, step in enumerate(g["steps"], 1)),
        "",
        f"**[{g['signup_label']}]({v['signup_url']})**",
        "",
    ]
    if existing:
        lines.append(f"当前已保存：`{config.masked_key(existing)}`"
                     "（下面留空提交 = 沿用这把 Key）")
    else:
        lines.append("拿到 Key 后在下面输入框里粘贴，点「验证并保存」会先"
                     "轻探活一次、通过才保存。")
    lines.append("Key 走卡片提交，**不会留在聊天记录里**。")
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                             "content": f"🔑 {v['display_name']} API Key"},
                   "template": "orange"},
        "body": {"elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            {"tag": "form", "name": "key_form", "elements": [
                {"tag": "input", "name": "api_key",
                 "label": {"tag": "plain_text", "content": "新 Key"},
                 "label_position": "top",
                 "placeholder": {"tag": "plain_text",
                                 "content": g["input_placeholder"]}},
                {"tag": "button", "name": "submit",
                 "text": {"tag": "plain_text", "content": "验证并保存"},
                 "type": "primary", "form_action_type": "submit",
                 "behaviors": [{"type": "callback",
                                "value": {"text": "__save_key",
                                          "vendor": v["id"]}}]},
            ]},
            {"tag": "column_set", "flex_mode": "flow",
             "horizontal_spacing": "8px", "columns": [
                 {"tag": "column", "width": "auto",
                  "elements": [_btn20("取消配置", "取消配置")]},
             ]},
        ]},
    }


def _model_pick_card(modality: str) -> dict:
    """N-A6 模型选择卡（#15④）：该模态全部厂商全部模型一卡单选（用户
    2026-10-22 拍板的交互形态）。无 Key 厂商的模型置灰（disabled 钮，
    服务端 handler 仍校验=双保险）；当前生效 ✓、档案默认 ⭐（「跟随默认」
    的体现：点推荐项即跟随，不设跳过分支）；note（价格/限制）进正文。
    每枚按钮 = 完备指令「选模型 <模态> <模型id>」，选定后强测 3 次全过
    才应用（2026-10-22 用户裁定，对齐 Windows 保存强测）。"""
    icons = {"asr": "🎙", "llm": "🧠", "tts": "🔊"}
    label = _MODALITY_LABELS[modality]
    cur = getattr(vendors, f"{modality}_selection")()
    lines = [f"给 **{label}** 挑一个模型——点按钮即选定，**自动测活 3 次，"
             "全部通过才生效**（有败不动原配置）。"]
    notes, actions, any_disabled = [], [], False
    for vid, prof in vendors._REGISTRY.items():
        sec = prof.get(modality) or {}
        models = sec.get("models") or []
        has = bool(_vendor_key(vid))
        default_id = models[0]["id"] if models else ""
        for m in models:
            is_cur = cur["vendor_id"] == vid and cur["model"] == m["id"]
            mark = "✓ " if is_cur else ("⭐ " if m["id"] == default_id else "")
            name = m.get("label") or m["id"]
            if has:
                actions.append(_button(f"{mark}{prof['short_name']}·{name}",
                                       f"选模型 {modality} {m['id']}",
                                       "primary" if is_cur else "default"))
            else:
                any_disabled = True
                actions.append(_disabled_button(
                    f"{mark}{prof['short_name']}·{name}",
                    f"需先配置 {prof['display_name']} 的 API Key"))
            if m.get("note"):
                notes.append(f"- {prof['short_name']}·{name}：{m['note']}")
    body = "\n".join(lines)
    if notes:
        body += "\n\n" + "\n".join(notes)
    if any_disabled:
        body += ("\n\n灰色模型需先配置对应厂商的 Key：发「配置」→"
                 "「🔑 Key 管理」。")
    rows = []
    for i in range(0, len(actions), 3):
        rows.append({"tag": "action", "actions": actions[i:i + 3]})
    rows.append({"tag": "action",
                 "actions": [_button("取消配置", "取消配置")]})
    rows.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": "按钮等同于回复对应文字指令。"}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                             "content": f"{icons[modality]} {label}——选模型"},
                   "template": "turquoise"},
        "elements": [{"tag": "div",
                      "text": {"tag": "lark_md", "content": body}},
                     *rows],
    }


def _config_hub_card() -> dict:
    """N-A7 配置总卡（#15④）：已配置机器「配置/换Key」的落点——当前三模态
    组合 + 各厂商 Key 掩码，按钮直达单项变更。课中可开（换模型同换Key
    先例：下一回合生效，不下课）；「↩️ 主页」课中由既有「主页」处理递
    课中控制卡，无语义错位。"""
    combo = ["**当前模型组合**"]
    for mod in ("asr", "llm", "tts"):
        sel = getattr(vendors, f"{mod}_selection")()
        ready = bool((sel.get("api_key") or "").strip())
        combo.append(f"- **{_MODALITY_LABELS[mod]}**："
                     f"{sel['profile']['display_name']} · {sel['model']}"
                     + ("" if ready else " ⚠️ Key 未配置"))
    keys = []
    for vid, prof in vendors._REGISTRY.items():
        k = _vendor_key(vid)
        keys.append(f"- {prof['display_name']} Key："
                    + (f"`{config.masked_key(k)}`" if k else "⚠️ 未配置"))
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "⚙️ 模型配置"},
                   "template": "orange"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": "\n".join(combo + [""] + keys)}},
            {"tag": "action", "actions": [
                _button("🎙 换语音识别", "选模型 asr"),
                _button("🧠 换思考模型", "选模型 llm"),
                _button("🔊 换语音合成", "选模型 tts"),
            ]},
            {"tag": "action", "actions": [
                _button("🔑 Key 管理", "Key管理"),
                _button("🩺 状态", "状态"),
                _button("↩️ 主页", "主页"),
            ]},
            {"tag": "note", "elements": [
                {"tag": "plain_text",
                 "content": "换模型自动测活 3 次、全过才生效；发「配置」随时"
                            "回到这张卡。按钮等同于回复对应文字指令。"}]},
        ],
    }


def _mgmt_menu_card(book_lines: list[str]) -> dict:
    """教材管理主菜单卡：书单进正文，按钮=等价文字指令。进入管理模式
    只发这一张卡——横幅/指令帮助不再作为独立消息（打字「帮助」随时
    可要，文字协议保留）。仍用 v1：纯按钮卡无 2.0 专属组件（非表单
    2.0 卡的现例只有回复卡 `_reply_card`）。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "📖 教材管理"},
                   "template": "purple"},
        "elements": [
            {"tag": "div",
             "text": {"tag": "lark_md", "content": "\n\n".join(book_lines)}},
            {"tag": "action", "actions": [
                _button("📖 建教材", "建教材", "primary"),
                _button("➕ 加单元", "加单元"),
                _button("✏️ 改单元", "改单元"),
            ]},
            {"tag": "action", "actions": [
                _button("✏️ 改书名", "改书名"),
                _button("🗑 删单元", "删单元"),
                _button("🗑 删教材", "删教材"),
            ]},
            {"tag": "action", "actions": [
                _button("📋 看教材", "看教材"),
                _button("📝 提取提示词", "提示词"),
                _button("↩️ 退出管理", "退出"),
            ]},
            {"tag": "note", "elements": [
                {"tag": "plain_text",
                 "content": "输入不影响学生上课。带序号的操作点上面按钮会递上"
                            "选择卡（书/单元点选即发完整指令）；向导内容步骤可发"
                            "「粘贴」整段贴入 AI 整理好的回复；发「帮助」看全部指令。"
                            "每个按钮等同回复对应文字指令。"}]},
    ],
    }


_SKIP_CARD = {
    "config": {"wide_screen_mode": True},
    "elements": [
        {"tag": "action", "actions": [
            _button("⏭ 跳过", "跳过", "primary"),
            _button("取消", "取消"),
        ]},
        {"tag": "note", "elements": [
            {"tag": "plain_text",
             "content": "按钮等同于回复「跳过」/「取消」。"}]},
    ],
}


_PASTE_CONFIRM_CARD = {
    "config": {"wide_screen_mode": True},
    "header": {"title": {"tag": "plain_text",
                         "content": "✅ 解析完成——采用这份内容？"},
               "template": "green"},
    "elements": [
        {"tag": "action", "actions": [
            _button("✅ 确认采用", "确认", "primary"),
            _button("🔄 重贴", "粘贴"),
            _button("取消", "取消"),
        ]},
        {"tag": "note", "elements": [
            {"tag": "plain_text",
             "content": "重贴 = 再粘贴一份整段回复重新解析。"
                        "按钮等同于回复文字指令。"}]},
    ],
}


def _markdown_card(title: str, content: str, template: str = "purple") -> dict:
    """通用信息展示卡（#22 三轮 P3）：纯文本展示升级为卡片正文，
    交互语义不变。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title},
                   "template": template},
        "elements": [{"tag": "div",
                      "text": {"tag": "lark_md", "content": content}}],
    }


# 裸序号指令（不带参数）→ 选择器卡（#22 三轮 P2）：点按钮 = 一条完整
# 指令，消灭「用法：X 书序号 单元序号」文本。目录为空或按钮超过阈值时
# 返回 None 回落原文字流——文字协议是天然兜底。
_SELECTOR_COMMANDS = {"改单元", "删单元", "看单元", "看内容",
                      "加单元", "改书名", "删教材"}
_UNIT_LEVEL_COMMANDS = {"改单元", "删单元", "看内容"}
_SELECTOR_MAX_BUTTONS = 30


def _selector_card(cmd: str, title: str | None = None) -> dict | None:
    """裸序号指令的扁平选择器卡：所有书的单元平铺成一枚枚按钮，每枚
    按钮 = 一条完整指令（如「改单元 1 2」，空格分隔）。只读构建，不碰
    向导状态机；带序号的完整指令照旧直达向导。title 可覆盖卡头（#25：
    「看教材」复用书级选择器，按钮=「看单元 N」）。"""
    books = []
    for idx, tb_id in enumerate(course_engine.get_textbook_names(), 1):
        books.append((idx, course_engine.book_title(tb_id) or tb_id,
                      course_engine.list_units_summary(tb_id)))
    if not books:
        return None
    unit_level = cmd in _UNIT_LEVEL_COMMANDS
    if unit_level:
        total = sum(len(units) for _, _, units in books)
        if total == 0 or total > _SELECTOR_MAX_BUTTONS:
            return None
    elif len(books) > _SELECTOR_MAX_BUTTONS:
        return None
    buttons = []
    for book_idx, btitle, units in books:
        if unit_level:
            for u_idx, u in enumerate(units, 1):
                label = u.get("title") or u.get("unit_id") or f"单元{u_idx}"
                # 指令语法=空格分隔（改单元 书序号 单元序号），不是连字符
                buttons.append(_button(f"{btitle} · {label}",
                                       f"{cmd} {book_idx} {u_idx}"))
        else:
            buttons.append(_button(f"{btitle}（{len(units)} 个单元）",
                                   f"{cmd} {book_idx}"))
    elements = [{"tag": "action", "actions": buttons[i:i + 3]}
                for i in range(0, len(buttons), 3)]
    elements.append({"tag": "note", "elements": [
        {"tag": "plain_text",
         "content": "点单元 = 发出对应完整指令；也可直接打字"
                    "（如「" + cmd + " 1 2」）。"}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                             "content": title or f"{cmd}——选一个"},
                   "template": "purple"},
        "elements": elements,
    }


def _unit_selector_card(book_idx: int, book_title: str,
                        units: list[dict]) -> dict | None:
    """书内单元选择器卡（#41 三层链）：「看单元 N」从文字计数清单升级为
    卡片——点单元 = 发出「看内容 书序号 单元序号」，直达「📄 单元内容」
    全文展示卡，看教材不再止步于「N 词、M 句式」。空书或按钮超过阈值时
    返回 None，回落原文字流。只读构建，不碰向导状态机。"""
    if not units or len(units) > _SELECTOR_MAX_BUTTONS:
        return None
    buttons = []
    for u_idx, u in enumerate(units, 1):
        label = u.get("title") or u.get("unit_id") or f"单元{u_idx}"
        # 序号空间与书选择器卡同源（get_textbook_names），点出来的指令
        # 由既有「看内容 N M」拦截渲染
        buttons.append(_button(label, f"看内容 {book_idx} {u_idx}"))
    elements = [{"tag": "action", "actions": buttons[i:i + 3]}
                for i in range(0, len(buttons), 3)]
    elements.append({"tag": "note", "elements": [
        {"tag": "plain_text",
         "content": "点单元看完整内容（词汇/句式/语法全文）。"}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                             "content": f"📖 {book_title}——选一个单元"},
                   "template": "purple"},
        "elements": elements,
    }


def _edit_field_card(label: str) -> dict:
    """改单元的字段选择卡：点按钮=回复「标题/词汇/句式/语法」。
    覆盖式语义（发完整新内容替换旧内容）由后续步骤的文字提示交代。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "✏️ 改单元"},
                   "template": "purple"},
        "elements": [
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content": label or "改哪一项？"}]},
            {"tag": "action", "actions": [
                _button("🏷 标题", "标题"),
                _button("🔤 词汇", "词汇", "primary"),
                _button("💬 句式", "句式"),
            ]},
            {"tag": "action", "actions": [
                _button("📚 语法", "语法"),
                _button("取消", "取消"),
            ]},
            {"tag": "note", "elements": [
                {"tag": "plain_text",
                 "content": "选好后机器人会先给你看当前内容，新内容是"
                            "整段覆盖（要保留的也要包含在内）。"}]},
        ],
    }


def _btn20(label: str, command: str, style: str = "default") -> dict:
    """2.0 回传按钮：behaviors 的 value.text 即等价文字指令，回调与 v1
    按钮同形（翻译层共用 _extract_card_action）。"""
    return {"tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": style,
            "behaviors": [{"type": "callback", "value": {"text": command}}]}


def _flow_form_card(label: str, placeholder: str,
                    extra_buttons: list[dict] | None = None,
                    marker: str = "__flow_submit",
                    note: str = "提交等同打字回复；多行内容（词汇/句式）请直接打字。") -> dict:
    """向导单值输入步骤的表单卡（2.0 结构，三要素同 _key_form_card）。
    提交内容经卡片回调拼成等价文字进同一条指令通道：默认 __flow_submit
    （= 打出来的那行字）；一键粘贴卡传 marker="__paste_submit"（等价文字
    = 「粘贴 + 整段内容」原子指令，见 _extract_card_action）。
    取消/完成等普通按钮放 form 外的 column_set 行，behaviors 回调即可。"""
    form_elements = [
        {"tag": "input", "name": "text",
         "label": {"tag": "plain_text", "content": label},
         "label_position": "top",
         "placeholder": {"tag": "plain_text", "content": placeholder}},
        {"tag": "button", "name": "submit",
         "text": {"tag": "plain_text", "content": "提交"},
         "type": "primary", "form_action_type": "submit",
         "behaviors": [{"type": "callback",
                        "value": {"text": marker}}]},
    ]
    columns = [{"tag": "column", "width": "auto", "elements": [b]}
               for b in (extra_buttons or []) + [_btn20("取消", "取消")]]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": [
            {"tag": "form", "name": "flow_form", "elements": form_elements},
            {"tag": "column_set", "flex_mode": "flow",
             "horizontal_spacing": "8px", "columns": columns},
            {"tag": "markdown",
             "content": note},
        ]},
    }


def _student_form_card(profile=None) -> dict:
    """建档/编辑学生表单卡（2.0，#55 二态参数化）：profile=None=新建态
    （字段对齐网页端——姓名必填，年龄/母语/兴趣选填）；传档案=编辑态
    （title「✏️ 编辑学生」、**无姓名字段**——身份走 marker
    `__student_edit_submit:名`；姓名与兴趣不可编辑，#55 裁定）。两态
    表单形态＝2026-10-26 真机定稿：母语 select 上方 markdown 题注
    （select_static 官方无 label；新建态补题注＝编辑态上线后真机验收
    发现的同款旧问题，随下版）、initial_option 预选当前值（下拉对勾
    已标注当前值，不加「（当前）」后缀）、placeholder 兼作选择
    面板标题。提交经卡片回调拼成等价文字进指令通道（新建
    __student_submit / 编辑 __student_edit_submit:名）。"""
    from ...student import LANGUAGE_BACKGROUNDS
    if profile is not None:
        si = profile.data.get("student_info", {})
        current_lang = si.get("language_background")
        if current_lang not in LANGUAGE_BACKGROUNDS:
            current_lang = "中文"
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": "✏️ 编辑学生"},
                       "template": "blue"},
            "body": {"elements": [
                {"tag": "markdown",
                 "content": f"正在编辑「{profile.student_name}」：留空年龄"
                            "＝保持原值；母语已预选当前值。提交后**下次开课"
                            "生效**（课中改动不进当前会话）。"},
                {"tag": "form", "name": "student_form", "elements": [
                    {"tag": "input", "name": "age",
                     "label": {"tag": "plain_text",
                               "content": "年龄（留空保持原值）"},
                     "label_position": "top",
                     "placeholder": {"tag": "plain_text",
                                     "content": f"当前：{profile.age}"}},
                    {"tag": "markdown", "content": "**母语**"},
                    {"tag": "select_static", "name": "language_background",
                     "placeholder": {"tag": "plain_text", "content": "母语"},
                     "initial_option": current_lang,
                     "options": [
                         {"text": {"tag": "plain_text", "content": v},
                          "value": v}
                         for v in LANGUAGE_BACKGROUNDS
                     ]},
                    {"tag": "button", "name": "submit",
                     "text": {"tag": "plain_text", "content": "💾 保存修改"},
                     "type": "primary", "form_action_type": "submit",
                     "behaviors": [{"type": "callback",
                                    "value": {"text":
                                              f"__student_edit_submit:"
                                              f"{profile.student_name}"}}]},
                ]},
            ]},
        }
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                             "content": "🧑‍🎓 新建学生"},
                   "template": "blue"},
        "body": {"elements": [
            {"tag": "markdown",
             "content": "网页端「新建学生」表单的聊天等价：姓名必填，"
                        "其余选填（留空用默认）。提交后自动设为"
                        "当前学生；建好后可在学生卡「✏️ 编辑学生」里修改。"},
            {"tag": "form", "name": "student_form", "elements": [
                {"tag": "input", "name": "name",
                 "label": {"tag": "plain_text", "content": "姓名（必填）"},
                 "label_position": "top",
                 "placeholder": {"tag": "plain_text", "content": "如：周一鸣"}},
                {"tag": "input", "name": "age",
                 "label": {"tag": "plain_text", "content": "年龄（选填）"},
                 "label_position": "top",
                 "placeholder": {"tag": "plain_text", "content": "如：9"}},
                {"tag": "markdown", "content": "**母语**"},
                {"tag": "select_static", "name": "language_background",
                 "placeholder": {"tag": "plain_text", "content": "母语 / 语言背景"},
                 "initial_option": "中文",
                 "options": [
                     {"text": {"tag": "plain_text", "content": "中文"},
                      "value": "中文"},
                     {"text": {"tag": "plain_text", "content": "海外华人家庭"},
                      "value": "海外华人家庭"},
                 ]},
                {"tag": "input", "name": "interests",
                 "label": {"tag": "plain_text",
                           "content": "兴趣（选填，顿号分隔）"},
                 "label_position": "top",
                 "placeholder": {"tag": "plain_text",
                                 "content": "如：dogs、画画"}},
                {"tag": "button", "name": "submit",
                 "text": {"tag": "plain_text", "content": "创建学生"},
                 "type": "primary", "form_action_type": "submit",
                 "behaviors": [{"type": "callback",
                                "value": {"text": "__student_submit"}}]},
            ]},
        ]},
    }


def _student_edit_pick_card():
    """编辑学生选择卡（N-A2b，#55）：每生一钮＝payload 完备的
    「编辑学生 名」。>9 档案同学生卡容量兜底（note 教打字）；无档案返回
    None（调用方回落人话引导建档）。"""
    from ...student import student_store
    students = student_store.list_students()
    if not students:
        return None
    rows = []
    for i in range(0, min(len(students), 9), 3):
        rows.append({"tag": "action", "actions": [
            _button(f"✏️ {s['name']}", f"编辑学生 {s['name']}")
            for s in students[i:i + 3]]})
    note = "点学生名 = 改这个学生的年龄/母语（下次开课生效）；" \
           "超过 9 位时发「编辑学生 学生名」。"
    rows.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": note}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                             "content": "✏️ 编辑学生——选一位"},
                   "template": "blue"},
        "elements": rows,
    }


class _UserSession:
    """One Feishu user's live kernel + serialization lock."""

    def __init__(self):
        self.kernel: ConversationKernel | None = None
        self.student_id: str = ""
        self.lock = asyncio.Lock()
        self.last_active = 0.0
        # 教材管理模式（仅管理员；挂在会话上，退出/下课即销毁）
        self.mgmt: TextbookFlow | None = None


class FeishuChannel:
    def __init__(self, client: FeishuClient = None):
        self.client = client or FeishuClient()
        self._sessions: dict[str, _UserSession] = {}
        # 已发过完整欢迎语的账号（进程内即可：重启后再迎一次无妨）
        self._greeted: set[str] = set()
        # open_id → student_id 的绑定存储（v4 抽到 core/identity.py，
        # 文件格式与旧版 data/feishu_bindings.json 完全一致，无缝沿用）。
        self._identities = IdentityMap(config.FEISHU_BINDINGS_PATH)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        # 开机推卡每进程只成功推一次（重连信号不重推）
        self._startup_card_sent = False
        # 配置向导轻会话态（#15④）：open_id → 下一个待选模态。只决定「选完
        # 之后发下一张还是回总卡」；每枚按钮都是完备指令可独立执行，陈旧卡
        # 乱序点按天然安全（缺省视为无向导，一切落配置总卡）。
        self._cfg_wizard: dict[str, str] = {}
        # 确认下课卡的最近请求时刻（#51 防抖）：open_id → time.time()
        self._end_confirm_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # identity: open_id → current student pointer（GUI 对齐：等同浏览器里
    # 当前选中的学生。不自动建档、没有绑定概念——建档走「新建学生」，
    # 切换走学生卡。存储格式与旧 data/feishu_bindings.json 完全一致。）
    # ------------------------------------------------------------------

    def current_student(self, open_id: str) -> str | None:
        """当前学生的 student_id；None = 还没选学生（首聊/从未选择）。"""
        return self._identities.get(open_id)

    # ------------------------------------------------------------------
    # SDK bridge: the lark ws client runs its own loop in a worker thread;
    # events hop into this loop's queue.
    # ------------------------------------------------------------------

    def start(self):
        """Blocking. Builds the SDK long connection and serves the channel."""
        if not self.client.configured():
            raise SystemExit(
                "FEISHU_APP_ID / FEISHU_APP_SECRET 未配置（.env），无法启动飞书通道。"
                "申请步骤见 docs/飞书接入.md")
        self._loop = asyncio.new_event_loop()
        self._queue = asyncio.Queue()
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._worker())

        import threading
        thread = threading.Thread(target=self._run_sdk, daemon=True)
        thread.start()
        logger.info("[FEISHU] channel started (long connection, no public port)")
        # 开机推卡不在这里发：REST 发送毫秒级可达，但卡片按钮的回调走
        # WebSocket——连接没就绪就推卡，用户点按钮就是"当前回调无服务"
        # 的死区（2026-09-23 第十二轮实测，2 分钟无人接收）。_run_sdk 在
        # 长连接首次就绪时投递 __ws_ready__，由 _worker 调 _push_startup_card。
        self._loop.run_forever()

    async def _push_startup_card(self):
        """开机主动推卡——长连接就绪（__ws_ready__）之后才发，卡只在能点
        的时候到。推给部署者：扫码部署时已拿到其 open_id，平台允许直接
        推送；其他使用者的 open_id 在首条消息前不可知，仍需先发消息触发
        ——平台硬约束，不是权限。仅当部署者还没有当前学生时推一次
        （成功后 _startup_card_sent 置位：重连信号不重推、重启不刷屏
        ——查 bindings 文件即可）。没配 Key 时推 Key 引导卡。失败只记
        日志，下次重连拿到新就绪信号时自然重试。"""
        admin = config.FEISHU_ADMIN_OPEN_ID
        if (not admin or self._startup_card_sent
                or self.current_student(admin)):
            return
        try:
            await self._send_text(admin, _WELCOME)
            if config.modalities_ready():
                await self._send_card(admin, self._student_pick_card(None))
            else:
                # 配置不齐（#15④）：推厂商选择卡（冷启动向导起点）
                self._cfg_wizard[admin] = "asr"
                await self._send_card(admin, _vendor_pick_card("cold"))
        except Exception as e:
            # 个人租户不可达（错误码 230101，2026-10-05 VPS 实验定谳）：应用
            # 能创建但消息进不了个人租户。部署期的探测/指引是主拦截点，用户
            # 看不见容器日志——这里只给能翻到日志的人一句人话。
            if "Sending messages to users is temporarily unavailable" in str(e):
                logger.error(
                    "[FEISHU] 应用落在个人租户：手机飞书「左上角头像 → 登录更多"
                    "账号 → 创建企业账号」（免费1分钟）后重新部署授权即可。")
            logger.error(f"[FEISHU] startup card push failed: {e}",
                         exc_info=True)
            return
        self._startup_card_sent = True
        self._greeted.add(admin)
        logger.info("[FEISHU] startup card pushed to admin (ws ready)")

    async def _notify_version_change(self):
        """升级完成通知（#13 Tier 2）：容器重建后的首次启动，对比 data/
        版本标记（APP_VERSION_MARKER）——有变化就推一张卡，把「升级了没」
        的悬念在聊天里闭环（用户看不见 docker inspect 与容器日志）。与开机
        欢迎卡相互独立：不受 _startup_card_sent / 无学生闸门影响——版本
        变更每版最多触发一次，不构成刷屏；回滚（新→旧）同样如实告知。
        标记只在发送成功后落盘，失败留给下一个就绪信号自然重试（与开机卡
        同一失败哲学）；首次部署（无旧标记）不推——首启引导是欢迎卡的
        职责，此时没有「变化」可言。"""
        cur = config.APP_VERSION
        if cur == "dev":
            return
        marker = config.APP_VERSION_MARKER
        try:
            last = marker.read_text(encoding="utf-8").strip() \
                if marker.exists() else ""
        except Exception:
            last = ""
        if last == cur:
            return
        admin = config.FEISHU_ADMIN_OPEN_ID
        if last and admin:
            try:
                await self._send_text(
                    admin, f"🔄 PrivaGlot 版本变更：{last} → {cur}")
                await self._send_card(admin, _version_card())
            except Exception as e:
                logger.error(f"[FEISHU] version-change notice failed: {e}")
                return  # 不落盘：重连后的就绪信号会重试
        try:
            marker.write_text(cur, encoding="utf-8")
        except Exception as e:
            logger.warning(f"[FEISHU] version marker write failed: {e}")

    def _run_sdk(self):
        import lark_oapi as lark
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self._on_message_event)
                   .register_p2_application_bot_menu_v6(self._on_menu_event)
                   .register_p2_card_action_trigger(self._on_card_action)
                   .build())

        channel = self

        class _ReadyClient(lark.ws.Client):
            """唯一挂点：连接成功（含重连）后投递就绪信号。SDK 的
            on_reconnected 只在"断线重连"时触发、首次连接不触发，所以
            覆写 _connect；推卡的一次性由 _startup_card_sent 把守，
            重连信号不会重推。"""

            async def _connect(self):
                await super()._connect()
                if (self._conn is not None and channel._loop is not None
                        and channel._queue is not None):
                    channel._loop.call_soon_threadsafe(
                        channel._queue.put_nowait,
                        {"message_type": "__ws_ready__"})

        ws_client = _ReadyClient(
            self.client.app_id, self.client.app_secret,
            event_handler=handler, log_level=lark.LogLevel.INFO)
        ws_client.start()  # blocks its own thread forever

    def _on_message_event(self, data):
        """SDK thread → asyncio loop."""
        try:
            item = self._extract_event(data)
            if item is not None:
                if item["message_type"] == "audio":
                    # 语音收到即挂「输入中」（#23）：覆盖排队期——其他用户
                    # 的回合正在处理时，反应也已经出现在这条语音上。
                    self._spawn_typing_react(item)
                self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except Exception as e:
            logger.error(f"[FEISHU] event parse failed: {e}")

    def _on_menu_event(self, data):
        """机器人菜单点击（application.bot.menu_v6）→ 当成文字指令处理。

        菜单在这里只是快捷方式：event_key 直接当作用户发出的文字进同一条
        指令通道。应用没有订阅此事件/没配菜单时这里永远不会触发，所有
        功能照常通过文字指令使用 —— 菜单不是任何功能的依赖。
        """
        try:
            item = self._extract_menu_event(data)
            if item is not None:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except Exception as e:
            logger.error(f"[FEISHU] menu event parse failed: {e}")

    def _on_card_action(self, data):
        """卡片按钮/表单回调（card.action.trigger）→ 翻译成等价文字指令。

        飞书要求长连接回调 3 秒内返回，所以这里只做解析 + 入队，真正的
        指令在 asyncio 循环里照常执行；回一个 toast 作为即时反馈。
        官方文档的长连接卡片回调正是这种 event_handler 用法。
        """
        item = None
        try:
            item = self._extract_card_action(data)
        except Exception as e:
            logger.error(f"[FEISHU] card action parse failed: {e}")
        if item is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        return self._card_toast("success" if item is not None else "error",
                                "已收到" if item is not None else "未识别的按钮")

    @staticmethod
    def _extract_card_action(data) -> dict | None:
        """P2CardActionTrigger → 与菜单点击同形的派发项。

        普通按钮：action.value.text 即等价指令；Key 表单卡 v2（__save_key，
        #15④）：form_value.api_key + 回调携带的厂商拼成「存Key 厂商 Key」
        （不作广告的内部协议）；历史旧卡（__submit_key）仍拼「换Key <key>」
        ——落退役路径；向导表单卡（__flow_submit）：form_value.text 就是
        那条「打出来的文字」；一键粘贴卡（__paste_submit）：等价文字 =
        「粘贴 + 整段内容」同条发送的原子指令（向导在内容步骤一次完成
        进粘贴态+解析）；建档表单卡（__student_submit）：拼成
        「新建学生 姓名,年龄,兴趣」。双下划线开头的私有 value（如
        __submit_key）是按钮标记，不进指令通道。
        """
        ev = getattr(data, "event", None)
        op = getattr(ev, "operator", None)
        open_id = (getattr(op, "open_id", "") or "") if op else ""
        action = getattr(ev, "action", None)
        if not open_id or action is None:
            return None
        form = getattr(action, "form_value", None) or {}
        value = getattr(action, "value", None) or {}
        marker = str(value.get("text") or "").strip()
        key = str(form.get("api_key") or "").strip()
        if marker == "__save_key":
            # N-A5 Key 表单卡 v2（#15④）：厂商在回调 value 里，空 Key 也
            # 照发（=沿用已保存 Key 的提交形态），由 handler 分辨
            vendor = str(value.get("vendor") or "").strip()
            text = f"存Key {vendor} {key}".strip()
        elif key:
            text = f"换Key {key}"  # 历史旧卡（N-A1）→ 「换Key <Key>」退役路径
        else:
            text = marker
            if text == "__flow_submit":
                text = str(form.get("text") or "").strip()
            elif text == "__paste_submit":
                body = str(form.get("text") or "").strip()
                text = f"粘贴\n{body}" if body else "粘贴"
            elif text == "__student_submit":
                # 建档表单卡：拼回「新建学生 姓名,年龄,语言背景,兴趣」
                # 后三项均可缺省；语言背景缺省时由建档路径回落「中文」。
                name = str(form.get("name") or "").strip()
                age = str(form.get("age") or "").strip()
                lang = str(form.get("language_background") or "").strip()
                interests = str(form.get("interests") or "").strip()
                # 逗号分隔的纯文字协议无法安全夹带含逗号的兴趣，语言背景
                # 用「语言:xxx」段传递，避免与兴趣列表歧义。
                parts = [name]
                if age:
                    parts.append(age)
                if interests:
                    parts.append(interests)
                if lang:
                    parts.append(f"语言:{lang}")
                text = "新建学生 " + ",".join(p for p in parts if p)
            elif text.startswith("__student_edit_submit:"):
                # 编辑表单卡（#55）：表单无姓名字段，目标名走 marker；
                # 年龄留空=沿用不拼段，母语 select 恒显式提交。
                name = text[len("__student_edit_submit:"):].strip()
                age = str(form.get("age") or "").strip()
                lang = str(form.get("language_background") or "").strip()
                parts = [name]
                if age:
                    parts.append(f"年龄:{age}")
                if lang:
                    parts.append(f"语言:{lang}")
                text = "编辑学生 " + " ".join(p for p in parts if p)
        if not text or text.startswith("__"):
            return None
        return {"open_id": open_id, "message_id": "",
                "message_type": "menu", "content": text}

    @staticmethod
    def _card_toast(toast_type: str, content: str):
        """卡片回调的即时反馈（可选：SDK 太老时静默降级为无响应）。"""
        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                CallBackToast,
                P2CardActionTriggerResponse,
            )
            resp = P2CardActionTriggerResponse()
            toast = CallBackToast()
            toast.type = toast_type
            toast.content = content
            resp.toast = toast
            return resp
        except Exception:
            return None

    @staticmethod
    def _extract_menu_event(data) -> dict | None:
        ev = getattr(data, "event", None)
        op = getattr(ev, "operator", None)
        open_id = (getattr(op, "operator_id", "") or "") if op else ""
        op_type = (getattr(op, "operator_type", "") or "").lower()
        key = (getattr(ev, "event_key", "") or "").strip()
        # operator_type 缺省按 open_id 处理（长连接 p2p 场景的常态）
        if not open_id or not key or op_type not in ("", "open_id"):
            return None
        return {"open_id": open_id, "message_id": "",
                "message_type": "menu", "content": key}

    @staticmethod
    def _extract_event(data) -> dict | None:
        """P2ImMessageReceiveV1 → dispatch item. None = not for us (group
        chats are out of v1 scope; sender or message shape unexpected)."""
        msg = data.event.message
        sender = data.event.sender
        open_id = (sender.sender_id.open_id if sender and sender.sender_id
                   else "")
        if not open_id or msg.chat_type != "p2p":
            return None
        return {
            "open_id": open_id,
            "message_id": msg.message_id,
            "message_type": msg.message_type,
            "content": msg.content or "",
        }

    async def _worker(self):
        while True:
            item = await self._queue.get()
            try:
                if item.get("message_type") == "__ws_ready__":
                    # 长连接就绪：现在才推开机卡，按钮点击才有人接收
                    await self._push_startup_card()
                    await self._notify_version_change()
                    continue
                await self._dispatch(item)
            except Exception as e:
                await self._handle_dispatch_error(item, e)

    async def _handle_dispatch_error(self, item: dict, e: Exception):
        """dispatch 兜底：带堆栈记日志（"0" 这类单行异常没有堆栈无法定位，
        2026-09-22 第八轮教训），并递上主页卡——任何错误后用户手里都要有
        可点的卡，拓扑不留死端；文字附「联系开发者」反馈出口（#30）。"""
        logger.error(f"[FEISHU] dispatch failed: {e}", exc_info=True)
        open_id = item.get("open_id", "")
        self._try_send_text(
            open_id,
            "⚠️ 服务内部错误，请稍后再试。仍未解决？点下面主页卡上的"
            "「📞 联系开发者」反馈给开发者。")
        try:
            await self._send_card(open_id,
                                  self._home_card(open_id))
        except Exception as e2:
            logger.error(f"[FEISHU] recovery card failed: {e2}")

    # ------------------------------------------------------------------
    # typing reaction（#23 等待焦虑：回合处理期间给学生的消息挂「输入中」，
    # 回复发出后删除；全程尽力而为，任何失败都不阻塞主管线）
    # ------------------------------------------------------------------

    def _spawn_typing_react(self, item: dict):
        """挂「输入中」。已在事件循环线程则直接起任务（回合兜底补挂走这
        条），SDK 回调线程则桥接到通道 loop（任务句柄写回 item，call_soon
        顺序保证写回先于 worker 取件）。"""
        if not item.get("message_id") or "typing_task" in item:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            self._typing_react(item)
        elif self._loop is not None:
            self._loop.call_soon_threadsafe(self._typing_react, item)

    def _typing_react(self, item: dict):
        async def _run():
            try:
                return await asyncio.to_thread(
                    self.client.create_reaction,
                    item.get("message_id") or "", TYPING_EMOJI)
            except Exception as e:
                logger.info(f"[FEISHU] typing reaction skipped: {e}")
                return ""
        item["typing_task"] = asyncio.create_task(_run())

    async def _typing_cleanup(self, item: dict):
        """回合终态（回复已发出或出错）：删掉挂上的「输入中」。等 create
        任务落地拿 reaction_id；删除失败同样静默（最坏=表情残留一条）。"""
        task = item.pop("typing_task", None)
        if task is None:
            return
        try:
            rid = await task
            if rid:
                await asyncio.to_thread(
                    self.client.delete_reaction,
                    item.get("message_id") or "", rid)
        except Exception as e:
            logger.info(f"[FEISHU] typing reaction cleanup skipped: {e}")

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, item: dict):
        open_id = item["open_id"]
        if not open_id:
            return
        sess = self._sessions.setdefault(open_id, _UserSession())
        async with sess.lock:
            try:
                if item["message_type"] == "audio":
                    await self._handle_voice(sess, item)
                elif item["message_type"] == "text":
                    await self._handle_text(sess, item)
                elif item["message_type"] == "menu":
                    # 菜单点击 = 用户替用户发了那条文字指令
                    await self._handle_text(
                        sess, {"open_id": open_id, "message_id": "",
                               "message_type": "text",
                               "content": json.dumps(
                                   {"text": item["content"]},
                                   ensure_ascii=False)})
                else:
                    await self._send_text(open_id,
                                          "我目前只能听语音和读文字哦。")
            except FeishuAPIError as e:
                logger.error(f"[FEISHU] api error: {e}")
                await self._send_text(open_id, f"⚠️ 飞书接口调用失败：{e}")
            except StudentSessionBusy:
                # 会话注册表拒绝了：学生在网页端（或另一个进程）正上课。
                await self._send_text(
                    open_id,
                    "⚠️ 这个学生现在正在网页端上课，一个学生同时只能有一节课。"
                    "请先在网页端「下课」，再来这里继续。")
            sess.last_active = time.time()

    def _release_session(self, sess: _UserSession):
        """下课/重开前统一走这里：释放会话注册表的占用并清空指针。"""
        if sess.kernel is not None and sess.student_id:
            registry.release(sess.student_id, sess.kernel)
        sess.kernel = None

    async def _handle_voice(self, sess: _UserSession, item: dict):
        open_id = item["open_id"]
        if sess.mgmt is not None:
            # 英文词汇经语音识别不可靠，教材管理只收文字
            if sess.mgmt.paste_active():
                await self._send_text(
                    open_id,
                    "正在等待粘贴——请把 AI 的回复用文字整段贴进来（语音没法"
                    "粘贴）；发「取消」退出粘贴。")
            else:
                await self._send_text(open_id,
                                      "教材管理进行中，请发文字；发「退出」返回上课。")
            return
        # GUI 对齐：课中的语音才是口语输入；冷启动语音和文字一样只得到
        # 引导（开课前网页端也没有可对话的老师）。
        if sess.kernel is not None and sess.kernel.state == "active":
            self._spawn_typing_react(item)  # 兜底补挂（事件入口已挂则跳过）
            try:
                idle = time.time() - (sess.last_active or 0)
                if idle > config.FEISHU_SESSION_IDLE_TIMEOUT:
                    # 隔夜挂着的课：这条冒泡先把它结算掉（报告卡随后指引）
                    await self._emit(open_id, sess.kernel.end_session())
                    self._release_session(sess)
                    return
                try:
                    file_key = json.loads(item["content"]).get("file_key", "")
                except json.JSONDecodeError:
                    file_key = ""
                if not file_key:
                    await self._send_text(open_id, "⚠️ 这条语音没有读出来，请再发一次。")
                    return
                opus_bytes = await asyncio.to_thread(
                    self.client.download_message_resource,
                    item["message_id"], file_key)
                await self._emit(open_id,
                                 sess.kernel.submit_audio(opus_bytes, ext=".opus"))
                return
            finally:
                await self._typing_cleanup(item)
        self._release_session(sess)
        await self._guide_to_start(sess, open_id)

    async def _handle_text(self, sess: _UserSession, item: dict):
        open_id = item["open_id"]
        try:
            text = (json.loads(item["content"]).get("text") or "").strip()
        except json.JSONDecodeError:
            text = ""
        if not text:
            return
        cmd = text.lower().strip()

        # ---- 教材管理模式：拦截一切文字，少数逃生指令放行 ----------------
        if sess.mgmt is not None:
            if cmd in ("退出", "退出管理", "结束管理", "取消管理"):
                sess.mgmt = None
                await self._send_text(open_id, "已退出教材管理。发「帮助」看全部指令。")
                # 拓扑闭环：退出管理即回到聊天页（面板卡）
                await self._send_card(open_id,
                                      self._home_card(open_id))
                return
            if cmd in ("下课", "下课了", "结束", "end", "下课确认",
                       "自由对话", "自由聊天",
                       "free", "上课", "继续上课", "继续", "start") \
                    or cmd.startswith("上课 "):
                sess.mgmt = None  # 用户要去上课了：退出管理，落到正常处理
            else:
                in_wizard = sess.mgmt.step_hint() is not None
                # 守卫：向导进行中收到菜单型指令 = 想回菜单——先走流程
                # 自身的「取消」，再执行新指令，指令绝不被当成步骤输入
                if in_wizard and cmd in _MGMT_COMMANDS:
                    for reply in sess.mgmt.handle("取消"):
                        await self._send_text(open_id, reply)
                # 裸序号指令 → 扁平选择器卡（#22 三轮 P2）：点按钮 = 完整
                # 指令；目录为空/超阈值时 _selector_card 返回 None，
                # 原样落进下面的文字流（「用法：…」兜底）
                if cmd in _SELECTOR_COMMANDS:
                    selector = _selector_card(cmd)
                    if selector is not None:
                        await self._send_card(open_id, selector)
                        return
                # 「看教材」→ 书选择器卡（#25 首例修复）：每书一按钮=
                # 「看单元 N」，消灭「看单元：看单元 序号；…」打字指引；
                # 空目录回落文字流（「还没有任何教材」兜底）
                if cmd in ("看教材", "教材列表", "列表"):
                    selector = _selector_card(
                        "看单元", title="📖 看教材——选一本书")
                    if selector is not None:
                        await self._send_card(open_id, selector)
                        return
                # 「看单元 N」→ 书内单元选择器卡（#41 三层链终点前一层）：
                # 点单元=「看内容 N M」直达全文卡；序号越界/空书/超阈值
                # 回落文字流（计数清单与用法提示兜底，文字协议不变）
                parts = cmd.split()
                if cmd.startswith("看单元") and len(parts) == 2 \
                        and parts[1].isdigit():
                    names = course_engine.get_textbook_names()
                    b_idx = int(parts[1])
                    if 1 <= b_idx <= len(names):
                        tb_id = names[b_idx - 1]
                        selector = _unit_selector_card(
                            b_idx, course_engine.book_title(tb_id),
                            course_engine.list_units_summary(tb_id))
                        if selector is not None:
                            await self._send_card(open_id, selector)
                            return
                # 看内容 N M → 展示卡（P3）；读不出/越界回落文字流拿错误文案
                if cmd.startswith("看内容"):
                    parts = cmd.split()
                    if len(parts) == 3 and parts[1].isdigit() \
                            and parts[2].isdigit():
                        lines = sess.mgmt.unit_content(int(parts[1]),
                                                       int(parts[2]))
                        if lines is not None:
                            await self._send_card(open_id, _markdown_card(
                                "📄 单元内容", "\n".join(lines)))
                            return
                # 「图文说明」→ 内置操作说明图（#37）：拦截必须在
                # flow.handle 之前——向导任意步骤（含粘贴等待态）都不吃它；
                # 不在 _MGMT_COMMANDS，不触发向导守卫；只发图，向导状态
                # 与卡片全都不动（图片落在历史卡上方，随手可回）
                if cmd in ("图文说明", "操作说明"):
                    await self._send_guide_image(open_id)
                    return
                replies = sess.mgmt.handle(text)
                for reply in replies:
                    await self._send_text(open_id, reply)
                if in_wizard and sess.mgmt.step_hint() is None:
                    # 向导终结（落盘/放弃/取消/守卫自动取消）→ 回到命令态，
                    # 重新递菜单卡（#25：向导终结不留「回到打字」死端，
                    # 原 was_confirm 特判只覆盖确认态解除；GUI：保存后停在
                    # 编辑器列表页）
                    await self._send_card(open_id, _mgmt_menu_card(
                        sess.mgmt.book_list_lines()))
                    return
                # 按向导当前步骤递上对应的卡（GUI 等效：「此刻需要你做
                # 选择」的时刻才有卡）——按钮只是快捷方式，文字回复照常优先
                await self._attach_mgmt_card(sess, open_id)
                return

        if cmd in ("帮助", "菜单", "指令", "help"):
            await self._send_text(open_id, _HELP_TEXT)
            return

        if cmd in ("面板", "面板卡", "控制面板", "按钮", "主页") \
                or cmd in ("选课", "选课程", "课程列表"):
            # 课中不分页（GUI 课中没有主页可回）：一律回课中控制卡
            if sess.kernel and sess.kernel.state == "active":
                await self._send_text(
                    open_id,
                    "课中还开着——点下面卡上的「⏹ 下课」，结束后自动回主页。")
                await self._send_card(open_id, _live_lesson_card())
                return
        if cmd in ("面板", "面板卡", "控制面板", "按钮", "主页"):
            # 拓扑 v2：面板卡退役，「面板/主页」= 打开主页（选课卡；
            # 没选学生时递学生卡）
            await self._send_card(open_id, self._home_card(open_id))
            return

        if cmd in ("选课", "选课程", "课程列表"):
            if not await self._require_student(sess, open_id):
                return
            await self._send_card(open_id, self._course_pick_card(
                self.current_student(open_id)))
            return

        if cmd in ("选老师", "换老师", "老师列表"):
            if not await self._require_student(sess, open_id):
                return
            await self._send_card(open_id, self._teacher_pick_card(
                self.current_student(open_id)))
            return

        if cmd.startswith("设老师"):
            if not await self._require_student(sess, open_id):
                return
            await self._handle_set_teacher(sess, open_id, text)
            return

        if cmd.startswith("设风格") or cmd.startswith("设定风格"):
            if not await self._require_student(sess, open_id):
                return
            await self._handle_set_style(sess, open_id, text)
            return

        if cmd == "下课确认":
            # #51：回复卡课堂操作面板「⏹ 下课」的承接——先确认，课不动；
            # 确认卡上的「✅ 确认下课」才是等价「下课」指令（走下方下课分支）。
            if sess.kernel and sess.kernel.state == "active":
                now = time.time()
                if now - self._end_confirm_at.get(open_id, 0.0) \
                        < _END_CONFIRM_DEBOUNCE:
                    return  # 连点防刷屏：toast「已收到」已给即时反馈
                self._end_confirm_at[open_id] = now
                await self._send_card(open_id, _end_confirm_card(
                    sess.kernel.session_stats()))
            else:
                await self._send_text(
                    open_id,
                    "现在没有进行中的课，发语音或说「上课」开始吧。")
            return

        if cmd in ("下课", "下课了", "结束", "end"):
            if sess.kernel and sess.kernel.state == "active":
                await self._emit(open_id, sess.kernel.end_session())
                self._release_session(sess)
            else:
                await self._send_text(open_id, "现在没有进行中的课，发语音或说「上课」开始吧。")
            return

        if cmd in ("学生", "学生列表", "选学生", "切换学生", "我的学生"):
            await self._send_card(open_id, self._student_pick_card(
                self.current_student(open_id)))
            return

        if cmd.startswith("新建学生") or cmd.startswith("建档"):
            await self._handle_new_student(sess, open_id, text)
            return

        if cmd.startswith("编辑学生") or cmd.startswith("改学生"):
            await self._handle_edit_student(sess, open_id, text)
            return

        if cmd in ("自由对话", "自由聊天", "free"):
            if not await self._require_student(sess, open_id):
                return
            if sess.kernel and sess.kernel.state == "active":
                await self._emit(open_id, sess.kernel.end_session())
            self._release_session(sess)
            kernel, begin_kwargs = await self._ensure_session(
                sess, open_id, force_mode="free")
            if kernel is not None and begin_kwargs:
                await self._emit(open_id, kernel.begin(**begin_kwargs))
                await self._send_card(open_id, _live_lesson_card(
                    "💬 自由对话中：发语音或文字即可聊天；点「下课」结束。"))
            return

        if cmd in ("上课", "继续上课", "继续", "start") or cmd.startswith("上课 "):
            if not await self._require_student(sess, open_id):
                return
            if sess.kernel and sess.kernel.state == "active":
                await self._emit(open_id, sess.kernel.end_session())
            self._release_session(sess)
            arg = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            pick = self._resolve_pick(open_id, arg)
            if pick == "invalid":
                # 序号无效（含陈旧主页卡点到已删单元，#25）：错误文案后落
                # 主页卡——此时旧课已结算，主页就是下一步的入口
                await self._send_card(open_id, self._home_card(open_id))
                return
            kernel, begin_kwargs = await self._ensure_session(
                sess, open_id, force_mode="lesson",
                lesson_pick=pick if isinstance(pick, tuple) else None)
            if kernel is None:
                # 从没上过课：给选课引导（只有一本教材时直接开）
                await self._offer_lesson_choice(sess, open_id)
                return
            if begin_kwargs:
                await self._emit(open_id, kernel.begin(**begin_kwargs))
                await self._send_card(open_id, _live_lesson_card())
            return

        if cmd == "进度":
            if not await self._require_student(sess, open_id):
                return
            # P3 展示卡：进度从纯文本升级为卡片（文字内容不变）
            await self._send_card(open_id, _markdown_card(
                "📈 学习进度",
                self._progress_text(self.current_student(open_id)),
                template="blue"))
            return

        if cmd.startswith("切换") or (cmd.startswith("绑定")
                                      and cmd != "绑定管理员"):
            await self._handle_bind(sess, open_id, text)
            return

        # ---- 管理面（全员开放，2026-09-22 裁定：应用场景只有学生本人与
        # 家庭成员，无陌生人；协议是纯文字，菜单点击也汇到这里） --------
        if cmd in ("状态", "服务器状态"):
            # P3 展示卡：状态从纯文本升级为卡片（文字内容不变；
            # 版本指令保持纯文本——升级命令需要可复制）
            await self._send_card(open_id, _markdown_card(
                "🩺 状态", self._status_text(), template="blue"))
            return

        if cmd in ("版本", "检查更新", "检查版本", "升级", "version"):
            await self._handle_version_check(open_id)
            return

        # ---- 配置面（#15④，全员开放同管理面；课中可操作，下一回合生效）--
        # 拦截序约束：这些指令必须先于 _require_student 闸门类处理（不配齐
        # 也要能进配置流）；管理态打出会被教材管理拦截层当流程输入吃掉
        # （与「状态/换Key」同类，#36 同型已接受裁定）。
        if cmd in ("配置", "模型配置") or cmd.startswith("换key") \
                or cmd.startswith("换 key"):
            await self._handle_config_entry(sess, open_id, text)
            return
        if cmd.startswith("选厂商"):
            await self._handle_pick_vendor(sess, open_id, text)
            return
        if cmd.startswith("选模型") or cmd in ("换语音识别", "换思考模型",
                                               "换语音合成"):
            await self._handle_pick_model(sess, open_id, text)
            return
        if cmd.startswith("存key"):
            # N-A5 表单提交的翻译目标（不作广告的内部协议）
            await self._handle_save_key(sess, open_id, text)
            return
        if cmd == "取消配置":
            await self._handle_config_cancel(sess, open_id)
            return
        if cmd in ("key管理", "key 管理"):
            await self._send_card(open_id, _vendor_pick_card("key"))
            return

        if cmd == "教材管理":
            if sess.kernel and sess.kernel.state == "active":
                await self._send_text(
                    open_id,
                    "这节课还开着——点下面卡上的「⏹ 下课」，结束后再来管理教材。")
                await self._send_card(open_id, _live_lesson_card())
                return
            sess.mgmt = TextbookFlow(CourseEngineStore())
            # 进入管理模式 = 一张菜单卡：书单在卡内，按钮=等价文字指令。
            # 横幅/指令帮助不再作为独立消息（打字「帮助」随时可要）。
            await self._send_card(open_id,
                                  _mgmt_menu_card(sess.mgmt.book_list_lines()))
            return

        if cmd in ("绑定管理员", "管理员绑定"):
            # 2026-09-22 裁定：管理功能全员开放，「管理员」概念退役。
            # （FEISHU_ADMIN_OPEN_ID 仍保留唯一用途：开机推卡需要知道
            # 部署者的 open_id——那是平台约束，不是权限。）
            await self._send_text(
                open_id,
                "管理功能（状态 / 换Key / 教材管理）已对所有人开放，"
                "「绑定管理员」这个指令退役了。")
            return

        # 课中的普通文字 = 口语输入，照常进老师（打字练习）。
        # 冷启动（没有进行中的课）的消息不召唤老师——GUI 对齐（2026-09-22
        # 原则裁定）：开课的主动权在学生，面板按钮 / 「上课」/「自由对话」
        # 才开课。隔夜挂着的课由这条冒泡先结算。
        if sess.kernel is not None and sess.kernel.state == "active":
            self._spawn_typing_react(item)
            try:
                idle = time.time() - (sess.last_active or 0)
                if idle <= config.FEISHU_SESSION_IDLE_TIMEOUT:
                    await self._emit(open_id, sess.kernel.submit_text(text))
                    return
                await self._emit(open_id, sess.kernel.end_session())
                self._release_session(sess)
                return  # 结算流自带报告+「接下来？」卡，指引已给到
            finally:
                await self._typing_cleanup(item)
        self._release_session(sess)
        await self._guide_to_start(sess, open_id)

    async def _handle_bind(self, sess: _UserSession, open_id: str, text: str):
        """「切换 学生名」（「绑定 学生名」同样可用）——把当前学生切到
        指定档案，等价于网页端选择器里点选那位学生。"""
        from ...student import student_store
        name = text[2:].strip().replace("：", "").replace(":", "").strip()
        if not name or name in ("学生", "学生列表", "选学生"):
            # 裸词（切换/切换学生）= 打开学生卡
            await self._send_card(open_id, self._student_pick_card(
                self.current_student(open_id)))
            return
        match = next((s for s in student_store.list_students()
                      if s["name"] == name), None)
        if match is None:
            await self._send_text(
                open_id,
                f"没有找到叫「{name}」的学生档案——下面是现有档案，重新点选：")
            await self._send_card(open_id, self._student_pick_card(
                self.current_student(open_id)))
            return
        if await self._refuse_while_active(sess, open_id, "切换学生"):
            return
        self._identities.bind(open_id, match["id"])
        await self._send_text(
            open_id, f"✅ 当前学生：{name}。点下面主页卡上的单元开始上课。")
        await self._send_card(open_id,
                              self._home_card(open_id))

    async def _handle_new_student(self, sess: _UserSession, open_id: str,
                                  text: str):
        """「新建学生 姓名[,年龄[,兴趣]]」——网页端新建学生表单的聊天
        等价物。无参数时递建档表单卡（提交经卡片回调拼回本指令）。"""
        from ...student import student_store
        arg = text[4:].strip() if text.startswith("新建学生") \
            else text[2:].strip()
        if not arg:
            await self._send_card(open_id, _student_form_card())
            return
        if await self._refuse_while_active(sess, open_id, "新建学生"):
            return
        parts = [p.strip() for p in arg.replace("，", ",").split(",")
                 if p.strip()]
        if len(parts) > 1 and not parts[1].isdigit():
            # 建档表单的年龄框是自由文本，提交也可能走到这里（#25）——
            # 回递表单卡，不教打字语法
            await self._send_text(
                open_id, "年龄需要是数字（留空默认 8 岁）——请在下面表单里"
                         "重新填写再提交：")
            await self._send_card(open_id, _student_form_card())
            return
        from ...student import LANGUAGE_BACKGROUNDS
        name = parts[0]
        age = int(parts[1]) if len(parts) > 1 else 8
        language_background = "中文"
        interest_raw = ""
        for p in parts[2:]:
            if p.startswith("语言:"):
                language_background = p[3:].strip() or "中文"
            elif p in LANGUAGE_BACKGROUNDS:
                language_background = p
            else:
                interest_raw = p
        interests = [w for w in re.split(r"[、 ]+", interest_raw) if w] \
            if interest_raw else []
        profile = student_store.create_student(
            name=name, age=age, language_background=language_background,
            interests=interests)
        self._identities.bind(open_id, profile.profile_path.stem)
        await self._send_text(
            open_id, f"✅ 已创建学生「{name}」（{age} 岁，{language_background}）"
                     f"并设为当前学生。点下面主页卡上的单元开始上课。")
        # 拓扑闭环：建档落地即送到聊天页（面板卡），不用手打「上课」
        await self._send_card(open_id,
                              self._home_card(open_id))

    async def _handle_edit_student(self, sess: _UserSession, open_id: str,
                                   text: str):
        """「编辑学生 [名 [年龄:x] [语言:x]]」（#55 档案编辑；旧词「改学生」
        同收）：裸指令或只带名字 → 编辑表单卡（名字裸打=想改这名）；带段=
        执行。课中拒绝（「新建学生」同族）；成功回执 + 学生卡（编辑的合理
        下一步是继续改或切换）。与「新建学生」同待遇：管理态内被流程吃掉。"""
        from ...student import LANGUAGE_BACKGROUNDS
        arg = text[4:].strip() if text.startswith("编辑学生") \
            else text[3:].strip()
        if not arg:
            card = _student_edit_pick_card()
            if card is None:
                await self._send_text(
                    open_id, "还没有学生档案——发「新建学生」先建一位。")
                return
            await self._send_card(open_id, card)
            return
        m = re.match(r"^(\S+)(?:\s+(.*))?$", arg, re.S)
        name, seg_raw = m.group(1), (m.group(2) or "").strip()
        segments = [t for t in seg_raw.split() if ":" in t]
        if not segments:
            # 「编辑学生 名」= 想编辑这名 → 编辑表单卡（当前值已预选）
            profile = self._find_profile(name)
            if profile is None:
                await self._send_text(
                    open_id, f"没有找到学生「{name}」——发「学生」看列表，"
                             f"或发「编辑学生」从卡片里选。")
                return
            await self._send_card(open_id, _student_form_card(profile))
            return
        if await self._refuse_while_active(sess, open_id, "编辑学生"):
            return
        profile = self._find_profile(name)
        if profile is None:
            await self._send_text(
                open_id, f"没有找到学生「{name}」——发「学生」看列表，"
                         f"或发「编辑学生」从卡片里选。")
            return
        age = lang = None
        for tok in segments:
            key, _, val = tok.partition(":")
            val = val.strip()
            if key == "年龄":
                if not val.isdigit():
                    await self._send_text(
                        open_id, "年龄需要是数字——发「编辑学生 "
                                 f"{profile.student_name}」在表单里改。")
                    return
                age = int(val)
            elif key == "语言":
                if val not in LANGUAGE_BACKGROUNDS:
                    await self._send_text(
                        open_id, "母语只认「" + "」或「".join(
                            LANGUAGE_BACKGROUNDS) + "」——发「编辑学生 "
                                 f"{profile.student_name}」在表单里改。")
                    return
                lang = val
            else:
                await self._send_text(
                    open_id, f"不认识的修改项「{key}」——可改：年龄:x / "
                             f"语言:x。")
                return
        if age is None and lang is None:
            await self._send_card(open_id, _student_form_card(profile))
            return
        profile.update_profile(age=age, language_background=lang)
        changes = []
        if age is not None:
            changes.append(f"年龄 → {age}")
        if lang is not None:
            changes.append(f"母语 → {lang}")
        await self._send_text(
            open_id, f"✅ 已更新学生「{profile.student_name}」："
                     f"{'；'.join(changes)}。下次开课生效。")
        await self._send_card(open_id, self._student_pick_card(
            self.current_student(open_id)))

    def _find_profile(self, name: str):
        """按显示名找档案（#55）：取列表首个同名者——建档时同名会生成
        递增文件名但显示名可重复，此处与「切换 名」的解析宽容度一致。"""
        from ...student import student_store
        for s in student_store.list_students():
            if s["name"] == name:
                try:
                    profile = student_store.get_profile(s["id"])
                    profile.load()
                    return profile
                except Exception:
                    return None
        return None

    async def _require_student(self, sess: _UserSession, open_id: str) -> bool:
        """GUI 对齐闸门：模型配置不齐先递厂商选择卡（网页端 setupOverlay
        人人可见的聊天等价，2026-09-22 裁定全员可配；#15④ 起闸门=能力
        判定 config.modalities_ready()）；没选学生时，一切开课/查进度动作
        先递学生卡。"""
        if not config.modalities_ready():
            await self._send_text(
                open_id, "这台 PrivaGlot 的模型配置还不完整（语音识别 / "
                         "思考模型 / 语音合成要各就各位），还开不了课。"
                         "在下面卡片里选厂商、配 Key、选模型，几分钟配好，"
                         "立即生效（不用重启）。")
            self._cfg_wizard[open_id] = "asr"
            await self._send_card(open_id, _vendor_pick_card("cold"))
            return False
        if self.current_student(open_id):
            return True
        await self._send_text(open_id,
                              "先选一位学生（或新建）——网页端「选择学生」"
                              "的聊天等价：")
        await self._send_card(open_id, self._student_pick_card(None))
        return False

    async def _refuse_while_active(self, sess: _UserSession,
                                   open_id: str,
                                   action: str = "进行该操作") -> bool:
        """切换/建档/换老师前 guard：课还开着时拒绝并递课中控制卡（#25：
        拒绝不教打字「下课」——卡上就有 ⏹ 下课按钮），避免注册表里的旧
        会话挂在已切换走的学生身上。返回 True = 已拒绝。"""
        if sess is not None and sess.kernel is not None \
                and sess.kernel.state == "active":
            await self._send_text(
                open_id,
                f"这节课还开着——点下面卡上的「⏹ 下课」，结束后再{action}。")
            await self._send_card(open_id, _live_lesson_card())
            return True
        return False

    async def _guide_to_start(self, sess: _UserSession, open_id: str):
        """冷启动统一引导（语音/文字共用）：配置不齐 → 厂商选择卡（#15④
        向导起点）；没选学生 → 欢迎（首次）或短提示 + 学生卡（网页端首屏
        就是选择器）；已选学生 → 提示+面板卡。"""
        if not config.modalities_ready():
            await self._send_text(
                open_id, "这台 PrivaGlot 的模型配置还不完整。在下面卡片里"
                         "选厂商、配 Key、选模型，几分钟配好，立即生效；"
                         "配好后再来选学生、上课。")
            self._cfg_wizard[open_id] = "asr"
            await self._send_card(open_id, _vendor_pick_card("cold"))
            return
        if self.current_student(open_id) is None:
            first = open_id not in self._greeted
            self._greeted.add(open_id)
            await self._send_text(
                open_id, _WELCOME if first else _COLD_NO_STUDENT_HINT)
            await self._send_card(open_id, self._student_pick_card(None))
            return
        await self._send_text(open_id, _COLD_TEXT_HINT)
        await self._send_card(open_id,
                              self._home_card(open_id))

    async def _attach_mgmt_card(self, sess: _UserSession, open_id: str):
        """按 TextbookFlow.step_hint() 的只读元数据递卡（文字协议不变，
        其它通道可完全忽略元数据）：confirm → 确认卡；paste_confirm →
        一键粘贴解析确认卡（重贴=「粘贴」指令复用）；skip → 跳过/取消；
        form → 单值输入表单卡（extra=done 时多一个「完成」按钮）；
        words → 一键粘贴卡（#22 二轮：整单元录入 = 网页端「粘贴框+文本域」
        的聊天等价物；粘贴进卡=主路径，手打多行=聊天输入框全屏）。"""
        hint = sess.mgmt.step_hint() if sess.mgmt is not None else None
        if hint is None:
            return
        expect = hint.get("expect")
        if expect == "confirm":
            await self._send_card(open_id, _CONFIRM_CARD)
        elif expect == "paste_confirm":
            await self._send_card(open_id, _PASTE_CONFIRM_CARD)
        elif expect == "skip":
            await self._send_card(open_id, _SKIP_CARD)
        elif expect == "edit_field":
            await self._send_card(open_id, _edit_field_card(
                str(hint.get("label") or "")))
        elif expect == "form":
            extra = None
            if hint.get("extra") == "done":
                extra = [_btn20("✅ 完成", "完成")]
            await self._send_card(open_id, _flow_form_card(
                str(hint.get("label") or ""),
                str(hint.get("placeholder") or ""), extra))
        elif expect == "words":
            await self._send_card(open_id, _flow_form_card(
                str(hint.get("label") or "粘贴 AI 整理的整段回复"),
                "把 AI 按提示词整理的回复整段粘贴到这里…",
                extra_buttons=[_btn20("📋 拿提示词", "提示词"),
                               _btn20("🖼 获取图文说明", "图文说明")],
                marker="__paste_submit",
                note="拍课本页 → 豆包/Kimi 按提示词整理 → 复制回复 → "
                     "长按上面输入框粘贴；提交后自动识别 [词汇]/[句式]/"
                     "[语法] 三段。手打多行内容：点底部输入框 ↗ 全屏后"
                     "直接发送。第一次操作？点「🖼 获取图文说明」看分步"
                     "图解。"))

    async def _send_guide_image(self, open_id: str):
        """「获取图文说明」（#37）：把内置的教材提取分步图解发给用户。
        image_key 即点即传不缓存——image_key 绑定当前应用名下，重扫部署
        换应用后缓存必失效（2026-10-14 拍板）。失败回人话不抛出；向导
        状态与卡片由调用方保证不动（拦截边直接 return）。"""
        try:
            data = await asyncio.to_thread(_GUIDE_IMAGE.read_bytes)
            image_key = await asyncio.to_thread(self.client.upload_image, data)
            await asyncio.to_thread(self.client.send_image, open_id, image_key)
        except Exception as e:
            logger.error(f"[FEISHU] guide image failed: {e}", exc_info=True)
            await self._send_text(
                open_id, "⚠️ 图文说明暂时发不出来，请稍后再点一次。")

    # ------------------------------------------------------------------
    # 管理面（状态 / 换Key / 教材管理）—— 全员开放（2026-09-22 裁定：
    # 应用场景只有学生本人与家庭成员，无陌生人；「管理员」概念退役，
    # FEISHU_ADMIN_OPEN_ID 仅剩开机推卡一个用途——平台约束而非权限）
    # ------------------------------------------------------------------

    def _status_text(self) -> str:
        from ...student import student_store
        lines = ["🩺 PrivaGlot 状态（飞书通道）"]
        lines.append(f"· 版本：{config.APP_VERSION}")
        # 三模态行（#15④）：对齐网页端组合展示（标签同款），Key 缺失指路「配置」
        for mod in ("asr", "llm", "tts"):
            sel = getattr(vendors, f"{mod}_selection")()
            has_key = bool((sel.get("api_key") or "").strip())
            lines.append(f"· {_MODALITY_LABELS[mod]}："
                         f"{sel['profile']['display_name']} · {sel['model']}"
                         + ("" if has_key
                            else " ⚠️ Key 未配置 —— 发「配置」补齐"))
        lines.append("· ffmpeg：" + ("就绪" if shutil.which("ffmpeg")
                                     else "⚠️ 未找到，语音转码不可用"))
        try:
            names = course_engine.get_textbook_names()
            units = sum(len(course_engine.list_units_summary(t)) for t in names)
            lines.append(f"· 教材：{len(names)} 本 / {units} 个单元"
                         + ("" if names else " —— 主页点「📖 管理教材」"))
        except Exception:
            lines.append("· 教材：读取失败（看服务日志）")
        try:
            lines.append(f"· 学生档案：{len(student_store.list_students())} 个")
        except Exception:
            pass
        lines.append(f"· 飞书绑定：{len(self._identities.bound_ids())} 个账号")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 配置面处理器（#15④，2026-10-22 用户裁定五项+三小点落地）：
    # 入口「配置/换Key」→ N-A7（齐）/ N-A4 cold（不齐，向导起点）；
    # 「选厂商」→ N-A5；「存Key」（表单翻译目标）→ 轻探活落盘 → 下一卡；
    # 「选模型」→ 强测 3 次全过才应用；「取消配置」→ 安全卡。
    # 零自动逻辑裁定不变：不重试、不降级、不换厂商，报错人话+指路。
    # ------------------------------------------------------------------

    def _safety_card(self, sess: _UserSession | None, open_id: str) -> dict:
        """配置流的取消/退出落点（会话状态纯函数，永不教打字）：
        课中→课中控制卡；三模态齐→配置总卡；不齐→厂商选择卡。"""
        if sess is not None and sess.kernel is not None \
                and sess.kernel.state == "active":
            return _live_lesson_card()
        if config.modalities_ready():
            return _config_hub_card()
        return _vendor_pick_card("cold")

    async def _handle_config_entry(self, sess: _UserSession, open_id: str,
                                   text: str):
        """「配置 / 换Key（裸）」：已配齐递配置总卡（N-A7）；不齐递厂商
        选择卡（N-A4 cold，冷启动向导起点并置轻会话态）。
        「换Key <Key>」带参协议退役（2026-10-22 用户裁定；智谱 Key 无
        前缀特征无法认领厂商，且 Key 不该进聊天记录）：说明文 + N-A4
        （Key 管理），对齐「绑定管理员」退役先例。"""
        parts = text.split(None, 1)
        if parts and parts[0].lower() in ("换key", "换 key") \
                and len(parts) > 1 and parts[1].strip():
            await self._send_text(
                open_id, "「换Key <Key>」的旧协议退役了。现在发「配置」进"
                         "模型配置：先选厂商，再在卡片里粘贴 Key（走卡片"
                         "提交，不进聊天记录），最后逐项选模型。")
            await self._send_card(open_id, _vendor_pick_card("key"))
            return
        if config.modalities_ready():
            await self._send_card(open_id, _config_hub_card())
        else:
            self._cfg_wizard[open_id] = "asr"
            await self._send_card(open_id, _vendor_pick_card("cold"))

    async def _handle_pick_vendor(self, sess: _UserSession, open_id: str,
                                  text: str):
        """「选厂商 <id/中文名>」→ 该厂商的 Key 表单卡（N-A5，档案渲染）。"""
        arg = text.split(None, 1)
        raw = (arg[1].strip().lower() if len(arg) > 1 else "")
        aliases = {"小米": "mimo", "mimo": "mimo",
                   "智谱": "zhipu", "zhipu": "zhipu", "glm": "zhipu"}
        vid = aliases.get(raw, raw)
        if vid not in vendors._REGISTRY:
            await self._send_text(open_id, "没认出这个厂商——在下面卡片里点选：")
            await self._send_card(open_id, _vendor_pick_card(
                "cold" if not config.modalities_ready() else "key"))
            return
        await self._send_card(open_id, _key_form_card(vid))

    async def _handle_save_key(self, sess: _UserSession, open_id: str,
                               text: str):
        """「存Key <厂商> <Key>」——Key 表单卡（N-A5）提交的翻译目标，
        不作广告的内部协议（Key 的正路 = 卡片提交，不进聊天记录）。
        轻探活 1 次：坏 Key 不落盘（对齐网页端 /api/setup/vendor-key 的
        「防好 Key 被误覆盖」语义）；留空提交 = 沿用已保存 Key。之后按
        向导态递下一卡（向导中→N-A6 模型卡；否则→N-A7 总卡）。"""
        parts = text.split(None, 2)
        vid = (parts[1].strip().lower() if len(parts) > 1 else "")
        key = (parts[2].strip().strip('"').strip("'") if len(parts) > 2
               else "")
        prof = vendors._REGISTRY.get(vid)
        if prof is None:
            await self._send_text(open_id,
                                  "没认出这个厂商——在下面卡片里重新进入。")
            await self._send_card(open_id, _vendor_pick_card("key"))
            return
        existing = _vendor_key(vid)
        if key:
            from ...verify_key import probe_llm, probe_tts, probe_asr
            # 轻探活用该厂商第一个可用模态的默认模型（同网页端 save_vendor_key）
            sec = {}
            for mod in ("llm", "tts", "asr"):
                sec = prof.get(mod) or {}
                if sec:
                    break
            model = ((sec.get("models") or [{}])[0].get("id")) or ""
            base = ((sec.get("base_url") or "").strip()
                    or config.base_url_for_key(key))
            probe = {"llm": probe_llm, "tts": probe_tts,
                     "asr": probe_asr}[mod]
            await self._send_text(
                open_id, f"正在验证 {prof['display_name']} Key"
                         "（会真调一次接口，几秒钟）…")
            result = await probe(key, base, model, vid)
            if not result.ok:
                await self._send_text(
                    open_id, f"⚠️ Key 验证失败，没有保存（原有配置未动）："
                             f"{result.message}")
                await self._send_card(open_id, _key_form_card(vid))
                return
            config.set_vendor_key(vid, key)
            await self._send_text(
                open_id, f"✅ {prof['display_name']} Key 已保存"
                         f"（{config.masked_key(key)}）。")
        elif not existing:
            await self._send_text(
                open_id, "Key 不能为空——请在下面卡片里粘贴后再提交。")
            await self._send_card(open_id, _key_form_card(vid))
            return
        # 留空且已有 Key = 沿用，直接进下一步
        if self._cfg_wizard.get(open_id):
            await self._send_card(
                open_id, _model_pick_card(self._cfg_wizard[open_id]))
        else:
            await self._send_card(open_id, _config_hub_card())

    async def _handle_pick_model(self, sess: _UserSession, open_id: str,
                                 text: str):
        """「选模型 <模态> [模型id]」（「换语音识别/换思考模型/换语音合成」
        = 打开对应模型卡）：无 id 递 N-A6；有 id 走档案内严格映射（
        2026-10-21 裁定）→ 强测 3 次全过才 apply_modality_config（单模态，
        2026-10-22 用户裁定对齐 Windows 保存强测）→ 回执 + 下一卡（向导序
        asr→llm→tts→完成；单换→N-A7）。TTS 切厂商回执附教师重选提示
        （教师目录随厂商走的既有裁定）。"""
        mod_alias = {"asr": "asr", "llm": "llm", "tts": "tts",
                     "语音识别": "asr", "思考模型": "llm", "语音合成": "tts"}
        parts = text.split()
        head = parts[0].lower() if parts else ""
        if head in ("换语音识别", "换思考模型", "换语音合成"):
            modality = mod_alias[head[1:]]
            model_id = ""
        elif head == "选模型":
            if len(parts) < 2:
                await self._send_card(open_id, _config_hub_card())
                return
            modality = mod_alias.get(parts[1].lower(), parts[1].lower())
            if modality not in ("asr", "llm", "tts"):
                await self._send_text(
                    open_id, "模态只有 语音识别 / 思考模型 / 语音合成 三种"
                             "——发「配置」从卡片点选。")
                await self._send_card(open_id, _config_hub_card())
                return
            model_id = parts[2].strip() if len(parts) > 2 else ""
        else:
            return
        if not model_id:
            await self._send_card(open_id, _model_pick_card(modality))
            return
        hit = None
        for vid, prof in vendors._REGISTRY.items():
            sec = prof.get(modality) or {}
            for m in sec.get("models", []):
                if m.get("id") == model_id:
                    hit = (vid, prof, sec, m)
        if hit is None:
            await self._send_text(
                open_id, f"没有找到模型「{model_id}」——请在下面卡片里点选。")
            await self._send_card(open_id, _model_pick_card(modality))
            return
        vid, prof, sec, m = hit
        key = _vendor_key(vid)
        if not key:
            await self._send_text(
                open_id, f"{prof['display_name']} 的 API Key 还没配置——"
                         "先在「🔑 Key 管理」里配好，再来选它的模型。")
            await self._send_card(open_id, _vendor_pick_card("key"))
            return
        from ...verify_key import probe_llm, probe_tts, probe_asr
        probe = {"llm": probe_llm, "tts": probe_tts, "asr": probe_asr}[modality]
        base = ((sec.get("base_url") or "").strip()
                or config.base_url_for_key(key))
        label = m.get("label") or model_id
        await self._send_text(
            open_id, f"正在测活 {_MODALITY_LABELS[modality]} → "
                     f"{prof['short_name']}·{label}（3 次，几秒钟）…")
        fail = None
        for _ in range(3):
            result = await probe(key, base, model_id, vid)
            if not result.ok:
                fail = result
                break
        if fail is not None:
            await self._send_text(
                open_id, f"⚠️ 测活未通过（3 次全过才生效，原配置未动）："
                         f"{fail.message}\n可以在下面卡片里换一个模型再试。")
            await self._send_card(open_id, _model_pick_card(modality))
            return
        config.apply_modality_config(
            {modality: {"vendor": vid, "model": model_id, "key": ""}})
        extra = ""
        if modality == "tts" and self.current_student(open_id):
            from ...teacher import teacher_store
            _, _, notice = teacher_store.resolve_for_student(
                self.current_student(open_id))
            if notice:
                extra = f"\n{notice}"
        await self._send_text(
            open_id, f"✅ {_MODALITY_LABELS[modality]} 已切换："
                     f"{prof['display_name']} · {label}（测活 3/3 通过）"
                     + extra)
        if not self._cfg_wizard.get(open_id):
            await self._send_card(open_id, _config_hub_card())
            return
        # 冷启动向导推进（轻会话态：固定顺序 asr→llm→tts→完成）
        remaining = _CFG_WIZARD_ORDER[_CFG_WIZARD_ORDER.index(modality) + 1:]
        if remaining:
            self._cfg_wizard[open_id] = remaining[0]
            await self._send_card(open_id, _model_pick_card(remaining[0]))
            return
        self._cfg_wizard.pop(open_id, None)
        combo = "\n".join(
            f"· {_MODALITY_LABELS[mm]}："
            f"{getattr(vendors, f'{mm}_selection')()['profile']['display_name']}"
            f" · {getattr(vendors, f'{mm}_selection')()['model']}"
            for mm in _CFG_WIZARD_ORDER)
        await self._send_text(open_id,
                              "🎉 模型配置完成，随时可以开课：\n" + combo)
        await self._send_card(open_id, self._home_card(open_id))

    async def _handle_config_cancel(self, sess: _UserSession | None,
                                    open_id: str):
        """「取消配置」——清向导态，落安全卡（会话状态纯函数）。"""
        self._cfg_wizard.pop(open_id, None)
        await self._send_text(open_id, "已退出模型配置。")
        await self._send_card(open_id, self._safety_card(sess, open_id))

    async def _handle_version_check(self, open_id: str):
        """「版本 / 检查更新」——升级路径的发现端（#13）：报当前版本，
        匿名查 TCR 公开仓最新版；有新版就给可整段复制的升级命令（= README
        一键部署命令，两处由测试锁定逐字符一致）。容器换不了自己——执行
        永远在宿主侧，这里只做发现与引导，绝不给「正在升级」的假象。"""
        cur = config.APP_VERSION
        if cur == "dev":
            await self._send_text(
                open_id, "当前版本 dev（本机源码形态），跳过在线版本检查。")
            return
        try:
            latest = await asyncio.to_thread(
                version_check.fetch_latest_version)
        except Exception as e:
            logger.warning(f"[FEISHU] version check failed: {e}")
            await self._send_text(
                open_id, f"⚠️ 检查更新失败（网络原因）。当前版本 {cur}；"
                         "稍后再试，或直接重跑部署命令——它本身就是升级命令。")
            return
        if not version_check.has_update(cur, latest):
            await self._send_text(open_id, f"✅ 已是最新版本（{cur}）。")
            return
        await self._send_text(
            open_id,
            f"🆕 发现新版本：{latest}（当前 {cur}）。把下面这条命令复制到 "
            "VPS 终端执行即可升级——同一条命令就是首次部署命令：数据自动"
            "快照，新版本起不稳会自动回滚；升级期间机器人离线约 1 分钟。\n\n"
            + version_check.UPDATE_COMMAND
            + "\n升级完成后机器人重启，会自动推送「已更新」通知。")

    # ------------------------------------------------------------------
    # lesson picking（学生第一次上课时的选教材路径）
    # ------------------------------------------------------------------

    def _resolve_pick(self, open_id: str, arg: str):
        """「上课 序号」/「上课 序号-单元序号」→ (textbook, unit_id)。
        arg 为空返回 None（走自动续学）；格式/范围错误回复用户并返回
        字符串 "invalid"。"""
        import re
        arg = (arg or "").strip()
        if not arg:
            return None
        names = course_engine.get_textbook_names()
        m = re.match(r"^(\d+)(?:-(\d+))?$", arg)
        if m is None or not names:
            self._try_send_text(
                open_id, "用法：上课 序号 或 上课 序号-单元序号"
                         "（直接发「上课」可看教材列表）。")
            return "invalid"
        book_index = int(m.group(1))
        if not (1 <= book_index <= len(names)):
            self._try_send_text(
                open_id, f"书序号超出范围（1-{len(names)}）。")
            return "invalid"
        book_id = names[book_index - 1]
        # 引擎目录里 units 是字典（unit_id 为键），必须走 summary API
        units = course_engine.list_units_summary(book_id)
        unit_index = int(m.group(2)) if m.group(2) else 1
        if not (1 <= unit_index <= len(units)):
            self._try_send_text(
                open_id, f"单元序号超出范围（1-{len(units)}）。")
            return "invalid"
        return book_id, units[unit_index - 1]["unit_id"]

    async def _offer_lesson_choice(self, sess: _UserSession, open_id: str) -> bool:
        """学生还没有任何课程进度时的选课引导。只有一本教材时直接开课
        （家庭部署的常态）；多本则发序号清单，等「上课 序号」。
        返回是否已经开了课。"""
        names = course_engine.get_textbook_names()
        if not names:
            await self._send_text(
                open_id,
                "还没有可选的课程内容——点下面主页卡的「📖 管理教材」建一本，"
                "或把教材 JSON 放进 data/curriculum/ 后重启服务。")
            await self._send_card(open_id, self._home_card(open_id))
            return False
        if len(names) == 1:
            # 引擎目录里 units 是字典（unit_id 为键），必须走 summary API
            units = course_engine.list_units_summary(names[0])
            if units:
                kernel, begin_kwargs = await self._ensure_session(
                    sess, open_id, force_mode="lesson",
                    lesson_pick=(names[0], units[0]["unit_id"]))
                if kernel is not None and begin_kwargs:
                    await self._emit(open_id, kernel.begin(**begin_kwargs))
                    await self._send_card(open_id, _live_lesson_card())
                return True
        # 多本教材：递选课卡（GUI coursePicker 等价），不再只发序号清单。
        await self._send_card(open_id, self._course_pick_card(
            self.current_student(open_id) or ""))
        return False

    # ------------------------------------------------------------------
    # session lifecycle for chat usage
    # ------------------------------------------------------------------

    async def _ensure_session(self, sess: _UserSession, open_id: str,
                              force_mode: str,
                              lesson_pick: tuple | None = None):
        """为「显式开课」（上课/自由对话按钮或指令）准备会话；调用方必须
        已通过 _require_student 确认当前学生存在。

        force_mode: "lesson"（自动续学上次单元，lesson_pick 可指定书/单元）
        或 "free"（自由对话）。返回 (kernel, begin_kwargs)；kernel None =
        要开课但没有可开的单元（调用方负责向用户解释）。抛出
        StudentSessionBusy 当该学生此刻在网页端/其他进程正上课——注册表
        拥有这个决定，_dispatch 负责渲染人话。冷启动（无活跃会话）永远
        不会走到这里——语音/文字的冷路径由 _guide_to_start 统一引导，
        这是 2026-09-22 原则裁定（GUI 对齐）后的结构。"""
        if sess.kernel and sess.kernel.state == "active":
            await self._emit(open_id, sess.kernel.end_session())
            self._release_session(sess)

        student_id = self.current_student(open_id)
        kernel = ConversationKernel()
        kernel.load_student(student_id)
        # 注册表确认这个学生此刻没有别的活会话（busy 直接抛出）。
        registry.register(student_id, kernel)
        sess.kernel = kernel
        sess.student_id = student_id

        # GUI 对齐：begin 携带该学生的老师/风格（与网页 begin_session 同形）。
        from ...teacher import teacher_store
        try:
            teacher, style_id, _ = teacher_store.resolve_for_student(student_id)
            teacher_id, style = teacher.id, style_id
        except Exception:
            teacher_id, style = "", ""

        textbook = unit_id = ""
        if force_mode == "lesson":
            if lesson_pick is not None:
                textbook, unit_id = lesson_pick
            else:
                pick = self._last_unit(student_id)
                if pick is None:
                    self._release_session(sess)
                    return None, {}
                textbook, unit_id = pick
        return kernel, {"textbook": textbook, "unit_id": unit_id,
                        "mode": force_mode,
                        "teacher_id": teacher_id, "style": style}

    def _last_unit(self, student_id: str) -> tuple | None:
        """(textbook, unit_id) most recently practiced, else None."""
        from ...student import student_store
        try:
            profile = student_store.get_profile(student_id)
            profile.load()
        except Exception:
            return None
        best, best_ts = None, ""
        for key, entry in (profile.course_progress or {}).items():
            ts = str(entry.get("last_practiced") or "")
            if not ts:
                continue
            if ts > best_ts:
                best, best_ts = key, ts
        if best is None:
            return None
        textbook, _, unit_id = best.rpartition("/")
        return (textbook, unit_id) if textbook and unit_id else None

    def _progress_text(self, student_id: str) -> str:
        from ...student import student_store
        try:
            profile = student_store.get_profile(student_id)
            profile.load()
        except Exception:
            return "还没有学生档案。"
        name = profile.student_name
        rows = []
        for key, entry in sorted((profile.course_progress or {}).items(),
                                 key=lambda kv: str(kv[1].get("last_practiced") or ""),
                                 reverse=True):
            prog = entry.get("progress")
            rows.append(f"· {key}："
                        + (f"{prog}%" if isinstance(prog, int) else "未评估")
                        + f"（练过 {entry.get('sessions', 0)} 次）")
        if not rows:
            return f"{name} 还没有上课记录，发语音开始第一课吧！"
        return f"{name} 的学习进度：\n" + "\n".join(rows[:10])

    # ------------------------------------------------------------------
    # kernel events → Feishu messages
    # ------------------------------------------------------------------

    async def _emit(self, open_id: str, aiter):
        """Consume one kernel event stream and render it onto Feishu.

        Rendering decisions (capability degradation, channel's right):
          - #24 语音为主：llm tokens accumulate into the transcript; at stream
            end the whole turn goes out as ONE reply card (audio components
            first, collapsible transcript panel below) — nothing is sent at
            llm_done any more;
          - no audio (TTS failed/empty) → plain text bubble fallback;
          - transcription/vocab_hit/student_info/setup events are ignored
            (the student's own voice is already in the chat; progress lives
            on the web UI and the 「进度」 command);
          - error bubbles are text-only.
        """
        text_buf: list[str] = []
        audio_wavs: list[bytes] = []

        async for ev in aiter:
            etype = ev.get("type")
            if etype == events.EVENT_LLM_TOKEN:
                text_buf.append(ev.get("text", ""))
            elif etype == events.EVENT_LLM_CLEANED:
                text_buf = [ev.get("text", "")]
            elif etype == events.EVENT_TTS_AUDIO:
                import base64
                audio_wavs.append(base64.b64decode(ev.get("data", "")))
            elif etype == events.EVENT_LESSON_COMPLETE:
                await self._send_text(open_id, _lesson_report_text(ev))
                # 报告卡：下一步动作按钮（文字指令照常可用）
                await self._send_card(open_id, _report_card())
            elif etype == events.EVENT_LESSON_EVAL_RESULT:
                await self._send_text(
                    open_id,
                    f"📈 本次课进度评估：{ev.get('progress', 0)}%\n"
                    f"{ev.get('note', '')}")
            elif etype == events.EVENT_LESSON_EVAL_FAILED:
                await self._send_text(open_id, ev.get("message", ""))
            elif etype == events.EVENT_FREE_SESSION_COMPLETE:
                await self._send_text(open_id, _free_summary_text(ev))
                # GUI 对齐：自由对话结束同样是课后出口——补报告卡，不留死端。
                await self._send_card(open_id, _report_card())
            elif etype == events.EVENT_LESSON_TARGETS:
                vocab = ev.get("vocabulary") or []
                if vocab:
                    await self._send_card(open_id, _targets_card(vocab))
            elif etype == events.EVENT_ERROR:
                await self._send_text(open_id, f"⚠️ {ev.get('message', '')}")
            # everything else: intentionally not rendered

        transcript = "".join(text_buf)
        if audio_wavs:
            await self._send_reply_card(open_id, audio_wavs, transcript)
        elif transcript:
            await self._send_text(open_id, transcript)

    async def _send_reply_card(self, open_id: str, wavs: list[bytes],
                               transcript: str):
        """#24 回复卡整链：合并 → 55s 分段 → 逐段转码上传 → 单卡发出。
        链上任一步失败（转码/上传/发送，卡片必未发出）→ 退回纯文字，
        原文不丢——与空 TTS 的回退边同语义（语音为主、文字兜底）。"""
        from ...core import codecs
        try:
            merged = await asyncio.to_thread(codecs.merge_wavs, wavs)
            parts = await asyncio.to_thread(codecs.split_wav, merged,
                                            VOICE_MAX_SECONDS)
            file_keys = []
            for part in parts:
                duration_ms = int(codecs.wav_duration_seconds(part) * 1000)
                opus = await codecs.wav_to_opus_async(part)
                file_keys.append(await asyncio.to_thread(
                    self.client.upload_opus, opus, duration_ms))
            await self._send_card(open_id, _reply_card(file_keys, transcript))
        except Exception as e:
            logger.warning(f"[FEISHU] reply card chain failed, text fallback: {e}")
            if transcript:
                await self._send_text(open_id, transcript)

    async def _send_text(self, open_id: str, text: str):
        if not text:
            return
        await asyncio.to_thread(self.client.send_text, open_id, text)

    async def _send_card(self, open_id: str, card: dict):
        await asyncio.to_thread(self.client.send_card, open_id, card)

    def _student_pick_card(self, current_id: str | None) -> dict:
        """学生选择卡 = 网页端学生选择器的等价物：全部档案任选（点=切为
        当前学生），➕ 新建学生 → 建档表单卡。家庭规模按 3 个一行排，
        超过 9 位时提示用「切换 学生名」。"""
        from ...student import student_store
        students = student_store.list_students()
        rows = []
        for i in range(0, min(len(students), 9), 3):
            rows.append({"tag": "action", "actions": [
                _button(s["name"], f"切换 {s['name']}")
                for s in students[i:i + 3]]})
        rows.append({"tag": "action", "actions": [
            _button("➕ 新建学生", "新建学生", "primary"),
            _button("✏️ 编辑学生", "编辑学生")]})
        # #30：主页的学生卡形态同样带联系入口——dispatch 兜底无学生时
        # 递的就是这张卡，按钮在两态都存在兜底指引才恒真。
        # #36：检查更新入口按钮化——等价指令=既有「检查更新」别名，
        # 零新协议；课中/冷启动直答，教材管理态被流程吃掉（#33 同型裁定）。
        rows.append({"tag": "action", "actions": [
            _link_button("📞 联系开发者", _CONTACT_URL),
            _button("🔄 检查更新", "检查更新")]})
        current = next((s["name"] for s in students if s["id"] == current_id),
                       None) if current_id else None
        note = "点学生名 = 切换为当前学生（网页端选择器的等价）；" \
               "超过 9 位时发「切换 学生名」。"
        if current:
            note = f"当前学生：{current}。" + note
        elif not students:
            note = "还没有学生档案——点「➕ 新建学生」创建第一位" \
                   "（也可先在网页端建好，再回来点选）。" + note
        rows.append({"tag": "note", "elements": [
            {"tag": "plain_text", "content": note}]})
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": "🧑‍🎓 选择学生"},
                       "template": "blue"},
            "elements": rows,
        }

    def _home_card(self, open_id: str) -> dict:
        """拓扑 v2（2026-09-23 用户确认）的主页卡：有当前学生 → 选课卡
        （= 网页端选课页，GUI 的枢纽页）；没有 → 学生卡。旧的「控制面板」
        卡不对应任何 GUI 页面，已退役——它承担的恢复点/随时找回职责由
        本助手接管（错误兜底、建档/切换/退出落点、课中引导一律回这里）。"""
        student = self.current_student(open_id)
        if student:
            return self._course_pick_card(student)
        return self._student_pick_card(None)

    def _course_pick_card(self, student_id: str) -> dict:
        """选课卡 = 聊天里的主页（拓扑 v2，2026-09-23 用户确认），即网页端
        coursePicker：单元列表点开即上课，创建教材/自由对话/选老师/换学生
        全部住在这张卡上——与 GUI 的层级一致，不再有「什么都能点的面板」。
        单元列表必须走 list_units_summary()：引擎目录里 units 是以 unit_id
        为键的字典，直接迭代拿到的是字符串（2026-09-23 真机崩溃根因）。"""
        from ...student import student_store
        try:
            profile = student_store.get_profile(student_id)
            profile.load()
            progress_map = profile.course_progress or {}
        except Exception:
            progress_map = {}
        names = course_engine.get_textbook_names()
        rows = []
        if not names:
            rows.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": "还没有教材。点下面「📖 管理教材」进入管理面"
                                   "建一本，或在网页端「我的教材」建好。"}})
        for bi, name in enumerate(names, 1):
            title = course_engine.book_title(name) or name
            units = course_engine.list_units_summary(name)
            rows.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": f"**📖 {title}**（{len(units)} 单元）"}})
            for ui, unit in enumerate(units, 1):
                uid = unit.get("unit_id") or ""
                utitle = unit.get("title") or uid
                entry = progress_map.get(f"{name}/{uid}") or {}
                pct = entry.get("progress")
                if isinstance(pct, int):
                    meta = f"进度 {pct}%"
                elif entry.get("sessions"):
                    meta = "✅ 练过"
                else:
                    meta = "○"
                rows.append({"tag": "action", "actions": [
                    _button(f"{utitle} · {meta}", f"上课 {bi}-{ui}")]})
        rows.append({"tag": "action", "actions": [
            _button("📖 管理教材", "教材管理", "primary"),
            _button("💬 自由对话", "自由对话"),
        ]})
        rows.append({"tag": "action", "actions": [
            _button("👩‍🏫 选老师", "选老师"),
            _button("👤 换学生", "学生"),
        ]})
        rows.append({"tag": "action", "actions": [
            _link_button("📞 联系开发者", _CONTACT_URL),
            _button("🔄 检查更新", "检查更新"),
            _button("⚙️ 模型配置", "配置"),
        ]})
        rows.append({"tag": "note", "elements": [
            {"tag": "plain_text",
             "content": "点单元 = 开始这一课；「上课」不带参数 = 从上次练的"
                        "单元继续。这张卡就是主页（网页端选课页的等价）。"}]})
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": "📚 选课主页"},
                       "template": "blue"},
            "elements": rows,
        }

    def _teacher_pick_card(self, student_id: str) -> dict:
        """选老师/风格卡 = 网页端高级设置的等价：点老师记住该老师（风格
        回落老师默认），点风格只改风格——与 change → teacher-choice 同语义。"""
        from ...teacher import teacher_store
        teachers = teacher_store.list_teachers()
        current_t, current_s, _ = (teacher_store.resolve_for_student(student_id)
                                if student_id else (None, None))
        rows = []
        for i in range(0, len(teachers), 3):
            row = []
            for t in teachers[i:i + 3]:
                is_cur = current_t is not None and t["id"] == current_t.id
                label = f"✓ {t['name']}" if is_cur else t["name"]
                row.append(_button(label, f"设老师 {t['id']}",
                                   "primary" if is_cur else "default"))
            rows.append({"tag": "action", "actions": row})
        style_row = []
        for sid, preset in config.STYLE_PRESETS.items():
            is_cur = current_s == sid
            label = f"✓ {preset['label']}" if is_cur else preset["label"]
            style_row.append(_button(label, f"设风格 {sid}",
                                     "primary" if is_cur else "default"))
        rows.append({"tag": "action", "actions": style_row})
        cur_name = current_t.name if current_t else "—"
        style_label = (config.STYLE_PRESETS.get(current_s or "") or {}).get(
            "label", current_s or "—")
        rows.append({"tag": "note", "elements": [
            {"tag": "plain_text",
             "content": f"当前：{cur_name} · {style_label}。"
                        "换老师会重置风格为该老师默认；选择会被记住，"
                        "下次开课沿用（与网页端一致）。"}]})
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": "👩‍🏫 选老师 / 风格"},
                       "template": "purple"},
            "elements": rows,
        }

    async def _handle_set_teacher(self, sess: _UserSession, open_id: str,
                                  text: str):
        """「设老师 {id}」——卡片按钮或打字；与网页 teacherSelect change 同。"""
        from ...teacher import teacher_store
        if await self._refuse_while_active(sess, open_id, "换老师"):
            return
        teacher_id = text[3:].strip() if text.startswith("设老师") else text.strip()
        try:
            teacher = teacher_store.get_teacher(teacher_id)
        except Exception:
            # 陈旧卡点到已删老师（#25）：错误文案后跟老师卡，不教打字
            await self._send_text(
                open_id, "没有这位老师——下面是现有老师，重新点选：")
            await self._send_card(open_id, self._teacher_pick_card(
                self.current_student(open_id)))
            return
        student_id = self.current_student(open_id)
        # 换老师：remember_choice 会把风格重置为该老师默认（与网页一致）。
        teacher_store.remember_choice(student_id, teacher.id, None)
        _, style_id, _ = teacher_store.resolve_for_student(student_id)
        style_label = (config.STYLE_PRESETS.get(style_id) or {}).get(
            "label", style_id)
        await self._send_text(
            open_id, f"✅ 当前老师：{teacher.name} · {style_label}（已记住）。"
                     f"发「上课」/「选课」开始，或再点风格微调。")
        await self._send_card(open_id, self._teacher_pick_card(student_id))
        await self._send_card(open_id, self._home_card(open_id))

    async def _handle_set_style(self, sess: _UserSession, open_id: str,
                                text: str):
        """「设风格 {style_id}」——只改风格，老师不动（与网页 styleSelect 一致）。"""
        from ...teacher import teacher_store
        if await self._refuse_while_active(sess, open_id, "设风格"):
            return
        raw = text[3:].strip() if text.startswith("设风格") else \
            (text[4:].strip() if text.startswith("设定风格") else text.strip())
        style_id = raw
        if raw not in config.STYLE_PRESETS:
            # 容忍用中文标签打字：温柔耐心 → gentle …
            style_id = next(
                (sid for sid, p in config.STYLE_PRESETS.items()
                 if p["label"] == raw or sid == raw), "")
        if not style_id:
            labels = "、".join(p["label"] for p in config.STYLE_PRESETS.values())
            await self._send_text(
                open_id, f"没有这种风格。可选：{labels}——在下面卡上点选：")
            await self._send_card(open_id, self._teacher_pick_card(
                self.current_student(open_id)))
            return
        student_id = self.current_student(open_id)
        teacher, _, _ = teacher_store.resolve_for_student(student_id)
        teacher_store.remember_choice(student_id, teacher.id, style_id)
        style_label = config.STYLE_PRESETS[style_id]["label"]
        await self._send_text(
            open_id, f"✅ 风格：{style_label}（老师仍为 {teacher.name}，已记住）。")
        await self._send_card(open_id, self._teacher_pick_card(student_id))
        await self._send_card(open_id, self._home_card(open_id))

    def _try_send_text(self, open_id: str, text: str):
        try:
            self.client.send_text(open_id, text)
        except Exception as e:
            logger.error(f"[FEISHU] fallback send failed: {e}")


def _lesson_report_text(ev: dict) -> str:
    used = ev.get("vocab_used") or []
    missed = ev.get("vocab_missed") or []
    lines = [
        f"📋 本课结束！共 {ev.get('total_turns', 0)} 轮，"
        f"时长 {ev.get('duration_minutes', 0)} 分钟。",
        f"词汇覆盖：{ev.get('accuracy', 0.0):.0%}",
    ]
    if used:
        lines.append("说出来的词：" + "、".join(used[:12]))
    if missed:
        lines.append("还没说到的词：" + "、".join(missed[:12])
                     + "（下次课会再出现）")
    return "\n".join(lines)


def _free_summary_text(ev: dict) -> str:
    lines = [f"💬 自由对话结束！共 {ev.get('total_turns', 0)} 轮，"
             f"时长 {ev.get('duration_minutes', 0)} 分钟。"]
    if ev.get("topics"):
        lines.append("聊到的话题：" + "、".join(ev["topics"][:8]))
    if ev.get("summary"):
        lines.append(str(ev["summary"]))
    return "\n".join(lines)
