"""上游获取与来源校验。

解决的问题：只填版本号，由系统去上游官方源取回归档，并用**独立于该次
下载**的凭据证明它确实是上游发布的那一份。

为什么不能"下载后自己算 sha256 存起来"——那样算出来的哈希描述的正是
刚下载的那个文件，无论它是不是被换过。中间人把归档换掉，算出的哈希
同样"匹配"。校验和只有来自另一条信任路径时才有意义。

因此这里按可信度分三级，逐级降级：

  1. pgp         上游用私钥签名归档，我们用固化在仓库里的公钥验签。
                 最强：签名无法伪造，且与传输通道无关。
  2. hash-index  上游在另一处（另一个域名／另一个仓库）发布哈希清单。
                 次之：攻击者需同时控制归档站点与清单站点。
  3. cross-source 从彼此独立的镜像各取一份，比对哈希是否一致。
                 兜底：用于既不签名也不发布哈希的项目（zlib、keepalived）。

三级都不可得时拒绝构建，而不是退回"自己算"。宁可挡住，也不给一个
看起来通过、实际什么都没验的绿灯。

PGP 验签有个常见的错误实现：只看 gpg 退出码。任何人都能生成密钥对
自签一个归档，验签自然通过。必须同时断言签名者指纹在白名单内 ——
这正是 _verify_pgp 里那一步的用意。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def _fetch_utils():
    """延迟取用 fetch 中的工具函数。

    刻意不在模块顶部导入：fetch 依赖 config，config 依赖 pyyaml。
    放在顶部会让本模块的纯逻辑（模板填充、登记项校验）在没装 pyyaml
    的机器上连导入都做不到，测试也就无从跑起。
    """
    from .fetch import USER_AGENT, sha256_file

    return USER_AGENT, sha256_file


def sha256_file(path: Path) -> str:
    return _fetch_utils()[1](path)


class UpstreamError(Exception):
    pass


#: 可信度由高到低，用于在多种手段都可用时择优，也用于报告里排序
METHOD_RANK = {"pgp": 1, "hash-index": 2, "cross-source": 3}


@dataclass
class Attestation:
    """一次来源校验的结果，可直接写进发布说明备查。"""

    name: str
    version: str
    method: str
    sha256: str
    detail: str
    #: 签名／哈希清单／镜像的地址，即校验凭据本身的出处
    source_url: str = ""
    #: 归档自身的下载地址
    tarball: str = ""
    signers: list[str] = field(default_factory=list)

    @property
    def rank(self) -> int:
        return METHOD_RANK.get(self.method, 99)

    def summary(self) -> str:
        label = {
            "pgp": "PGP 签名验证",
            "hash-index": "上游哈希清单",
            "cross-source": "多源交叉比对",
        }.get(self.method, self.method)
        return f"{self.name} {self.version}：{label} — {self.detail}"


@dataclass
class UpstreamSpec:
    """某个组件的上游登记信息，来自 components.yaml 的 upstreams 段。"""

    name: str
    tarball: str
    method: str
    signature: str = ""
    keyring: str = ""
    fingerprints: list[str] = field(default_factory=list)
    index: str = ""
    pattern: str = ""
    mirrors: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, raw: dict) -> "UpstreamSpec":
        if not isinstance(raw, dict):
            raise UpstreamError(f"upstreams.{name} 应为映射")
        tarball = str(raw.get("tarball") or "").strip()
        if not tarball:
            raise UpstreamError(f"upstreams.{name} 缺少 tarball")

        verify = raw.get("verify") or {}
        if not isinstance(verify, dict):
            raise UpstreamError(f"upstreams.{name}.verify 应为映射")
        method = str(verify.get("method") or "").strip()
        if method not in METHOD_RANK:
            raise UpstreamError(
                f"upstreams.{name}.verify.method 取值应为 "
                f"{'/'.join(METHOD_RANK)}，实为 {method!r}"
            )

        spec = cls(
            name=name,
            tarball=tarball,
            method=method,
            signature=str(verify.get("signature") or ""),
            keyring=str(verify.get("keyring") or ""),
            fingerprints=[
                _normalize_fpr(f) for f in (verify.get("fingerprints") or [])
            ],
            index=str(verify.get("index") or ""),
            pattern=str(verify.get("pattern") or ""),
            mirrors=[str(m) for m in (verify.get("mirrors") or [])],
        )
        spec._validate()
        return spec

    def _validate(self) -> None:
        if self.method == "pgp":
            if not self.signature:
                raise UpstreamError(f"upstreams.{self.name}: pgp 需要 signature")
            if not self.keyring:
                raise UpstreamError(f"upstreams.{self.name}: pgp 需要 keyring")
            if not self.fingerprints:
                raise UpstreamError(
                    f"upstreams.{self.name}: pgp 必须登记 fingerprints，"
                    f"否则任何自签归档都能通过验签"
                )
        elif self.method == "hash-index":
            if not self.index or not self.pattern:
                raise UpstreamError(
                    f"upstreams.{self.name}: hash-index 需要 index 与 pattern"
                )
        elif self.method == "cross-source" and len(self.mirrors) < 1:
            raise UpstreamError(
                f"upstreams.{self.name}: cross-source 至少需要一个 mirrors 条目"
                f"（与 tarball 构成两个独立来源）"
            )

    def tarball_url(self, version: str) -> str:
        return _fill(self.tarball, version)

    def signature_url(self, version: str) -> str:
        # 允许写成 "{tarball}.asc" 这种相对形式，省得把长地址抄两遍
        url = self.signature.replace("{tarball}", self.tarball)
        return _fill(url, version)


def _normalize_fpr(value: str) -> str:
    return re.sub(r"\s+", "", str(value)).upper()


def _fill(template: str, version: str) -> str:
    """把模板里的 {version} 换成实际版本号。

    刻意不用 str.format：哈希清单的匹配式里含 [0-9a-f]{64} 这类正则
    量词，format 会把 {64} 当成占位符而抛 KeyError。这里只做一处
    定点替换，模板中其余的花括号原样保留。
    """
    return template.replace("{version}", version)


class UpstreamResolver:
    """把「组件名 + 版本号」解析为「归档 + 可信的期望哈希」。"""

    def __init__(self, repo_root: Path, cache_dir: Path, log=print) -> None:
        self.repo_root = Path(repo_root)
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.log = log

    # ── 对外接口 ────────────────────────────────────────────────────

    def expected_hash(self, spec: UpstreamSpec, version: str) -> Attestation | None:
        """在下载归档之前，尽可能先取得权威期望哈希。

        hash-index 能在下载前给出答案，于是错误的版本号或被篡改的归档
        在传输阶段就会暴露，不必等到几百 MB 下完。pgp 做不到这一点
        （签名验的是文件本身），返回 None 表示"待归档就位后再验"。
        """
        if spec.method == "hash-index":
            return self._from_index(spec, version)
        return None

    def verify(
        self, spec: UpstreamSpec, version: str, archive: Path
    ) -> Attestation:
        """对已经落盘的归档做来源校验。"""
        if spec.method == "pgp":
            return self._verify_pgp(spec, version, archive)
        if spec.method == "hash-index":
            att = self._from_index(spec, version)
            actual = sha256_file(archive)
            if actual != att.sha256:
                raise UpstreamError(
                    f"{spec.name} {version}: 归档与上游哈希清单不符\n"
                    f"  清单 {att.sha256}\n  实际 {actual}\n"
                    f"  清单来源 {att.source_url}"
                )
            return att
        if spec.method == "cross-source":
            return self._verify_cross(spec, version, archive)
        raise UpstreamError(f"{spec.name}: 未知校验方式 {spec.method}")

    # ── 一级：PGP ───────────────────────────────────────────────────

    def _verify_pgp(
        self, spec: UpstreamSpec, version: str, archive: Path
    ) -> Attestation:
        if not shutil.which("gpg"):
            raise UpstreamError(
                f"{spec.name}: 需要 gpg 验签但系统未安装。"
                f"请安装 gnupg2 后重试。"
            )

        keyring = self.repo_root / spec.keyring
        if not keyring.is_file():
            raise UpstreamError(
                f"{spec.name}: 公钥文件缺失 {keyring}。"
                f"执行 scripts/fetch-keys.sh 获取。"
            )

        sig_url = spec.signature_url(version)
        sig_path = self.cache / f"{archive.name}{Path(sig_url).suffix}"
        self.log(f"  [签名] {sig_url}")
        self._curl(sig_url, sig_path)

        # 用一次性 GNUPGHOME，既不读也不写系统 keyring —— 构建机上的
        # 信任状态不应影响判定结果，判定只依据仓库里固化的那份公钥。
        with tempfile.TemporaryDirectory(prefix="sprixin-gpg-") as home:
            base = ["gpg", "--homedir", home, "--batch", "--no-tty"]
            imp = subprocess.run(
                [*base, "--import", str(keyring)],
                capture_output=True, text=True,
            )
            if imp.returncode != 0:
                raise UpstreamError(
                    f"{spec.name}: 导入公钥失败\n{imp.stderr.strip()[:300]}"
                )

            proc = subprocess.run(
                [*base, "--status-fd", "1", "--verify", str(sig_path), str(archive)],
                capture_output=True, text=True,
            )

        status = proc.stdout
        if proc.returncode != 0:
            raise UpstreamError(
                f"{spec.name} {version}: PGP 验签失败\n"
                f"{(proc.stderr or status).strip()[:400]}"
            )

        # 退出码为 0 只说明"签名与某个已导入的公钥匹配"。由于导入的
        # 只有仓库里固化的公钥，范围已经收窄；但仍要断言指纹确在白名单，
        # 防止公钥文件本身被替换后无声放行。
        signers = re.findall(r"VALIDSIG ([0-9A-F]+)", status)
        allowed = {_normalize_fpr(f) for f in spec.fingerprints}
        matched = [s for s in signers if _normalize_fpr(s) in allowed]
        if not matched:
            raise UpstreamError(
                f"{spec.name} {version}: 签名有效，但签名者不在白名单内\n"
                f"  实际签名者 {signers or '未知'}\n"
                f"  白名单 {sorted(allowed)}"
            )

        return Attestation(
            name=spec.name,
            version=version,
            method="pgp",
            sha256=sha256_file(archive),
            detail=f"签名者 {matched[0]}",
            source_url=sig_url,
            signers=matched,
        )

    # ── 二级：上游哈希清单 ──────────────────────────────────────────

    def _from_index(self, spec: UpstreamSpec, version: str) -> Attestation:
        url = spec.index.format(version=version)
        self.log(f"  [清单] {url}")
        text = self._curl_text(url)

        pattern = _fill(spec.pattern, re.escape(version))
        m = re.search(pattern, text, re.MULTILINE)
        if not m:
            raise UpstreamError(
                f"{spec.name} {version}: 上游哈希清单中没有该版本的记录\n"
                f"  清单 {url}\n"
                f"  多半是版本号写错了，或该版本尚未发布"
            )

        digest = m.group(1).lower()
        if len(digest) != 64:
            raise UpstreamError(
                f"{spec.name} {version}: 清单中取到的不是 sha256（{digest[:16]}…）"
            )

        return Attestation(
            name=spec.name,
            version=version,
            method="hash-index",
            sha256=digest,
            detail=f"取自上游哈希清单",
            source_url=url,
        )

    # ── 三级：多源交叉 ──────────────────────────────────────────────

    def _verify_cross(
        self, spec: UpstreamSpec, version: str, archive: Path
    ) -> Attestation:
        actual = sha256_file(archive)
        checked: list[str] = []

        for mirror in spec.mirrors:
            url = mirror.format(version=version)
            self.log(f"  [镜像] {url}")
            tmp = self.cache / f".xcheck-{spec.name}-{version}"
            try:
                self._curl(url, tmp)
                mirror_hash = sha256_file(tmp)
            except UpstreamError as exc:
                self.log(f"         镜像不可用，跳过：{exc}")
                continue
            finally:
                tmp.unlink(missing_ok=True)

            if mirror_hash != actual:
                raise UpstreamError(
                    f"{spec.name} {version}: 镜像与官方源内容不一致，拒绝使用\n"
                    f"  官方 {actual}\n"
                    f"  镜像 {mirror_hash}（{url}）\n"
                    f"  这可能意味着其中一方被篡改，需人工核实"
                )
            checked.append(url)

        if not checked:
            raise UpstreamError(
                f"{spec.name} {version}: 所有交叉验证镜像均不可用，"
                f"无法确认归档来源，拒绝构建"
            )

        return Attestation(
            name=spec.name,
            version=version,
            method="cross-source",
            sha256=actual,
            detail=f"与 {len(checked)} 个独立镜像比对一致",
            source_url=checked[0],
        )

    # ── 下载helpers ────────────────────────────────────────────────

    def _curl(self, url: str, target: Path) -> None:
        proc = subprocess.run(
            [
                "curl", "-fL", "--retry", "3", "--retry-delay", "2",
                "--connect-timeout", "20", "-m", "1800",
                "-A", _fetch_utils()[0], "-o", str(target), url,
            ],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            target.unlink(missing_ok=True)
            raise UpstreamError(
                f"取回失败（curl {proc.returncode}）: {url}\n"
                f"{proc.stderr.strip()[:200]}"
            )

    def _curl_text(self, url: str) -> str:
        proc = subprocess.run(
            [
                "curl", "-fsSL", "--retry", "3", "--retry-delay", "2",
                "--connect-timeout", "20", "-m", "120",
                "-A", _fetch_utils()[0], url,
            ],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise UpstreamError(
                f"取回失败（curl {proc.returncode}）: {url}\n"
                f"{proc.stderr.strip()[:200]}"
            )
        return proc.stdout
