"""components.yaml 的解析与校验。

该文件是整套构建系统的唯一事实来源：升级组件即修改其中的版本号与校验和。
本模块把它读成结构化对象，并在读取时就做完整性检查，避免错误配置拖到
构建中途才暴露。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# sha256 尚未锁定时的占位符
LOCK_PLACEHOLDER = "LOCK"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ConfigError(Exception):
    """components.yaml 存在问题时抛出。"""


@dataclass(frozen=True)
class VendoredLib:
    """随包分发的依赖库。"""

    name: str
    version: str
    url: str
    sha256: str

    @property
    def archive_name(self) -> str:
        return Path(self.url).name

    @property
    def locked(self) -> bool:
        return bool(_SHA256_RE.match(self.sha256))


@dataclass(frozen=True)
class Component:
    """安装包中的一个组件。

    build 取值：
        compile     需在基线容器内编译（nginx / redis / keepalived）
        relink-nif  只重建 NIF，其余沿用上游产物（rabbitmq）
        repack      Java/Go 产物，跨发行版通用，直接重新打包（nacos 等）
    """

    name: str
    build: str
    version: str
    url: str | None = None
    url_per_arch: dict[str, str] = field(default_factory=dict)
    version_per_arch: dict[str, str] = field(default_factory=dict)
    sha256: str = LOCK_PLACEHOLDER
    configure: list[str] = field(default_factory=list)
    cppflags: list[str] = field(default_factory=list)
    vendor: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    nifs: list[str] = field(default_factory=list)
    otp_tag: str | None = None
    local_only: bool = False
    note: str | None = None

    VALID_BUILDS = ("compile", "relink-nif", "repack")

    def version_for(self, arch: str) -> str:
        return self.version_per_arch.get(arch, self.version)

    def url_for(self, arch: str) -> str | None:
        raw = self.url_per_arch.get(arch, self.url)
        if raw is None:
            return None
        return raw.replace("{version}", self.version_for(arch))

    def archive_name_for(self, arch: str) -> str | None:
        url = self.url_for(arch)
        return Path(url).name if url else None

    @property
    def needs_compile(self) -> bool:
        return self.build in ("compile", "relink-nif")

    @property
    def locked(self) -> bool:
        return bool(_SHA256_RE.match(self.sha256))


@dataclass(frozen=True)
class Config:
    package_name: str
    package_version: str
    top_dir: str
    glibc_max: str
    baseline_images: dict[str, str]
    architectures: list[str]
    vendored_libs: dict[str, VendoredLib]
    components: dict[str, Component]
    overlay_files: list[str]
    source_path: Path

    # ── 读取 ────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"找不到组件清单: {path}") from None
        except yaml.YAMLError as exc:
            raise ConfigError(f"组件清单不是合法的 YAML: {exc}") from None

        if not isinstance(raw, dict):
            raise ConfigError("组件清单顶层必须是映射")

        pkg = _require(raw, "package", dict)
        baseline = _require(raw, "baseline", dict)

        libs: dict[str, VendoredLib] = {}
        for name, spec in (raw.get("vendored_libs") or {}).items():
            libs[name] = VendoredLib(
                name=name,
                version=str(_require(spec, "version", (str, int, float), ctx=name)),
                url=_require(spec, "url", str, ctx=name),
                sha256=str(spec.get("sha256", LOCK_PLACEHOLDER)),
            )

        comps: dict[str, Component] = {}
        for name, spec in (raw.get("components") or {}).items():
            build = _require(spec, "build", str, ctx=name)
            if build not in Component.VALID_BUILDS:
                raise ConfigError(
                    f"组件 {name} 的 build 取值 {build!r} 无效，"
                    f"应为 {Component.VALID_BUILDS} 之一"
                )
            comps[name] = Component(
                name=name,
                build=build,
                version=str(spec.get("version", "")),
                url=spec.get("url"),
                url_per_arch=dict(spec.get("url_per_arch") or {}),
                version_per_arch={
                    k: str(v) for k, v in (spec.get("version_per_arch") or {}).items()
                },
                sha256=str(spec.get("sha256", LOCK_PLACEHOLDER)),
                configure=list(spec.get("configure") or []),
                cppflags=list(spec.get("cppflags") or []),
                vendor=list(spec.get("vendor") or []),
                artifacts=list(spec.get("artifacts") or []),
                nifs=list(spec.get("nifs") or []),
                otp_tag=spec.get("otp_tag"),
                local_only=bool(spec.get("local_only", False)),
                note=spec.get("note"),
            )

        cfg = cls(
            package_name=_require(pkg, "name", str, ctx="package"),
            package_version=str(_require(pkg, "version", str, ctx="package")),
            top_dir=_require(pkg, "top_dir", str, ctx="package"),
            glibc_max=str(_require(baseline, "glibc_max", str, ctx="baseline")),
            baseline_images=dict(_require(baseline, "images", dict, ctx="baseline")),
            architectures=list(raw.get("architectures") or []),
            vendored_libs=libs,
            components=comps,
            overlay_files=list(raw.get("overlay_files") or []),
            source_path=path,
        )
        cfg.validate()
        return cfg

    # ── 校验 ────────────────────────────────────────────────────────

    def validate(self) -> None:
        if not self.architectures:
            raise ConfigError("architectures 不能为空")

        for arch in self.architectures:
            if arch not in self.baseline_images:
                raise ConfigError(f"架构 {arch} 缺少对应的基线镜像")

        for comp in self.components.values():
            for lib in comp.vendor:
                if lib not in self.vendored_libs:
                    raise ConfigError(
                        f"组件 {comp.name} 引用了未定义的随包库 {lib}"
                    )
            if comp.local_only:
                continue
            for arch in self.architectures:
                if comp.url_for(arch) is None:
                    raise ConfigError(f"组件 {comp.name} 在 {arch} 上缺少下载地址")

    def unlocked(self) -> list[str]:
        """返回尚未锁定 sha256 的条目。

        允许存在（首次引入组件时需要先下载才能得到校验和），
        但构建时会明确提示，避免在无人值守流程里静默跳过完整性校验。
        """
        pending = [f"vendored_libs.{n}" for n, l in self.vendored_libs.items() if not l.locked]
        pending += [
            f"components.{n}"
            for n, c in self.components.items()
            if not c.locked and not c.local_only
        ]
        return pending

    # ── 便捷访问 ────────────────────────────────────────────────────

    def compile_components(self) -> list[Component]:
        return [c for c in self.components.values() if c.build == "compile"]

    def baseline_image(self, arch: str) -> str:
        return self.baseline_images[arch]


def _require(mapping: dict[str, Any], key: str, types, ctx: str = "") -> Any:
    if key not in mapping:
        where = f"{ctx}." if ctx else ""
        raise ConfigError(f"缺少必填项 {where}{key}")
    value = mapping[key]
    if not isinstance(value, types):
        where = f"{ctx}." if ctx else ""
        raise ConfigError(f"{where}{key} 类型不正确")
    return value
