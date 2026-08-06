"""目标机登记与远程实机验证。

把「拿包去某台机器上装一遍跑一遍」这件事自动化：为每个目标系统登记一台
真实机器的 SSH 连接信息，之后一键把指定的安装包送过去，以普通用户完整
安装、启动、自检，再回收现场。

凭据存放在 secrets/targets.json（权限 600，且 secrets/ 已在 .gitignore 中）。
口令终究是明文落盘的 —— 这台构建机本就持有目标机的登录凭据，安全边界在于
机器本身，而非文件格式。因此不做形同虚设的混淆，只把它挡在仓库之外，
并在接口上永不回显。

刻意以普通用户身份执行验证：现场只有普通用户，没有 root。用 root 验证会
得出与现场不符的结论 —— nginx 的 getgrnam("nobody") 报错就是这么来的，
那是 root 专有的失败路径，普通用户根本走不到。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class TargetError(Exception):
    pass


@dataclass
class Target:
    """某个目标系统对应的一台真实机器。"""

    name: str                      # 与验证清单中的 target 对应
    host: str = ""
    port: int = 22
    username: str = "root"
    password: str = ""
    #: 实际执行验证时切换到的普通用户；现场即以此身份运行
    run_as: str = "sprixin"
    note: str = ""

    def redacted(self) -> dict:
        """对外表示：永不回显口令。"""
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "run_as": self.run_as,
            "note": self.note,
            "configured": bool(self.host and self.password),
        }


class TargetStore:
    def __init__(self, secrets_dir: Path) -> None:
        self.path = Path(secrets_dir) / "targets.json"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        old = os.umask(0o077)
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            self.path.chmod(0o600)
        finally:
            os.umask(old)

    # ── 增删查 ──────────────────────────────────────────────────────

    def get(self, name: str) -> Target | None:
        raw = self._load().get(name)
        if not raw:
            return None
        return Target(
            name=name,
            host=raw.get("host", ""),
            port=int(raw.get("port", 22) or 22),
            username=raw.get("username", "root"),
            password=raw.get("password", ""),
            run_as=raw.get("run_as", "sprixin"),
            note=raw.get("note", ""),
        )

    def put(self, t: Target) -> None:
        if not t.name:
            raise TargetError("缺少目标系统名")
        if not t.host:
            raise TargetError("缺少主机地址")
        data = self._load()
        prev = data.get(t.name) or {}
        data[t.name] = {
            "host": t.host,
            "port": t.port,
            "username": t.username,
            # 留空表示沿用原口令，便于只改端口或备注而不必重输
            "password": t.password or prev.get("password", ""),
            "run_as": t.run_as,
            "note": t.note,
        }
        self._save(data)

    def delete(self, name: str) -> None:
        data = self._load()
        if data.pop(name, None) is None:
            raise TargetError(f"未登记 {name}")
        self._save(data)

    def all(self) -> dict[str, Target]:
        return {n: self.get(n) for n in self._load()}


# ── 远程执行 ────────────────────────────────────────────────────────


def _ssh_base(t: Target) -> list[str]:
    if not t.password:
        raise TargetError(f"{t.name}: 未设置口令")
    return [
        "sshpass", "-p", t.password,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=30",
        "-p", str(t.port),
        f"{t.username}@{t.host}",
    ]


def check(t: Target) -> dict:
    """连通性与前置条件探测。

    验证要以普通用户身份进行，故此处一并确认该用户存在、可切换，
    以及磁盘是否放得下 —— 这些若到验证跑了一半才发现，代价高得多。
    """
    if not shutil_which("sshpass"):
        raise TargetError("构建机上缺少 sshpass，无法使用口令登录")

    probe = r"""
. /etc/os-release 2>/dev/null
echo "os=${PRETTY_NAME:-未知}"
echo "arch=$(uname -m)"
echo "glibc=$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')"
echo "hostname=$(hostname)"
if getent hosts "$(hostname)" 2>/dev/null | grep -qv '^fe80:'; then
  echo "hostname_resolves=yes"
else
  echo "hostname_resolves=no"
fi
id RUNAS >/dev/null 2>&1 && echo "runas=yes" || echo "runas=no"
command -v runuser >/dev/null 2>&1 && echo "runuser=yes" || echo "runuser=no"
echo "free=$(df -Pk "$(getent passwd RUNAS | cut -d: -f6)" 2>/dev/null | tail -1 | awk '{print $4}')"
busy=""
for p in 6379 9000 8848 8086 5672 15672; do
  (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ":$p\b" && busy="$busy $p"
done
echo "busy_ports=$busy"
""".replace("RUNAS", shlex.quote(t.run_as).strip("'"))

    proc = subprocess.run(
        [*_ssh_base(t), probe], capture_output=True, text=True, timeout=90,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        err = [e for e in err if "Warning: Permanently" not in e and "post-quantum" not in e]
        raise TargetError(f"连接失败：{' '.join(err[-2:]) or '未知原因'}")

    info: dict = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()

    free_kb = int(info.get("free") or 0)
    warnings = []
    if info.get("runas") != "yes":
        warnings.append(f"目标机上没有用户 {t.run_as}，无法以现场身份验证")
    if info.get("runuser") != "yes" and t.username == "root":
        warnings.append("目标机缺少 runuser，将退回 su（可能需要口令）")
    if free_kb and free_kb < 3 * 1024 * 1024:
        warnings.append(f"可用空间仅 {free_kb // 1024} MB，建议至少 3 GB")
    if info.get("busy_ports"):
        warnings.append(f"以下端口已被占用：{info['busy_ports']}，验证会失败")
    if info.get("hostname_resolves") == "no":
        warnings.append(
            f"主机名 {info.get('hostname')} 解析不出可用地址，"
            f"包内会自动启用 inetrc 处理"
        )

    return {"ok": True, "info": info, "warnings": warnings}


def shutil_which(cmd: str) -> str | None:
    import shutil

    return shutil.which(cmd)


def run_verify(t: Target, package: Path, script: Path, log=print) -> dict:
    """把安装包送到目标机，以普通用户完整验证一遍。

    包经 SSH 推送而非让目标机反向下载：目标机未必能访问构建机的控制台，
    现场网络分段是常态。
    """
    if not package.is_file():
        raise TargetError(f"找不到安装包 {package}")
    if not script.is_file():
        raise TargetError(f"找不到验证脚本 {script}")

    remote_pkg = f"/tmp/{package.name}"
    remote_script = "/tmp/sprixin-verify.sh"

    scp = [
        "sshpass", "-p", t.password, "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=20",
        "-P", str(t.port),
    ]
    log(f"  推送验证脚本 → {t.host}")
    r = subprocess.run([*scp, str(script), f"{t.username}@{t.host}:{remote_script}"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise TargetError(f"脚本推送失败：{r.stderr.strip()[:200]}")

    log(f"  推送安装包 {package.name}（{package.stat().st_size // 1048576} MB）…")
    r = subprocess.run([*scp, str(package), f"{t.username}@{t.host}:{remote_pkg}"],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise TargetError(f"安装包推送失败：{r.stderr.strip()[:200]}")

    runas = shlex.quote(t.run_as)
    inner = f"chmod 755 {remote_script}; cp {remote_pkg} /tmp/pkg.tar.gz; chmod 644 /tmp/pkg.tar.gz"
    # 以普通用户执行：现场即是如此，用 root 会得出与现场不符的结论
    if t.username == "root":
        cmd = (
            f"{inner}; "
            f"if command -v runuser >/dev/null 2>&1; then "
            f"runuser -l {runas} -c 'bash {remote_script} /tmp/pkg.tar.gz'; "
            f"else su - {runas} -c 'bash {remote_script} /tmp/pkg.tar.gz'; fi; "
            f"rc=$?; rm -f {remote_pkg} /tmp/pkg.tar.gz {remote_script}; exit $rc"
        )
    else:
        cmd = (
            f"{inner}; bash {remote_script} /tmp/pkg.tar.gz; "
            f"rc=$?; rm -f {remote_pkg} /tmp/pkg.tar.gz {remote_script}; exit $rc"
        )

    log("  开始验证（完整安装、启动、自检，随后自动清理）")
    proc = subprocess.run([*_ssh_base(t), cmd], capture_output=True, text=True, timeout=5400)
    out = proc.stdout or ""
    passed = "总体: PASS" in out
    return {
        "passed": passed,
        "returncode": proc.returncode,
        "output": out,
        "stderr": (proc.stderr or "")[-2000:],
    }
