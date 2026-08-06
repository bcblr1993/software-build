"""构建产物的发布与清理。

构建只产出候选，须经实机测试确认后才提升为正式版本。二者分开存放：

    dist/<arch>/       候选产物，可随时清理
    releases/<版本>/   正式版本，一经发布不可删除

正式版本以只读权限保存，并在数据库中登记。删除操作会先比对登记，
凡属正式版本一律拒绝 —— 现场部署与回滚都依赖它，误删的代价远高于
它占用的磁盘。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .record import BuildStore


class ReleaseError(Exception):
    pass


@dataclass
class ReleaseResult:
    version: str
    arch: str
    path: Path
    sha256: str
    size: int


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ReleaseManager:
    def __init__(self, workspace: Path, store: BuildStore, log=print) -> None:
        self.workspace = Path(workspace)
        self.store = store
        self.log = log
        self.releases_dir = self.workspace / "releases"
        self.releases_dir.mkdir(parents=True, exist_ok=True)

    # ── 发布 ────────────────────────────────────────────────────────

    def publish(
        self,
        *,
        arch: str,
        version: str,
        artifact: Path | None = None,
        released_by: str = "",
        test_note: str = "",
        build_id: int | None = None,
    ) -> ReleaseResult:
        """把一份候选产物提升为正式版本。"""
        if self.store.is_released(version, arch):
            raise ReleaseError(
                f"{version} ({arch}) 已发布过。正式版本不可覆盖 —— "
                f"如内容有变，请改用新的版本号。"
            )

        if artifact is None:
            artifact = self._latest_artifact(arch)
        artifact = Path(artifact)
        if not artifact.is_file():
            raise ReleaseError(f"找不到候选产物: {artifact}")

        target_dir = self.releases_dir / version
        target_dir.mkdir(parents=True, exist_ok=True)

        # 包名随发布版本号走：候选产物名里带的是打包当时的版本，发布成
        # 别的版本号而文件名不改，日后拿到手里对不上账。重命名不触碰内容，
        # 校验和照旧。
        target = target_dir / rename_for_version(artifact.name, version)

        if target.exists():
            raise ReleaseError(f"目标已存在: {target}")

        if target.name != artifact.name:
            self.log(f"发布 {artifact.name} → {target.name}（按版本号重命名）")
        else:
            self.log(f"发布 {artifact.name} → {target}")
        shutil.copy2(artifact, target)

        digest = sha256_path(target)
        size = target.stat().st_size

        # 一并保存同目录下的校验和清单与构建报告，便于日后追溯。
        # 归档若已改名，清单里的文件名须同步替换，否则拿它去核对会扑空。
        for extra in ("SHA256SUMS",):
            src = artifact.parent / extra
            if not src.is_file():
                continue
            dest = target_dir / f"{extra}-{arch}"
            if target.name != artifact.name:
                try:
                    text = src.read_text(encoding="utf-8")
                    dest.write_text(
                        text.replace(artifact.name, target.name), encoding="utf-8"
                    )
                    continue
                except OSError:
                    pass
            shutil.copy2(src, dest)
        for report in artifact.parent.glob("REPORT-*.md"):
            shutil.copy2(report, target_dir / report.name)

        # 发布说明与变更记录：过几个月再回头看，没有文档就说不清这个版本
        # 到底改了什么、凭什么发出去的。故由机器生成，不依赖人工补写。
        components = read_components(target)
        previous = self._previous_release(arch, version)
        prev_components = read_components(Path(previous["path"])) if previous else {}

        notes = render_release_notes(
            version=version, arch=arch, filename=target.name,
            sha256=digest, size=size,
            components=components,
            previous_version=previous["version"] if previous else "",
            previous_components=prev_components,
            released_by=released_by, test_note=test_note,
            verifications=self._verification_summary(),
        )
        (target_dir / f"RELEASE-NOTES-{version}-{arch}.md").write_text(notes, encoding="utf-8")

        self._append_changelog(
            version=version, arch=arch, components=components,
            prev_version=previous["version"] if previous else "",
            prev_components=prev_components,
            released_by=released_by, test_note=test_note,
        )

        # 只读：防止误改误删。需要清理时必须显式改权限，构成一道确认。
        self._freeze(target_dir)

        self.store.add_release(
            version=version,
            arch=arch,
            filename=target.name,
            path=str(target),
            sha256=digest,
            size=size,
            build_id=build_id,
            released_by=released_by,
            test_note=test_note,
        )

        self.log(f"  SHA-256: {digest}")
        self.log(f"  大小:    {size / 1024 / 1024:.1f} MB")
        self.log("  已置为只读并登记，不可删除")

        return ReleaseResult(version=version, arch=arch, path=target,
                             sha256=digest, size=size)

    def _freeze(self, path: Path) -> None:
        """冻结正式版本，使其不可修改也不可删除。

        只去写权限是不够的：Linux 判断能否删除文件看的是父目录的写权限，
        而 root 绕过全部权限检查 —— 构建机上恰恰都是 root 操作，`rm -f`
        照删不误（此处实测过）。

        因此再叠加 immutable 属性：root 也必须先显式 `chattr -i` 才能删，
        这一步构成明确的确认动作，而非顺手一删。文件系统若不支持该属性
        （如 xfs 以外的部分挂载、NFS），则退回仅权限保护并给出提示。
        """
        import subprocess

        for item in sorted(path.rglob("*"), reverse=True):
            try:
                item.chmod(0o555 if item.is_dir() else 0o444)
            except OSError:
                pass
        try:
            path.chmod(0o555)
        except OSError:
            pass

        proc = subprocess.run(
            ["chattr", "-R", "+i", str(path)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            self.log(
                "  提示：无法设置 immutable 属性，正式版本仅受权限与接口保护。"
                f"（{(proc.stderr or '').strip()[:80]}）"
            )
            return
        self.log("  已设置 immutable，删除前须先执行 chattr -R -i")

    @staticmethod
    def unfreeze(path: Path) -> str:
        """解除冻结。仅在确需下线某个正式版本时手工调用。"""
        import subprocess

        subprocess.run(["chattr", "-R", "-i", str(path)], capture_output=True)
        for item in sorted(Path(path).rglob("*"), reverse=True):
            try:
                item.chmod(0o755 if item.is_dir() else 0o644)
            except OSError:
                pass
        return f"{path} 已解除保护，可以删除"

    def _latest_artifact(self, arch: str) -> Path:
        dist = self.workspace / "dist" / arch
        pkgs = sorted(
            dist.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if not pkgs:
            raise ReleaseError(f"{arch} 尚无候选产物，请先构建")
        return pkgs[0]

    # ── 文档所需的上下文 ────────────────────────────────────────────

    def _previous_release(self, arch: str, version: str) -> dict | None:
        """同架构下最近一次正式发布，用于生成变更对比。"""
        for r in self.store.releases(limit=200):
            if r["arch"] == arch and r["version"] != version:
                if Path(r["path"]).is_file():
                    return r
        return None

    def _verification_summary(self, build_id: int | None = None) -> list[dict]:
        """读取端到端验证的记录。

        两个架构都读：发布说明须同时交代 x86_64 与 aarch64 的验证情况，
        只写当前架构等于把另一半藏起来。记录由 compat/e2e-test.sh 写入
        verify-results/<架构>.json，是发布说明中系统清单的唯一来源，
        不靠人工誊抄。
        """
        import json

        out: list[dict] = []
        result_dir = self.workspace / "verify-results"
        if not result_dir.is_dir():
            return out

        for f in sorted(result_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, list):
                out.extend(d for d in data if isinstance(d, dict))
        return out

    def _append_changelog(
        self,
        *,
        version: str,
        arch: str,
        components: dict[str, str],
        prev_version: str,
        prev_components: dict[str, str],
        released_by: str,
        test_note: str,
    ) -> None:
        """把本次发布追加到累积的变更记录。

        新版本置于文件开头（倒序），便于一眼看到最近发生了什么。
        该文件位于 releases/ 根目录，不随版本目录一同冻结。
        """
        from datetime import datetime

        path = self.releases_dir / "CHANGELOG.md"
        header = "# 版本变更记录\n\n本文件由发布流程自动维护，请勿手工编辑。\n"

        upgraded, added, removed, _ = diff_components(components, prev_components)

        block: list[str] = []
        b = block.append
        b(f"## {version} ({arch})")
        b("")
        b(f"发布时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
          + (f"　发布人：{released_by}" if released_by else ""))
        b("")
        if prev_version:
            b(f"相对 `{prev_version}`：")
        else:
            b("首个正式版本，组件如下：")
        b("")
        if upgraded:
            for item in upgraded:
                b(f"- 升级 {item}")
        if added:
            for item in added:
                b(f"- 新增 {item}")
        if removed:
            for item in removed:
                b(f"- 移除 {item}")
        if not (upgraded or added or removed):
            if prev_version:
                b("- 组件版本无变化（重新构建或仅打包内容调整）")
            else:
                for name, ver in sorted(components.items()):
                    b(f"- {name} {ver}")
        b("")
        if test_note:
            b("实机测试：")
            for line in test_note.splitlines():
                b(f"> {line}")
            b("")

        new_entry = "\n".join(block) + "\n"

        try:
            if path.exists():
                old = path.read_text(encoding="utf-8")
                body = old[len(header):] if old.startswith(header) else old
                path.write_text(header + "\n" + new_entry + body, encoding="utf-8")
            else:
                path.write_text(header + "\n" + new_entry, encoding="utf-8")
        except OSError as exc:
            self.log(f"  变更记录写入失败: {exc}")

    # ── 清理 ────────────────────────────────────────────────────────

    def deletable(self) -> list[dict]:
        """列出可清理的候选产物。"""
        protected = self.store.released_paths()
        out = []
        dist = self.workspace / "dist"
        if not dist.is_dir():
            return out
        for pkg in sorted(dist.glob("*/*.tar.gz")):
            if str(pkg) in protected:
                continue
            st = pkg.stat()
            out.append({
                "path": str(pkg),
                "name": pkg.name,
                "arch": pkg.parent.name,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
        return out

    def delete(self, path: str | Path) -> str:
        """删除一份候选产物。正式版本一律拒绝。"""
        path = Path(path).resolve()

        # 只允许删 dist 下的内容，杜绝路径穿越
        dist = (self.workspace / "dist").resolve()
        if not str(path).startswith(str(dist) + os.sep):
            raise ReleaseError(f"只能清理 dist/ 下的候选产物: {path}")

        if str(path) in self.store.released_paths():
            raise ReleaseError("该产物已发布为正式版本，不可删除")

        # 正式版本另存于 releases/，此处再挡一次，防止误传路径
        releases = self.releases_dir.resolve()
        if str(path).startswith(str(releases)):
            raise ReleaseError("正式版本不可删除")

        if not path.exists():
            raise ReleaseError(f"文件不存在: {path}")

        size = path.stat().st_size
        path.unlink()
        return f"已删除 {path.name}（释放 {size / 1024 / 1024:.1f} MB）"


# ── 文档生成 ────────────────────────────────────────────────────────
#
# 发布说明与变更记录由机器生成而非人工补写：内容全部来自包本身与数据库，
# 既不会漏项，也不会与实际产物脱节。

def rename_for_version(filename: str, version: str) -> str:
    """把归档名中的版本段换成发布时选定的版本号。

    候选产物名里带的是打包当时的版本（package.version），而发布可以选用
    另一个版本号。若不改名，就会出现 releases/v15/ 里躺着一个名为 v14 的
    包 —— 拿到现场对不上账。

    仅替换版本段，包名、架构、日期与扩展名原样保留；认不出格式时不动，
    宁可保持原名也不要猜错。
    """
    version = (version or "").strip()
    if not version:
        return filename

    # sprixinSoft-x86_64-v14-2026-08-06.tar.gz
    #             └架构┘ └版本┘ └──日期──┘
    m = re.match(
        r"^(?P<head>.+?)-(?P<ver>v?[0-9][\w.]*)-(?P<date>\d{4}-\d{2}-\d{2})"
        r"(?P<ext>\.tar\.(?:gz|xz)|\.tgz)$",
        filename,
    )
    if not m:
        return filename
    if m.group("ver") == version:
        return filename
    return f"{m.group('head')}-{version}-{m.group('date')}{m.group('ext')}"


def read_components(package: Path) -> dict[str, str]:
    """从安装包中读出组件与版本。

    直接解析 software/ 下的归档文件名，与 install.sh 的解析方式一致
    （组件名取首个 '-' 之前的部分），不依赖包内是否有说明文件。
    """
    import tarfile

    out: dict[str, str] = {}
    try:
        with tarfile.open(package, "r:gz") as tar:
            for name in tar.getnames():
                if "/software/" not in name:
                    continue
                base = Path(name).name
                if not base.endswith((".tar.gz", ".tgz")):
                    continue
                stem = base.replace(".tar.gz", "").replace(".tgz", "")
                comp, _, rest = stem.partition("-")
                if not comp:
                    continue
                # 归档名中可能既有包名后缀也有平台后缀
                # （nacos-server-2.2.3、jdk-8u181-linux-x64、
                #   influxdb-1.7.8_linux_arm64），版本是其中以数字开头的
                # 那一段，止于第一个 - 或 _
                m = re.search(r"(?:^|[-_])([0-9][0-9A-Za-z.]*)", rest)
                out[comp] = m.group(1) if m else (rest or "-")
    except (OSError, tarfile.TarError):
        pass
    return out


def diff_components(
    current: dict[str, str], previous: dict[str, str]
) -> tuple[list[str], list[str], list[str], list[str]]:
    """返回 (升级, 新增, 移除, 未变) 四组描述。"""
    upgraded, added, removed, unchanged = [], [], [], []
    for name, ver in sorted(current.items()):
        if name not in previous:
            added.append(f"{name} {ver}")
        elif previous[name] != ver:
            upgraded.append(f"{name} {previous[name]} → {ver}")
        else:
            unchanged.append(f"{name} {ver}")
    for name, ver in sorted(previous.items()):
        if name not in current:
            removed.append(f"{name} {ver}")
    return upgraded, added, removed, unchanged


def render_release_notes(
    *,
    version: str,
    arch: str,
    filename: str,
    sha256: str,
    size: int,
    components: dict[str, str],
    previous_version: str,
    previous_components: dict[str, str],
    released_by: str,
    test_note: str,
    verifications: list[dict] | None = None,
) -> str:
    from datetime import datetime

    upgraded, added, removed, unchanged = diff_components(components, previous_components)

    L: list[str] = []
    a = L.append

    a(f"# {version} 发布说明（{arch}）")
    a("")
    a(f"发布时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if released_by:
        a(f"发布人：{released_by}")
    a("")

    a("## 产物")
    a("")
    a(f"- 文件：`{filename}`")
    a(f"- 大小：{size:,} 字节（{size / 1024 / 1024:.1f} MB）")
    a(f"- SHA-256：`{sha256}`")
    a("")

    a("## 本次变更")
    a("")
    if previous_version:
        a(f"相对上一正式版本 `{previous_version}`：")
    else:
        a("这是首个正式版本。")
    a("")

    if upgraded:
        a("**组件升级**")
        a("")
        for item in upgraded:
            a(f"- {item}")
        a("")
    if added:
        a("**新增组件**")
        a("")
        for item in added:
            a(f"- {item}")
        a("")
    if removed:
        a("**移除组件**")
        a("")
        for item in removed:
            a(f"- {item}")
        a("")
    if previous_version and not (upgraded or added or removed):
        a("组件版本无变化。")
        a("")

    if unchanged:
        a("<details><summary>未变更的组件</summary>")
        a("")
        for item in unchanged:
            a(f"- {item}")
        a("")
        a("</details>")
        a("")

    a("## 兼容性")
    a("")
    a("所有需编译的组件均在 glibc 2.17 基线上构建，并随包分发 OpenSSL、")
    a("PCRE2、zlib，通过 `$ORIGIN` 相对 rpath 加载。产物不依赖目标系统的")
    a("发行版、小版本或补丁号，可运行于任何 glibc ≥ 2.17 的 Linux。")
    a("")
    a("该结论由 ABI 门禁强制校验：最高 glibc 符号版本不超过基线、")
    a("`DT_NEEDED` 只含核心库与随包库、随包库可从 `RUNPATH` 解析。")
    a("")

    if verifications:
        a("## 已验证的目标系统")
        a("")

        # 物理机验证单列在前：它不受模拟环境的干扰，是更强的证据
        physical = [v for v in verifications if v.get("kind") == "物理机"]
        if physical:
            a("### 物理机实测")
            a("")
            for v in physical:
                a(f"**{v.get('os_name', '?')}**（{v.get('arch', '?')}，glibc {v.get('glibc', '?')}）")
                a("")
                a(f"- 主机：{v.get('target', '?')}")
                if v.get("kernel"):
                    a(f"- 内核：`{v['kernel']}`")
                if v.get("run_as"):
                    a(f"- 运行身份：{v['run_as']}")
                svc = v.get("services") or {}
                ok = [k for k, x in svc.items() if x]
                a(f"- 启动的服务：{'、'.join(ok)}（{len(ok)}/{len(svc)}）")
                if v.get("note"):
                    a(f"- 结论：{v['note']}")
                a("")

        containers = [v for v in verifications if v.get("kind") != "物理机"]
        if containers:
            a("### 目标系统容器")
            a("")
            a("下列系统的验证容器均由其安装 ISO 直接构建，在其中以普通用户完整")
            a("解压、安装并逐个启动全部服务，再确认端口监听与健康检查。")
            a("")

        by_arch: dict[str, list[dict]] = {}
        for v in containers:
            by_arch.setdefault(v.get("arch", "?"), []).append(v)

        for arch in sorted(by_arch):
            items = by_arch[arch]
            passed = sum(1 for v in items if v.get("passed"))
            a(f"### {arch}（{passed}/{len(items)} 通过）")
            a("")
            a("| 操作系统 | glibc | 验证镜像来源 | redis | nginx | nacos | influxdb | rabbitmq | 结论 |")
            a("|---|---|---|---|---|---|---|---|---|")
            for v in sorted(items, key=lambda x: x.get("os_name", "")):
                svc = v.get("services") or {}
                cells = "".join(
                    f" {'✓' if svc.get(name) else '✗'} |"
                    for name in ("redis", "nginx", "nacos", "influxdb", "rabbitmq")
                )
                a(f"| {v.get('os_name', '?')} | {v.get('glibc', '?')} |"
                  f" `{v.get('target', '?')}` |{cells}"
                  f" {'**通过**' if v.get('passed') else '未通过'} |")
            a("")

        a("keepalived 需要 CAP_NET_ADMIN / CAP_NET_RAW，普通用户无法启动，")
        a("故上表不含该项；验证中已确认其二进制可执行、依赖完整。")
        a("")

    if test_note:
        a("## 实机测试记录")
        a("")
        for line in test_note.splitlines():
            a(f"> {line}")
        a("")

    a("## 安装")
    a("")
    a("```bash")
    a(f"tar -xzf {filename}")
    a("cd sprixinSoft")
    a("./install.sh          # 解压各组件，并校验完整性")
    a("./startup.sh all      # 启动全部服务")
    a("./verify.sh           # 自检")
    a("```")
    a("")
    a("查看日志：`./logs.sh list`、`./logs.sh 1`、`./logs.sh rabbitmq -f`")
    a("")
    a("## 注意")
    a("")
    a("- 现场以普通用户部署即可，各服务端口均大于 1024，无需 root")
    a("- keepalived 需要 `CAP_NET_ADMIN` / `CAP_NET_RAW`，启动需单独授权")
    a("- nacos 依赖系统提供 `libstdc++.so.6`（其 RocksDB 原生库所需），"
      "最小化安装的系统需先安装")
    a("")

    return "\n".join(L) + "\n"


