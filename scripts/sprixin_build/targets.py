"""目标机器管理与远程实机验证。

机器与目标系统分开管理：机器池里登记若干台真实机器，验证清单上再把某个
目标系统绑定到其中一台。这样同一台机器换绑系统、或临时停用某台机器，
都不必重填一遍连接信息。

绑定是一对一的：一台机器只装一个操作系统，因而只能承担一个目标系统的
验证。同一台机器若被绑到两处，必然有一处是错的 —— 而错误的那处会得出
"该系统已实机验证"的结论，验的却是另一个系统。这种错看起来是成功的，
比验证失败更危险，故在绑定时就挡住。

登录用户即验证用户，不做身份切换：现场只有普通用户，填谁就以谁的身份跑，
所见即所得。用 root 验证会得出与现场不符的结论 —— nginx 的
getgrnam("nobody") 报错就是这么来的，那是 root 专有的失败路径。

凭据存放在 secrets/targets.json（权限 600，且 secrets/ 已在 .gitignore 中）。
口令终究是明文落盘的 —— 这台构建机本就持有目标机的登录凭据，安全边界在于
机器本身，而非文件格式。因此不做形同虚设的混淆，只把它挡在仓库之外，
并在接口上永不回显。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class TargetError(Exception):
    pass


@dataclass
class Machine:
    """一台可用于验证的真实机器。"""

    id: str = ""
    label: str = ""                # 便于辨认的名字，如「凝思 6.0.99 测试机」
    host: str = ""
    port: int = 22
    #: 登录并运行验证的用户。就用它，不再切换身份。
    username: str = "sprixin"
    password: str = ""
    note: str = ""

    def redacted(self) -> dict:
        return {
            "id": self.id,
            "label": self.label or self.host,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "note": self.note,
            "configured": bool(self.host and self.password),
        }


def _new_id() -> str:
    return f"m{int(time.time() * 1000) % 100000000:08d}"


class TargetStore:
    """机器池与「目标系统 → 机器」的绑定关系。"""

    def __init__(self, secrets_dir: Path) -> None:
        self.path = Path(secrets_dir) / "targets.json"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # ── 持久化 ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"machines": {}, "bindings": {}}
        if not isinstance(raw, dict):
            return {"machines": {}, "bindings": {}}

        # 旧格式是「目标系统名 → 连接信息」的平铺表。就地迁移：每条化为一台
        # 机器，并保留原有的绑定关系，免得已配好的信息要重填一遍。
        if "machines" not in raw:
            machines, bindings = {}, {}
            for name, cfg in raw.items():
                if not isinstance(cfg, dict) or not cfg.get("host"):
                    continue
                mid = _new_id() + str(len(machines))
                machines[mid] = {
                    "label": cfg.get("host", ""),
                    "host": cfg.get("host", ""),
                    "port": int(cfg.get("port", 22) or 22),
                    # 旧格式区分登录用户与验证身份，现已统一。取验证身份 ——
                    # 那才是当初真正跑验证的那个用户。
                    "username": cfg.get("run_as") or cfg.get("username") or "sprixin",
                    "password": cfg.get("password", ""),
                    "note": cfg.get("note", ""),
                }
                bindings[name] = mid
            return {"machines": machines, "bindings": bindings}

        return {
            "machines": raw.get("machines") or {},
            "bindings": raw.get("bindings") or {},
        }

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        old = os.umask(0o077)
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            self.path.chmod(0o600)
        finally:
            os.umask(old)

    # ── 机器池 ──────────────────────────────────────────────────────

    def machines(self) -> dict[str, Machine]:
        data = self._load()
        out = {}
        for mid, m in data["machines"].items():
            out[mid] = Machine(
                id=mid,
                label=m.get("label", ""),
                host=m.get("host", ""),
                port=int(m.get("port", 22) or 22),
                username=m.get("username", "sprixin"),
                password=m.get("password", ""),
                note=m.get("note", ""),
            )
        return out

    def machine(self, mid: str) -> Machine | None:
        return self.machines().get(mid)

    def save_machine(self, m: Machine) -> str:
        if not m.host:
            raise TargetError("缺少主机地址")
        if not m.username:
            raise TargetError("缺少登录用户")

        data = self._load()
        mid = m.id or _new_id()
        prev = data["machines"].get(mid) or {}

        for other, raw in data["machines"].items():
            if other == mid:
                continue
            if raw.get("host") == m.host and int(raw.get("port", 22)) == m.port:
                raise TargetError(
                    f"{m.host}:{m.port} 已在机器列表中（{raw.get('label') or other}）"
                )

        data["machines"][mid] = {
            "label": m.label or m.host,
            "host": m.host,
            "port": m.port,
            "username": m.username,
            # 留空表示沿用原口令，便于只改端口或备注而不必重输
            "password": m.password or prev.get("password", ""),
            "note": m.note,
        }
        self._save(data)
        return mid

    def delete_machine(self, mid: str) -> None:
        data = self._load()
        if data["machines"].pop(mid, None) is None:
            raise TargetError("没有这台机器")
        # 连带解除绑定，避免留下指向空机器的悬挂引用
        data["bindings"] = {k: v for k, v in data["bindings"].items() if v != mid}
        self._save(data)

    # ── 绑定 ────────────────────────────────────────────────────────

    def bindings(self) -> dict[str, str]:
        return self._load()["bindings"]

    def bind(self, target: str, mid: str) -> None:
        data = self._load()
        if not mid:                       # 空值表示解除绑定
            data["bindings"].pop(target, None)
            self._save(data)
            return
        if mid not in data["machines"]:
            raise TargetError("没有这台机器")

        for other, bound in data["bindings"].items():
            if other != target and bound == mid:
                m = data["machines"][mid]
                raise TargetError(
                    f"该机器已绑定到「{other}」。一台机器只装一个操作系统，"
                    f"只能对应一个目标系统 —— 请先解除原有绑定。"
                )
        data["bindings"][target] = mid
        self._save(data)

    def for_target(self, target: str) -> Machine | None:
        mid = self._load()["bindings"].get(target)
        return self.machine(mid) if mid else None


# ── 远程执行 ────────────────────────────────────────────────────────


def _ssh_base(m: Machine) -> list[str]:
    if not m.password:
        raise TargetError("未设置口令")
    return [
        "sshpass", "-p", m.password, "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=30",
        "-p", str(m.port), f"{m.username}@{m.host}",
    ]


def _os_family(text: str) -> str:
    """从系统名或条目名中提取发行版族。

    两边写法并不统一（条目名可能是镜像文件名，系统名来自 os-release），
    故只比对能可靠识别的关键词，认不出时返回空串并放弃比对 —— 宁可不报，
    也好过因命名差异误报把人吓住。
    """
    t = (text or "").lower()
    for key, fam in (
        ("centos", "centos"), ("anolis", "anolis"),
        ("kylinsec", "kylin"), ("kylin", "kylin"),
        ("linx", "linx"), ("ns-", "linx"),
        ("ubuntu", "ubuntu"), ("debian", "debian"),
        ("openeuler", "openeuler"), ("uos", "uos"),
    ):
        if key in t:
            return fam
    return ""


def check(m: Machine, *, expect_target: str = "", expect_glibc: str = "") -> dict:
    """连通性与前置条件探测。"""
    import shutil

    if not shutil.which("sshpass"):
        raise TargetError("构建机上缺少 sshpass，无法使用口令登录")

    probe = r"""
. /etc/os-release 2>/dev/null
echo "os=${PRETTY_NAME:-未知}"
echo "arch=$(uname -m)"
echo "glibc=$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')"
echo "hostname=$(hostname)"
echo "whoami=$(id -un) uid=$(id -u)"
if getent hosts "$(hostname)" 2>/dev/null | grep -qv '^fe80:'; then
  echo "hostname_resolves=yes"
else
  echo "hostname_resolves=no"
fi
echo "free=$(df -Pk "$HOME" 2>/dev/null | tail -1 | awk '{print $4}')"
busy=""
for p in 6379 9000 8848 8086 5672 15672; do
  (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ":$p\b" && busy="$busy $p"
done
echo "busy_ports=$busy"
"""
    proc = subprocess.run([*_ssh_base(m), probe],
                          capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        err = [e for e in (proc.stderr or "").strip().splitlines()
               if "Warning: Permanently" not in e and "post-quantum" not in e]
        raise TargetError(f"连接失败：{' '.join(err[-2:]) or '未知原因'}")

    info: dict = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()

    warnings = []

    uid = ""
    mm = re.search(r"uid=(\d+)", info.get("whoami", ""))
    if mm:
        uid = mm.group(1)
    if uid == "0":
        warnings.append(
            "当前以 root 登录。现场只有普通用户，用 root 验证会得出与现场不符的"
            "结论（例如 nginx 会去解析 nobody 组，普通用户根本不走这条路）。"
            "请改用普通用户。"
        )

    if expect_target:
        fe, fa = _os_family(expect_target), _os_family(info.get("os", ""))
        if fe and fa and fe != fa:
            warnings.append(
                f"这台机器上跑的是 {info.get('os')}，与「{expect_target}」"
                f"看起来不是同一个系统，请确认没有选错机器。"
            )
    if expect_glibc and info.get("glibc") and info["glibc"] != expect_glibc:
        warnings.append(
            f"机器 glibc 为 {info['glibc']}，该条目的容器验证记录是 {expect_glibc}；"
            f"若确属同一系统的不同小版本，可以继续。"
        )

    free_kb = int(info.get("free") or 0)
    if free_kb and free_kb < 3 * 1024 * 1024:
        warnings.append(f"{m.username} 的家目录可用空间仅 {free_kb // 1024} MB，建议至少 3 GB")
    if info.get("busy_ports"):
        warnings.append(f"以下端口已被占用：{info['busy_ports']}，验证会失败")
    if info.get("hostname_resolves") == "no":
        warnings.append(
            f"主机名 {info.get('hostname')} 解析不出可用地址，包内会自动启用 inetrc 处理"
        )

    return {"ok": True, "info": info, "warnings": warnings}


def run_verify(m: Machine, package: Path, script: Path, log=print) -> dict:
    """把安装包送到机器上，以登录用户完整验证一遍。

    包经 SSH 推送而非让目标机反向下载：目标机未必能访问构建机的控制台，
    现场网络分段是常态。
    """
    if not package.is_file():
        raise TargetError(f"找不到安装包 {package}")
    if not script.is_file():
        raise TargetError(f"找不到验证脚本 {script}")

    scp = [
        "sshpass", "-p", m.password, "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=20", "-P", str(m.port),
    ]
    dest = f"{m.username}@{m.host}"

    log(f"  推送验证脚本 → {m.host}")
    r = subprocess.run([*scp, str(script), f"{dest}:/tmp/sprixin-verify.sh"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise TargetError(f"脚本推送失败：{r.stderr.strip()[:200]}")

    log(f"  推送安装包 {package.name}（{package.stat().st_size // 1048576} MB）…")
    r = subprocess.run([*scp, str(package), f"{dest}:/tmp/sprixin-pkg.tar.gz"],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise TargetError(f"安装包推送失败：{r.stderr.strip()[:200]}")

    log(f"  开始验证（以 {m.username} 身份完整安装、启动、自检，随后自动清理）")
    cmd = (
        "bash /tmp/sprixin-verify.sh /tmp/sprixin-pkg.tar.gz; rc=$?; "
        "rm -f /tmp/sprixin-pkg.tar.gz /tmp/sprixin-verify.sh; exit $rc"
    )
    proc = subprocess.run([*_ssh_base(m), cmd],
                          capture_output=True, text=True, timeout=5400)
    out = proc.stdout or ""
    return {
        "passed": "总体: PASS" in out,
        "returncode": proc.returncode,
        "output": out,
        "stderr": (proc.stderr or "")[-2000:],
    }
