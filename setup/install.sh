#!/usr/bin/env bash
# PrivaGlot 一键安装（在镜像内执行——宿主机只需要 Docker，不需要 Python/仓库文件）：
#
#   IMG=<镜像地址> && docker run --rm -e IMAGE="$IMG" -v /opt/privaglot:/work "$IMG" bash setup/install.sh
#
# 做三件事：扫码创建飞书应用 -> 三键写入 /work/.env（保留文件里已有的键）
# -> 打印第二步的启动命令。幂等：.env 已有飞书凭据时跳过扫码。
# qr.json 含 App Secret，无论成败退出时都会删除。
#
# 管理员 = 扫码人：必须让部署者本人用自己手机飞书扫，别代扫。
set -euo pipefail

WORK="${WORK:-/work}"
ENV_FILE="$WORK/.env"
QR="$WORK/qr.json"
# 宿主机上 /work 对应的路径（第二步 docker run 在宿主机执行，必须用宿主路径）。
# 指南约定的安装目录是 /opt/privaglot，与第一行命令的 -v 保持一致。
HOST_ROOT="${HOST_ROOT:-/opt/privaglot}"

mkdir -p "$WORK/data"
trap 'rm -f "$QR"' EXIT

if [ -f "$ENV_FILE" ] && grep -q '^FEISHU_APP_ID=..' "$ENV_FILE" \
   && grep -q '^FEISHU_APP_SECRET=..' "$ENV_FILE"; then
  echo "== 检测到 $ENV_FILE 已有飞书凭据，跳过扫码 =="
  echo "  （要重新创建应用：删掉该文件里的 FEISHU_ 行后重跑本命令）"
else
  echo "== 第 1 步：创建飞书应用（扫码）=="
  echo "  请用【你自己的】手机飞书扫码——扫码的人将自动成为管理员，"
  echo "  开机欢迎卡、Key 引导卡只发给他，不要让别人代扫。"
  echo "  扫码前提：账号名下要有【自己创建】的企业——纯个人账号（豆包"
  echo "  账号）会部署成功但机器人无法触达你。先创建企业：手机飞书"
  echo "  左上角头像 → 登录更多账号 → 创建企业账号（免费，1 分钟），"
  echo "  再扫码。别用公司号（应用会建进雇主组织）。"
  echo
  set +e
  python setup/feishu_qr_register.py --out "$QR" --no-browser
  rc=$?
  set -e
  case "$rc" in
    0) ;;
    1) echo "x 已取消，没有创建任何应用。重跑本命令可再来一次。"; exit 1;;
    2) echo "x 授权没有完成（链接过期或被拒绝）。重跑本命令可再来一次。"; exit 1;;
    4) echo "x 触达探测穷尽：应用仍建在个人租户（账号名下没有自己创建的企业）。"
       echo "  手机飞书创建企业（左上角头像 → 登录更多账号 → 创建企业账号）"
       echo "  后重跑本命令重新扫码即可；已装好的 Docker 会自动跳过。"; exit 1;;
    *) echo "x 环境或网络异常（退出码 $rc）。确认容器能访问 open.feishu.cn 后重跑。"; exit 1;;
  esac

  echo
  echo "== 第 2 步：把飞书三键写入 .env（保留文件里已有的其它键）=="
  python - "$ENV_FILE" "$QR" <<'PYEOF'
import json
import sys
from pathlib import Path

env_path, qr_path = Path(sys.argv[1]), Path(sys.argv[2])
q = json.loads(qr_path.read_text(encoding="utf-8"))

new = {}
if q.get("client_id"):
    new["FEISHU_APP_ID"] = q["client_id"]
if q.get("client_secret"):
    new["FEISHU_APP_SECRET"] = q["client_secret"]
if q.get("open_id"):
    # 扫码人 = 管理员（bootstrap.ps1:624-626 同款；漏了它开机不推卡，见 #10）
    new["FEISHU_ADMIN_OPEN_ID"] = q["open_id"]
if q.get("tenant_brand") == "lark":
    new["FEISHU_DOMAIN"] = "https://open.larksuite.com"

missing = [k for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET",
                       "FEISHU_ADMIN_OPEN_ID") if k not in new]
if missing:
    print("x 授权结果缺少：" + "、".join(missing))
    print("  缺 FEISHU_ADMIN_OPEN_ID 会导致开机不推卡。请重跑本命令重新扫码。")
    sys.exit(2)

lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
out, written = [], set()
for ln in lines:
    key = ln.split("=", 1)[0].strip() if "=" in ln else ""
    if key in new:
        if key not in written:          # 旧值被新值替换；重复行丢弃
            out.append(f"{key}={new[key]}")
            written.add(key)
    else:
        out.append(ln)                  # 其它键（MIMO key 等）原样保留
for k, v in new.items():
    if k not in written:
        out.append(f"{k}={v}")

env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
env_path.chmod(0o600)
print("x 飞书三键已写入 .env（含 FEISHU_ADMIN_OPEN_ID，开机推卡正常）")
PYEOF
fi

echo
echo "√ 安装完成。$ENV_FILE 里的键名（不回显值）："
echo "  $(grep -oE '^[A-Za-z_]+=' "$ENV_FILE" | tr -d '=' | tr '\n' ' ')"
echo
echo "== 启动 =="
echo "若你用的是 README 的一键命令（带自动启动），容器会紧接着自动拉起，下面这条不用管。"
echo "若没有，手动执行："
echo
echo "docker run -d --name privaglot --restart unless-stopped --memory 768m --env-file $HOST_ROOT/.env -v $HOST_ROOT/data:/app/data ${IMAGE:-<镜像地址>} python run_feishu.py"
echo
echo "（重跑一键命令即可升级——它会自动替换旧容器并在启动异常时回滚；"
echo "  手动执行本条时，同名容器已存在就先：docker rm -f privaglot）"
