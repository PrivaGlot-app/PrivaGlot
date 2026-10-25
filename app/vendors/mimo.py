"""小米 MiMo 供应商档案 —— 全项目「小米怎么接、怎么注册、怎么说」的唯一编辑处。

改这里的任何一行之后：
  1. 网页引导层、飞书 Key 引导卡、验证错误文案在下次请求时自动生效（运行期
     现读，不用重启也不缓存）；
  2. README.md 的标记段落需要跑一次
     `python setup/sync_guidance.py` 刷新（静态文档没人给它发请求）；
  3. 发版前可跑 `python setup/sync_guidance.py --check` 断言文档没漂移。

guide 里文本的加粗统一用 **……** 约定：网页端转 <b>，飞书 lark_md 与
Markdown 原生支持。verify 里 {code}/{err}/{prefixes} 是 format 占位。

v2 多厂商（#15，2026-10-21 起）：档案开始承载「说明书」——每模态一段
（协议族/quirks/错误方言/重试豁免），由 app/engines/ 的协议积木消费。
Phase 1 只落 llm 段；asr/tts 段随各自分期迁入。
"""
_LLM_DEFAULT = "mimo-v2.6-flash"

PROFILE = {
    "id": "mimo",
    "display_name": "小米 MiMo",
    "short_name": "MiMo",

    # 两条通道：Key 前缀就是通道标识，列表第一条是默认通道。
    # （Token Plan 是给开发者的另一条计费通道，见 README「Token Plan 接入」。）
    "channels": [
        {"prefix": "sk-", "label": "按量付费",
         "base_url": "https://api.xiaomimimo.com/v1"},
        {"prefix": "tp-", "label": "Token Plan",
         "base_url": "https://token-plan-cn.xiaomimimo.com/v1"},
    ],

    # 注册入口。signup_url 带邀请参数（归属只靠链接参数，不单独展示邀请码）；
    # platform_url 是不带参数的裸平台地址（文档里「不想走邀请链接」的那条）。
    "signup_url": "https://platform.xiaomimimo.com?ref=F4DHWR",
    "platform_url": "https://platform.xiaomimimo.com",

    # 三模态模型名默认值（旧消费方读取的兼容键）；.env 的 LLM_MODEL /
    # MIMO_ASR_MODEL / MIMO_TTS_MODEL 仍可覆盖（键名是历史遗留，改了会碎掉
    # 存量用户的 .env，冻结）。llm 键与下方 llm 段同源于 _LLM_DEFAULT。
    # 2026-10-07：小米公告 mimo-v2.5(-pro) 于 2026-10-21 10:00 下线，LLM 默认值
    # 切到 mimo-v2.6-flash；ASR/TTS 不在下线名单，维持 v2.5（用户向平台核实）。
    "models": {
        "llm": _LLM_DEFAULT,
        "asr": "mimo-v2.5-asr",
        "tts": "mimo-v2.5-tts",
    },

    # ------------------------------------------------------------------
    # LLM 模态说明书（v2）。base_url=None = 走 channels 的 Key 前缀路由；
    # quirks/errors/transient_retry 的契约见 app/engines/llm_openai_chat.py。
    # 小米侧暂无已知瞬时拥塞码 → 重试列表留空（不重试）。
    # ------------------------------------------------------------------
    "llm": {
        "protocol": "openai_chat",
        "base_url": None,
        "key_env": "MIMO_API_KEY",
        "models": [
            {"id": _LLM_DEFAULT,
             "label": "MiMo v2.6 Flash",
             # 模型备注(note)＝配置页下拉里的说明文字：价格/政策变了只改这里，
            # 保存即生效（配置页现读 catalog，无需同步文档、无需重启）。
             "note": "按量计费，价格以小米控制台为准；新用户有机会获赠体验金",
             "quirks": {
                 "thinking": "disabled_param",
                 "max_tokens_param": "max_completion_tokens",
             }},
        ],
        "transient_retry": {"codes": [], "max_attempts": 1, "backoff_s": 3},
        "errors": {
            "402": "no_quota",
            "403": "forbidden",
            "404": "model_missing",
        },
        "no_key_hint": "⚠️ 还没有配置小米 API Key。请在浏览器里打开 http://localhost:8000 填写。",
    },

    # ------------------------------------------------------------------
    # TTS 模态说明书（v2，Phase 2 迁入）。生成式朗读协议：chat 形状 +
    # audio 参数；风格三档 = 自然语言提示（小米无语速旋钮，历史形态）。
    # 中文预置音色（冰糖/茉莉/苏打/白桦）刻意不暴露：中文的英文口音朗读
    # 正是「真外教」感（原 config.ALLOWED_VOICES 注释，随段迁入）。
    # ------------------------------------------------------------------
    "tts": {
        "protocol": "mimo_generative_chat",
        "base_url": None,
        "key_env": "MIMO_API_KEY",
        "models": [
            {"id": "mimo-v2.5-tts",
             "label": "MiMo v2.5 TTS",
             "note": "限时免费（以小米平台公告为准）",
             "quirks": {}},
        ],
        "voices": ["Mia", "Chloe", "Milo", "Dean"],
        "default_voice": "Dean",
        "default_teacher": "dean",
        # 风格渲染：cue = 把 config.STYLE_PRESETS[style]["tts"] 的自然语言
        # 提示拼进朗读指令
        "style_render": {"mode": "cue"},
        "transient_retry": {"codes": [], "max_attempts": 1, "backoff_s": 3},
        "errors": {
            "402": "no_quota",
            "403": "forbidden",
            "404": "model_missing",
        },
        "no_key_hint": "⚠️ 还没有配置小米 API Key。请在浏览器里打开 http://localhost:8000 填写。",
    },

    # ------------------------------------------------------------------
    # ASR 模态说明书（v2，Phase 3 迁入）。audio-in-chat 协议（chat 形状 +
    # input_audio）。官方文档无单次时长上限（格式限 mp3/wav）；2026-10-21
    # 官方文档页曾现「V2.5 系列已下线」横幅，API 实测 mimo-v2.5-asr 存活
    # （横幅按 LLM 下线口径误伤，探针定谳，勿据此改模型名）。
    # ------------------------------------------------------------------
    "asr": {
        "protocol": "mimo_audio_chat",
        "base_url": None,
        "key_env": "MIMO_API_KEY",
        "models": [
            {"id": "mimo-v2.5-asr",
             "label": "MiMo v2.5 ASR",
             "note": "0.5 元/小时（按音频时长计费）；无单次时长硬限（格式限 mp3/wav）",
             "quirks": {}},
        ],
        "languages": ["auto", "zh", "en"],
        "transient_retry": {"codes": [], "max_attempts": 1, "backoff_s": 3},
        "errors": {
            "402": "no_quota",
            "403": "forbidden",
            "404": "model_missing",
        },
        "no_key_hint": "⚠️ 还没有配置小米 API Key。请在浏览器里打开 http://localhost:8000 填写。",
    },

    # ------------------------------------------------------------------
    # 获取 Key 的引导素材（网页引导层 / 飞书引导卡 / 文档同步脚本共同渲染）。
    # 小米的活动、入口、价格随时会变 —— 变了只改这里，然后跑
    # `python setup/sync_guidance.py` 刷新 README 与图解里的标记段落。
    # 文案保持「表面中性」：不写「粘到下面」「三步搞定」这类绑定某个
    # 界面布局的措辞，各表面自己加过渡语。
    # ------------------------------------------------------------------
    "guide": {
        "title_first": "🔑 先填一把小米 MiMo API Key",
        "title_replace": "🔑 换一把小米 MiMo API Key",
        "intro": "语音识别、对话、语音合成都由小米 MiMo 云端完成，所以需要一把 Key。",
        "preamble": ("注册**免费**，**不需要购买页面上展示的任何订阅套餐**：若打开是"
                     "「订阅计划」页（$44/$88 那种），不用买，点右上角「**控制台**」"
                     "注册/登录就行。"),
        "steps": [
            "**打开注册页** —— 用手机号注册/登录；新用户**有机会**获赠体验金，"
            "能否到账以平台活动为准",
            "登录后进左侧「**API Keys**」→ 右上「**新建 API Key**」，名称随便填（如 PrivaGlot）",
            "复制那串 **sk-** 开头的字符（**只显示这一次**）",
        ],
        "signup_label": "打开注册页",
        "input_placeholder": "在这里按 Ctrl+V 粘贴 sk- 开头的 API Key",
        "pricing_note": "没领到体验金也不影响：小米 1 元起充，到 控制台 → 账户余额 先充 5 元就够开始。",
        # bonus/topup 是文档同步脚本的组合素材（README/图解用它们拼段落，
        # 网页与飞书只用上面 pricing_note 整句）
        "bonus_note": "新用户**有机会**获赠体验金，能否到账以平台活动页面为准。",
        "topup_note": "余额不足时到 控制台 → 账户余额 在线充值即可（1 元起充，先充 5 元就够开始）。",
        "verify_busy": "正在验证…（会真调一次小米接口，通常几秒钟）",
    },

    # ------------------------------------------------------------------
    # Key 验证失败时给非技术用户的中文说明（verify_key.py 渲染）。
    # 只放话术；「怎么探活」的协议（chat + audio 探测 TTS）是 MiMo 特有的，
    # 仍住在 verify_key.py，随配置页期分化为档案内探活描述（#15）。
    # ------------------------------------------------------------------
    "verify": {
        "bad_key": "API Key 无效或已失效（401）。请回平台重新创建一个 Key 再填一次。",
        "no_quota": ("账户余额不足（402）。体验金可能没到账或已过期 —— 到 控制台 → 账户余额 "
                     "在线充值即可（1 元起充，支持支付宝/微信，先充 5 元就够）。"),
        "forbidden": "这把 Key 没有调用权限（403）。请确认它属于你填的那条通道。",
        "server_error": "小米服务端错误（{code}）。稍后重试即可。",
        "no_network": "连不上小米接口（{err}）。请检查网络或代理设置。",
        "prefix_hint": "注意：Key 一般以 {prefixes} 开头，这串看起来不太对。",
    },
}
