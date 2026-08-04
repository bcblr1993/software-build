#!/usr/bin/env python3
"""sprixin-build 命令行入口。

Web 控制台调用的是同一套 sprixin_build 包，两条入口的构建逻辑完全一致，
不会出现"页面上能构建、命令行构建不出来"的分歧。

常用：
    build.py import-base <既有包.tar.gz> --arch x86_64   以既有包作为基准
    build.py fetch --arch x86_64                          获取并校验上游源码
    build.py build --arch x86_64                          编译 + 门禁 + 打包
    build.py all                                          全架构完整流程
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from sprixin_build import (  # noqa: E402
    Builder,
    BuildError,
    Config,
    ConfigError,
    Fetcher,
    FetchError,
    run_gate,
)
from sprixin_build.packager import Packager, PackageError, clean_tree  # noqa: E402

# 归档源地址：x86_64 走 centos-vault，aarch64 走 centos-altarch
VAULT_URLS = {
    "x86_64": "https://mirrors.aliyun.com/centos-vault/7.9.2009",
    "aarch64": "https://mirrors.aliyun.com/centos-altarch/7.9.2009",
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def section(title: str) -> None:
    log("")
    log("─" * 60)
    log(f" {title}")
    log("─" * 60)


class Workspace:
    """构建工作区的目录约定。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.cache = self.root / "cache"
        self.out = self.root / "out"
        self.base = self.root / "base"
        self.dist = self.root / "dist"
        self.logs = self.root / "logs"
        for d in (self.cache, self.out, self.base, self.dist, self.logs):
            d.mkdir(parents=True, exist_ok=True)

    def arch_out(self, arch: str) -> Path:
        return self.out / arch

    def arch_base(self, arch: str) -> Path:
        return self.base / arch


# ── 子命令 ──────────────────────────────────────────────────────────


def cmd_import_base(args, cfg: Config, ws: Workspace) -> int:
    """把既有安装包解压为基准，供构建时继承配置与静态文件。

    现场的 nginx.conf、redis.conf、keepalived.conf 都是定制过的，
    用上游默认配置覆盖会直接改变现场行为。以既有包为基准，只替换
    编译产物，是保证"安装目录结构与配置保持一致"最可靠的方式。
    """
    src = Path(args.package)
    if not src.exists():
        log(f"找不到包: {src}")
        return 2

    dest = ws.arch_base(args.arch)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    section(f"导入基准包 {src.name} → {args.arch}")

    staging = dest / ".staging"
    staging.mkdir()
    with tarfile.open(src, "r:gz") as tar:
        tar.extractall(staging, filter="tar")

    tops = [p for p in staging.iterdir() if p.is_dir()]
    if len(tops) != 1:
        log(f"包内顶层目录数量异常: {[p.name for p in tops]}")
        return 1
    top = tops[0]

    software = top / "software"
    if not software.is_dir():
        log("包内缺少 software/ 目录")
        return 1

    count = 0
    for archive in sorted(software.iterdir()):
        if not archive.name.endswith((".tar.gz", ".tgz")):
            continue
        # install.sh 用 ${i%%-*} 取组件名，此处保持一致
        comp = archive.name.split("-")[0]
        target = dest / comp
        target.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(archive, "r:gz") as tar:
                members = tar.getmembers()
                # 与 install.sh 的 --strip-components 1 等价。
                # 硬链接的 linkname 也指向包内路径，必须同步剥离顶层目录，
                # 否则解压时会找不到链接目标（rabbitmq 的 escript 即如此）。
                kept = []
                for m in members:
                    parts = Path(m.name).parts
                    if len(parts) <= 1:
                        continue
                    m.name = str(Path(*parts[1:]))
                    if m.islnk():
                        lparts = Path(m.linkname).parts
                        if len(lparts) > 1:
                            m.linkname = str(Path(*lparts[1:]))
                    kept.append(m)
                for m in kept:
                    tar.extract(m, target, filter="tar")
        except (tarfile.TarError, KeyError) as exc:
            log(f"  {archive.name} 解压失败: {exc}")
            continue
        count += 1
        log(f"  {comp:12s} ← {archive.name}")

    # 顶层脚本也一并留存，便于对照现有行为
    for item in top.iterdir():
        if item.is_file():
            shutil.copy2(item, dest / item.name)

    shutil.rmtree(staging)
    log(f"\n基准包已导入 {count} 个组件 → {dest}")
    return 0


def cmd_fetch(args, cfg: Config, ws: Workspace) -> int:
    section(f"获取上游源码（{args.arch}）")
    fetcher = Fetcher(ws.cache, log=log)
    try:
        fetcher.fetch_all(cfg, args.arch)
    except FetchError as exc:
        log(f"\n{exc}")
        return 1
    log("\n全部归档校验通过")
    return 0


def cmd_build(args, cfg: Config, ws: Workspace) -> int:
    arch = args.arch
    started = time.time()

    builder = Builder(
        repo_root=REPO_ROOT,
        cache_dir=ws.cache,
        out_dir=ws.out,
        log_dir=ws.logs,
        log=log,
    )

    # ── 基线镜像 ────────────────────────────────────────────────
    section(f"基线镜像（{arch}）")
    try:
        builder.ensure_baseline(arch, cfg.baseline_image(arch), VAULT_URLS[arch])
    except BuildError as exc:
        log(str(exc))
        return 1

    # ── 随包依赖库 ──────────────────────────────────────────────
    section(f"随包依赖库（{arch}）")
    sysroot = builder.reset_sysroot(arch) if not args.keep_sysroot else builder.sysroot_volume_name(arch)
    env = {
        "LANG": "C",
        "LC_ALL": "C",
        **{
            f"{name.upper()}_VERSION": lib.version
            for name, lib in cfg.vendored_libs.items()
        },
    }
    run = builder.run_recipe(
        component="vendored-libs",
        arch=arch,
        image=cfg.baseline_image(arch),
        recipe="00-vendored-libs.sh",
        env=env,
        sysroot_volume=sysroot,
    )
    if not run.ok:
        log("随包依赖库构建失败，终止")
        return 1

    # ── 组件 ────────────────────────────────────────────────────
    targets = cfg.compile_components()
    if args.component:
        targets = [c for c in targets if c.name in args.component]
        if not targets:
            log(f"没有匹配的组件: {args.component}")
            return 2

    failures: list[str] = []
    for comp in targets:
        section(f"{comp.name} {comp.version_for(arch)}（{arch}）")
        recipe = f"{comp.name}.sh"
        if not (REPO_ROOT / "scripts" / "recipes" / recipe).exists():
            log(f"跳过：尚未提供配方 {recipe}")
            continue

        base_dir = ws.arch_base(arch) / comp.name
        run = builder.run_recipe(
            component=comp.name,
            arch=arch,
            image=cfg.baseline_image(arch),
            recipe=recipe,
            env={
                "LANG": "C",
                "LC_ALL": "C",
                f"{comp.name.upper()}_VERSION": comp.version_for(arch),
            },
            base_package=base_dir if base_dir.is_dir() else None,
            sysroot_volume=sysroot,
        )
        if not run.ok:
            failures.append(comp.name)

    if failures:
        log(f"\n以下组件构建失败: {', '.join(failures)}")
        return 1

    # ── ABI 门禁 ────────────────────────────────────────────────
    section(f"ABI 门禁（基线 glibc {cfg.glibc_max}）")
    gate_script = REPO_ROOT / "scripts" / "lib" / "abi-gate.sh"
    result = run_gate(gate_script, cfg.glibc_max, ws.arch_out(arch))
    log(result.output)
    if not result.passed:
        log("门禁未通过，拒绝出包")
        return 1

    # ── 打包 ────────────────────────────────────────────────────
    if args.no_package:
        log("\n按要求跳过打包")
        return 0

    section("打包")
    packager = Packager(out_dir=ws.dist / arch / "software", log=log)
    inner = []
    try:
        for comp_dir in sorted(ws.arch_out(arch).iterdir()):
            if not comp_dir.is_dir():
                continue
            comp = cfg.components.get(comp_dir.name)
            version = comp.version_for(arch) if comp else "0"
            removed = clean_tree(comp_dir)
            if removed:
                log(f"  {comp_dir.name}: 清理运行时残留 {removed} 项")
            inner.append(packager.make_inner(comp_dir.name, version, comp_dir))
    except PackageError as exc:
        log(str(exc))
        return 1

    if not inner:
        log("没有可打包的产物")
        return 1

    log(f"\n本轮产出 {len(inner)} 个内层归档，用时 {time.time() - started:.0f}s")
    log("完整外层包的组装需要全部组件就绪，当前仅编译组件已完成。")
    return 0


def cmd_gate(args, cfg: Config, ws: Workspace) -> int:
    target = Path(args.path) if args.path else ws.arch_out(args.arch)
    gate_script = REPO_ROOT / "scripts" / "lib" / "abi-gate.sh"
    result = run_gate(gate_script, args.glibc or cfg.glibc_max, target)
    log(result.output)
    return 0 if result.passed else 1


def cmd_status(args, cfg: Config, ws: Workspace) -> int:
    section("组件清单")
    log(f"包名 {cfg.package_name}  版本 {cfg.package_version}  顶层目录 {cfg.top_dir}")
    log(f"基线 glibc {cfg.glibc_max}   架构 {', '.join(cfg.architectures)}")
    log("")
    log(f"{'组件':<14}{'方式':<12}{'版本':<12}{'随包库'}")
    for comp in cfg.components.values():
        vendor = ",".join(comp.vendor) or "-"
        log(f"{comp.name:<14}{comp.build:<12}{comp.version or '(分架构)':<12}{vendor}")

    pending = cfg.unlocked()
    if pending:
        log("")
        log("以下条目的 sha256 尚未锁定：")
        for name in pending:
            log(f"  {name}")

    log("")
    section("工作区")
    for arch in cfg.architectures:
        out = ws.arch_out(arch)
        base = ws.arch_base(arch)
        built = sorted(p.name for p in out.iterdir() if p.is_dir()) if out.is_dir() else []
        based = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
        log(f"{arch}:")
        log(f"  已构建 {', '.join(built) if built else '(无)'}")
        log(f"  基准包 {', '.join(based) if based else '(无)'}")
    return 0


# ── 入口 ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build.py",
        description="跨操作系统离线安装包构建",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "components.yaml"),
        help="组件清单路径",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("SPRIXIN_WORKSPACE", "/root/sprixin-build"),
        help="构建工作区（存放 cache/out/dist）",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="显示清单与工作区状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("import-base", help="导入既有安装包作为基准")
    p.add_argument("package")
    p.add_argument("--arch", required=True)
    p.set_defaults(func=cmd_import_base)

    p = sub.add_parser("fetch", help="获取并校验上游源码")
    p.add_argument("--arch", required=True)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("build", help="编译、门禁、打包")
    p.add_argument("--arch", required=True)
    p.add_argument("--component", action="append", help="只构建指定组件，可重复")
    p.add_argument("--keep-sysroot", action="store_true", help="复用上次的 sysroot")
    p.add_argument("--no-package", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("gate", help="单独执行 ABI 门禁")
    p.add_argument("--arch", default="x86_64")
    p.add_argument("--path")
    p.add_argument("--glibc")
    p.set_defaults(func=cmd_gate)

    args = parser.parse_args(argv)

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        log(f"组件清单有误: {exc}")
        return 2

    ws = Workspace(Path(args.workspace))
    return args.func(args, cfg, ws)


if __name__ == "__main__":
    sys.exit(main())
