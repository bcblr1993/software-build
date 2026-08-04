"""ABI 门禁的调用封装。

判定逻辑只有一份实现，在 scripts/lib/abi-gate.sh —— 该脚本可脱离本系统
独立使用（例如临时检查一个第三方二进制），因此不在此处用 Python 重写，
以免两份实现随时间产生偏差。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_FAIL_RE = re.compile(r"^\s*\[FAIL\]\s+(.*)$")
_TARGET_RE = re.compile(r"^→\s+(.*)$")
_SUMMARY_RE = re.compile(r"检查 (\d+) 个 ELF · 通过 (\d+) · 失败 (\d+) · 警告 (\d+)")


@dataclass
class GateFinding:
    binary: str
    message: str


@dataclass
class GateResult:
    passed: bool
    checked: int = 0
    ok: int = 0
    failed: int = 0
    warned: int = 0
    findings: list[GateFinding] = field(default_factory=list)
    output: str = ""

    def summary(self) -> str:
        if self.passed:
            return f"ABI 门禁通过（{self.checked} 个 ELF）"
        detail = "；".join(f"{f.binary}: {f.message}" for f in self.findings[:3])
        return f"ABI 门禁未通过（{self.failed} 项）：{detail}"


def run_gate(script: Path, baseline_glibc: str, *paths: Path) -> GateResult:
    """对给定路径执行 ABI 门禁。

    paths 可以是目录（递归检查其中的可执行文件与共享库）或单个文件。
    """
    cmd = ["bash", str(script), baseline_glibc, *[str(p) for p in paths]]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = proc.stdout + proc.stderr

    result = GateResult(passed=proc.returncode == 0, output=output)

    current = "?"
    for line in output.splitlines():
        target = _TARGET_RE.match(line)
        if target:
            current = target.group(1).strip()
            continue
        fail = _FAIL_RE.match(line)
        if fail:
            result.findings.append(GateFinding(binary=current, message=fail.group(1).strip()))
            continue
        summary = _SUMMARY_RE.search(line)
        if summary:
            result.checked = int(summary.group(1))
            result.ok = int(summary.group(2))
            result.failed = int(summary.group(3))
            result.warned = int(summary.group(4))

    # 退出码 2 表示用法错误或路径不存在，与"检查未通过"要区分开
    if proc.returncode == 2:
        result.passed = False
        if not result.findings:
            result.findings.append(GateFinding("?", output.strip()[:200] or "门禁脚本执行失败"))

    return result
