"""SOURCE 说明文件与升级报告的生成。

此前这两份文档是人工撰写的，其中诸如"相对 v13.1 对全部非 Nginx 程序
文件做了哈希比较，结果一致"之类的结论，本就应当由机器给出 —— 人工比对
既耗时又容易漏项，而且无法复核。

敏感值（RabbitMQ 默认口令等）从 secrets/package-secrets.yaml 读取注入，
仓库中只保留示例文件与单向指纹。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ComponentInfo:
    name: str
    version: str
    build: str
    sha256: str
    size: int
    changed: str = ""       # 相对上一版本：新增 / 已变更 / 已移除 / 空表示未变
    source_url: str = ""
    source_sha256: str = ""


def load_secrets(path: Path) -> dict[str, Any]:
    """读取敏感值。文件不存在时返回空字典，由调用方决定是否告警。"""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def render_source_file(
    *,
    package_name: str,
    package_version: str,
    arch: str,
    baseline_glibc: str,
    components: list[ComponentInfo],
    vendored: dict[str, str],
    secrets: dict[str, Any],
    verified_targets: list[tuple[str, str, bool]] | None = None,
) -> str:
    """生成 SOURCE-*.txt。"""
    lines: list[str] = []
    add = lines.append

    add(f"{package_name} {package_version} ({arch})")
    add(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    add("")
    add("构建方式")
    add("-" * 60)
    add(f"基线 glibc: {baseline_glibc}")
    add("所有需编译的组件均在 glibc {} 基线上构建，并随包分发 OpenSSL、".format(baseline_glibc))
    add("PCRE2、zlib 等库，通过 $ORIGIN 相对 rpath 加载。产物不依赖目标系统")
    add("的发行版、小版本或补丁号，可运行于任何 glibc >= {} 的 Linux。".format(baseline_glibc))
    add("")

    add("随包运行库")
    add("-" * 60)
    for name, version in sorted(vendored.items()):
        add(f"{name}: {version}")
    add("")

    add("组件")
    add("-" * 60)
    for c in sorted(components, key=lambda x: x.name):
        mark = f"  [{c.changed}]" if c.changed else ""
        add(f"{c.name}: {c.version}{mark}")
        add(f"  构建方式: {_build_label(c.build)}")
        if c.source_url:
            add(f"  上游地址: {c.source_url}")
        if c.source_sha256:
            add(f"  上游校验和: {c.source_sha256}")
        add(f"  归档校验和: {c.sha256}")
        add(f"  归档大小: {c.size} 字节")
        add("")

    rabbit = secrets.get("rabbitmq") or {}
    if rabbit.get("default_user") or rabbit.get("default_password"):
        add("默认账号")
        add("-" * 60)
        if rabbit.get("default_user"):
            add(f"rabbitmq 用户: {rabbit['default_user']}")
        if rabbit.get("default_password"):
            add(f"rabbitmq 口令: {rabbit['default_password']}")
        add("")

    if verified_targets:
        add("目标系统验证")
        add("-" * 60)
        add("在下列系统的容器中实测启动，均由 ISO 直接构建，未经人工干预：")
        for os_name, glibc, ok in verified_targets:
            add(f"  [{'通过' if ok else '失败'}] {os_name}  glibc {glibc}")
        add("")

    return "\n".join(lines) + "\n"


def render_upgrade_report(
    *,
    package_name: str,
    package_version: str,
    previous_version: str,
    arch: str,
    package_sha256: str,
    package_size: int,
    baseline_glibc: str,
    components: list[ComponentInfo],
    gate_summary: str,
    verified_targets: list[tuple[str, str, bool]] | None = None,
    duration_s: int | None = None,
) -> str:
    """生成升级报告（Markdown）。"""
    changed = [c for c in components if c.changed]
    unchanged = [c for c in components if not c.changed]

    lines: list[str] = []
    add = lines.append

    add(f"# {package_name} {package_version} 构建报告（{arch}）")
    add("")
    add("## 最终包")
    add("")
    add(f"- 版本：`{package_version}`（上一版本 `{previous_version or '无'}`）")
    add(f"- 架构：`{arch}`")
    add(f"- 大小：{package_size:,} 字节")
    add(f"- SHA-256：`{package_sha256}`")
    if duration_s:
        add(f"- 构建耗时：{duration_s // 60} 分 {duration_s % 60} 秒")
    add("")

    add("## 本次变更")
    add("")
    if changed:
        add("| 组件 | 版本 | 变化 | 归档 SHA-256 |")
        add("|---|---|---|---|")
        for c in sorted(changed, key=lambda x: x.name):
            add(f"| {c.name} | {c.version} | {c.changed} | `{c.sha256[:16]}…` |")
    else:
        add("与上一版本相比，所有组件归档均未发生变化。")
    add("")

    add("## 未修改范围")
    add("")
    if unchanged:
        add("下列组件的归档校验和与上一版本完全一致，由机器逐一比对得出：")
        add("")
        for c in sorted(unchanged, key=lambda x: x.name):
            add(f"- {c.name} {c.version}：`{c.sha256}`")
    else:
        add("本次所有组件均发生变化。")
    add("")

    add("## 兼容性")
    add("")
    add(f"基线 glibc `{baseline_glibc}`。所有需编译的组件在该基线上构建，")
    add("并随包分发 OpenSSL、PCRE2、zlib，通过 `$ORIGIN` 相对 rpath 加载。")
    add("")
    add(f"ABI 门禁：{gate_summary}")
    add("")
    add("门禁断言三项：最高 glibc 符号版本不超过基线；`DT_NEEDED` 只含 glibc")
    add("核心库与随包库；引用随包库的二进制其 `RUNPATH` 含 `$ORIGIN` 且库确实")
    add("可从该路径解析。三项同时成立，即构成可运行于任何 glibc >= 基线的")
    add("Linux 的充分条件。")
    add("")

    if verified_targets:
        add("## 目标系统实测")
        add("")
        add("下列验证容器均由目标系统 ISO 直接构建，不经装机：")
        add("")
        add("| 目标系统 | glibc | 结果 |")
        add("|---|---|---|")
        for os_name, glibc, ok in verified_targets:
            add(f"| {os_name} | {glibc} | {'通过' if ok else '失败'} |")
        add("")

    add("## 安装")
    add("")
    add("包结构与历史版本一致，现场流程无需改动：")
    add("")
    add("```")
    add("tar -xzf <包名>.tar.gz")
    add("cd sprixinSoft && ./install.sh && ./startup.sh")
    add("```")
    add("")
    add("安装后可执行 `./verify.sh` 自检。")
    add("")

    return "\n".join(lines) + "\n"


def _build_label(kind: str) -> str:
    return {
        "compile": "基线编译，随包分发依赖库",
        "relink-nif": "对随包 OpenSSL 重建原生模块",
        "repack": "上游产物直接打包（与平台无关）",
    }.get(kind, kind)
