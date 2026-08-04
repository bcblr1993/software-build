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

    @property
    def verified(self) -> bool:
        return self.expected == self.sha256

    @property
    def unlocked(self) -> bool:
        """校验和尚未在清单中锁定。"""
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
    def __init__(self, cache_dir: Path, log=print) -> None:
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.log = log

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

    def fetch_all(self, cfg: Config, arch: str) -> list[FetchResult]:
        """获取指定架构所需的全部上游归档。"""
        results: list[FetchResult] = []

        self.log(f"随包依赖库（{len(cfg.vendored_libs)} 项）")
        for lib in cfg.vendored_libs.values():
            results.append(self._fetch_lib(lib))

        comps = [c for c in cfg.components.values() if not c.local_only]
        self.log(f"组件（{len(comps)} 项，架构 {arch}）")
        for comp in comps:
            results.append(self._fetch_component(comp, arch))

        self._report(results)
        return results

    def _fetch_lib(self, lib: VendoredLib) -> FetchResult:
        return self.fetch(lib.name, lib.url, lib.sha256)

    def _fetch_component(self, comp: Component, arch: str) -> FetchResult:
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
