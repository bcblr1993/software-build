"""安装包组装。

产出结构必须与历史版本逐字一致 —— 现场的安装与启停流程依赖它：

    sprixinSoft/
    ├── install.sh
    ├── startup.sh
    ├── shutdown.sh
    ├── verify.sh
    └── software/
        ├── nginx-1.31.3.tar.gz
        └── ...

install.sh 的解压逻辑为：

    for i in $(ls software); do
      mkdir ${i%%-*} && tar -zxmf software/$i -C ${i%%-*} --strip-components 1
    done

由此产生两条硬性约束，本模块据此实现：

1. 内层归档必须命名为 `<组件名>-<版本>.tar.gz`，组件名中不能含 `-`，
   否则 ${i%%-*} 截出的目录名不对。
2. 内层归档必须恰有一层顶层目录，因为解压时 --strip-components 1。

归档同时做可复现处理（固定时间戳与属主、条目排序）：相同输入产出
相同 sha256，"未修改范围与上一版本一致"因而可由机器证明，无需人工比对。
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import tarfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 运行时残留：现有 ARM 包的 nginx 内层归档中混入了 logs/nginx.pid 与
# *_temp/ 目录，说明它是从跑过的目录直接打包的。此处统一剔除。
RUNTIME_JUNK = {
    "nginx.pid", "access.log", "error.log", ".DS_Store",
}
RUNTIME_JUNK_DIRS = {
    "client_body_temp", "proxy_temp", "fastcgi_temp", "uwsgi_temp", "scgi_temp",
    "__MACOSX",
}
# macOS 归档残留
JUNK_PREFIXES = ("._", ".AppleDouble")

# 可复现归档所用的固定时间戳（2020-01-01 UTC）
FIXED_MTIME = 1577836800

_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


class PackageError(Exception):
    pass


@dataclass
class InnerArchive:
    component: str
    version: str
    path: Path
    sha256: str
    size: int

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass
class PackageResult:
    path: Path
    sha256: str
    size: int
    top_dir: str
    inner: list[InnerArchive] = field(default_factory=list)

    def summary(self) -> str:
        mb = self.size / 1024 / 1024
        return f"{self.path.name}  {mb:.1f} MB  sha256 {self.sha256[:16]}…"


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _should_skip(name: str) -> bool:
    base = Path(name).name
    if base in RUNTIME_JUNK or base.startswith(JUNK_PREFIXES):
        return True
    parts = set(Path(name).parts)
    return bool(parts & RUNTIME_JUNK_DIRS)


def _normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """抹去与内容无关的元数据，使归档可复现。"""
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = FIXED_MTIME
    return info


class Packager:
    def __init__(self, *, out_dir: Path, log=print) -> None:
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.log = log

    # ── 内层归档 ────────────────────────────────────────────────────

    def make_inner(self, component: str, version: str, src_dir: Path) -> InnerArchive:
        """把一个组件目录打成内层归档。

        产出 <component>-<version>.tar.gz，内含单层顶层目录 <component>/。
        """
        if not _NAME_RE.match(component):
            raise PackageError(
                f"组件名 {component!r} 含有 install.sh 无法处理的字符："
                f"其解压逻辑用 ${{i%%-*}} 截取目录名，组件名中不能含 '-'"
            )
        src_dir = Path(src_dir)
        if not src_dir.is_dir():
            raise PackageError(f"组件目录不存在: {src_dir}")

        dest = self.out / f"{component}-{version}.tar.gz"
        dest.unlink(missing_ok=True)

        entries: list[tuple[str, Path]] = []
        for path in sorted(src_dir.rglob("*")):
            rel = path.relative_to(src_dir).as_posix()
            if _should_skip(rel):
                continue
            entries.append((f"{component}/{rel}", path))

        # gzip 的 mtime 也要固定，否则同样内容每次打包 sha256 都不同
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            root = tarfile.TarInfo(f"{component}")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            tar.addfile(_normalize(root))
            for arcname, path in entries:
                info = tar.gettarinfo(str(path), arcname=arcname)
                info = _normalize(info)
                if path.is_file() and not path.is_symlink():
                    with path.open("rb") as fh:
                        tar.addfile(info, fh)
                else:
                    tar.addfile(info)

        import gzip

        with dest.open("wb") as fh:
            with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=FIXED_MTIME) as gz:
                gz.write(raw.getvalue())

        digest = sha256_path(dest)
        size = dest.stat().st_size
        self.log(f"  {dest.name}  {size / 1024 / 1024:.1f} MB  {digest[:16]}…")
        return InnerArchive(component, version, dest, digest, size)

    # ── 外层包 ──────────────────────────────────────────────────────

    def assemble(
        self,
        *,
        package_name: str,
        top_dir: str,
        arch: str,
        version_tag: str,
        inner: list[InnerArchive],
        overlay_dir: Path,
        overlay_files: list[str],
        extra_files: dict[str, str] | None = None,
        dest_dir: Path | None = None,
    ) -> PackageResult:
        """组装最终安装包。"""
        dest_dir = Path(dest_dir or self.out)
        dest_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y-%m-%d")
        name = f"{package_name}-{arch}-{version_tag}-{stamp}.tar.gz"
        dest = dest_dir / name
        dest.unlink(missing_ok=True)

        self.log(f"组装 {name}")

        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            root = tarfile.TarInfo(top_dir)
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            tar.addfile(_normalize(root))

            # 顶层脚本
            for fname in overlay_files:
                src = Path(overlay_dir) / fname
                if not src.exists():
                    raise PackageError(f"overlay 缺少文件: {src}")
                info = tar.gettarinfo(str(src), arcname=f"{top_dir}/{fname}")
                info = _normalize(info)
                if fname.endswith(".sh"):
                    info.mode = 0o755
                with src.open("rb") as fh:
                    tar.addfile(info, fh)

            # 生成的文本文件（SOURCE / 版本标记等）
            for rel, content in (extra_files or {}).items():
                data = content.encode("utf-8")
                info = tarfile.TarInfo(f"{top_dir}/{rel}")
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(_normalize(info), io.BytesIO(data))

            # software/
            sw = tarfile.TarInfo(f"{top_dir}/software")
            sw.type = tarfile.DIRTYPE
            sw.mode = 0o755
            tar.addfile(_normalize(sw))

            for item in sorted(inner, key=lambda i: i.filename):
                info = tar.gettarinfo(
                    str(item.path), arcname=f"{top_dir}/software/{item.filename}"
                )
                info = _normalize(info)
                with item.path.open("rb") as fh:
                    tar.addfile(info, fh)

        import gzip

        with dest.open("wb") as fh:
            with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=FIXED_MTIME) as gz:
                gz.write(raw.getvalue())

        digest = sha256_path(dest)
        result = PackageResult(
            path=dest,
            sha256=digest,
            size=dest.stat().st_size,
            top_dir=top_dir,
            inner=list(inner),
        )
        self.log(f"  {result.summary()}")
        return result

    # ── 校验和清单 ──────────────────────────────────────────────────

    def write_checksums(self, result: PackageResult, dest: Path | None = None) -> Path:
        dest = Path(dest or result.path.parent / "SHA256SUMS")
        lines = [f"{result.sha256}  {result.path.name}"]
        lines += [
            f"{item.sha256}  software/{item.filename}"
            for item in sorted(result.inner, key=lambda i: i.filename)
        ]
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return dest

    # ── 与既有包比对 ────────────────────────────────────────────────

    @staticmethod
    def compare_with(previous: Path, result: PackageResult) -> dict[str, str]:
        """比对新包与既有包的内层归档，判断哪些组件确实发生了变化。

        用于回答"本次只动了 nginx，其余组件未修改"这类问题 ——
        历史上这是靠人工逐个算哈希填进报告的。
        """
        changed: dict[str, str] = {}
        try:
            with tarfile.open(previous, "r:gz") as tar:
                old: dict[str, str] = {}
                for member in tar.getmembers():
                    if "/software/" not in member.name or not member.isfile():
                        continue
                    fh = tar.extractfile(member)
                    if fh is None:
                        continue
                    digest = hashlib.sha256()
                    for block in iter(lambda: fh.read(1024 * 1024), b""):
                        digest.update(block)
                    old[Path(member.name).name] = digest.hexdigest()
        except (OSError, tarfile.TarError) as exc:
            return {"__error__": f"无法读取既有包 {previous}: {exc}"}

        new = {i.filename: i.sha256 for i in result.inner}

        for name, digest in new.items():
            if name not in old:
                changed[name] = "新增"
            elif old[name] != digest:
                changed[name] = "已变更"
        for name in old:
            if name not in new:
                changed[name] = "已移除"
        return changed


def clean_tree(path: Path) -> int:
    """就地清理目录中的运行时残留，返回删除数量。"""
    removed = 0
    for item in sorted(Path(path).rglob("*"), reverse=True):
        rel = item.relative_to(path).as_posix()
        if _should_skip(rel):
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            removed += 1
    return removed
