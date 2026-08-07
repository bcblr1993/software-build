"""源码获取与完整性校验。

所有上游归档统一落在 cache/ 下，按文件名去重。已存在且校验通过的
不重复下载 —— 内网出口带宽有限，重复拉取动辄数百 MB 的归档不可接受。

校验和来自 components.yaml。若某项尚未锁定（值为 LOCK），下载后会
算出实际值并提示写回清单，但不会自动改写：校验和应经人工核对一次
再提交，否则完整性校验就失去了意义。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Component, Config, VendoredLib

CHUNK = 1024 * 1024

# 部分上游站点（如麒麟信安镜像站）带 WAF，默认 UA 会被拦成 HTML 页面。
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class FetchError(Exception):
    pass


@dataclass
class FetchResult:
    name: str
    path: Path
    sha256: str
    expected: str
    downloaded: bool
    #: 已登记上游时，此处记录用何种手段证明了归档来源
    attestation: object | None = None

    @property
    def verified(self) -> bool:
        return self.expected == self.sha256

    @property
    def unlocked(self) -> bool:
        """校验和尚未在清单中锁定。

        已通过上游校验的条目不算未锁定 —— 它的可信度来自签名或上游
        哈希清单，本就不依赖清单里预先抄好的那一行。
        """
        if self.attestation is not None:
            return False
        return not _is_sha256(self.expected)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


class Fetcher:
    def __init__(self, cache_dir: Path, log=print, repo_root: Path | None = None) -> None:
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
        self._resolver = None

    @property
    def resolver(self):
        """延迟构造：未登记上游的场合不必付出任何代价。"""
        if self._resolver is None:
            from .upstream import UpstreamResolver

            self._resolver = UpstreamResolver(self.repo_root, self.cache, log=self.log)
        return self._resolver

    # ── 已登记上游者走这条路 ────────────────────────────────────────

    def fetch_upstream(self, spec, version: str) -> FetchResult:
        """按上游登记信息取回归档，并证明其来源。

        与 fetch() 的差别在于「期望哈希从哪来」：这里来自上游的签名或
        哈希清单，而不是清单里预先抄好的一行。因此升级版本时只需改
        版本号，不必再人工填 sha256 —— 填错的那类故障从根上没有了。
        """
        from .upstream import UpstreamError

        url = spec.tarball_url(version)
        target = self.cache / Path(url).name

        # hash-index 能在下载前给出期望值，于是版本号写错、归档被换
        # 这类问题在传输阶段就暴露，不必等几百 MB 下完。
        pre = self.resolver.expected_hash(spec, version)
        if pre is not None and target.exists() and target.stat().st_size > 0:
            if sha256_file(target) == pre.sha256:
                self.log(f"  [缓存] {target.name}")
                pre.tarball = url
                return FetchResult(spec.name, target, pre.sha256, pre.sha256, False, pre)
            self.log(f"  [失效] {target.name} 与上游哈希不符，重新下载")
            target.unlink()

        downloaded = False
        if not target.exists() or target.stat().st_size == 0:
            self.log(f"  [下载] {url}")
            self._download(url, target)
            downloaded = True
            self._reject_html(spec.name, target)

        try:
            att = self.resolver.verify(spec, version, target)
        except UpstreamError:
            # 校验不过的归档不留在 cache 里，否则下次会被当成"已缓存"
            # 直接采用 —— 那等于把一次失败的校验变成永久的放行。
            target.unlink(missing_ok=True)
            raise

        att.tarball = url
        self.log(f"  [校验] {att.summary()}")
        return FetchResult(spec.name, target, att.sha256, att.sha256, downloaded, att)

    def _reject_html(self, name: str, target: Path) -> None:
        head = target.read_bytes()[:512].lstrip().lower()
        if head.startswith((b"<!doctype", b"<html")):
            target.unlink()
            raise FetchError(
                f"{name}: 下载到的是 HTML 页面而非归档，"
                f"上游可能有 WAF 拦截或地址已失效"
            )

    def fetch(self, name: str, url: str, expected: str) -> FetchResult:
        target = self.cache / Path(url).name
        downloaded = False

        if target.exists() and target.stat().st_size > 0:
            actual = sha256_file(target)
            if not _is_sha256(expected) or actual == expected:
                self.log(f"  [缓存] {target.name}")
                return FetchResult(name, target, actual, expected, False)
            self.log(f"  [失效] {target.name} 校验和不符，重新下载")
            target.unlink()

        self.log(f"  [下载] {url}")
        self._download(url, target)
        downloaded = True

        if not target.exists() or target.stat().st_size == 0:
            raise FetchError(f"{name}: 下载结果为空")

        # WAF 拦截时返回的是 HTML 页面而非归档，提前识别以免在解压阶段
        # 才报出难以理解的错误。
        head = target.read_bytes()[:512].lstrip().lower()
        if head.startswith((b"<!doctype", b"<html")):
            target.unlink()
            raise FetchError(
                f"{name}: 下载到的是 HTML 页面而非归档，"
                f"上游可能有 WAF 拦截或地址已失效"
            )

        actual = sha256_file(target)
        return FetchResult(name, target, actual, expected, downloaded)

    def _download(self, url: str, target: Path) -> None:
        tmp = target.with_suffix(target.suffix + ".part")
        cmd = [
            "curl", "-fL",
            "--retry", "5",
            "--retry-delay", "3",
            "--connect-timeout", "30",
            "-A", USER_AGENT,
            "-C", "-",
            "-o", str(tmp),
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise FetchError(
                f"下载失败（curl 退出码 {proc.returncode}）: {url}\n"
                f"{proc.stderr.strip()[:300]}"
            )
        shutil.move(str(tmp), str(target))

    # ── 批量 ────────────────────────────────────────────────────────

    def fetch_all(
        self, cfg: Config, arch: str, skip: set[str] | None = None,
        only: set[str] | None = None,
    ) -> list[FetchResult]:
        """获取指定架构所需的全部上游归档。

        skip 用于排除已有上传件的组件：nacos 与 influxdb 允许自行改好后
        上传，打包时用的就是上传的那一份，再把上游原版拉一遍纯属白费 ——
        nacos 单个就有 200MB，在内网 1M 出口上要半小时。

        only 用于只编译个别组件的场合。此前无论要编译什么都会遍历全部
        上游，于是"只重编 nginx"也要去取 rabbitmq 的签名，一次网络抖动
        就把整个构建带崩 —— 而那个组件根本没打算动。
        """
        results: list[FetchResult] = []
        ups = getattr(cfg, "upstreams", {}) or {}
        skip = skip or set()

        self.log(f"随包依赖库（{len(cfg.vendored_libs)} 项）")
        for lib in cfg.vendored_libs.values():
            results.append(self._fetch_lib(lib, ups))

        comps = [c for c in cfg.components.values() if not c.local_only]
        if only:
            comps = [c for c in comps if c.name in only]
        self.log(f"组件（{len(comps)} 项，架构 {arch}）")
        for comp in comps:
            if comp.name in skip:
                self.log(f"  [跳过] {comp.name}：将采用已上传的归档")
                continue
            results.append(self._fetch_component(comp, arch, ups))

        self._report(results)
        return results

    def _fetch_lib(self, lib: VendoredLib, ups: dict) -> FetchResult:
        if lib.name in ups:
            return self.fetch_upstream(ups[lib.name], lib.version)
        return self.fetch(lib.name, lib.url, lib.sha256)

    def _fetch_component(self, comp: Component, arch: str, ups: dict) -> FetchResult:
        if comp.name in ups:
            return self.fetch_upstream(ups[comp.name], comp.version_for(arch))
        url = comp.url_for(arch)
        if url is None:
            raise FetchError(f"{comp.name}: {arch} 缺少下载地址")
        return self.fetch(comp.name, url, comp.sha256)

    def _report(self, results: list[FetchResult]) -> None:
        bad = [r for r in results if not r.unlocked and not r.verified]
        pending = [r for r in results if r.unlocked]

        if pending:
            self.log("")
            self.log("以下条目的校验和尚未锁定，实测值如下，核对后写入 components.yaml：")
            for r in pending:
                self.log(f"  {r.name}: {r.sha256}")

        if bad:
            detail = "\n".join(
                f"  {r.name}\n    期望 {r.expected}\n    实际 {r.sha256}" for r in bad
            )
            raise FetchError(f"以下归档校验和不符，拒绝继续构建：\n{detail}")
