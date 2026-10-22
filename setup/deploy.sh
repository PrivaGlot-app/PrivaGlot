#!/bin/bash
# PrivaGlot 宿主侧部署/升级脚本（#27 两段式：逻辑随镜像分发，微引导解压后执行）：
#
#   IMG=<镜像地址> && bash deploy.sh "$IMG"
#
# 微引导（README「Linux VPS 一键部署」）只负责宿主侧的 Docker 就绪与拉镜像，
# 然后用 `docker run --rm … cp /app/setup/deploy.sh` 把本脚本解压到 /opt/privaglot
# 执行。文本住在镜像里 = 与镜像永远同版本；执行在宿主机上 = 容器换不了自己
# （不挂 docker.sock 红线），拉起/删除容器只能宿主做。
#
# 职责（部署 = 升级同一条，幂等）：
#   首次配置（install.sh：写 .env + 扫码建飞书应用，已有凭据自动跳过）
#   → 快照 data/（保留最近 3 份）→ 记录运行中容器的版本（回滚钥匙）
#   → 删旧容器 → 启动新容器 → 60 秒自检 → 起不稳自动回滚旧版本。
set -eu

[ "${1:-}" ] || { echo "用法: $0 <镜像地址>"; exit 1; }
IMG="$1"
REPO="${IMG%:*}"        # 回滚用：去掉 tag 得到镜像仓库地址
D=/opt/privaglot

# 容器启动参数的唯一事实源（升级/回滚共用这一处，改参数只改这里）
run_ver() { docker run -d --name privaglot --restart unless-stopped --memory 768m \
  --env-file "$D/.env" -v "$D/data:/app/data" "$1" python run_feishu.py; }

# 首次配置：写 .env、扫码创建飞书应用（install.sh 幂等，已有凭据自动跳过；
# 失败时它自己输出人话指引，set -e 就此中止——不动已运行的旧容器）
docker run --rm -e IMAGE="$IMG" -v "$D:/work" "$IMG" bash setup/install.sh

# 升级保护：快照数据 + 记住当前运行版本
PREV=$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' privaglot 2>/dev/null || true)
tar czf "$D/data-$(date +%Y%m%d%H%M%S).tgz" -C "$D" data
ls -1t "$D"/data-*.tgz 2>/dev/null | tail -n +4 | xargs -r rm -f

# 换容器
docker rm -f privaglot 2>/dev/null || true
if run_ver "$IMG"; then
  echo "容器已启动，60 秒后自检（可给机器人发条消息确认可达）……"; sleep 60
  if [ "$(docker inspect -f '{{.State.Running}}' privaglot 2>/dev/null || echo no)" = true ] \
     && [ "$(docker inspect -f '{{.RestartCount}}' privaglot 2>/dev/null || echo x)" = 0 ]; then
    echo "√ 启动正常，部署/升级完成。数据快照保留在 $D/data-*.tgz（最近 3 份）"
  elif [ -n "$PREV" ]; then
    echo "! 新版本 60 秒内未稳定，自动回滚到 :$PREV"
    docker rm -f privaglot 2>/dev/null || true; run_ver "$REPO:$PREV"
    echo "已回滚。请把本段终端输出发给项目作者排查。"
  else
    echo "! 启动异常且无旧版本可回滚（首次部署）。请把本段输出发给项目作者。"; fi
elif [ -n "$PREV" ]; then
  echo "! 新容器启动失败，自动回滚到 :$PREV"
  docker rm -f privaglot 2>/dev/null || true; run_ver "$REPO:$PREV"
  echo "已回滚。请把本段终端输出发给项目作者排查。"
else
  echo "! 新容器启动失败（首次部署，无旧版本可回滚）。请把本段输出发给项目作者。"
fi
