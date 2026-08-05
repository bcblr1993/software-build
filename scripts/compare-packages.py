#!/usr/bin/env python3
"""对照两个安装包，输出差异报告。

用于回答升级后最常被问到的问题：这一版到底改了什么、有没有动到不该动的
地方。逐项由机器比对，不依赖人工核对 —— 历史上这类结论是手写在升级
报告里的，既费时又无从复核。

比对三个层面：

    组件与版本   谁升了、谁新增、谁移除
    归档校验和   版本号未变但内容变了的，会被单独指出
    顶层文件     脚本与说明文件的增减

用法:
    compare-packages.py <旧包> <新包> [-o 报告.md]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tarfile
from pathlib import Path


def read_package(path: Path) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """返回 ({组件: (版本, 归档 sha256)}, 顶层文件集合)。"""
    components: dict[str, tuple[str, str]] = {}
    toplevel: set[str] = set()

    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if len(parts) < 2:
                continue

            # 顶层文件（install.sh、README 等）
            if len(parts) == 2 and member.isfile():
                toplevel.add(parts[1])
                continue

            if "software" not in parts or not member.isfile():
                continue

            base = Path(member.name).name
            if not base.endswith((".tar.gz", ".tgz")):
                continue

            stem = base.replace(".tar.gz", "").replace(".tgz", "")
            comp, _, rest = stem.partition("-")
            if not comp:
                continue
            m = re.search(r"(?:^|[-_])([0-9][0-9A-Za-z.]*)", rest)
            version = m.group(1) if m else (rest or "-")

            fh = tar.extractfile(member)
            digest = "?"
            if fh is not None:
                h = hashlib.sha256()
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
                digest = h.hexdigest()

            components[comp] = (version, digest)

    return components, toplevel


def render(old_path: Path, new_path: Path,
           old: dict, new: dict,
           old_top: set[str], new_top: set[str]) -> str:
    L: list[str] = []
    a = L.append

    a("# 安装包对照报告")
    a("")
    a(f"- 旧包：`{old_path.name}`")
    a(f"- 新包：`{new_path.name}`")
    a("")

    upgraded, rebuilt, added, removed, identical = [], [], [], [], []

    for name in sorted(set(old) | set(new)):
        if name not in old:
            added.append((name, new[name][0]))
        elif name not in new:
            removed.append((name, old[name][0]))
        else:
            ov, oh = old[name]
            nv, nh = new[name]
            if ov != nv:
                upgraded.append((name, ov, nv))
            elif oh != nh:
                rebuilt.append((name, nv, oh, nh))
            else:
                identical.append((name, nv))

    a("## 结论")
    a("")
    a(f"- 版本升级：{len(upgraded)} 项")
    a(f"- 版本未变但内容已变：{len(rebuilt)} 项")
    a(f"- 新增：{len(added)} 项　移除：{len(removed)} 项")
    a(f"- 完全一致：{len(identical)} 项")
    a("")

    if upgraded:
        a("## 版本升级")
        a("")
        a("| 组件 | 旧版本 | 新版本 |")
        a("|---|---|---|")
        for name, ov, nv in upgraded:
            a(f"| {name} | {ov} | {nv} |")
        a("")

    if rebuilt:
        a("## 版本未变但内容已变")
        a("")
        a("版本号相同而归档校验和不同，说明是重新编译或打包方式变化所致。")
        a("升级组件时这类项目值得留意 —— 它意味着实际交付内容与上一版不同。")
        a("")
        a("| 组件 | 版本 | 旧校验和 | 新校验和 |")
        a("|---|---|---|---|")
        for name, ver, oh, nh in rebuilt:
            a(f"| {name} | {ver} | `{oh[:16]}…` | `{nh[:16]}…` |")
        a("")

    if added:
        a("## 新增组件")
        a("")
        for name, ver in added:
            a(f"- {name} {ver}")
        a("")

    if removed:
        a("## 移除组件")
        a("")
        for name, ver in removed:
            a(f"- {name} {ver}")
        a("")

    if identical:
        a("## 完全一致的组件")
        a("")
        a("归档校验和与旧包逐字节相同，可确认未受本次变更影响。")
        a("")
        for name, ver in identical:
            a(f"- {name} {ver}")
        a("")

    only_old = old_top - new_top
    only_new = new_top - old_top
    if only_old or only_new:
        a("## 顶层文件差异")
        a("")
        for f in sorted(only_new):
            a(f"- 新增 `{f}`")
        for f in sorted(only_old):
            a(f"- 移除 `{f}`")
        a("")

    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="对照两个安装包")
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()

    old_path, new_path = Path(args.old), Path(args.new)
    for p in (old_path, new_path):
        if not p.is_file():
            print(f"找不到: {p}", file=sys.stderr)
            return 2

    print(f"读取 {old_path.name}", file=sys.stderr)
    old, old_top = read_package(old_path)
    print(f"读取 {new_path.name}", file=sys.stderr)
    new, new_top = read_package(new_path)

    report = render(old_path, new_path, old, new, old_top, new_top)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"报告已写入 {args.output}", file=sys.stderr)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
