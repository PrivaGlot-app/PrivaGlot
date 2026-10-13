"""小米 MiMo 供应商档案 —— 全项目「小米怎么接、怎么注册、怎么说」的唯一编辑处。

改这里的任何一行之后：
  1. 网页引导层、飞书 Key 引导卡、验证错误文案在下次请求时自动生效（运行期
     现读，不用重启也不缓存）；
  2. README.md 的标记段落需要跑一次
     `python setup/sync_guidance.py` 刷新（静态文档没人给它发请求）；
  3. 发版前可跑 `python setup/sync_guidance.py --check` 断言文档没漂移。

guide 里文本的加粗统一用 **……** 约定：网页端转 <b>，飞书 lark_md 与
Markdown 原生支持。verify 里 {code}/{err}/{prefixes} 是 format 占位。
"""
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

    # 三模态模型名默认值；.env 的 LLM_MODEL / MIMO_ASR_MODEL / MIMO_TTS_MODEL
    # 仍可覆盖（键名是历史遗留，改了会碎掉存量用户的 .env，冻结）。
    # 2026-10-07：小米公告 mimo-v2.5(-pro) 于 2026-10-21 10:00 下线，LLM 默认值
    # 切到 mimo-v2.6-flash；ASR/TTS 不在下线名单，维持 v2.5（用户向平台核实）。
    "models": {
        "llm": "mimo-v2.6-flash",
        "asr": "mimo-v2.5-asr",
        "tts": "mimo-v2.5-tts",
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
    # 仍住在 verify_key.py，多厂商拆分时再随档案分化（问题清单 #15）。
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
