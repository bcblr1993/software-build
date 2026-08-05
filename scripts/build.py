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
import json
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
from sprixin_build.report import (  # noqa: E402
    ComponentInfo,
    load_secrets,
    render_source_file,
    render_upgrade_report,
)

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
    archives: dict[str, str] = {}
    for archive in sorted(software.iterdir()):
        if not archive.name.endswith((".tar.gz", ".tgz")):
            continue
        # install.sh 用 ${i%%-*} 取组件名，此处保持一致
        comp = archive.name.split("-")[0]
        archives[comp] = archive.name
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

    # 记录组件与原始归档名的对应关系。
    # 打包时需要还原原始文件名（含版本号），且基准包中可能存在清单未定义的
    # 组件 —— ARM 包的 compat-libs 即是一例，漏掉它会导致新包缺少内容。
    manifest = {
        "source_package": src.name,
        "arch": args.arch,
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "components": archives,
    }
    (dest / "import-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    shutil.rmtree(staging)
    log(f"\n基准包已导入 {count} 个组件 → {dest}")
    unknown = [n for n in archives if n not in cfg.components]
    if unknown:
        log(f"其中 {', '.join(unknown)} 未在组件清单中定义，将按原样打包")
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

    return do_package(args, cfg, ws, arch=arch, gate_summary=result.summary(),
                      started=started)


def do_package(args, cfg: Config, ws: Workspace, *, arch: str,
               gate_summary: str = "", started: float | None = None) -> int:
    """组装完整安装包。

    组件来源有两处：新编译的取自 out/，其余（Java/Go 产物等无需编译的）
    取自基准包。基准包中可能存在组件清单未定义的内容 —— ARM 包的
    compat-libs 即是一例，必须一并打包，否则新包会比原包少东西。
    """
    section(f"组装安装包（{arch}）")

    base_dir = ws.arch_base(arch)
    out_dir = ws.arch_out(arch)

    manifest_path = base_dir / "import-manifest.json"
    archives: dict[str, str] = {}
    if manifest_path.exists():
        try:
            archives = json.loads(manifest_path.read_text(encoding="utf-8")).get("components", {})
        except (OSError, json.JSONDecodeError):
            archives = {}

    # 汇总所有组件：新编译的优先，其余取自基准包
    sources: dict[str, Path] = {}
    if base_dir.is_dir():
        for d in sorted(base_dir.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                sources[d.name] = d
    if out_dir.is_dir():
        for d in sorted(out_dir.iterdir()):
            if d.is_dir():
                sources[d.name] = d

    for name in cfg.excluded_components:
        if sources.pop(name, None) is not None:
            log(f"  排除组件: {name}（已在清单中声明不再发布）")

    if not sources:
        log("没有可打包的组件，请先执行 build 或 import-base")
        return 1

    stage_dir = ws.dist / arch / "software"
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    packager = Packager(out_dir=stage_dir, log=log)

    inner = []
    infos: list[ComponentInfo] = []
    try:
        for name, path in sorted(sources.items()):
            comp = cfg.components.get(name)
            if comp:
                version = comp.version_for(arch)
                kind = comp.build
            else:
                # 清单未定义：从基准包的原始归档名还原版本，保持文件名一致
                original = archives.get(name, "")
                version = original[len(name) + 1:].replace(".tar.gz", "").replace(".tgz", "") or "1.0"
                kind = "repack"

            removed = clean_tree(path)
            if removed:
                log(f"  {name}: 清理运行时残留 {removed} 项")

            item = packager.make_inner(
                name, version, path, original_name=archives.get(name, "")
            )
            inner.append(item)
            infos.append(ComponentInfo(
                name=name, version=version, build=kind,
                sha256=item.sha256, size=item.size,
                source_url=(comp.url_for(arch) or "") if comp else "",
                source_sha256=(comp.sha256 if comp and comp.locked else ""),
            ))
    except PackageError as exc:
        log(str(exc))
        return 1

    # ── 与既有包对照 ────────────────────────────────────────────
    stamp = datetime.now().strftime("%Y-%m-%d")
    will_produce = f"{cfg.package_name}-{arch}-{cfg.package_version}-{stamp}.tar.gz"
    previous = _find_previous_package(ws, arch, exclude=will_produce)
    changes: dict[str, str] = {}

    # ── 生成 SOURCE 文件 ────────────────────────────────────────
    secrets = load_secrets(ws.root / "secrets" / "package-secrets.yaml")
    if not secrets:
        log("提示: 未找到 secrets/package-secrets.yaml，SOURCE 中将不含默认账号信息")

    verified = _load_verified_targets(ws, arch)

    source_text = render_source_file(
        package_name=cfg.package_name,
        package_version=cfg.package_version,
        arch=arch,
        baseline_glibc=cfg.glibc_max,
        components=infos,
        vendored={n: l.version for n, l in cfg.vendored_libs.items()},
        secrets=secrets,
        verified_targets=verified,
    )

    extra = {
        f"SOURCE-{cfg.package_name}-{arch}.txt": source_text,
        f"version.{cfg.package_version}": f"{cfg.package_version}\n{arch}\n",
    }

    # 基准包顶层的其余文件（如 README-LinxOS-6.0.99.md）一并保留：
    # 现场可能依赖它们，不该因为换了构建方式就凭空消失。
    generated_prefixes = ("SOURCE-", "version.", "import-manifest")
    for item in sorted(base_dir.iterdir()):
        if not item.is_file():
            continue
        if item.name in cfg.overlay_files:
            continue
        if item.name.startswith(generated_prefixes):
            continue
        try:
            extra[item.name] = item.read_text(encoding="utf-8")
            log(f"  保留基准包文件: {item.name}")
        except (OSError, UnicodeDecodeError):
            continue

    # ── 组装 ────────────────────────────────────────────────────
    overlay_dir = REPO_ROOT / "overlay"
    missing = [f for f in cfg.overlay_files if not (overlay_dir / f).exists()]
    if missing:
        # 缺失的脚本从基准包补齐，保证现场行为不变
        for f in missing:
            src = base_dir / f
            if src.exists():
                shutil.copy2(src, overlay_dir / f)
                log(f"  从基准包补入 overlay/{f}")
        missing = [f for f in cfg.overlay_files if not (overlay_dir / f).exists()]
        if missing:
            log(f"overlay 缺少文件: {', '.join(missing)}")
            return 1

    try:
        result = packager.assemble(
            package_name=cfg.package_name,
            top_dir=cfg.top_dir,
            arch=arch,
            version_tag=cfg.package_version,
            inner=inner,
            overlay_dir=overlay_dir,
            overlay_files=cfg.overlay_files,
            extra_files=extra,
            dest_dir=ws.dist / arch,
        )
    except PackageError as exc:
        log(str(exc))
        return 1

    checksums = packager.write_checksums(result, ws.dist / arch / "SHA256SUMS")
    log(f"  校验和清单: {checksums}")

    if previous:
        log(f"\n与既有包对照: {previous.name}")
        changes = Packager.compare_with(previous, result)
        if "__error__" in changes:
            log(f"  {changes['__error__']}")
            changes = {}
        else:
            for info in infos:
                info.changed = changes.get(f"{info.name}-{info.version}.tar.gz", "")
            if changes:
                for fname, state in sorted(changes.items()):
                    log(f"  {state}: {fname}")
            else:
                log("  所有内层归档与既有包完全一致")

    # ── 升级报告 ────────────────────────────────────────────────
    report = render_upgrade_report(
        package_name=cfg.package_name,
        package_version=cfg.package_version,
        previous_version=previous.name if previous else "",
        arch=arch,
        package_sha256=result.sha256,
        package_size=result.size,
        baseline_glibc=cfg.glibc_max,
        components=infos,
        gate_summary=gate_summary or "本次未执行",
        verified_targets=verified,
        duration_s=int(time.time() - started) if started else None,
    )
    report_path = ws.dist / arch / f"REPORT-{cfg.package_version}-{arch}.md"
    report_path.write_text(report, encoding="utf-8")
    log(f"  构建报告: {report_path}")

    log("")
    log(f"安装包: {result.path}")
    log(f"SHA-256: {result.sha256}")
    return 0


def _find_previous_package(ws: Workspace, arch: str, exclude: str = "") -> Path | None:
    """找出用于对照的既有包。

    以基准包来源为准 —— 那才是现场正在使用的上一个正式版本。dist 下的
    产物可能是同一版本的重复构建，拿它对照会得出"无变化"的空结论。
    """
    manifest = ws.arch_base(arch) / "import-manifest.json"
    if manifest.exists():
        try:
            name = json.loads(manifest.read_text(encoding="utf-8")).get("source_package")
        except (OSError, json.JSONDecodeError):
            name = None
        if name:
            for root in (Path("/root/sprixinSoft_v13"), ws.root, ws.dist / arch):
                p = root / name
                if p.exists():
                    return p

    # 退而求其次：dist 下最近一次的产物，但要排除本次即将覆盖的同名文件
    candidates = sorted(
        (p for p in (ws.dist / arch).glob("*.tar.gz")
         if p.is_file() and p.name != exclude),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _load_verified_targets(ws: Workspace, arch: str) -> list[tuple[str, str, bool]]:
    """读取目标系统验证结果（由 compat/verify-all.sh 产生）。"""
    path = ws.root / "verify-results" / f"{arch}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [(d.get("os", "?"), d.get("glibc", "?"), bool(d.get("passed"))) for d in data]


def cmd_package(args, cfg: Config, ws: Workspace) -> int:
    return do_package(args, cfg, ws, arch=args.arch)


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

    p = sub.add_parser("package", help="组装安装包（不重新编译）")
    p.add_argument("--arch", required=True)
    p.set_defaults(func=cmd_package)

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
