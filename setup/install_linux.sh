#!/usr/bin/env bash
# =============================================================
# PrivaGlot Linux VPS 一键部署（飞书形态，源码方式）
#
# 用法（在项目根目录）：
#   sudo bash setup/install_linux.sh feishu      # 长连接出站，零公网端口，
#                                                # 无需域名/HTTPS/备案
#   sudo bash setup/install_linux.sh feishu --reinstall   # 重建 .venv
#
# 做五件事：
#   1. apt 安装 ffmpeg 与 Python venv（要求 Python >= 3.11，见下方探测逻辑）
#   2. 创建 .venv 并安装 requirements.txt（失败自动换清华镜像）
#   3. 检查 .env（缺文件时从 .env.example 生成，密钥要自己填）
#   4. 生成并启用 systemd 服务（privaglot-feishu）
#   5. 跑一次 setup/check_env.py 自检并打印验收指引
#
# 服务管理：
#   systemctl status  privaglot-feishu
#   journalctl -u privaglot-feishu -f        # 看日志
#   systemctl restart privaglot-feishu       # 改 .env / 教材后重启
#
# 数据：学生档案/会话/教材都在项目 data/ 目录，备份 = 打包 data/。
# =============================================================
set -uo pipefail

MODE="${1:-}"
REINSTALL="${2:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
SERVICE_FEISHU="privaglot-feishu"

say()  { echo "  · $*"; }
good() { echo "  √ $*"; }
bad()  { echo "  × $*" >&2; }

# ---- 0. 参数 ---------------------------------------------------------------
case "$MODE" in
  feishu) : ;;
  *)
    echo "用法: sudo bash setup/install_linux.sh feishu [--reinstall]"
    exit 4 ;;
esac
SUDO_USER_NAME="${SUDO_USER:-}"
if [ -z "$SUDO_USER_NAME" ] || [ "$SUDO_USER_NAME" = "root" ]; then
  RUN_USER="root"
else
  RUN_USER="$SUDO_USER_NAME"   # systemd 服务以部署者身份跑，别用 root
fi

# ---- 1. 系统依赖 -----------------------------------------------------------
if [ ! -x "$(command -v apt-get)" ]; then
  bad "本脚本目前只覆盖 Debian/Ubuntu（apt）。其他发行版请照注释手动安装："
  bad "  ffmpeg + python3(>=3.11) + venv，然后从第 2 步开始手动执行。"
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
say "apt 安装系统依赖（ffmpeg、python3-venv）..."
apt-get update -qq
apt-get install -y -qq ffmpeg python3 python3-venv python3-pip >/dev/null \
  || { bad "apt 安装失败，请检查网络/源"; exit 1; }

# Python 版本：Debian 12 = 3.11、Ubuntu 22.04 = 3.10（需 3.12）、24.04 = 3.12。
PYBIN=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    v=$("$cand" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    case "$v" in
      3.11|3.12|3.13) PYBIN="$(command -v "$cand")"; break ;;
    esac
  fi
done
if [ -z "$PYBIN" ]; then
  bad "没有找到 Python >= 3.11。Ubuntu 22.04 的解决办法："
  bad "  sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt-get install -y python3.11 python3.11-venv"
  bad "然后重新运行本脚本。"
  exit 1
fi
good "Python $($PYBIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')（$PYBIN）"

# ---- 2. 虚拟环境与依赖 -----------------------------------------------------
if [ ! -x "$PY" ] || [ "$REINSTALL" = "--reinstall" ]; then
  say "创建虚拟环境 $VENV ..."
  rm -rf "$VENV"
  "$PYBIN" -m venv "$VENV" || { bad "venv 创建失败"; exit 1; }
fi
say "安装 Python 依赖（首次约 1-3 分钟）..."
if ! "$PY" -m pip install -q -r "$ROOT/requirements.txt" --disable-pip-version-check; then
  say "默认源失败，换清华镜像重试..."
  "$PY" -m pip install -q -r "$ROOT/requirements.txt" \
      -i https://pypi.tuna.tsinghua.edu.cn/simple --disable-pip-version-check \
    || { bad "依赖安装失败，请检查网络后重跑本脚本"; exit 1; }
fi
good "依赖安装完成"

# ---- 3. .env ---------------------------------------------------------------
if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  say "已从 .env.example 生成 .env —— 还没有密钥，服务起不来！"
  say "  nano $ROOT/.env   填入 MIMO_API_KEY（飞书模式另填 FEISHU_APP_ID/SECRET）"
  say "  然后 systemctl restart privaglot-feishu"
else
  say ".env 已存在，保持不变"
fi
# .env 里有 API Key，权限收紧到仅属主可读写
chmod 600 "$ROOT/.env" 2>/dev/null || true
chown -R "$RUN_USER" "$ROOT/data" 2>/dev/null || true

# ---- 4. systemd 服务 -------------------------------------------------------
write_unit() { # $1=unit 名  $2=描述  $3=启动命令
  cat > "/etc/systemd/system/$1.service" <<EOF
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$ROOT
ExecStart=$PY $3
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# ffmpeg 由脚本装进 /usr/bin；项目自带的 tools/ffmpeg 仅 Windows 使用

[Install]
WantedBy=multi-user.target
EOF
}
[ "$RUN_USER" != "root" ] && chown -R "$RUN_USER" "$VENV" 2>/dev/null || true

systemctl daemon-reload
if [ "$MODE" = "feishu" ]; then
  write_unit "$SERVICE_FEISHU" "PrivaGlot feishu bot (long connection)" "run_feishu.py"
  systemctl enable --now "$SERVICE_FEISHU" >/dev/null 2>&1
  good "飞书机器人已启动：systemctl status $SERVICE_FEISHU"
fi

# ---- 5. 自检与验收指引 -----------------------------------------------------
echo ""
CHECK_RC=0
if [ -x "$PY" ]; then
  "$PY" "$ROOT/setup/check_env.py" --mode "$MODE" || CHECK_RC=$?
fi

echo ""
echo "============================================================="
echo " 部署完成（形态：飞书）。"
echo " 飞书版：手机飞书给机器人发语音即可上课。"
echo " 凭据没配的话先填 .env 再 systemctl restart $SERVICE_FEISHU。"
echo " 日志：journalctl -u privaglot-feishu -f"
echo "============================================================="
if [ "$CHECK_RC" -ne 0 ]; then
  echo " 注意：环境自检未通过（见上方 FAIL 行），请按提示处理后重启服务。"
  exit "$CHECK_RC"
fi
exit 0
