"""
飞书应用「扫码一键创建」（v4 部署加速，由 setup\\bootstrap.ps1 调用）

在终端发起一次 OAuth Device Flow：打印并打开飞书授权页，用户用手机飞书
扫一扫并在手机上点确认后，飞书自动创建一个智能体应用——机器人能力、
消息收发 / 语音资源等权限、WebSocket 长连接事件订阅（im.message.receive_v1
等）全部按官方模板预置完毕——再把 App ID / App Secret 直接回传给本脚本，
写入 --out 指定的 JSON 文件，由 bootstrap.ps1 接着落进 .env。
全程不需要打开飞书开放平台手动建应用。

官方文档：
    https://open.feishu.cn/document/mcp_open_tools/scan-to-create-an-app-in-one-click
SDK 要求：lark-oapi >= 1.5.5（requirements.txt 已含）。

用法：
    python setup/feishu_qr_register.py --out "%TEMP%\\privaglot-feishu-qr.json"
    python setup/feishu_qr_register.py --out ... --app-id cli_xxx   # 给已有应用增量授权
    python setup/feishu_qr_register.py --out ... --no-browser       # 不自动打开浏览器

退出码：0 成功 / 1 用户取消 / 2 授权失败（过期、拒绝等，重跑可再来）/
        3 环境问题（SDK 过旧、网络不通、写文件失败）/
        4 可达性探测穷尽（应用落在个人租户且救援轮用完；建团队后重跑即可）
"""
import argparse
import json
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_NAME = "PrivaGlot AI 外教"
DEFAULT_DESC = "跟着 AI 老师练口语：发语音即可上课，下课自动出学习报告"
# 头像预填（扫码创建官方 app_preset.avatar：单个 URL 或 1-6 个 URL 列表，创建页
# 默认选中第一个，用户扫码后仍可在页面手动更换）。URL 必须公网匿名可达——拉图的
# 是飞书创建页和扫码确认的手机，不是本机（部署形态零公网端口，机器没有公网地址）。
# 组织仓转公开前保持空串 = 跳过预填，行为与旧版完全一致；转公开后回填一行即生效
# （随之后的版本发出，见 docs/开发账本.md §3「Windows 分发公开激活」第④件事）：
# DEFAULT_AVATAR_URL = ("https://gitee.com/priva-glot-app/PrivaGlot-app"
#                       "/raw/master/static/logo-avatar-640.png")
DEFAULT_AVATAR_URL = ""


def _fail(msg: str, code: int) -> int:
    print(f"\n× {msg}")
    return code


def _errtext(e: Exception) -> str:
    code = getattr(e, "code", "error")
    desc = getattr(e, "description", str(e))
    return f"{code}：{desc}" if desc else str(code)


# ---- 可达性探测与救援循环（A+B 一揽子，2026-10-05）----------------------
# 根因（2026-09-25 两扫对照 + 2026-10-05 VPS 实验定谳）：纯个人账号（豆包）
# 扫码能创建应用、自动发布、token 全通，但应用落进个人租户——手机端无企业
# 功能面，服务端按「用户不可达」拒发。事后建团队救不回已建的应用（应用和
# open_id 在创建瞬间就钉死在当时的租户），唯一出路是建团队后重新扫码，让
# 新应用一出生就落进企业租户。所以：注册成功立刻 REST 试发一条消息探测，
# 命中个人租户签名就现场指引 + 倒计时自动重扫（救援循环）。

_PROBE_TEXT = ("PrivaGlot 部署测试：机器人触达链路正常 ✅ 回到服务器继续即可，"
               "稍后这里会收到引导卡。")
# 错误码 230101 为主匹配键、msg 文案为辅（文本可能被平台改写，码更稳）
_PROBE_NO_ORG_CODE = 230101
_PROBE_NO_ORG_MSG = "Sending messages to users is temporarily unavailable"
NO_ORG_CONFIRM_ATTEMPTS = 3    # 命中后再复核 2 次：防「组织新应用发布未完成」撞同款瞬时错误（D3 防误伤）
NO_ORG_CONFIRM_INTERVAL = 15
UNKNOWN_ATTEMPTS = 3           # 未知错误重试后降级放行——未知永远不拦组织用户
UNKNOWN_RETRY_INTERVAL = 20
RESCUE_LIMIT = 3               # 救援轮数：每轮重新扫码建新应用
RESCUE_WAIT_SECONDS = 120      # 留给用户建团队的时间（README 一键命令的 docker run 无 TTY，不能「按回车」交互）


def _classify_probe(code: int, msg: str) -> str:
    """探测结果三分类（纯函数，可单测）：ok / no_org（个人租户签名）/ unknown。"""
    if code == 0:
        return "ok"
    if code == _PROBE_NO_ORG_CODE or _PROBE_NO_ORG_MSG in (msg or ""):
        return "no_org"
    return "unknown"


def _feishu_post(url: str, payload: dict, token: str = None) -> dict:
    """极简 REST POST（标准库，setup 脚本不引重依赖）。返回解析后的 JSON。"""
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _probe_once(client_id: str, client_secret: str, open_id: str,
                tenant_brand: str):
    """用刚拿到的凭据试发一条测试消息。返回 (code, msg)；
    网络异常归为 (-1, 原因)，由 _classify_probe 落入 unknown 走降级。"""
    base = ("https://open.larksuite.com" if tenant_brand == "lark"
            else "https://open.feishu.cn")
    try:
        tok = _feishu_post(
            f"{base}/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": client_id, "app_secret": client_secret})
    except Exception as e:
        return -1, f"网络异常（换 token）：{e}"
    if tok.get("code") != 0 or not tok.get("tenant_access_token"):
        return tok.get("code", -1), f"换 token 失败：{tok.get('msg') or tok}"
    try:
        resp = _feishu_post(
            f"{base}/open-apis/im/v1/messages?receive_id_type=open_id",
            {"receive_id": open_id, "msg_type": "text",
             "content": json.dumps({"text": _PROBE_TEXT}, ensure_ascii=False)},
            token=tok["tenant_access_token"])
    except Exception as e:
        return -1, f"网络异常（发消息）：{e}"
    return resp.get("code", -1), resp.get("msg") or ""


def _probe_with_retry(client_id: str, client_secret: str, open_id: str,
                      tenant_brand: str):
    """带 D3 防误伤协议的探测。返回 (verdict, detail)：
    - ok：直接通过（探针消息就是用户手机上第一个「活了」信号）
    - no_org：签名命中后再复核 2 次，仍命中才定谳
    - unknown：重试后仍未知 → 降级放行，不拦部署"""
    no_org_tries = 0
    unknown_tries = 0
    while True:
        code, msg = _probe_once(client_id, client_secret, open_id, tenant_brand)
        verdict = _classify_probe(code, msg)
        if verdict == "ok":
            return "ok", ""
        if verdict == "no_org":
            no_org_tries += 1
            if no_org_tries >= NO_ORG_CONFIRM_ATTEMPTS:
                return "no_org", f"code={code} msg={msg}"
            print(f"  · 命中个人租户签名（{no_org_tries}/{NO_ORG_CONFIRM_ATTEMPTS}），"
                  f"{NO_ORG_CONFIRM_INTERVAL} 秒后复核…", flush=True)
            time.sleep(NO_ORG_CONFIRM_INTERVAL)
        else:
            unknown_tries += 1
            if unknown_tries >= UNKNOWN_ATTEMPTS:
                return "unknown", f"code={code} msg={msg}"
            print(f"  · 探测遇未知错误（{unknown_tries}/{UNKNOWN_ATTEMPTS}），"
                  f"{UNKNOWN_RETRY_INTERVAL} 秒后重试…", flush=True)
            time.sleep(UNKNOWN_RETRY_INTERVAL)


def _print_rescue_guidance() -> None:
    print()
    print("  " + "!" * 62)
    print("  × 检测到你的飞书账号还没有企业：应用能创建，但个人账号的")
    print("    机器人无法触达你（消息进不了个人租户）。")
    print("  请现在用手机创建企业（免费，约 1 分钟）：")
    print("    ① 飞书 App 左上角头像 → 「登录更多账号」→「创建企业账号」")
    print("    ② 输入手机号码；后面两步（完善基础信息、邀请同事）")
    print("       都能直接跳过")
    print("    ③ 创建完确认左上角头像已切到新企业——脚本会自动重新")
    print("       出二维码，到时再扫一次即可（新应用建进你的企业）。")
    print("  随时 Ctrl+C 可退出；重跑部署命令也能继续。")
    print("  " + "!" * 62, flush=True)


def _rescue_wait(seconds: int) -> None:
    """倒计时等待。一键命令的 docker run 无 -it（无 TTY），不能用
    「按回车继续」的交互，只能倒计时后自动重新出二维码。"""
    remaining = seconds
    while remaining > 0:
        print(f"  · 等你创建企业…还剩 {remaining} 秒（Ctrl+C 可退出）", flush=True)
        chunk = min(30, remaining)
        time.sleep(chunk)
        remaining -= chunk


def _spawn_preheat() -> None:
    """扫码等待空档的预热（2026-09-23 第十二轮死区修复）。用户扫码确认的
    几十秒里机器正好干等：让后台进程先把机器人要用的重依赖 import 一遍
    ——pyc 落盘、杀软首扫、磁盘缓存全部提前付掉。否则 run_feishu.py 稍后
    的 import 会在用户面前再慢一遍（实测冷机把长连接拖到 1-2 分钟后，
    用户点卡片按钮就是无人接收的死区）。预热失败无关紧要：它只是加速。"""
    import subprocess
    code = (
        "import sys\n"
        "try:\n"
        "    import compileall\n"
        "    compileall.compile_dir(sys.argv[1], quiet=2)\n"
        "except Exception:\n"
        "    pass\n"
        "import lark_oapi, websockets, httpx\n"
    )
    app_dir = str(Path(__file__).resolve().parent.parent / "app")
    try:
        subprocess.Popen(
            [sys.executable, "-c", code, app_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="飞书应用扫码一键创建")
    parser.add_argument("--out", required=True, help="凭据 JSON 输出路径")
    parser.add_argument("--app-id", default=None,
                        help="已有应用的 App ID（走增量授权，而不是新建）")
    parser.add_argument("--name", default=DEFAULT_NAME, help="预填的应用名称")
    parser.add_argument("--desc", default=DEFAULT_DESC, help="预填的应用描述")
    parser.add_argument("--avatar", default=DEFAULT_AVATAR_URL,
                        help="预填的应用头像 URL（须公网匿名可达；空串=跳过预填）")
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器（只打印授权链接）")
    parser.add_argument("--no-probe", action="store_true",
                        help="跳过可达性探测与救援循环（离线测试用）")
    args = parser.parse_args()

    # lark_oapi 是个重包，冷启动（杀软首次扫描、磁盘冷缓存）import 要几十秒。
    # 提示必须打在 import 之前，否则用户会盯着黑窗口以为卡死了。
    print("== 飞书应用一键创建（扫码）==")
    print("  正在加载飞书 SDK，最长约半分钟，请稍候…", flush=True)
    try:
        import lark_oapi as lark
    except ImportError:
        return _fail("没有找到 lark-oapi。请先装依赖：pip install -r requirements.txt", 3)

    if not hasattr(lark, "register_app"):
        return _fail("本机 lark-oapi 版本过旧（扫码创建需要 >= 1.5.5）。"
                     "请执行：pip install -U lark-oapi", 3)

    try:
        from lark_oapi.scene.registration.errors import (
            AppAccessDeniedError,
            AppExpiredError,
            RegisterAppError,
        )
    except ImportError:  # 极老版本兜底：当普通失败处理
        class RegisterAppError(Exception):
            code = "error"
            description = ""

        AppAccessDeniedError = AppExpiredError = RegisterAppError

    state = {"last_status": ""}

    def on_qr_code(info):
        _spawn_preheat()  # 用户扫码的空档，提前把重依赖 import 遍
        url = info["url"]
        expire_min = int(info.get("expire_in", 600)) // 60
        print()
        print("  " + "=" * 60)
        print("  请用手机飞书 App 的「扫一扫」扫描浏览器页面上的二维码，")
        print(f"  并在手机上点确认（链接约 {expire_min} 分钟内有效）。")
        print("  确认后应用自动创建：机器人能力、消息权限、长连接事件")
        print("  订阅都由飞书配好，不需要再去开放平台手动设置。")
        print("  ⚠ 前提：账号名下要有自己创建的企业——纯个人账号请先创建：")
        print("    左上角头像 → 登录更多账号 → 创建企业账号（免费 1 分钟），")
        print("    完成后再扫；别用公司号。")
        print("  " + "=" * 60)
        print("  若浏览器没打开，把下面这条链接复制到浏览器或手机：")
        print(f"  {url}")
        print(flush=True)
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass  # 没有默认浏览器也不影响：用户还能复制链接

    def on_status_change(info):
        status = info.get("status", "")
        if status == state["last_status"]:
            return
        state["last_status"] = status
        if status == "polling":
            print("  · 等待扫码确认中…（按 Ctrl+C 可取消）", flush=True)
        elif status == "slow_down":
            print(f"  · 轮询被限速，放宽到每 {info.get('interval', '?')} 秒一次", flush=True)
        elif status == "domain_switched":
            print("  · 检测到国际版 Lark 组织，已自动切换域名", flush=True)

    app_preset = {}
    if args.name:
        app_preset["name"] = args.name
    if args.desc:
        app_preset["desc"] = args.desc
    if args.avatar:
        app_preset["avatar"] = args.avatar

    if args.app_id:
        print(f"  模式：给已有应用 {args.app_id} 增量授权")

    max_attempts = 1 + (0 if args.no_probe else RESCUE_LIMIT)
    out = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            print(f"\n== 重新出二维码（救援第 {attempt - 1}/{max_attempts - 1} 轮）==",
                  flush=True)
        try:
            result = lark.register_app(
                on_qr_code=on_qr_code,
                on_status_change=on_status_change,
                app_preset=app_preset or None,
                app_id=args.app_id,
            )
        except KeyboardInterrupt:
            note = ("（之前轮次创建的应用留在你账号里，不影响重跑）"
                    if attempt > 1 else "（没有创建任何应用）")
            return _fail(f"已取消。{note}", 1)
        except RegisterAppError as e:
            if isinstance(e, AppExpiredError):
                return _fail("二维码已过期或确认超时。重新运行部署脚本即可再试一次。", 2)
            if isinstance(e, AppAccessDeniedError):
                return _fail(f"授权被拒绝（{_errtext(e)}）。", 2)
            return _fail(f"创建没有完成（{_errtext(e)}）。", 2)
        except Exception as e:  # 网络不通、代理问题等
            return _fail(f"网络或环境异常：{e}", 3)

        user = result.get("user_info") or {}
        out = {
            "client_id": result["client_id"],
            "client_secret": result["client_secret"],
        }
        if user.get("open_id"):
            out["open_id"] = user["open_id"]
        if user.get("tenant_brand"):
            out["tenant_brand"] = user["tenant_brand"]

        out_path = Path(args.out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception as e:
            return _fail(f"凭据文件写入失败（{out_path}）：{e}", 3)

        print(f"\n√ 应用创建成功！App ID: {out['client_id']}")
        print("  App Secret 不在屏幕回显，已写入凭据文件，由启动脚本接着写入 .env。")
        print(f"  凭据文件：{out_path}")

        if args.no_probe or not out.get("open_id"):
            break
        print("  正在探测机器人可达性（往你手机发一条测试消息）…", flush=True)
        verdict, detail = _probe_with_retry(
            out["client_id"], out["client_secret"], out["open_id"],
            out.get("tenant_brand") or "")
        if verdict == "ok":
            print("√ 探测通过：测试消息已发出（请看手机）——触达链路正常。")
            print(f"  （探测留证：tenant_brand={out.get('tenant_brand') or '<空>'}）")
            break
        if verdict == "unknown":
            print(f"⚠ 探测未通过且原因未知（{detail}）。不拦部署、继续启动；"
                  "若稍后手机收不到任何卡片，看 tutor.log 的 startup card 一行。")
            break
        # no_org：应用落在了个人租户——指引 + 倒计时后自动重扫
        if attempt < max_attempts:
            _print_rescue_guidance()
            try:
                _rescue_wait(RESCUE_WAIT_SECONDS)
            except KeyboardInterrupt:
                return _fail("已取消。本轮创建的应用落在个人租户（无法触达你），"
                             "不影响重跑部署。", 1)
            continue
        return _fail(
            f"救援 {RESCUE_LIMIT} 轮后探测仍未通过——机器人依然触达不了你。"
            "请确认已创建企业（飞书 App 左上角头像 → 登录更多账号 → "
            "创建企业账号），重跑部署命令重新扫码即可"
            "（已装好的东西都会自动跳过）。", 4)

    if out and out.get("tenant_brand") == "lark":
        print("  检测到国际版 Lark 组织，启动脚本会同时写好 FEISHU_DOMAIN。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
