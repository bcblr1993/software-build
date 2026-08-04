#!/usr/bin/env python3
"""从 ISO 的 repodata 建立「共享库 → 提供它的 rpm」索引。

由包名猜测提供者是行不通的：libnss3.so 在 nss 包里，libglib-2.0.so.0 在
glib2 包里，libsqlite3.so.0 在 sqlite-libs 包里，liblzma.so.5 在 xz-libs 包里，
各发行版还各有出入。而每张安装盘的 repodata/*primary.xml* 中本就记录了
每个包的 provides（含 so 名），直接读它即可，无需逐个解开 rpm。

用法:
    rpm-index.py <ISO 挂载点> <所需 so 名>...
输出:
    每行 "<so 名> <rpm 相对路径>"，未找到的 so 不输出。
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# primary.xml 里的 provides 形如 libfoo.so.1()(64bit)，取括号前的部分
_SO_RE = re.compile(r"^([^(]+\.so[^(]*)")


def _open_maybe_compressed(path: Path):
    name = path.name
    if name.endswith(".gz"):
        return gzip.open(path, "rb")
    if name.endswith(".xz"):
        return lzma.open(path, "rb")
    if name.endswith(".bz2"):
        return bz2.open(path, "rb")
    return path.open("rb")


def build_index(mount: Path) -> dict[str, str]:
    """返回 {so 名: rpm 相对路径}。"""
    index: dict[str, str] = {}

    # 一张盘可能有多个仓库（Anolis 8 分 BaseOS 与 AppStream）
    primaries = sorted(mount.rglob("repodata/*primary.xml*"))
    if not primaries:
        return index

    for primary in primaries:
        # location 中的路径相对于该仓库根目录，即 repodata 的上一级
        repo_root = primary.parent.parent
        try:
            with _open_maybe_compressed(primary) as fh:
                # iterparse 逐个处理，避免把整份 XML 读进内存
                for _, elem in ET.iterparse(fh, events=("end",)):
                    if not elem.tag.endswith("}package") and elem.tag != "package":
                        continue

                    href = ""
                    provides: list[str] = []
                    # provides 与 requires 用的都是 <rpm:entry>，因此不能对整棵
                    # 子树做遍历 —— 那会把依赖误当成提供，闭包将永不收敛，
                    # 并把 anaconda、emacs 这类大包一并拖进最小 rootfs。
                    for child in elem:
                        tag = child.tag.rsplit("}", 1)[-1]
                        if tag == "location":
                            href = child.get("href", "")
                        elif tag == "format":
                            for fmt_child in child:
                                if fmt_child.tag.rsplit("}", 1)[-1] != "provides":
                                    continue
                                for entry in fmt_child:
                                    if entry.tag.rsplit("}", 1)[-1] != "entry":
                                        continue
                                    m = _SO_RE.match(entry.get("name", ""))
                                    if m:
                                        provides.append(m.group(1))

                    if href:
                        rel = str((repo_root / href).relative_to(mount))
                        for so in provides:
                            # 同一个 so 可能被多个包提供，先到先得即可
                            index.setdefault(so, rel)
                    elem.clear()
        except (OSError, ET.ParseError):
            continue

    return index


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    mount = Path(sys.argv[1])
    wanted = sys.argv[2:]

    index = build_index(mount)
    if not index:
        print("未能从 repodata 建立索引", file=sys.stderr)
        return 1

    if not wanted:
        print(f"索引到 {len(index)} 个共享库", file=sys.stderr)
        return 0

    for so in wanted:
        path = index.get(so)
        if path:
            print(f"{so} {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
