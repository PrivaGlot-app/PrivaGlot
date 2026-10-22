"""智谱 GLM 供应商档案 —— 多厂商时代第二家（#15，2026-10-21 接入）。

与 mimo.py 同构：本文件是「智谱怎么接、怎么注册、怎么说」的唯一编辑处。
与小米的两点形态差异：
  - Key 形如 id.secret，无前缀语义（不走路由，直连固定 base_url）；
  - 计费颗粒度按模型而异：glm-4.7-flash 免费、glm-tts 按字符（约 2 元/万
    字符，token 资源包盖不住它——2026-10-21 实测踩坑，见开发账本 #15 备忘）。

协议事实全部实测过（探针 C:\\tool\\zhipu-probe\\，2026-10-21 七轮）：
GLM 免费档 1305 拥塞常态；5.x 强制思考（1210）；ASR 音频 ≤30s（1214）。
Phase 1 只落 llm 段；tts/asr 段随各自分期迁入（实测结论暂存开发账本）。
"""

PROFILE = {
    "id": "zhipu",
    "display_name": "智谱 GLM",
    "short_name": "GLM",

    # 智谱无通道路由：Key 不分前缀，直连固定地址。channels 保留单条以兼容
    # 档案契约；空 prefix 匹配任意 Key，base_url_for_key 对智谱返回同一地址。
    "channels": [
        {"prefix": "", "label": "标准接入",
         "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    ],

    # 注册入口。signup_url 带邀请参数（归属只靠链接参数，不单独展示邀请码，
    # 与 mimo.py 同约定）；platform_url 是不带参数的裸平台地址。
    "signup_url": "https://www.bigmodel.cn/invite?icode=cVdnXnIWp4Vmra3m5gwo0EjPr3uHog9F4g5tjuOUqno%3D",
    "platform_url": "https://open.bigmodel.cn",

    # 旧消费方兼容键（现役只读默认厂商 mimo；此处齐备以过档案契约）。
    "models": {
        "llm": "glm-4.7-flash",
        "asr": "glm-asr-2512",
        "tts": "glm-tts",
    },

    # ------------------------------------------------------------------
    # LLM 模态说明书（v2）。两个可选模型 quirks 不同，逐模型声明：
    # 4.7 必须显式关思考（默认开，思考会烧 token）；5.x 必须不发思考参数
    # （发了 1210）且 max_tokens 放宽（思考 116 token 起步，预算小则空回复）。
    # ------------------------------------------------------------------
    "llm": {
        "protocol": "openai_chat",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "key_env": "ZHIPU_API_KEY",
        "models": [
            {"id": "glm-4.7-flash",
             "label": "GLM-4.7-Flash",
             "note": "限时免费，不消耗 token；高峰期可能拥堵（自动重试 2 次）",
             "quirks": {
                 "thinking": "disabled_param",
                 "max_tokens_param": "max_tokens",
             }},
            {"id": "glm-5.3-flash",
             "label": "GLM-5.3-Flash",
             "note": "强制思考（不可关闭）：思考也计 token，回合预算已自动放宽；按量计费",
             "quirks": {
                 "thinking": "omit",
                 "max_tokens_param": "max_tokens",
                 "min_max_tokens": 1024,
             }},
        ],
        # 免费档 1305「访问量过大」实测常态 → 窄豁免：仅此码自动重试 2 次
        "transient_retry": {"codes": ["1305"], "max_attempts": 2, "backoff_s": 3},
        "errors": {
            "429:1113": "no_quota",
            "402": "no_quota",
            "403:1220": "forbidden",
            "400:1211": "model_missing",
            "404": "model_missing",
            "400:1214": "invalid_request",
        },
        "no_key_hint": "⚠️ 还没有配置智谱 GLM 的 API Key。网页端在模型配置页"
                       "填写，飞书端发「配置」选厂商（对应环境变量"
                       " ZHIPU_API_KEY）。",
    },

    # ------------------------------------------------------------------
    # TTS 模态说明书（v2，Phase 2 迁入）。真逐字朗读器（ASR 回环 0.97×7
    # 实测，无对话抢话）；语速真参数（0.5–2 实测单调有效）→ 风格三档参数化；
    # 按字符计费（约 2 元/万字符）——token 资源包盖不住它（1113 真因）。
    # ------------------------------------------------------------------
    "tts": {
        "protocol": "openai_speech",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "key_env": "ZHIPU_API_KEY",
        "models": [
            {"id": "glm-tts",
             "label": "GLM-TTS",
             "note": "按字符计费：2 元/万字符；单次输入 ≤1024 字符",
             "quirks": {}},
        ],
        "voices": ["tongtong", "jam", "xiaochen", "chuichui",
                   "kazi", "douji", "luodo"],
        "default_voice": "tongtong",
        "default_teacher": "tongtong",
        # 风格渲染：speed = 风格三档映射为语速参数（未知风格 = 1.0）
        "style_render": {"mode": "speed",
                         "gentle": 0.85, "calm": 1.0, "lively": 1.15},
        "transient_retry": {"codes": [], "max_attempts": 1, "backoff_s": 3},
        "errors": {
            "429:1113": "no_quota",
            "402": "no_quota",
            "403:1220": "forbidden",
            "400:1214": "invalid_request",
            "404": "model_missing",
        },
        "limits": {"max_input_chars": 1024},
        "billing": "characters",
        "no_key_hint": "⚠️ 还没有配置智谱 GLM 的 API Key。网页端在模型配置页"
                       "填写，飞书端发「配置」选厂商（对应环境变量"
                       " ZHIPU_API_KEY）。",
    },

    # ------------------------------------------------------------------
    # ASR 模态说明书（v2，Phase 3）。英语多口音实测可用（美式/英式）；
    # 音频 ≤30 秒硬限（1214 超限明确报错）；hotwords 热词表 ≤100（实测
    # 接受逗号分隔字符串）；≈0.0002 元/秒。
    # ------------------------------------------------------------------
    "asr": {
        "protocol": "openai_transcriptions",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "key_env": "ZHIPU_API_KEY",
        "models": [
            {"id": "glm-asr-2512",
             "label": "GLM-ASR-2512",
             "note": "英语多口音；音频 ≤30 秒硬限；16 元/百万 Tokens（约 0.0002 元/秒）",
             "quirks": {}},
        ],
        "languages": ["auto"],
        "limits": {"max_audio_seconds": 30},
        "hotwords": {"max": 100},
        "transient_retry": {"codes": [], "max_attempts": 1, "backoff_s": 3},
        "errors": {
            "429:1113": "no_quota",
            "402": "no_quota",
            "403:1220": "forbidden",
            "400:1214": "invalid_request",
            "404": "model_missing",
        },
        "no_key_hint": "⚠️ 还没有配置智谱 GLM 的 API Key。网页端在模型配置页"
                       "填写，飞书端发「配置」选厂商（对应环境变量"
                       " ZHIPU_API_KEY）。",
    },

    # ------------------------------------------------------------------
    # 引导/话术素材。Phase 1 的网页引导层仍只渲染默认厂商（读法B 过渡期），
    # 这里齐备是为档案契约与配置页期备料；文案届时随三列配置 UI 一起打磨。
    # ------------------------------------------------------------------
    "guide": {
        "title_first": "🔑 先填一把智谱 GLM API Key",
        "title_replace": "🔑 换一把智谱 GLM API Key",
        "intro": "对话由智谱 GLM 云端完成，所以需要一把 Key。",
        "preamble": "注册需手机号，建议完成实名认证以解锁全部模型与赠送额度。",
        "steps": [
            "**打开注册页** —— 用手机号注册/登录 open.bigmodel.cn",
            "进「**API Keys**」→「**新建 API Key**」，名称随便填（如 PrivaGlot）",
            "复制整串 Key（形如 `id.secret`，**只显示这一次**）",
        ],
        "signup_label": "打开注册页",
        "input_placeholder": "在这里按 Ctrl+V 粘贴智谱 API Key（id.secret 形态）",
        "pricing_note": "对话模型 glm-4.7-flash 限时免费，不消耗 token；语音类模型单独计费。",
        "bonus_note": "新用户有机会获赠 tokens 组合资源包，覆盖范围以平台页面为准。",
        "topup_note": "余额不足时到 智谱控制台 → 费用中心 在线充值即可。",
        "verify_busy": "正在验证…（会真调一次智谱接口，通常几秒钟）",
    },

    "verify": {
        "bad_key": "智谱 API Key 无效（401）。请回 open.bigmodel.cn 重新创建一个再填。",
        "no_quota": ("智谱账户余额/资源包不足（429/1113）。对话模型 glm-4.7-flash 免费不消耗"
                     "额度；语音合成按字符计费（约 2 元/万字符），token 资源包覆盖不到它 ——"
                     "请到智谱控制台确认余额与资源包，或换一个模型（网页端在模型配置页，"
                     "飞书端发「配置」）。"),
        "forbidden": "这把 Key 没有该模型的调用权限（403/1220）。请换一个模型"
                     "（网页端在模型配置页，飞书端发「配置」）。",
        "server_error": "智谱服务端错误（{code}）。稍后重试即可。",
        "no_network": "连不上智谱接口（{err}）。请检查网络或代理设置。",
        "prefix_hint": "智谱 Key 形如 id.secret（无 sk- 前缀），请整串复制粘贴。",
        "model_missing": "该智谱模型不存在或无权限（1211/1220）。请换一个模型"
                         "（网页端在模型配置页，飞书端发「配置」）。",
    },
}
