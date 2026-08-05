#!/usr/bin/env python3
"""从 Debian 系安装盘建立「文件 → 提供它的 deb」索引。

凝思 6.0.80 与 6.0.100 是 Debian 系（分别基于 jessie 与 buster），
而非 EL 系，因此需要与 rpm-index.py 对应的一套。

与 RPM 不同，Debian 的 Packages 索引中并不登记包提供哪些 .so ——
依赖解析走的是 shlibs/symbols 机制。但安装盘通常带有 Contents-<arch>.gz，
其中逐行记录了「文件路径 → 所属包」，正是这里需要的映射。

若安装盘没有 Contents 文件，则退回按包名启发式匹配（libfoo.so.1 →
libfoo1 / libfoo），准确率较低但聊胜于无。

用法:
    deb-index.py <ISO 挂载点> --arch <架构> <所需 so 名>...
输出:
    每行 "<so 名> <deb 相对路径>"
"""

from __future__ import annotations

import gzip
import lzma
import re
import sys
from pathlib import Path


def _open_maybe_compressed(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    if path.name.endswith(".xz"):
        return lzma.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def load_package_paths(mount: Path, arch: str) -> dict[str, str]:
    """从 Packages 索引读出 {包名: deb 相对路径}。"""
    out: dict[str, str] = {}
    patterns = [f"dists/*/*/binary-{arch}/Packages*", f"dists/*/*/binary-all/Packages*"]

    for pattern in patterns:
        for index in sorted(mount.glob(pattern)):
            if index.suffix not in (".gz", ".xz", "") or index.name.endswith(".diff"):
                continue
            try:
                with _open_maybe_compressed(index) as fh:
                    name = ""
                    filename = ""
                    for line in fh:
                        line = line.rstrip("\n")
                        if not line:
                            if name and filename:
                                out.setdefault(name, filename)
                            name = filename = ""
                        elif line.startswith("Package: "):
                            name = line[9:].strip()
                        elif line.startswith("Filename: "):
                            filename = line[10:].strip()
                    if name and filename:
                        out.setdefault(name, filename)
            except OSError:
                continue
    return out


def load_contents(mount: Path, arch: str) -> dict[str, str]:
    """从 Contents-<arch> 读出 {文件路径: 包名}。

    行格式为 "路径<空白>区域/包名[,区域/包名...]"，取第一个包即可。
    """
    out: dict[str, str] = {}
    for pattern in (f"dists/*/*/Contents-{arch}*", f"dists/*/Contents-{arch}*"):
        for index in sorted(mount.glob(pattern)):
            try:
                with _open_maybe_compressed(index) as fh:
                    for line in fh:
                        parts = line.rsplit(None, 1)
                        if len(parts) != 2:
                            continue
                        path, pkgs = parts[0].strip(), parts[1].strip()
                        pkg = pkgs.split(",")[0].rsplit("/", 1)[-1]
                        if pkg:
                            out.setdefault("/" + path.lstrip("/"), pkg)
            except OSError:
                continue
    return out


def guess_packages(so: str) -> list[str]:
    """由 .so 名推测包名，用于安装盘没有 Contents 的情形。

    Debian 的库包命名惯例是「库名 + ABI 版本」，如
    libtinfo.so.5 → libtinfo5，libstdc++.so.6 → libstdc++6。
    """
    base = so.split(".so")[0]
    m = re.search(r"\.so\.(\d+)", so)
    ver = m.group(1) if m else ""

    # Debian 包名不用下划线，库名中的下划线一律写成连字符：
    # libcom_err.so.2 → libcom-err2，libkysec_extend.so.0 → libkysec-extend0
    bases = [base]
    if "_" in base:
        bases.append(base.replace("_", "-"))

    cands: list[str] = []
    for b in bases:
        if ver:
            # 两种惯例并存：libtinfo.so.5 → libtinfo5，
            # 而 libssh2.so.1 → libssh2-1、libpcre2-8.so.0 → libpcre2-8-0
            cands.append(f"{b}{ver}")
            cands.append(f"{b}-{ver}")
        cands.append(b)

    # 少数包名末尾带 .0，如 libbz2.so.1.0 → libbz2-1.0
    m2 = re.search(r"\.so\.([\d.]+)", so)
    if m2 and m2.group(1) != ver:
        for b in bases:
            cands.append(f"{b}-{m2.group(1)}")
    return cands


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    mount = Path(sys.argv[1])
    args = sys.argv[2:]
    arch = ""
    if len(args) >= 2 and args[0] == "--arch":
        arch = args[1]
        args = args[2:]
    wanted = args

    pkg_paths = load_package_paths(mount, arch)
    if not pkg_paths:
        print("未能从 Packages 索引建立包列表", file=sys.stderr)
        return 1

    contents = load_contents(mount, arch)

    if not wanted:
        print(f"索引到 {len(pkg_paths)} 个包，{len(contents)} 条文件记录", file=sys.stderr)
        return 0

    # Contents 的键是完整路径，按文件名建一层反查
    by_basename: dict[str, str] = {}
    for path, pkg in contents.items():
        by_basename.setdefault(Path(path).name, pkg)

    for so in wanted:
        pkg = by_basename.get(so)
        if not pkg:
            for cand in guess_packages(so):
                if cand in pkg_paths:
                    pkg = cand
                    break
        if pkg and pkg in pkg_paths:
            print(f"{so} {pkg_paths[pkg]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
