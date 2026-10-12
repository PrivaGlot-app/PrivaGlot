"""
PrivaGlot 环境自检（模式感知，跨平台）

所有部署形态的核心依赖完全一致（Python 3.11+ / ffmpeg / requirements /
.env），差异只在「跑哪个入口、要不要反代」。这个检查器按模式只报该模式
真正需要的东西，被三处复用：

  - bootstrap.ps1（Windows 部署引擎，启动前预检）
  - install_linux.sh（Linux VPS 一键，systemd 装完自检）
  - run_feishu.py --selftest（部署完的快速验收）

用法（在项目根目录）：
    python setup/check_env.py --mode web           # 本机/服务器网页模式
    python setup/check_env.py --mode feishu        # 飞书机器人模式
    python setup/check_env.py --mode feishu --with-api   # 加真实 API 探活

退出码：0 通过 / 1 环境问题（缺 ffmpeg、缺依赖等） /
        2 配置问题（.env 缺项）/ 3 API 探活失败 / 4 参数错误
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, ENV_FAIL, CFG_FAIL, API_FAIL, ARG_FAIL = 0, 1, 2, 3, 4

results: list[tuple[str, str, str]] = []  # (level, line, detail)


def ok(line: str, detail: str = ""):
    results.append(("ok", line, detail))


def fail(line: str, detail: str = ""):
    results.append(("FAIL", line, detail))


def hint(line: str, detail: str = ""):
    results.append(("提示", line, detail))


def print_report() -> bool:
    """Print all results; True when no FAIL lines."""
    has_fail = False
    for level, line, detail in results:
        if level == "ok":
            print(f"ok   {line}")
        elif level == "FAIL":
            has_fail = True
            print(f"FAIL {line}")
        else:
            print(f"提示 {line}")
        if detail and level != "ok":
            for wrapped in _wrap(detail):
                print(f"     {wrapped}")
    return not has_fail


def _wrap(text: str, width: int = 72) -> list[str]:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [text]


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def check_python() -> bool:
    v = sys.version_info
    if (v.major, v.minor) >= (3, 11):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    fail(f"Python 版本过低（{v.major}.{v.minor}）",
         "本项目需要 Python 3.11 或更高版本。Windows 请重跑部署脚本；"
         "Linux 见 setup/install_linux.sh 顶部的说明。")
    return False


def _find_ffmpeg() -> tuple[str | None, bool]:
    r"""(path, is_project_local)。与 bootstrap 的 Find-Ffmpeg 同一定义：
    系统 PATH 之外，一键脚本 PyPI 兜底装出的 ffmpeg 在项目 tools\ 下
    （启动器运行时自动加进 PATH，所以不在系统 PATH 里也算已安装）。"""
    p = shutil.which("ffmpeg")
    if p:
        return p, False
    local = ROOT / "tools" / "ffmpeg" / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if local.exists():
        return str(local), True
    return None, False


def check_ffmpeg(need_libopus: bool) -> bool:
    ffmpeg, project_local = _find_ffmpeg()
    if not ffmpeg:
        fail("找不到 ffmpeg（录音转码必需）",
             "Windows：重新运行部署脚本会自动安装；"
             "Debian/Ubuntu：sudo apt-get install -y ffmpeg")
        return False
    where = "项目自带，启动时自动加入 PATH" if project_local else "在 PATH 上"
    ok(f"ffmpeg {where}（{ffmpeg}）")
    if not need_libopus:
        return True
    try:
        enc = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        fail(f"ffmpeg 无法执行：{e}")
        return False
    if "libopus" in enc:
        ok("ffmpeg 含 libopus 编码器（飞书语音条必需）")
        return True
    fail("ffmpeg 缺少 libopus 编码器，无法生成飞书语音条",
         "Windows：用 https://www.gyan.dev/ffmpeg/builds/ 的 release-full 或 "
         "essentials 版；Debian/Ubuntu：apt 的 ffmpeg 自带 libopus，"
         "检查是否装了精简静态版")
    return False


def check_pip_deps(mode: str) -> bool:
    base = ["fastapi", "uvicorn", "httpx", "openai", "dotenv"]
    if mode == "feishu":
        base.append("lark_oapi")
    missing = []
    for mod in base:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if not missing:
        ok(f"Python 依赖齐全（{len(base)} 个关键包）")
        return True
    fail("缺少 Python 依赖：" + ", ".join(missing),
         "在项目根目录执行：pip install -r requirements.txt"
         "（或重跑部署脚本）")
    return False


def _env_vars() -> dict:
    """Read .env files directly (not via app.config, so this works even when
    dependencies are missing). data/.env（卡片填 Key 的落点）后读并覆盖根
    .env，与 app/config.py 的加载优先级保持一致。"""
    env = {}
    for path in (ROOT / ".env", ROOT / "data" / ".env"):
        if path.exists():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    return env


def check_env_file(mode: str) -> bool:
    path = ROOT / ".env"
    if not path.exists():
        fail("没有 .env 配置文件",
             "Windows：部署脚本会自动从 .env.example 生成；"
             "Linux：cp .env.example .env 然后填入密钥")
        return False
    env = _env_vars()
    problems = []

    key = env.get("MIMO_API_KEY", "").strip()
    if not key or key == "your_mimo_api_key_here":
        # 2026-09-23 裁定：Key 不再在部署脚本里注入，配置入口收敛到产品界面
        # （网页=引导层自动弹出；飞书=聊天里的 Key 引导卡）。缺 Key 不阻断
        # 部署——服务照常起，用户在界面里填，验证通过立即生效。
        hint("MIMO_API_KEY 未填——启动后在网页引导层或飞书 Key 引导卡里粘贴"
             "即可，无需重启")
    else:
        chan = "Token Plan" if key.startswith("tp-") else "按量付费"
        ok(f"MiMo API Key 已填（{chan} 通道）")

    if mode == "feishu":
        app_id = env.get("FEISHU_APP_ID", "").strip()
        secret = env.get("FEISHU_APP_SECRET", "").strip()
        if app_id and secret:
            ok(f"飞书应用凭据已填（{app_id[:8]}…）")
        else:
            problems.append(
                "FEISHU_APP_ID / FEISHU_APP_SECRET 未填——申请步骤见 docs/飞书接入.md")

    if env.get("DEBUG_PROMPTS", "0").strip().lower() in ("1", "true", "yes"):
        hint("DEBUG_PROMPTS=1：完整 prompt（含学生原话）正在写入 "
             "data/prompt_logs/，生产环境请改回 0")

    if problems:
        fail(".env 配置不完整", "；".join(problems))
        return False
    return True


def check_data_dir() -> bool:
    data = ROOT / "data"
    try:
        data.mkdir(exist_ok=True)
        probe = data / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        ok(f"data 目录可写（{data}）")
        return True
    except Exception as e:
        fail(f"data 目录不可写：{e}",
             "学生档案、会话记录、教材都写在 data/ 里，必须有写权限")
        return False


def check_api(mode: str) -> bool:
    """真实探活（--with-api）：会发一次极小的真实请求。"""
    import asyncio
    all_ok = True
    from app import config
    if config.is_configured():
        from app.verify_key import verify_api_key
        try:
            result = asyncio.run(
                verify_api_key(config.LLM_API_KEY, config.LLM_BASE_URL,
                               config.LLM_MODEL, config.TTS_MODEL))
            if result.ok:
                ok("MiMo API 探活通过")
            else:
                all_ok = False
                fail(f"MiMo API 探活失败：{result.message}", result.detail or "")
        except Exception as e:
            all_ok = False
            fail(f"MiMo API 探活异常：{e}")
    if mode == "feishu":
        from app.channels.feishu.client import FeishuClient
        client = FeishuClient()
        if client.configured():
            try:
                token = client.tenant_access_token()
                if token:
                    ok("飞书凭据探活通过（tenant_access_token 获取成功）")
                else:
                    all_ok = False
                    fail("飞书凭据探活失败：拿到了空 token")
            except Exception as e:
                all_ok = False
                fail(f"飞书凭据探活失败：{e}",
                     "检查 FEISHU_APP_ID / FEISHU_APP_SECRET 与网络；"
                     "沙盒/内网环境需能访问 open.feishu.cn")
        else:
            hint("未配置飞书凭据，跳过飞书探活")
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="PrivaGlot 环境自检")
    parser.add_argument("--mode", choices=("web", "feishu"),
                        default="web")
    parser.add_argument("--with-api", action="store_true",
                        help="发一次极小的真实请求探活（消耗少量额度）")
    args = parser.parse_args()

    print(f"== PrivaGlot 环境自检 · 模式 {args.mode} ==")
    env_ok = check_python()
    env_ok = check_ffmpeg(need_libopus=args.mode == "feishu") and env_ok
    env_ok = check_pip_deps(args.mode) and env_ok
    env_ok = check_data_dir() and env_ok
    cfg_ok = check_env_file(args.mode)
    api_ok = True
    if args.with_api:
        try:
            api_ok = check_api(args.mode)
        except ImportError as e:
            fail(f"无法做 API 探活（依赖缺失）：{e}")
            api_ok = False

    passed = print_report()
    if passed:
        print("== 自检通过 ==")
        return OK
    print("== 自检未通过 ==")
    if not env_ok:
        return ENV_FAIL
    if not cfg_ok:
        return CFG_FAIL
    if not api_ok:
        return API_FAIL
    return ENV_FAIL


if __name__ == "__main__":
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
