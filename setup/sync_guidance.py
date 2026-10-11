"""引导素材同步器 —— 厂商文案的「单一事实源」落到静态文档的最后一公里。

README.md 是静态文档，没法像网页引导层/飞书卡片那样运行期从供应商档案
（app/vendors/<厂商>.py）取文案；但它又必须和产品里的话术同源，否则改一次
小米的活动就要人肉改多处（网页、飞书、README），漏一处就是用户看到两套说法。

做法：文档里的对应段落用一对 HTML 注释标记包住（GitHub 渲染时不可见）——

  <!-- vendor-guide:<名字> begin -->
  （由本脚本生成，不要手改）
  <!-- vendor-guide:<名字> end -->

标记外的内容（产品设计说明、截图、目录树……）本脚本永不触碰。
小米的活动/链接/话术变了：只改 app/vendors/mimo.py，然后——

  python setup/sync_guidance.py            # 重写所有标记段落
  python setup/sync_guidance.py --check    # 只检查（发版前自检/CI），漂移则退出码 1
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.vendors import active  # noqa: E402

README = ROOT / "README.md"


def _joiner(text: str) -> str:
    """跟随文件既有换行风格（仓库两份文档都是 CRLF）。"""
    return "\r\n" if "\r\n" in text else "\n"


# ---------------------------------------------------------------- markdown 生成
# 模板只负责把档案字段拼成该文档的行文；新的事实（活动、价格、入口、链接）
# 一律写回 app/vendors/mimo.py，模板里不许出现厂商事实。

def readme_web_step(v: dict) -> str:
    """README「第三步」首段：部署完成 → 网页里填 Key 的引导。"""
    g = v["guide"]
    return (
        f"浏览器打开后，页面上会直接向你要一把{v['display_name']} API Key —— "
        f"{g['intro']}点页面上的「{g['signup_label']}」按钮，用手机号注册即可。"
        f"{g['preamble']}"
        f"{g['bonus_note']}没领到体验金也不影响：{g['topup_note']}"
    )


def readme_channels(v: dict) -> str:
    """README「第三步」尾段：直连地址与两条通道的说明。"""
    payg = v["channels"][0]
    plan = v["channels"][1] if len(v["channels"]) > 1 else None
    lines = [f"不想走邀请链接也行，直接访问 <{v['platform_url']}> 注册，费用一样。", ""]
    tail = (f"作者自用的 {plan['label']} 是另一条通道，"
            f"见下方「{plan['label']} 接入（开发者可选）」。") if plan else ""
    lines.append(f"它用的是**{payg['label']}**接口（Key 形如 "
                 f"`{payg['prefix']}xxxxx`）。{tail}")
    return "\n".join(lines)


# 名字 -> (文档, 生成函数)
SPANS = {
    "readme-web-step": (README, readme_web_step),
    "readme-channels": (README, readme_channels),
}


def _replace_span(text: str, path: Path, name: str, content: str) -> str:
    pat = re.compile(
        rf"(<!-- vendor-guide:{name} begin -->\r?\n)(.*?)"
        rf"(<!-- vendor-guide:{name} end -->)",
        re.DOTALL,
    )
    m = pat.search(text)
    if m is None:
        raise SystemExit(f"{path}: 缺少标记对 vendor-guide:{name} —— "
                         f"请先在文档里放好 begin/end 两行")
    nl = _joiner(text)
    body = content.replace("\n", nl)
    return pat.sub(lambda _: m.group(1) + body + nl + m.group(3), text, count=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="只检查不写入；有漂移时退出码 1")
    args = parser.parse_args()

    v = active()
    by_path: dict = {}
    for name, (path, builder) in SPANS.items():
        by_path.setdefault(path, []).append((name, builder))

    drift = []
    for path, builders in by_path.items():
        # newline="" 保留文件原有的 CRLF：read_text 的默认通用换行会把
        # \r\n 吃成 \n，写回时整个文件的换行风格就被改掉了（git 大 diff）。
        with path.open("r", encoding="utf-8", newline="") as f:
            old = f.read()
        new = old
        for name, builder in builders:
            new = _replace_span(new, path, name, builder(v))
        if new != old:
            if args.check:
                drift.append(str(path))
            else:
                with path.open("w", encoding="utf-8", newline="") as f:
                    f.write(new)
                print(f"已更新 {path.relative_to(ROOT)}")
    if args.check:
        if drift:
            print("引导素材与供应商档案不一致（跑 python setup/sync_guidance.py 同步）：")
            for p in drift:
                print(" -", p)
            return 1
        print("引导素材一致：README.md 与 app/vendors/ 档案同源。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
