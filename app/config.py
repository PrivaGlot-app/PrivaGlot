"""
Configuration module for PrivaGlot
"""
import os
from pathlib import Path
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent.parent


def _load_env_files(root_env: Path, data_env: Path) -> None:
    """两层加载，后读的赢（override=True，同名键后者覆盖前者，且都盖过
    系统环境变量）。拆成函数是为了让优先级可被测试锁定。"""
    load_dotenv(root_env, override=True)
    load_dotenv(data_env, override=True)


#   1) 项目根 .env —— 部署/开发配置（飞书凭据、模型名覆盖等）；本机形态在
#      老位置手工填的 MIMO_API_KEY 继续有效（兼容读法）。
#   2) data/.env —— 网页引导层 / 飞书卡片「填 Key」的写入落点，运行期权威。
#      Docker 形态下 data/ 是唯一挂载卷，凭据只有落这里才能在容器升级
#      重建（rm+run）后存活 —— 曾写容器内 /app/.env（可写层），升级即丢
#      且无任何报错（问题清单 #9，2026-10-07 修复）。
_load_env_files(_PROJECT_ROOT / ".env", _PROJECT_ROOT / "data" / ".env")

# 运行期凭据的读写落点（_persist_env / apply_api_key 用）。
_ENV_PATH = _PROJECT_ROOT / "data" / ".env"

# 供应商事实（通道、注册链接、模型名默认值）的唯一来源是 app/vendors/。
# 这里只保留旧常量名做兼容引用，改小米的活动/文案请去 app/vendors/mimo.py。
from .vendors import active as _vendor_active, base_url_for_key as _base_url_for_key  # noqa: E402
from . import vendors as _vendors  # noqa: E402

_VENDOR = _vendor_active()
PAYG_BASE_URL = _VENDOR["channels"][0]["base_url"]
PLAN_BASE_URL = _VENDOR["channels"][1]["base_url"]
REFERRAL_URL = _VENDOR["signup_url"]

# LLM Settings (Xiaomi MiMo)
# Two channels, identical models, different billing and different credentials:
#   按量付费 (default)  https://api.xiaomimimo.com/v1              key sk-xxxxx
#   Token Plan          https://token-plan-cn.xiaomimimo.com/v1    key tp-xxxxx
# The pay-as-you-go channel is the default on purpose. A Token Plan
# subscription is licensed for coding / agent workloads; running a tutoring
# service on top of it is not what the plan is sold for. Token Plan stays
# supported for developers who want it — see README, "Token Plan 接入".
# 下面这些值在运行期可能被 apply_api_key() 改写（用户在网页里填 Key），
# 所以用到它们的地方都要现读，不要缓存进对象属性 —— 见 llm.py / asr.py / tts.py。
LLM_API_KEY = os.getenv("MIMO_API_KEY", "")
LLM_BASE_URL = os.getenv("MIMO_BASE_URL", PAYG_BASE_URL)
LLM_MODEL = os.getenv("LLM_MODEL", "") or _VENDOR["models"]["llm"]

# ASR Settings (Cloud - MiMo only, no local whisper)
ASR_API_KEY = LLM_API_KEY  # Same key
ASR_BASE_URL = LLM_BASE_URL
ASR_MODEL = os.getenv("MIMO_ASR_MODEL", "") or _VENDOR["models"]["asr"]

# TTS Settings (Cloud - MiMo only, no Edge fallback)
TTS_API_KEY = LLM_API_KEY
TTS_BASE_URL = LLM_BASE_URL
TTS_MODEL = os.getenv("MIMO_TTS_MODEL", "") or _VENDOR["models"]["tts"]
# NOTE: TTS voice and delivery style come from the selected teacher profile
# (app/bundled_teachers/, overridable in data/teachers/), not from here.


def is_configured() -> bool:
    """有没有一把真 Key。占位符等同于没填。"""
    key = (LLM_API_KEY or "").strip()
    return bool(key) and key != "your_mimo_api_key_here"


def modalities_ready() -> bool:
    """能力判定（#15④ 飞书闸门，2026-10-22 用户裁定）：三模态各自解析出的
    Key 均非空——只关心「能不能把课上起来」，不要求显式选择（读法B 的默认
    回落算数）。与网页端 configured=显式配置 的差异：飞书没有「首屏强推一次
    配置」的机制；旧部署（只有 MIMO_API_KEY）三模态同回落小米，天然过闸
    零感知。"""
    for mod in ("llm", "tts", "asr"):
        sel = getattr(_vendors, f"{mod}_selection")()
        if not (sel.get("api_key") or "").strip():
            return False
    return True


def base_url_for_key(key: str) -> str:
    """Key 前缀就是通道（sk- 按量付费默认，tp- Token Plan）。见 app/vendors/。"""
    return _base_url_for_key(key)


def _persist_env(values: dict) -> None:
    """就地改 data/.env（运行期凭据的权威文件）：同名的键替换，缺的追加，
    注释和其它键原样保留。"""
    lines = []
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8-sig").splitlines()
    remaining = dict(values)
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name = stripped.split("=", 1)[0].strip()
            if name in remaining:
                out.append(f"{name}={remaining.pop(name)}")
                continue
        out.append(line)
    out.extend(f"{name}={value}" for name, value in remaining.items())
    _ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def apply_api_key(key: str) -> str:
    """运行期换 Key：改内存里的配置 + 写回 data/.env（挂载卷，容器重建不丢），
    返回最终用的 BASE_URL。

    网页上填 Key 走的就是这里 —— 服务不用重启，也不用用户去命令行里敲。
    """
    global LLM_API_KEY, ASR_API_KEY, TTS_API_KEY
    global LLM_BASE_URL, ASR_BASE_URL, TTS_BASE_URL

    key = (key or "").strip()
    base = base_url_for_key(key)
    LLM_API_KEY = ASR_API_KEY = TTS_API_KEY = key
    LLM_BASE_URL = ASR_BASE_URL = TTS_BASE_URL = base
    os.environ["MIMO_API_KEY"] = key
    os.environ["MIMO_BASE_URL"] = base
    # DEBUG_PROMPTS 永远是 0：它会把学生原话写进 data/prompt_logs/，与隐私定位冲突
    _persist_env({"MIMO_API_KEY": key, "MIMO_BASE_URL": base, "DEBUG_PROMPTS": "0"})
    return base

def apply_modality_config(spec: dict) -> dict:
    """配置页保存（#15 配置页期，2026-10-21）：三模态显式配置的应用+持久化。
    spec = {modality: {vendor, model, key}}，调用方（/api/setup/config）已
    全部探活通过才走到这里。键按厂商落到档案声明的 key_env，同厂商多模态
    共用一把（同名键覆盖即去重）；被换掉的旧厂商 Key 留在 data/.env 不删
    （显式厂商键优先，切回即用）。返回写盘的键值表（测试/展示用）。"""
    env_changes = {}
    for mod in ("llm", "tts", "asr"):
        entry = (spec or {}).get(mod) or {}
        vendor_id = str(entry.get("vendor") or "").strip()
        model = str(entry.get("model") or "").strip()
        key = str(entry.get("key") or "").strip()
        if not vendor_id or not model:
            continue
        sec = _vendors._REGISTRY.get(vendor_id, {}).get(mod) or {}
        env_changes[f"{mod.upper()}_VENDOR"] = vendor_id
        env_changes[f"{mod.upper()}_MODEL"] = model
        key_env = sec.get("key_env")
        if key_env and key:
            env_changes[key_env] = key
    if not env_changes:
        return {}
    for name, value in env_changes.items():
        os.environ[name] = value
    # DEBUG_PROMPTS 永远是 0：它会把学生原话写进 data/prompt_logs/，与隐私定位冲突
    _persist_env({**env_changes, "DEBUG_PROMPTS": "0"})
    return env_changes


def vendor_key_env(vendor_id: str) -> str:
    """厂商级 Key 的环境变量名（该厂商任一模态档案声明的第一个）。"""
    for mod in ("llm", "tts", "asr"):
        sec = (_vendors._REGISTRY.get(vendor_id) or {}).get(mod) or {}
        if sec.get("key_env"):
            return sec["key_env"]
    raise ValueError(f"未知厂商: {vendor_id}")


def set_vendor_key(vendor_id: str, key: str) -> str:
    """配置页「保存 Key」（Key 管理与三列保存解耦，#15）：按厂商写 key_env
    并持久化。调用方（/api/setup/vendor-key）已做过一次轻探活。返回 env 名。"""
    key_env = vendor_key_env(vendor_id)
    os.environ[key_env] = key
    # DEBUG_PROMPTS 永远是 0（隐私不变量）
    _persist_env({key_env: key, "DEBUG_PROMPTS": "0"})
    return key_env


def masked_key(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 12:
        return "•" * len(key)
    return key[:6] + "…" + key[-4:]


# Server Settings
# 网页只属于本机：绑定钉死回环，不从 .env 读 —— 不提供改绑定的入口，
# 「误绑 0.0.0.0 把无 TLS 的网页暴露给局域网」便无从发生（2026-10-07 定调：
# 软路由/局域网网页方案废除，非本机场景一律走飞书形态）。
SERVER_HOST = "127.0.0.1"
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

# LLM streaming limits — prevent runaway generation
LLM_MAX_TOKENS_PER_TURN = int(os.getenv("LLM_MAX_TOKENS_PER_TURN", "300"))

# Audio Settings
SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "16000"))
CHANNELS = int(os.getenv("CHANNELS", "1"))

# Debug prompt logging
DEBUG_PROMPTS = os.getenv("DEBUG_PROMPTS", "0").strip().lower() in ("1", "true", "yes")

# Directory paths
DATA_DIR = _PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
SESSIONS_DIR = DATA_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
PROMPT_LOG_DIR = DATA_DIR / "prompt_logs"
TEACHERS_DIR = DATA_DIR / "teachers"

# App version（#13 升级闭环）：发布镜像在构建期烤入（Dockerfile ARG/ENV，
# tools/build_image.sh 传 --build-arg，与 org.opencontainers.image.version
# label 同源，verify_image 核对两者一致）；本机源码运行没有它 = "dev"，
# 飞书通道据此跳过在线版本检查。版本标记文件给「升级完成推卡」做 diff：
# 容器重建后首启对比上一版本号，变更即通知（data/ 是唯一重建存活的挂载卷）。
APP_VERSION = os.getenv("APP_VERSION", "dev").strip() or "dev"
APP_VERSION_MARKER = DATA_DIR / ".app_version"

# === Feishu channel (v4, see app/channels/feishu/ and docs/飞书接入.md) ===
# 企业自建应用的凭据（开放平台 → 凭证与基础信息）。留空 = 飞书通道不启动。
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
# 飞书（境内）与 Lark（海外）同构不同域名；按需切换。
FEISHU_DOMAIN = os.getenv("FEISHU_DOMAIN", "https://open.feishu.cn").strip().rstrip("/")
# open_id → student_id 绑定文件（首次联系人自动建档后写在这里）
FEISHU_BINDINGS_PATH = DATA_DIR / "feishu_bindings.json"
# 会话空闲超过该秒数后，下一条消息先自动下课（结算+评估）再开新课
FEISHU_SESSION_IDLE_TIMEOUT = int(os.getenv("FEISHU_SESSION_IDLE_TIMEOUT", "7200"))
# 管理指令（状态/换Key/教材管理）只认这个 open_id。扫码部署时由
# bootstrap 从 SDK 回传的 user_info 自动写入；手动部署直接填这个值。
# （「绑定管理员」聊天指令已退役，见 app/channels/feishu/channel.py。）
FEISHU_ADMIN_OPEN_ID = os.getenv("FEISHU_ADMIN_OPEN_ID", "").strip()

# 跨进程会话互斥锁目录（core/session_registry.py 按 student_id 持有 OS
# 文件锁；both 部署是两个进程，内存表互相看不见，靠它守住"同一份进度"）
SESSION_LOCKS_DIR = DATA_DIR / "session_locks"

# === Teacher settings ===
# One teacher per MiMo preset English voice: a teacher's identity IS the voice
# name, so no separate persona name is invented. Each preset carries that voice's
# default delivery style. Presets live in app/bundled_teachers/ and ship with the
# code, so a bind mount over data/ cannot hide them; data/teachers/ holds only what
# you add yourself. `_active.json` is runtime state (which teacher each student
# picked) and carries student ids — it is gitignored, and the loader skips every
# file whose name starts with "_".
DEFAULT_TEACHER_ID = "dean"

# Preset voices offered by mimo-v2.5-tts. The model also ships Chinese voices
# (冰糖/茉莉/苏打/白桦), deliberately not exposed: the English-accent reading of
# a Chinese sentence is the "real foreign teacher" feel this project keeps.
ALLOWED_VOICES = ("Mia", "Chloe", "Milo", "Dean")
DEFAULT_VOICE = "Dean"

# Delivery style — one dimension, three tiers. MiMo exposes no numeric speed
# knob, so a tier is a natural-language cue. It is injected into BOTH the LLM
# prompt (shapes the wording the model writes) and the TTS instruction
# (directs how that same text is read); sending it to only one of the two
# makes the text and the delivery fight each other.
STYLE_PRESETS = {
    "gentle": {
        "label": "温柔耐心",
        "llm": "gentle and patient; warm, unhurried wording",
        "tts": "Read slowly and gently, with patience.",
    },
    "calm": {
        "label": "平静沉稳",
        "llm": "calm and steady; clear, even wording",
        "tts": "Read at a natural, steady pace.",
    },
    "lively": {
        "label": "活泼热情",
        "llm": "lively and enthusiastic; upbeat wording",
        "tts": "Read with bright, energetic delivery.",
    },
}
DEFAULT_STYLE = "calm"

# === Lesson System Prompt Template (v3 — unit-based, no completion judgment) ===
# The bound unit's [UNIT MATERIAL] block is rendered into {unit_block}.
# Unit-specific teaching guidance lives inside the unit block, so these base
# rules stay content-agnostic.
SYSTEM_PROMPT_TEMPLATE = """You are "{teacher_name}", a warm ESL teacher chatting with {student_name}, age {student_age}, language background: {language_background}.
Your delivery style: {style_line}.
{unit_block}
[RULES]
1. English only. Plain text only — this is voice chat, never use markdown (**bold**, lists, etc).
2. CORRECTION COMES FIRST. Start every turn by checking the student's last sentence for a clear language error (grammar, word choice, word form). If there is one: briefly acknowledge what they meant, then naturally model the correct form, then move on. Only the most important error, one per turn — no grammar lectures. Never say "that's wrong".
3. If you cannot tell what the student meant (it may be a mishearing), do not guess a correction — ask "Do you mean ...?" instead.
4. Turn shape: [correction if needed] + [one short praise like "Nice!"] + [your question]. Praise is optional and correction replaces it when space is tight; the question is mandatory every turn.
5. Keep turns short: 1-3 short sentences, usually under 20 words. You may use up to 30 words on a turn that corrects an error. A real teacher is brief by default and says more only when it matters.
6. If the student speaks Chinese, briefly answer in Chinese, then continue in English.
7. Never say goodbye or try to end the conversation — the student decides when to stop.
8. Stay in your teacher role: never recite numbers, lists, song lyrics, or long stories; warmly steer any request that is not conversation back to the chat.
"""

# === Free Conversation Prompt (fully decoupled from the curriculum) ===
# A companion chat driven by the student's interests and the teacher's memory
# of recent conversations ({memory_block}). No unit material, no
# correction-first turn shape, no word budget — the goal is keeping the
# student happily talking, not practicing specific material.
FREE_SYSTEM_PROMPT_TEMPLATE = """You are "{teacher_name}", a friendly English-speaking companion chatting with {student_name}, age {student_age}, language background: {language_background}.
Your delivery style: {style_line}.
{memory_block}
[RULES]
1. English only. Plain text only — this is voice chat, never use markdown (**bold**, lists, etc).
2. This is casual conversation, not a lesson. Make it fun: follow the student's interests, ask curious follow-up questions, react like a real friend, and now and then suggest a related new topic.
3. Light-touch correction only: when something is hard to understand or a very basic error appears, naturally weave the correct form into your reply ("Oh, you went there? Cool!"). Never interrupt the flow, never say "that's wrong", no grammar lectures — most mistakes you simply let pass.
4. Keep turns natural: usually 1-3 short sentences. Ask at least one question most turns, but a pure reaction without a question is fine now and then.
5. If the student speaks Chinese, briefly answer in Chinese, then continue in English.
6. Never say goodbye or try to end the conversation — the student decides when to stop.
7. Stay in your role: never recite numbers, lists, song lyrics, or long stories; warmly steer any request that is not conversation back to the chat.
8. No textbooks, no homework, no quizzes — you remember what the student likes and you keep them talking about it.
"""
