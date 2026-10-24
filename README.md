<p align="center"><img src="static/logo-full.png" alt="PrivaGlot logo" width="360"></p>

# PrivaGlot

一个面向儿童/青少年的一对一 AI 英语口语陪练：学生开口说话，云端完成
语音识别 → 大模型生成教师回复 → 语音合成，像对讲机一样轮流对话。

**Priva**cy + Poly**glot** —— 学生档案、会话记录、教材内容全部只存在你自己的
机器上：本项目没有中心服务器，也没有遥测。语音识别与合成由你配置的云端模型
厂商完成（默认小米 MiMo，可在应用内「模型配置页」更换），哪些数据会离开本机
见「[数据与隐私](#数据与隐私)」。项目官网：**[privaglot.com](https://privaglot.com)**。

> **从哪开始？** 在自己的电脑上用 → 直接看下方「快速开始」，不需要服务器；
> 想让孩子在手机飞书上随时练 → 先看[从零购买服务器](#从零购买服务器)，再走一键部署；
> 只想先了解项目 → 逛逛[官网](https://privaglot.com)；
> **AI 助手/代理** → 直接读文末[给AI助手](#给ai助手)，以该章为部署的唯一事实源。

## 功能

- **课程模式**：按教材单元推进，纠正优先、短句为主、每轮必有一个问题。
- **课程续学**：每节课结束后由 AI 评估学习情况，自动决定下次要练的内容——
  没练过的下次一定出现，练过的由 AI 决定是否再现；单元以进度百分比展示。
  学到一半退出，再进来就是续学，不从头重复。
- **我的教材**：网页里创建和维护自己的课本——拍课本 → AI 提取 → 粘贴 → 保存，
  全程不接触 JSON 和命令行，保存即生效；飞书端同样支持卡片化管理教材。
- **自由会话模式**：完全脱离教材，围绕学生兴趣聊天，保留近期话题记忆。
- **学生档案**：姓名、年龄、语言背景、兴趣、单元进度与 AI 续学材料、最近话题。
- **教师档案**：教师目录随语音合成厂商走——小米 MiMo 提供 Dean / Chloe /
  Milo / Mia 四位，智谱提供 Tongtong 等七位；说话风格可按学生覆盖并记住。
- **词汇报告**：课后给出本单元词汇覆盖情况（纯本地统计，不调 LLM）。
- **模型配置**：语音识别、思考模型、语音合成三件套在应用内「模型配置页」
  各自选厂商与模型、填各自的 Key（默认小米 MiMo，可换智谱等），Key 掩码
  保存、随时更换；保存时每项强测 3 次，全过才生效。
- **多入口**：本机网页（全保真，语音为主——整轮回复逐句朗读，听不懂可点开
  原文）、飞书机器人（手机语音条）、终端 REPL——三者接入的是同一个对话内核。

## 快速开始（Windows）

### 1. 下载发行包并解压

打开 [Gitee 的 Release 页](https://gitee.com/priva-glot-app/PrivaGlot-app/releases/latest)
（自动定位到最新版本），下载 `privaglot-<版本号>-win64.zip`，解压到无需管理员
权限的目录（如 `D:\PrivaGlot`；避开 `C:\Program Files`——部署脚本要往安装目录
写文件）。

这是给使用者的推荐获取方式：包内自带 `VERSION`，以后**每次双击启动都会自动
检查更新**，发现新版自动下载升级，`data/` 里的数据与配置不受影响。GitHub 仓库
页面的 Code → Download ZIP 与 `git clone` 得到的是纯源码（无 `VERSION`），
**不会触发自动更新**，只适合开发者，见下方「源码运行」小节。

### 2. 双击「一键启动.bat」

自动安装 Python 依赖与 ffmpeg，启动服务并打开浏览器。首次约 5～10 分钟
（视网速），之后每次启动只需几秒。

### 3. 在模型配置页选厂商并填 Key

<!-- vendor-guide:readme-web-step begin -->
浏览器打开后会进入「模型配置页」——语音识别、对话、语音合成三件套各自选厂商、各填一把 Key。三件套默认都在 小米 MiMo 的云端完成，下文以它为例：点页面上的「↗ 打开 小米 MiMo 注册页」按钮，用手机号注册即可。注册**免费**，**不需要购买页面上展示的任何订阅套餐**：若打开是「订阅计划」页（$44/$88 那种），不用买，点右上角「**控制台**」注册/登录就行。新用户**有机会**获赠体验金，能否到账以平台活动页面为准。没领到体验金也不影响：余额不足时到 控制台 → 账户余额 在线充值即可（1 元起充，先充 5 元就够开始）。
<!-- vendor-guide:readme-web-step end -->

拿到 Key 后，回「模型配置页」把 Key 粘进对应厂商那一列（同一厂商只填一次），
点「**💾 保存并测活**」：保存时每项会真发极小的请求、强测 3 次，三次全过才
生效——Key 写错、余额不足、网络不通都会当场用中文说明原因，改好再来一次
即可，不需要重启服务。以后想换厂商、模型或 Key，点页面下方的「⚙️ API Key」
随时打开配置页。

<!-- vendor-guide:readme-channels begin -->
不想走邀请链接也行，直接访问 <https://platform.xiaomimimo.com> 注册，费用一样。

它用的是**按量付费**接口（Key 形如 `sk-xxxxx`）。作者自用的 Token Plan 是另一条通道，见下方「Token Plan 接入（开发者可选）」。
<!-- vendor-guide:readme-channels end -->

## 部署

| 形态 | 启动方式 | 域名/HTTPS/备案 | 适用 |
|---|---|---|---|
| 网页版（仅本机） | 「一键启动.bat」或 `python run.py` | 不需要（localhost 豁免） | 本机上课 |
| 飞书机器人 | `python run_feishu.py`（推荐走下方 VPS 一键部署） | 全不需要 | 学生手机随时练 |

两种形态核心依赖相同（Python 3.11+ / ffmpeg / requirements.txt / `.env`），
差异只在启动入口。网页服务把监听地址钉死在 `127.0.0.1`，因此**除本机外的
使用场景一律走飞书形态**——学生手机上的入口就是它。

### 从零购买服务器

飞书形态需要一台入门云服务器（2核2G 足够）。全程网页操作，不需要提前装任何东西：

1. **买服务器**：推荐腾讯云「轻量应用服务器」2核2G 档，购买入口：
   <https://curl.qcloud.com/6Gjk4JDU> 。下单时选三样：地域选离你近的（如上海/广州）；
   镜像选 **Ubuntu Server 22.04 LTS**（或 24.04）；套餐选 **2核2G**。其余默认即可。
   选境内厂商还有个实际好处：本项目镜像仓库在腾讯云，境内服务器拉取最快。
2. **打开终端**：买好后进入云控制台，找到你的服务器点「登录」，浏览器里直接出现
   网页终端，不需要安装任何 SSH 客户端。若登录用的不是 root 账户，先在终端执行
   `sudo -i` 切换到 root。
3. **粘贴一键命令**：回到下方「Linux VPS：飞书机器人（一键部署/升级）」，复制**整段**
   命令粘贴进终端回车，之后按提示用手机完成飞书授权即可。

> 也可以让腾讯云网页终端里的内置 AI 助手代劳：把 README 的**原文直链**发给它——
> https://gitee.com/priva-glot-app/PrivaGlot-app/raw/master/README.md （raw 地址
> 一次抓取必达；仓库主页是「网页外壳」，会挡住不执行 JavaScript 的抓取工具）——
> 并点名「按其中『Linux VPS：飞书机器人（一键部署/升级）』一节的整段命令部署」。
> 文末「[给AI助手](#给ai助手)」一章就是为它准备的。

### Linux VPS：飞书机器人（一键部署/升级）

学生在手机飞书里给机器人发语音即可上课，下课自动出报告和 AI 进度评估。
机器人通过**长连接（出站 WebSocket）**收发消息：不需要域名、HTTPS、备案，
也不需要任何公网入站端口，任意一台 2C/2G 的 VPS 都能跑。部署时缺应用会引导
**手机授权一键创建**（权限与事件订阅自动配好）。交互说明与排障见
**[docs/飞书接入.md](docs/飞书接入.md)**。

复制**整段**命令到 VPS 终端执行（自动装 Docker → 探活修复 → 拉镜像 → 授权 →
写入凭据 → 启动容器；装过的机器重跑同一条命令即为升级）：

```bash
( set -e; \
[ "$(id -u)" = 0 ] || { echo '需要 root：请先执行 sudo -i 切换到 root，再重新粘贴整段命令'; exit 1; }; \
[ "$(uname -m)" = x86_64 ] || { echo "仅支持 x86_64 服务器，当前 $(uname -m)"; exit 1; }; \
ok() { docker info >/dev/null 2>&1 && return 0; \
  systemctl reset-failed docker.socket docker.service 2>/dev/null || true; \
  systemctl restart docker 2>/dev/null || true; sleep 3; docker info >/dev/null 2>&1; }; \
command -v docker >/dev/null 2>&1 || { . /etc/os-release; case "$ID" in ubuntu|debian) ;; *) \
  echo "仅支持 Ubuntu/Debian（apt），当前系统 $ID。请先手动安装 Docker：https://docs.docker.com/engine/install/ 然后重跑本命令"; exit 1;; esac; \
  apt-get update && apt-get install -y docker.io; }; \
ok || { echo '! Docker 服务未就绪。请把 systemctl status docker docker.socket 和 journalctl -u docker -n 50 的输出发给项目作者排查'; exit 1; }; \
echo "环境就绪：$(docker --version)"; \
IMG=ccr.ccs.tencentyun.com/privaglot/privaglot_public:latest; D=/opt/privaglot; \
docker pull "$IMG" && mkdir -p "$D" && \
docker run --rm -v "$D:/work" "$IMG" cp /app/setup/deploy.sh /work/deploy.sh && \
bash "$D/deploy.sh" "$IMG" )
```

要点：

- **两段式设计**：粘贴的这段短引导只负责宿主机侧的事——把 Docker 弄到可用
  （没装就装、装了就实探服务、起不来先自动抢救）并拉取镜像；首次配置、数据
  备份、换容器、60 秒自检与失败回滚都由**随镜像分发的 `setup/deploy.sh`**
  执行（引导把它解压到 `/opt/privaglot` 后运行），安装逻辑与镜像永远同版本。
- **授权前提**：你的飞书账号名下要有**自己创建**的企业——纯个人账号（豆包账号）
  会部署成功但机器人无法触达你；先在手机飞书创建企业（左上角头像 → 登录更多账号
  → 创建企业账号，免费 1 分钟）再授权，**别用公司号**。部署脚本会自动探测触达
  链路，命中时现场指引并自动重新给出授权链接。
- 终端会打印一条**飞书授权链接**（约 60 分钟有效）→ 把它复制到**手机浏览器**
  打开、用自己的手机飞书确认 → 凭据自动写入、容器自动启动 → 手机收到 Key 引导卡。
  中断（Ctrl+C）后重跑同一条命令即可续传。
- **部署 = 升级**：发新版后重跑即可升级。顺序保证安全——先拉新镜像（失败即中止，
  旧容器毫发无伤）→ 快照 `data/`（保留最近 3 份）→ 重建容器 → 60 秒自检，
  起不稳**自动回滚**到旧版本。升级期间机器人离线约 1 分钟。
- 飞书里发「**版本**」或「**检查更新**」：机器人报当前/最新版本，有新版时直接
  给出这条升级命令；升级完成后自动推送「✅ 已更新」通知卡。

源码方式部署（apt 装依赖 + systemd 守护 + 自检）：

```bash
sudo bash setup/install_linux.sh feishu
```

### 源码运行（本机网页版，开发者）

获取源码：

```bash
git clone https://github.com/PrivaGlot-app/PrivaGlot.git        # GitHub 原始仓
# git clone https://gitee.com/priva-glot-app/PrivaGlot-app.git  # 国内网络走 Gitee 同树镜像
```

注意：clone 与 GitHub 页面 Download ZIP 都**没有 `VERSION` 文件**，双击
「一键启动.bat」时自更新检查会静默跳过（设计如此，不是故障）；要在本机测试
自更新链路，设环境变量 `PRIVAGLOT_UPDATE_URL` 指向更新清单地址即可（1.28+
版本支持该覆盖）。

1. **环境要求**：Python 3.11+；**ffmpeg** 在 `PATH` 中（浏览器录制的 WebM/Opus
   要转成 WAV 再送 ASR；Docker 镜像内自带，无需安装）；所选厂商的 API Key
   （默认小米 MiMo，也可启动后在网页「模型配置页」里选厂商并填）。
2. **安装依赖**：

   ```bash
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt        # Windows
   # source .venv/bin/activate && pip install -r requirements.txt   # macOS / Linux
   ```

3. **配置**：复制 `.env.example` 为 `.env`，填入 `MIMO_API_KEY`
   （也可不填，启动后在网页「模型配置页」里选厂商并填 Key，见「环境变量」
   的优先级说明）。
4. **启动**：

   ```bash
   .venv\Scripts\python run.py
   ```

   浏览器打开 http://localhost:8000 —— `localhost` 属于安全上下文，不需要
   HTTPS 就能使用麦克风。

## 教材格式

**推荐用网页里的「📖 我的教材」创建和维护**：拍课本 → AI 提取 → 粘贴 → 保存，
保存后立即出现在选课页。完整说明见 **[docs/教材规范.md](docs/教材规范.md)**。要点：

- 一本教材就是一个 JSON 文件，放在 `data/curriculum/`；随应用分发的示例教材在
  `app/bundled_curriculum/`，同名（`id` 相同）时你自己那份优先。
- `id` 是教材唯一标识，**也是学生进度记录的键**，定下来别改；文件名随便改。
- `units` 数组的顺序就是上课顺序；一个单元只有三个列表：
  `vocabulary` / `patterns` / `grammar`。

```json
{
  "unit_id": "unit_1",
  "title": "Unit 1 My Day",
  "vocabulary": ["wake up", "breakfast", "school"],
  "patterns": [
    "What time do you get up? - I usually get up at seven."
  ],
  "grammar": [
    "Simple present tense for daily routines (e.g., 'I get up at seven.')"
  ]
}
```

- `vocabulary` **必填且不能为空**（课后词汇报告的统计对象）；`patterns` 和
  `grammar` 可以为空。内容都是字符串数组，只写英文。
- `unit_id` / `title` 可省略：按数组位置自动补 `unit_1`、`unit_2`……
- 手工放好文件后**重启服务**才会出现在选课页（网页里保存的不用）。校验不通过或
  JSON 语法错误的文件会被**静默跳过**（仅日志留一行 error）。
- 批量放教材前可先跑 `python setup/check_curriculum.py` 体检：扫描内置与用户
  两个教材目录，列出每本教材并报出格式问题。

## 技术栈与目录结构

FastAPI + WebSocket 后端，原生 HTML/JS 前端（无构建步骤），`openai` SDK 调用
MiMo 的 OpenAI 兼容接口。架构为**无头对话内核 + 多通道接入**：`app/core/`
是不知道任何平台存在的对话内核（课程编排、会话注册、音频归一化），网页、飞书、
终端 REPL 都是它的通道适配（`app/channels/`）——新增通道不需要修改内核。

```
一键启动.bat          Windows 一键入口（网页单形态；飞书部署走 VPS 一键命令）
setup/                部署脚本（bootstrap.ps1 / install.sh / install_linux.sh /
                      deploy.sh 宿主侧部署升级，随镜像分发）
                      + 环境自检 check_env.py + 引导素材同步 sync_guidance.py
app/
  core/               对话内核：课程编排 / 会话注册 / 音频归一化（平台无关）
  channels/           通道适配：web_ws（网页）/ feishu（飞书）/ repl（终端）
  vendors/            多厂商语音供应商档案（厂商选择 / 注册引导 / 模型名 / 验证话术的唯一编辑处）
  bundled_curriculum/ 随应用分发的示例教材与模板
  bundled_teachers/   随应用分发的预置教师（Dean / Chloe / Milo / Mia）
static/index.html     前端单页
Dockerfile            容器镜像（自带 ffmpeg）
run.py                网页版启动入口
run_feishu.py         飞书通道启动入口
run_repl.py           终端 REPL 启动入口（脱离任何图形界面跑通一节课）
docs/教材规范.md       教材格式规范
docs/飞书接入.md       飞书机器人接入与排障
data/                 运行时数据：学生档案 / 会话记录 / 自建教材 / data/.env
                      （Docker 形态的唯一挂载卷，不入库）
```

## 环境变量

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `MIMO_API_KEY` | 小米 MiMo API Key，默认厂商三件套共用；不填也可启动后在网页「模型配置页」里选厂商填 Key | 无 |
| `MIMO_BASE_URL` | MiMo 接口地址（按量付费） | `https://api.xiaomimimo.com/v1` |
| `LLM_MODEL` | 对话模型 | `mimo-v2.6-flash` |
| `MIMO_ASR_MODEL` | 语音识别模型 | `mimo-v2.5-asr` |
| `MIMO_TTS_MODEL` | 语音合成模型 | `mimo-v2.5-tts` |
| `VENDOR_ID` | 激活的厂商（`mimo` / `zhipu`） | `mimo` |
| `LLM_VENDOR` / `TTS_VENDOR` / `ASR_VENDOR` | 按模态覆盖厂商（部署者逃生门） | 跟随 `VENDOR_ID` |
| `ZHIPU_API_KEY` | 智谱 API Key（厂商选智谱时使用） | 无 |
| `LLM_MAX_TOKENS_PER_TURN` | 单轮回复的 token 上限，防跑飞 | `300` |
| `SERVER_PORT` | 监听端口（地址钉死 `127.0.0.1`，不可配置） | `8000` |
| `SAMPLE_RATE` / `CHANNELS` | 录音参数 | `16000` / `1` |
| `DEBUG_PROMPTS` | 是否把完整 prompt 写入 `data/prompt_logs/` | `0` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用凭据（不填则飞书通道不启动） | 空 |
| `FEISHU_DOMAIN` | 飞书开放平台域名（海外 Lark 填 larksuite 域名） | `https://open.feishu.cn` |
| `FEISHU_SESSION_IDLE_TIMEOUT` | 飞书会话空闲秒数，超时先自动下课再开新课 | `7200` |
| `FEISHU_ADMIN_OPEN_ID` | 开机推送引导卡的飞书 open_id | 空 = 授权部署自动写入 |

> **配置的优先级**：`app/config.py` 按顺序读两份 `.env`，同名键**后读的覆盖
> 先读的**，且两者都盖过系统环境变量：
>
> 1. **项目根 `.env`** —— 手工部署配置。本机部署在这个老位置填 Key 继续有效。
> 2. **`data/.env`** —— 网页「模型配置页」/ 飞书配置卡写厂商与 Key 的落点，运行期权威。
>    Docker 形态下 `data/` 是唯一挂载卷，Key 落在这里才能在容器升级重建后存活
>    （旧版本把 Key 写在容器内部的 `/app/.env`，升级重建即丢且无报错）。
>
> 普通用户路径：厂商、模型、Key 全部在应用内「模型配置页」里选填即可，
> 上表这些 env 键主要是部署者逃生门。
>
> 注意：配置文件里若留着占位符，它会盖掉系统环境变量里的真 Key，调用接口时才报
> 401；反过来，想临时用系统环境变量覆盖文件是无效的，得改文件。

> **`DEBUG_PROMPTS` 请在生产环境保持 0**：打开后每轮完整 prompt（含学生原话）
> 都会落盘到 `data/prompt_logs/`，与项目的隐私定位冲突。

## 安全模型

- **网页只属于本机**：`app/config.py` 里 `SERVER_HOST = "127.0.0.1"` 钉死、
  不从 `.env` 读取——不存在把网页暴露给局域网/公网的配置入口，误绑定无从发生。
  `localhost` 是浏览器认定的安全上下文，无需 HTTPS 即可使用麦克风。
- **无对外攻击面**：服务只被本机进程够得着，没有需要鉴权防御的暴露面；
  飞书形态不起任何 HTTP 服务（长连接纯出站）。
- 源码方式部署（`install_linux.sh`）以非 root 用户运行，`.env` 权限收紧为 600。

## 数据与隐私

**一、会离开这台电脑的**

对话过程中，录音会发送到你配置的模型厂商云端接口做语音识别（默认小米 MiMo，
可在应用内「模型配置页」更换，如智谱）；识别出的文字和教师的回复文本由云端
大模型生成，语音也由云端合成。也就是说，**孩子的语音和对话文字会经过所选
厂商的服务器**。

这些数据不经过本项目作者的任何服务器：作者不运营任何中转服务，没有任何回传
通道，看不到你的数据。

**二、只留在你这台电脑上的**

- `data/students/` 学生档案：姓名、年龄、兴趣、单元进度
- `data/sessions/` 逐轮会话记录
- `data/curriculum/` 你自己创建的教材内容
- `data/teachers/_active.json` 每个学生上次选的教师与风格
- `data/prompt_logs/` 仅当 `DEBUG_PROMPTS=1` 时生成，默认关闭

这些文件不会上传到任何地方。想彻底删除，删掉整个 `data/` 目录即可；想备份，
把 `data/` 打包拷走即可。

**三、绝对不会出去的**

本仓库不含任何学生数据，也没有遥测、统计上报或"体验改进计划"。服务端不落盘
学生语音原文——音频即用即弃，仅存 ASR 识别文本。

## Token Plan 接入（开发者可选）

作者本机用的是 Token Plan 订阅，所以这条通道也完整支持。但请注意：

- Token Plan 是**订阅制、按套餐限量**的产品，面向 coding / agent 类负载。
  把它当业务后端跑一个陪练服务，不在该产品的使用范围内，有违反使用政策的嫌疑。
  **面向普通用户请一律用按量付费接口**，也就是本项目的默认值。
- Key 前缀就是通道标识：`sk-` = 按量付费，`tp-` = Token Plan。
- 两条通道的模型 ID 完全一致，切换只需要改 `.env` 两行：

```ini
MIMO_API_KEY=tp-xxxxxxxx
MIMO_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
```

`setup/bootstrap.ps1` 会在你粘贴 Key 时按前缀自动选通道，不用手改。

## 给AI助手

本节的读者是 AI 助手/代理（例如云服务器网页终端里的内置助手，或任何能阅读本仓库、
替用户执行命令的 AI）。规则三条：**以本节为唯一部署事实源**；本节没写的步骤不要
编造；本节写明的命令必须原样使用，禁止改写、拆分或省略任何一行。

**获取本文档**：若你手里只有仓库地址、或抓到的是「网页外壳」（抓取工具通常不执行
JavaScript），改抓 raw 直链，一次拿到纯 Markdown 原文：
`https://gitee.com/priva-glot-app/PrivaGlot-app/raw/master/README.md`
（GitHub 镜像：`https://raw.githubusercontent.com/PrivaGlot-app/PrivaGlot/main/README.md`）。

### 第一步：判断用户的形态

- 用户要在**自己电脑**上使用 → 不需要服务器：Windows 用户走本文「快速开始（Windows）」，
  其他系统/开发者走「源码运行」。无须读本节剩余部分。
- 用户要在**手机飞书**上使用 → 需要一台 Linux 服务器，继续往下。
- 用户**还没有服务器** → 推荐腾讯云「轻量应用服务器」2核2G 档、Ubuntu Server
  22.04/24.04 镜像，购买入口：https://curl.qcloud.com/6Gjk4JDU 。买好后让用户从
  云控制台打开网页终端，继续往下。

### 第二步：核对前置条件（逐项确认）

| 条件 | 核对方式 | 不满足时 |
|---|---|---|
| x86_64 架构 | `uname -m` | 换机器；ARM 不支持 |
| Ubuntu/Debian | `cat /etc/os-release` | 重装为 Ubuntu 22.04/24.04 |
| root 身份 | `id -u` 输出 0 | 先执行 `sudo -i` 再继续 |
| 出站网络可用 | 能访问飞书开放平台与镜像仓库 | 检查安全组/防火墙出站规则 |

配置 2核2G 起、磁盘剩余 ≥2GB。不要预装 Python 或 ffmpeg——容器镜像自带。

### 第三步：执行一键部署命令

把本文「Linux VPS：飞书机器人（一键部署/升级）」一节代码块里的**整段命令**原样
执行（或逐字交给用户执行）。三条铁律：

1. **整段**：命令是两段式设计——粘贴段只负责装 Docker、拉镜像、从镜像里解压出
   真正的部署脚本 `deploy.sh` 并运行；首次配置、飞书授权、写凭据、启动、60 秒
   自检与自动回滚都在 `deploy.sh` 里。只摘「核心行」、改写、截断都必然失败。
2. **原样**：不替换镜像地址、不加 sudo 前缀、不拆成多条命令。
3. **可重入**：中断（Ctrl+C）后重跑同一条命令即续传；已部署的机器重跑同一条
   命令即为升级。

### 第四步：预期输出与失败处理

成功路径按序出现三个标志：`环境就绪：Docker version …` → 镜像拉取完成 →
打印一条**飞书授权链接**。把链接转述给用户或终端时**必须原样**：放进独立代码块，
不转义、不解码、不折行——链接长且带查询参数，任何字符改写都会使它失效。之后引导
用户**复制到手机浏览器**打开、用手机飞书确认（电脑浏览器完成不了手机端确认，
这是最常见的卡点）→ 凭据自动写入、容器自动启动 → 用户手机收到 Key 引导卡。
授权链接约 60 分钟有效。

失败时：收集 `systemctl status docker docker.socket` 与 `journalctl -u docker -n 50`
两段输出交给用户（转项目作者排查）。不要自行换源、重装系统或改写命令。

### 必须主动告知用户的坑

- **纯个人飞书租户会「部署成功但机器人无法触达」**：账号名下要有用户**自己创建**
  的飞书企业（免费，1 分钟），不要用公司号。部署脚本探测到会现场指引重建。
- 镜像仓库在腾讯云（境内）：海外 VPS 拉取可能很慢甚至超时，推荐境内厂商。
- 部署 = 升级：之后发新版重跑同一条命令即可，自带数据快照与自动回滚；也可让
  用户在飞书里发「检查更新」由机器人给出最新命令。

除本节所述外不存在其他部署步骤：改防火墙、装依赖、手动 docker run 都不需要。

## License

[MIT](LICENSE)
