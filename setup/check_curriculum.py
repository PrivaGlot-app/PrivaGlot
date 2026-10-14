"""
PrivaGlot —— 教材自检

用法：
    python setup/check_curriculum.py                  # 扫捆绑 + 用户两个教材目录
    python setup/check_curriculum.py temp             # 检查一个目录（递归找 *.json）
    python setup/check_curriculum.py 某本教材.json     # 检查单个文件

教材形态只有一种 —— 「整本教材一个 JSON 文件」：

    {
      "format": "privaglot-textbook/1",
      "id": "renjiao-pep-4a",
      "title": "人教版四年级上册",
      "units": [ {unit}, ... ]
    }

内容性检查（词汇非空、混中文、重复项、unit_id 重复……）与运行时保存共用
同一套逻辑（app.course_engine.validate_book_dict），自检结果和运行时一致。

检查项：

  * JSON 能不能解析（被截断时直接说「像是被截断了」，而不是只报一个行号）
  * format / id / title / units 齐不齐，id 里有没有空格或斜杠
  * 词汇表非空；patterns / grammar 允许为空
  * 三个列表里有没有混进中文（这套系统是纯英文口语陪练）
  * 词汇 / 句式有没有重复项，unit_id 有没有重复

退出码：0 全部通过，1 有问题需要处理。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.course_engine import (  # noqa: E402
    FORMAT_ID,
    course_engine,
    validate_book_dict,
)

DIRS = (
    ("捆绑", course_engine.bundled_dir),
    ("用户", course_engine.user_dir),
)

ID_FORBIDDEN = re.compile(r"[\s/\\]")


def check_book_file(path: Path) -> tuple[dict | None, list[str]]:
    """检查一本「整本教材一个 JSON 文件」。"""
    problems: list[str] = []
    raw = path.read_text(encoding="utf-8-sig", errors="replace")

    if raw.lstrip().startswith("```"):
        problems.append("文件里有 markdown 代码围栏（```），应该只留裸 JSON")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        # 截断是最常见的失败：AI 输出到一半就停了。
        hint = "（像是被截断了，建议分单元重做再合并）" \
            if not raw.rstrip().endswith("}") else ""
        problems.append(f"JSON 解析失败：第 {e.lineno} 行第 {e.colno} 列 {e.msg}{hint}")
        return None, problems

    if not isinstance(data, dict):
        problems.append(f"顶层必须是 JSON 对象，实际是 {type(data).__name__}")
        return None, problems

    if not str(data.get("id") or "").strip():
        problems.append("缺 id（它是学生进度记录的键，系统生成的教材自带）")
    elif ID_FORBIDDEN.search(str(data["id"])):
        problems.append(f"id {data['id']!r} 含空格或斜杠，会弄坏进度记录")

    fmt = data.get("format")
    if fmt and fmt != FORMAT_ID:
        problems.append(f"format 应为 {FORMAT_ID!r}，实际是 {fmt!r}")

    problems.extend(validate_book_dict(data))
    return data, problems


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv]
    if not targets:
        targets = [base for _, base in DIRS]

    results = []
    seen_ids: dict[str, str] = {}

    for target in targets:
        target = target if target.is_absolute() else (Path.cwd() / target)
        if not target.exists():
            print(f"找不到路径：{target}")
            return 1

        if target.is_file():
            data, problems = check_book_file(target)
            results.append((target.name, data or {},
                            len((data or {}).get("units") or []), problems))
            continue

        for path in sorted(target.rglob("*.json")):
            if any(part.startswith("_") for part in path.relative_to(target).parts):
                continue  # 模板、草稿、迁移备份
            data, problems = check_book_file(path)
            book_id = str((data or {}).get("id") or "")
            rel = str(path.relative_to(target))
            if book_id:
                if book_id in seen_ids:
                    problems.append(f"id {book_id!r} 和 {seen_ids[book_id]} 重复"
                                    "（进度按 id 存，两本书会互相覆盖）")
                seen_ids[book_id] = rel
            results.append((rel, data or {},
                            len((data or {}).get("units") or []), problems))

    if not results:
        print("没有找到任何教材。")
        return 1

    bad = 0
    for name, data, unit_count, problems in results:
        if problems:
            bad += 1
        print(f"{'✗' if problems else '✓'} {name}")
        print(f"    {str(data.get('title') or '-') } · {unit_count} 单元")
        for problem in problems:
            print(f"    - {problem}")

    print()
    if bad:
        print(f"{len(results)} 本教材，其中 {bad} 本需要处理。")
        return 1
    print(f"全部通过：{len(results)} 本教材。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
