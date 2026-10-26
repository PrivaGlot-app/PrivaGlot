"""
版本感知与升级检查（问题清单 #13 升级路径，2026-10-07 Tier 2）。

升级路径的硬边界：容器换不了自己——拉镜像、删容器、起新容器是宿主
Docker 守护进程的职权（不挂 docker.sock，那是把宿主 root 等价权交给
容器）。所以容器内只做「发现」与「引导」，执行永远在宿主侧。本模块
提供：

- UPDATE_COMMAND —— 官网短命令（#50，2026-10-24）：与 README「Linux VPS
  一键部署」同一条一行命令；重逻辑本体在 privaglot.com/install.sh（与 README
  折叠备用命令同源）＋镜像内 setup/deploy.sh（Docker 没装则装、装了则实探
  守护进程、起不来先 reset-failed + restart 抢救 → 拉镜像 → 解压执行 deploy.sh，
  部署 = 升级同一条）。tests/kernel/test_version_check.py 逐字符锁定本常量与
  README 短命令块一致，并把两段式微引导的安全形状锁进 README 备用命令块，
  容器生命周期锁进 setup/deploy.sh（#17 教训：同一文本两处手抄必漂移，必须
  机器锁死）。
- fetch_latest_version() —— 匿名走 registry v2 token 舞步，查 TCR 公开仓
  tags 里的最高版本号。端点与 tools/promote_public.sh 的 anon_check
  逐字符同源（https 前缀、/v2/ 路径、realm/service 解析、token JSON
  的空格容忍）——那里踩过的 308/404 坑不再踩第二遍。
- parse_version / latest_version_from_tags / has_update —— 纯函数，
  离线可测。

版本号本体在构建期烤进镜像：Dockerfile ARG/ENV APP_VERSION（与
org.opencontainers.image.version label 同源，tools/verify_image.sh
核对两者一致），运行期经 app/config.APP_VERSION 读取；"dev" = 没走
发版管线的本机源码运行，跳过在线检查。
"""
import json
import re
import urllib.error
import urllib.request

REGISTRY = "https://ccr.ccs.tencentyun.com"
REPOSITORY = "privaglot/privaglot_public"

UPDATE_COMMAND = "curl -fsSL https://privaglot.com/install.sh | bash"


def parse_version(tag: str):
    """'1.13' / 'v1.13' → (1, 13)；其余（latest / dev / 垃圾值）→ None。"""
    m = re.fullmatch(r"v?(\d+)\.(\d+)", str(tag or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def latest_version_from_tags(tags) -> str | None:
    """tags 里的最高 x.y 版本 tag；没有版本形 tag（空仓/只有 latest）返回 None。"""
    versioned = [t for t in (tags or []) if parse_version(t)]
    if not versioned:
        return None
    return max(versioned, key=parse_version)


def has_update(current: str, latest: str | None) -> bool:
    """当前版本可解析且 latest 严格更高才算有更新（dev / 解析失败 → False）。"""
    c, l = parse_version(current), parse_version(latest or "")
    return bool(c and l and l > c)


def _auth_param(challenge: str, name: str) -> str:
    """从 Www-Authenticate 挑战头取 realm/service 等带引号参数。"""
    m = re.search(rf'{name}="([^"]+)"', challenge or "")
    return m.group(1) if m else ""


def fetch_latest_version(timeout: float = 15.0) -> str:
    """匿名查 TCR 公开仓 tags，返回最高版本号；失败抛 RuntimeError
    （调用方翻译成人话，异常不冒进聊天）。三步舞步与 promote_public.sh
    的 anon_check 同源：直连 401 挑战 → 匿名 token → Bearer GET 200。"""
    url = f"{REGISTRY}/v2/{REPOSITORY}/tags/list"
    try:
        urllib.request.urlopen(urllib.request.Request(url), timeout=timeout)
        raise RuntimeError("registry 未要求鉴权，响应异常")
    except urllib.error.HTTPError as e:
        if e.status != 401:
            raise RuntimeError(f"registry 返回 HTTP {e.status}")
        challenge = e.headers.get("Www-Authenticate", "")
    realm = _auth_param(challenge, "realm")
    service = _auth_param(challenge, "service")
    if not realm:
        raise RuntimeError("没解析到 Www-Authenticate realm（TCR 响应变了？）")
    token_url = f"{realm}?service={service}&scope=repository:{REPOSITORY}:pull"
    with urllib.request.urlopen(token_url, timeout=timeout) as r:
        token = (json.loads(r.read().decode("utf-8")) or {}).get("token", "")
    if not token:
        raise RuntimeError("registry 没有回匿名 token")
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        tags = (json.loads(r.read().decode("utf-8")) or {}).get("tags") or []
    latest = latest_version_from_tags(tags)
    if latest is None:
        raise RuntimeError("registry tags 里没有版本号")
    return latest
