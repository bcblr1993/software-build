#!/usr/bin/env python3
"""构建控制台 HTTP 服务。

刻意只用标准库实现：这套系统面向的是离线、带宽受限的内网环境，
让构建机反过来依赖 pip 能否装上 FastAPI 是不合适的。所需能力
（REST、SSE、静态文件、会话）标准库都能覆盖。

    python3 web/server.py --port 8899

首次访问引导绑定验证器；此后凭动态验证码登录即可触发构建。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(WEB_DIR))

from auth import Authenticator  # noqa: E402
from sprixin_build.config import Config, ConfigError  # noqa: E402
from sprixin_build.record import BuildStore  # noqa: E402

SESSION_COOKIE = "sprixin_session"


class BuildRunner:
    """串行执行构建任务，并向订阅者广播日志。

    构建是重资源操作（编译、QEMU 模拟、容器），同一时刻只允许一个，
    避免两次构建争抢 sysroot 卷导致产物被污染。
    """

    def __init__(self, repo_root: Path, workspace: Path, store: BuildStore) -> None:
        self.repo_root = repo_root
        self.workspace = workspace
        self.store = store
        self._lock = threading.Lock()
        self._current: dict | None = None
        self._subscribers: list[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._history: list[str] = []

    @property
    def busy(self) -> bool:
        return self._current is not None

    @property
    def current(self) -> dict | None:
        return dict(self._current) if self._current else None

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._sub_lock:
            for line in self._history[-200:]:
                q.put(line)
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _emit(self, line: str) -> None:
        self._history.append(line)
        if len(self._history) > 5000:
            self._history = self._history[-2000:]
        with self._sub_lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(line)
                except queue.Full:
                    pass

    def start(
        self,
        *,
        arch: str,
        components: list[str],
        operator: str,
        action: str = "build",
        parallel: str = "auto",
    ) -> tuple[bool, str]:
        """启动一项任务。

        action 取值：
            build    编译 + 门禁 + 内层归档
            package  组装安装包（不重新编译）
            verify   在全部目标系统容器中完整安装并启动
        """
        if action not in ("all", "build", "package", "verify"):
            return False, f"未知的操作: {action}"

        with self._lock:
            if self.busy:
                return False, "已有任务在进行中"
            self._history = []
            self._current = {
                "action": action,
                "arch": arch,
                "components": components,
                "operator": operator,
                "parallel": parallel,
                "started_at": time.time(),
            }
        threading.Thread(
            target=self._run,
            args=(action, arch, components, operator, parallel),
            daemon=True,
        ).start()
        return True, {
            "all": "全流程已启动",
            "build": "构建已启动",
            "package": "打包已启动",
            "verify": "验证已启动",
        }[action]

    def start_remote_verify(self, *, machine, target: str, package, arch: str,
                            operator: str, script) -> tuple[bool, str]:
        """在真实机器上跑一遍验证，通过后自动勾上清单。

        与构建共用同一把锁：两者都要独占目标机与网络带宽，并发跑只会
        互相拖慢，日志也会交织成一团。
        """
        with self._lock:
            if self.busy:
                return False, "已有任务在进行中"
            self._history = []
            self._current = {
                "action": "remote-verify", "arch": arch,
                "components": [target], "operator": operator,
                "parallel": "serial", "started_at": time.time(),
            }
        threading.Thread(
            target=self._run_remote_verify,
            args=(machine, target, package, arch, operator, script),
            daemon=True,
        ).start()
        return True, f"已开始在 {machine.host} 上验证 {target}"

    def _run_remote_verify(self, machine, target, package, arch, operator, script) -> None:
        from sprixin_build.checklist import Checklist
        from sprixin_build.targets import TargetError, run_verify

        rc = 1
        try:
            self._emit(f"目标系统 : {target}")
            self._emit(f"目标机器 : {machine.label or machine.host}")
            self._emit(f"登录身份 : {machine.username}@{machine.host}:{machine.port}")
            self._emit(f"安装包   : {package.name}")
            self._emit("")
            result = run_verify(machine, package, script, log=self._emit)
            for line in (result["output"] or "").splitlines():
                self._emit(line)
            if result["passed"]:
                # 验证通过即代表这台真机确认过了，自动勾上，免去人工再点一次
                Checklist(self.workspace).mark(
                    package.name, target, passed=True, operator=operator,
                    note=f"{machine.label or machine.host}（自动验证）",
                )
                self._emit("")
                self._emit(f"✓ {target} 实机验证通过，已在清单中勾选")
                rc = 0
            else:
                self._emit("")
                self._emit(f"✗ {target} 实机验证未通过，清单未勾选")
                if result["stderr"]:
                    self._emit(result["stderr"][-800:])
        except TargetError as exc:
            self._emit(f"验证失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            self._emit(f"验证过程异常：{exc}")
        finally:
            self._emit(f"__DONE__ {rc}")
            self._current = None

    def _build_command(self, action: str, arch: str, components: list[str]) -> list[str]:
        if action == "verify":
            # 取该架构最近产出的安装包
            dist = self.workspace / "dist" / arch
            pkgs = sorted(dist.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not pkgs:
                return []
            return ["bash", str(self.repo_root / "compat" / "e2e-test.sh"), str(pkgs[0])]

        cmd = [
            sys.executable,
            str(self.repo_root / "scripts" / "build.py"),
            "--workspace", str(self.workspace),
            action,
        ]
        # all 不限定架构时构建清单中的全部，这正是"一键出全部包"的用法
        if action != "all" or arch not in ("", "all"):
            cmd += ["--arch", arch]
        if action == "build":
            for c in components:
                cmd += ["--component", c]
        return cmd

    def _run(
        self, action: str, arch: str, components: list[str],
        operator: str, parallel: str = "auto",
    ) -> None:
        import subprocess

        cmd = self._build_command(action, arch, components)
        if not cmd:
            self._emit(f"{arch} 尚无可验证的安装包，请先执行构建与打包")
            self._emit("__DONE__ 1")
            self._current = None
            return

        self._emit(f"$ {' '.join(cmd)}")
        rc = 1
        try:
            env = dict(os.environ)
            # 并发度交给脚本内的容量探测解析：auto / serial / 具体数字
            env["SPRIXIN_PARALLEL"] = parallel
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(self.repo_root),
                env=env,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                self._emit(line.rstrip("\n"))
            proc.wait()
            rc = proc.returncode
        except Exception as exc:  # noqa: BLE001
            self._emit(f"构建进程异常: {exc}")
        finally:
            self._emit(f"__DONE__ {rc}")
            self._current = None


class Handler(BaseHTTPRequestHandler):
    server_version = "sprixin-build"
    protocol_version = "HTTP/1.1"

    # ── 基础设施 ────────────────────────────────────────────────────

    def log_message(self, fmt: str, *args) -> None:  # 降噪
        if self.path.startswith("/api/build/log"):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _cookies(self) -> dict[str, str]:
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _subject(self) -> str | None:
        return self.server.auth.validate_session(self._cookies().get(SESSION_COOKIE))

    def _require_auth(self) -> str | None:
        subject = self._subject()
        if subject is None:
            self._send_json({"error": "未登录或会话已过期"}, 401)
            return None
        return subject

    # ── 路由 ────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if route == "/download":
            # 不要求会话：产物常在另一台机器上用 wget/curl 取走。
            # 凭证与路径绑定并有有效期，见 auth.sign_download。
            return self._api_download()

        if route in ("/", "/index.html"):
            return self._serve_static("index.html")
        if route.startswith("/static/"):
            return self._serve_static(route[len("/static/"):])

        if route == "/guest" or route == "/guest/":
            return self._serve_static("guest.html")
        if route == "/api/public":
            return self._api_public_data()
        if route == "/download/public":
            return self._api_public_download()

        if route == "/api/status":
            return self._send_json({
                "bound": self.server.auth.is_bound,
                "authenticated": self._subject() is not None,
                "subject": self._subject() or "",
                "busy": self.server.runner.busy,
                "current": self.server.runner.current,
            })

        if route == "/api/components":
            if self._require_auth() is None:
                return
            return self._api_components()

        if route == "/api/history":
            if self._require_auth() is None:
                return
            return self._api_history()

        if route == "/api/capacity":
            if self._require_auth() is None:
                return
            return self._api_capacity()

        if route == "/api/releases":
            if self._require_auth() is None:
                return
            return self._api_releases()

        if route == "/api/artifacts":
            if self._require_auth() is None:
                return
            return self._api_artifacts()

        if route == "/api/members":
            return self._api_members()

        if route == "/api/checklist":
            return self._api_checklist()

        if route == "/api/machines":
            return self._api_machines()

        if route == "/api/download-link":
            if self._require_auth() is None:
                return
            return self._api_download_link()

        if route == "/api/build/log":
            if self._require_auth() is None:
                return
            return self._api_build_log()

        self._send_json({"error": "未找到"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        route = urllib.parse.urlparse(self.path).path

        # 上传须在读取 body 之前分流：_body() 会一次性读完整个请求体并按
        # JSON 解析，上传的二进制会被它消费掉，随后的读取将无数据可读而阻塞。
        if route == "/api/upload":
            if self._require_auth() is None:
                return
            return self._api_upload()

        body = self._body()

        if route == "/api/enroll/begin":
            return self._api_enroll_begin(body)
        if route == "/api/enroll/confirm":
            return self._api_enroll_confirm(body)
        if route == "/api/login":
            return self._api_login(body)
        if route == "/api/logout":
            return self._api_logout()
        if route == "/api/members/remove":
            return self._api_member_remove(body)
        if route == "/api/checklist/mark":
            return self._api_checklist_mark(body)
        if route == "/api/artifacts/publish":
            return self._api_artifact_publish(body)
        if route == "/api/machines/save":
            return self._api_machine_save(body)
        if route == "/api/machines/delete":
            return self._api_machine_delete(body)
        if route == "/api/machines/bind":
            return self._api_machine_bind(body)
        if route == "/api/machines/check":
            return self._api_machine_check(body)
        if route == "/api/targets/verify":
            subject = self._require_auth()
            if subject is None:
                return
            return self._api_target_verify(body, subject)

        if route == "/api/components":
            if self._require_auth() is None:
                return
            return self._api_update_components(body)

        if route == "/api/build":
            subject = self._require_auth()
            if subject is None:
                return
            return self._api_build(body, subject)

        if route == "/api/release":
            subject = self._require_auth()
            if subject is None:
                return
            return self._api_release(body, subject)

        if route == "/api/artifact/delete":
            if self._require_auth() is None:
                return
            return self._api_delete_artifact(body)

        self._send_json({"error": "未找到"}, 404)

    # ── 认证 ────────────────────────────────────────────────────────

    def _api_enroll_begin(self, body: dict | None = None) -> None:
        auth = self.server.auth
        body = body or {}
        name = str(body.get("name") or "").strip()

        # 首位成员无需登录 —— 系统刚装好时还没有人能登录。此后再添加
        # 成员必须由已登录者发起，否则任何人都能给自己开一个账号进来。
        operator = ""
        if auth.is_bound:
            operator = self._require_auth()
            if operator is None:
                return
            if not name:
                return self._send_json({"error": "请填写新成员的名称"}, 400)
        name = name or "admin"

        try:
            secret, uri = auth.begin_enrollment(name)
        except (PermissionError, ValueError) as exc:
            return self._send_json({"error": str(exc)}, 409)
        # 分组显示便于手工输入
        grouped = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
        self._send_json({
            "name": name, "secret": secret,
            "secret_grouped": grouped, "uri": uri,
            "invited_by": operator or "",
        })

    def _api_enroll_confirm(self, body: dict) -> None:
        auth = self.server.auth
        name = str(body.get("name") or "admin").strip()
        # 已有成员时，这是在给别人开账号，操作者身份记进审计字段
        operator = ""
        if auth.is_bound:
            operator = self._subject() or ""
        try:
            ok = auth.confirm_enrollment(
                str(body.get("code", "")), account=name, added_by=operator,
            )
        except (PermissionError, ValueError) as exc:
            return self._send_json({"error": str(exc)}, 409)
        if not ok:
            return self._send_json({"error": "验证码不正确，请确认设备时间准确"}, 400)

        # 首次绑定即视为本人登录；由他人代开的账号不顺带签发会话，
        # 否则操作者的浏览器会变成新成员的身份。
        if operator:
            return self._send_json({"ok": True, "name": name})
        self._send_with_session({"ok": True, "name": name}, auth.issue_session(name))

    def _api_login(self, body: dict) -> None:
        auth = self.server.auth
        name = str(body.get("name") or "").strip()
        # 省略用户名时（只有一位成员的场合允许）把身份补全，否则会话将以
        # 空名义签发，发布记录里的操作者就成了一片空白。
        if not name:
            name = auth.sole_member_name()
        ok, msg = auth.verify(str(body.get("code", "")), account=name)
        if not ok:
            return self._send_json({"error": msg}, 401)
        self._send_with_session(
            {"ok": True, "message": msg, "name": name}, auth.issue_session(name),
        )

    def _api_members(self) -> None:
        if self._require_auth() is None:
            return
        self._send_json({"members": self.server.auth.members()})

    def _api_member_remove(self, body: dict) -> None:
        operator = self._require_auth()
        if operator is None:
            return
        try:
            self.server.auth.remove_member(str(body.get("name", "")), operator)
        except (PermissionError, ValueError) as exc:
            return self._send_json({"error": str(exc)}, 409)
        self._send_json({"ok": True})

    def _send_with_session(self, payload: dict, token: str) -> None:
        """发送 JSON 响应并附带会话 Cookie。

        Set-Cookie 必须与响应头一同发出，因此不能先调用 _send_json
        再补设 —— 那时响应头已经写出，Cookie 不会生效。
        """
        body_out = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_out)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800",
        )
        self.end_headers()
        self.wfile.write(body_out)

    def _api_logout(self) -> None:
        body_out = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_out)))
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(body_out)

    # ── 组件 ────────────────────────────────────────────────────────

    def _api_components(self) -> None:
        try:
            cfg = Config.load(self.server.config_path)
        except ConfigError as exc:
            return self._send_json({"error": str(exc)}, 500)

        # 已登记上游者，其校验凭据来自上游签名或哈希清单，界面上不必
        # 再要求填 sha256 —— 那一栏正是 redis 8.8.0 那次填错版本的地方。
        ups = getattr(cfg, "upstreams", {}) or {}
        method_label = {
            "pgp": "PGP 签名验证",
            "hash-index": "上游哈希清单",
            "cross-source": "多源交叉比对",
        }

        comps = []
        for c in cfg.components.values():
            spec = ups.get(c.name)
            comps.append({
                "name": c.name,
                "build": c.build,
                "version": c.version,
                "version_per_arch": c.version_per_arch,
                "vendor": c.vendor,
                "locked": c.locked,
                "upstream": {
                    "method": spec.method,
                    "label": method_label.get(spec.method, spec.method),
                    "tarball": spec.tarball,
                } if spec else None,
            })
        self._send_json({
            "package": {
                "name": cfg.package_name,
                "version": cfg.package_version,
                "top_dir": cfg.top_dir,
            },
            "baseline": {"glibc_max": cfg.glibc_max},
            "architectures": cfg.architectures,
            "vendored_libs": {
                n: {"version": l.version, "locked": l.locked}
                for n, l in cfg.vendored_libs.items()
            },
            "components": comps,
        })

    def _api_update_components(self, body: dict) -> None:
        """更新组件版本。

        只改动 version 与 sha256 两个字段，采用逐行文本替换而非重写
        整个 YAML —— 清单里的注释记录了大量决策依据（为何禁用 LVS、
        为何固定 OpenSSL 1.1 等），用 yaml.dump 回写会把它们全部丢掉。
        """
        updates = body.get("updates") or {}
        if not isinstance(updates, dict) or not updates:
            return self._send_json({"error": "没有需要更新的内容"}, 400)

        path = Path(self.server.config_path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError as exc:
            return self._send_json({"error": f"无法读取清单: {exc}"}, 500)

        changed: list[str] = []
        current: str | None = None
        in_components = False

        for i, line in enumerate(lines):
            stripped = line.rstrip("\n")
            if stripped.startswith("components:"):
                in_components = True
                continue
            if in_components and stripped and not stripped[0].isspace():
                in_components = False
            if not in_components:
                continue

            if len(line) - len(line.lstrip()) == 2 and stripped.endswith(":"):
                current = stripped.strip().rstrip(":")
                continue

            if current in updates:
                spec = updates[current]
                for key in ("version", "sha256"):
                    if key not in spec:
                        continue
                    marker = f"{key}:"
                    if stripped.strip().startswith(marker):
                        indent = line[: len(line) - len(line.lstrip())]
                        value = str(spec[key])
                        quoted = f'"{value}"' if key == "version" else value
                        lines[i] = f"{indent}{key}: {quoted}\n"
                        changed.append(f"{current}.{key} = {value}")

        if not changed:
            return self._send_json({"error": "清单中没有匹配到可更新的字段"}, 400)

        backup = path.with_suffix(".yaml.bak")
        try:
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            path.write_text("".join(lines), encoding="utf-8")
            Config.load(path)  # 立即回读校验，避免写坏清单
        except (OSError, ConfigError) as exc:
            if backup.exists():
                path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
            return self._send_json({"error": f"更新失败，已回滚: {exc}"}, 500)

        self._send_json({"ok": True, "changed": changed})

    # ── 构建 ────────────────────────────────────────────────────────

    def _api_build(self, body: dict, subject: str) -> None:
        arch = str(body.get("arch") or "x86_64")
        action = str(body.get("action") or "build")
        components = body.get("components") or []
        if not isinstance(components, list):
            components = []
        ok, msg = self.server.runner.start(
            arch=arch,
            components=[str(c) for c in components],
            operator=subject,
            action=action,
            parallel=str(body.get("parallel") or "auto"),
        )
        self._send_json({"ok": ok, "message": msg}, 200 if ok else 409)

    def _api_build_log(self) -> None:
        """以 SSE 推送构建日志。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = self.server.runner.subscribe()
        try:
            while True:
                try:
                    line = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps({"line": line}, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if line.startswith("__DONE__"):
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.runner.unsubscribe(q)

    # ── 上传 ────────────────────────────────────────────────────────

    def _api_upload(self) -> None:
        """接收自行准备的组件归档，打包时优先采用。

        仅开放给无需编译的组件（build: repack）—— nacos 是 jar、influxdb
        与 chronograf 是 Go 静态产物，都不链接系统库，改完直接替换即可用。
        需编译的组件（nginx、redis 等）不在此列：它们必须走基线编译与
        ABI 门禁，否则跨系统兼容性就无从保证。
        """
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        component = (params.get("component") or [""])[0].strip()
        arch = (params.get("arch") or ["all"])[0].strip()

        try:
            cfg = Config.load(self.server.config_path)
        except ConfigError as exc:
            return self._send_json({"error": str(exc)}, 500)

        comp = cfg.components.get(component)
        if comp is None:
            return self._send_json({"error": f"未知组件: {component}"}, 400)
        if comp.build != "repack":
            return self._send_json({
                "error": f"{component} 需在基线上编译并通过 ABI 门禁，不支持直接上传；"
                         f"可上传的组件：" + "、".join(
                             n for n, c in cfg.components.items() if c.build == "repack")
            }, 400)

        archs = list(cfg.architectures) if arch in ("", "all") else [arch]
        for a in archs:
            if a not in cfg.architectures:
                return self._send_json({"error": f"未知架构: {a}"}, 400)

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return self._send_json({"error": "空文件"}, 400)
        if length > 2 * 1024**3:
            return self._send_json({"error": "文件超过 2GB"}, 413)

        # 先落到临时文件，校验通过再放入正式位置，避免半截文件被用于构建
        uploads = self.server.runner.workspace / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        tmp = uploads / f".incoming-{component}-{int(time.time())}"

        received = 0
        try:
            with tmp.open("wb") as fh:
                while received < length:
                    chunk = self.rfile.read(min(1024 * 1024, length - received))
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            return self._send_json({"error": f"写入失败: {exc}"}, 500)

        if received != length:
            tmp.unlink(missing_ok=True)
            return self._send_json({"error": "上传不完整"}, 400)

        ok, detail = self._inspect_archive(tmp, component)
        if not ok:
            tmp.unlink(missing_ok=True)
            return self._send_json({"error": detail}, 400)

        import hashlib
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()

        placed = []
        for a in archs:
            dest_dir = uploads / a
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{component}.tar.gz"
            dest.write_bytes(tmp.read_bytes())
            placed.append(str(dest))
        tmp.unlink(missing_ok=True)

        self._send_json({
            "ok": True,
            "component": component,
            "arch": archs,
            "size": received,
            "sha256": digest,
            "detail": detail,
            "message": f"{component} 已接收，下次打包将采用该归档",
        })

    @staticmethod
    def _inspect_archive(path: Path, component: str) -> tuple[bool, str]:
        """检查上传的归档是否可用于打包。

        要求与 install.sh 的解压方式一致：gzip 压缩的 tar，且恰有一层
        顶层目录（解压时会 --strip-components 1）。不合规的包装到现场
        才会暴露，代价太高，故在接收时就拦下。
        """
        import tarfile

        try:
            with tarfile.open(path, "r:gz") as tar:
                names = tar.getnames()[:4000]
        except (tarfile.TarError, OSError) as exc:
            return False, f"不是有效的 tar.gz 归档: {exc}"

        if not names:
            return False, "归档为空"

        tops = {n.split("/")[0] for n in names if n and not n.startswith("/")}
        if len(tops) != 1:
            return False, (
                f"归档需恰有一层顶层目录（install.sh 解压时会 --strip-components 1），"
                f"当前顶层为: {'、'.join(sorted(tops)[:5])}"
            )

        return True, f"顶层目录 {tops.pop()}/，共 {len(names)} 项"

    # ── 发布 ────────────────────────────────────────────────────────

    def _release_manager(self):
        from sprixin_build.release import ReleaseManager
        return ReleaseManager(self.server.runner.workspace, self.server.store, log=lambda *_: None)

    def _api_releases(self) -> None:
        """已发布的正式版本。"""
        rels = self.server.store.releases()
        self._send_json({"releases": [
            {
                "id": r["id"], "version": r["version"], "arch": r["arch"],
                "filename": r["filename"], "sha256": r["sha256"], "size": r["size"],
                "released_by": r["released_by"], "released_at": r["released_at"],
                "test_note": r["test_note"] or "", "path": r["path"],
            } for r in rels
        ]})

    def _public_names(self) -> set[str]:
        return self._public_view().published_names()

    def _api_artifacts(self) -> None:
        """候选产物。正式版本不在其中，因而不会被误删。"""
        try:
            items = self._release_manager().deletable()
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)
        public = self._public_names()
        for it in items:
            it["public"] = it.get("name") in public
        self._send_json({"artifacts": items})

    def _checklist(self):
        from sprixin_build.checklist import Checklist

        return Checklist(Path(self.server.workspace))

    def _latest_artifact(self, arch: str) -> Path | None:
        dist = Path(self.server.workspace) / "dist" / arch
        pkgs = sorted(
            dist.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True
        ) if dist.is_dir() else []
        return pkgs[0] if pkgs else None

    def _api_checklist(self) -> None:
        """某个候选产物的验证清单与发布资格。"""
        if self._require_auth() is None:
            return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        arch = (params.get("arch") or ["x86_64"])[0].strip()
        pkg = (params.get("package") or [""])[0].strip()

        if not pkg:
            latest = self._latest_artifact(arch)
            if latest is None:
                return self._send_json({
                    "arch": arch, "items": [], "total": 0, "releasable": False,
                    "blocked_reason": f"{arch} 尚无候选产物",
                })
            pkg = latest.name

        self._send_json(self._checklist().status(arch, pkg))

    def _api_checklist_mark(self, body: dict) -> None:
        operator = self._require_auth()
        if operator is None:
            return
        pkg = str(body.get("package") or "").strip()
        target = str(body.get("target") or "").strip()
        if not pkg or not target:
            return self._send_json({"error": "需指定产物与目标系统"}, 400)

        self._checklist().mark(
            pkg, target,
            passed=bool(body.get("passed", True)),
            operator=operator,
            note=str(body.get("note") or ""),
        )
        arch = str(body.get("arch") or "x86_64").strip()
        self._send_json({"ok": True, "status": self._checklist().status(arch, pkg)})

    # ── 访客视图（无需登录）────────────────────────────────────────

    def _public_view(self):
        from public import PublicView

        return PublicView(Path(self.server.workspace), self.server.store)

    def _api_public_data(self) -> None:
        """访客页面所需的全部数据。

        这是整套系统里唯一不校验会话的数据接口，因此只返回 PublicView
        组装过的字段 —— 服务器路径、目标机凭据、成员名单、构建日志
        都不在其中。下载链接在此一并签发，访客无须也无法自行指定路径。
        """
        pv = self._public_view()
        auth = self.server.auth
        host = self.headers.get("Host") or "localhost"

        releases = pv.releases()
        for r in releases:
            for b in r.get("builds", []):
                full = (Path(self.server.workspace) / "releases"
                        / r["version"] / b["filename"])
                if full.is_file():
                    b["download"] = self._sign_public(
                        auth, "release", r["version"], b["filename"], host)

        artifacts = pv.artifacts()
        for a in artifacts:
            if (Path(self.server.workspace) / "dist" / a["arch"] / a["filename"]).is_file():
                a["download"] = self._sign_public(
                    auth, "artifact", a["arch"], a["filename"], host)

        self._send_json({
            "package": "sprixinSoft",
            "releases": releases,
            "artifacts": artifacts,
        })

    def _sign_public(self, auth, kind: str, key: str, filename: str, host: str) -> dict:
        """签发访客下载链接。

        刻意不用管理端那套以绝对路径为凭据的链接：它会把
        /root/sprixin-build/... 原样带到访客面前，白白泄露构建机的目录
        结构。这里只签「类别 + 版本或架构 + 文件名」，服务端据此在既定的
        两个目录内组装路径，访客既看不到也指定不了别处。

        访客不像管理员能随时重新生成链接，有效期给长一些。
        """
        sig, expiry = auth.sign_download(f"public:{kind}:{key}:{filename}", ttl=7 * 86400)
        query = urllib.parse.urlencode({
            "kind": kind, "key": key, "name": filename, "exp": expiry, "sig": sig,
        })
        url = f"http://{host}/download/public?{query}"
        return {"url": url, "curl": f"curl -fL -o '{filename}' '{url}'"}

    def _api_public_download(self) -> None:
        """访客下载。只认 releases/<版本>/ 与已公开的 dist/<架构>/。"""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        kind = (params.get("kind") or [""])[0]
        key = (params.get("key") or [""])[0]
        name = (params.get("name") or [""])[0]
        expiry = (params.get("exp") or [""])[0]
        sig = (params.get("sig") or [""])[0]

        ok, why = self.server.auth.verify_download(
            f"public:{kind}:{key}:{name}", expiry, sig
        )
        if not ok:
            return self._send_json({"error": why}, 403)

        # 组装路径前先挡住穿越：这几个值来自 URL，虽有签名保护，
        # 但签名只保证"没被改过"，不保证"内容是安全的"。
        if not name or "/" in name or ".." in name or "/" in key or ".." in key:
            return self._send_json({"error": "参数不合法"}, 400)

        ws = Path(self.server.workspace)
        if kind == "release":
            target = ws / "releases" / key / name
        elif kind == "artifact":
            if name not in self._public_view().published_names():
                return self._send_json({"error": "该产物未公开"}, 403)
            target = ws / "dist" / key / name
        else:
            return self._send_json({"error": "参数不合法"}, 400)

        target = target.resolve()
        allowed = [(ws / "releases").resolve(), (ws / "dist").resolve()]
        if not any(str(target).startswith(str(a) + "/") for a in allowed):
            return self._send_json({"error": "参数不合法"}, 400)
        if not target.is_file():
            return self._send_json({"error": "文件不存在"}, 404)

        self._stream_file(target)

    def _api_artifact_publish(self, body: dict) -> None:
        """管理员切换某个候选产物对访客的可见性。"""
        if self._require_auth() is None:
            return
        name = str(body.get("filename") or "").strip()
        if not name:
            return self._send_json({"error": "缺少文件名"}, 400)
        self._public_view().set_published(name, bool(body.get("public")))
        self._send_json({"ok": True})

    # ── 目标机器管理与远程实机验证 ──────────────────────────────────

    def _target_store(self):
        from sprixin_build.targets import TargetStore

        return TargetStore(Path(self.server.workspace) / "secrets")

    def _api_machines(self) -> None:
        """机器池与绑定关系。口令永不回显。"""
        if self._require_auth() is None:
            return
        store = self._target_store()
        self._send_json({
            "machines": [m.redacted() for m in store.machines().values()],
            "bindings": store.bindings(),
        })

    def _api_machine_save(self, body: dict) -> None:
        if self._require_auth() is None:
            return
        from sprixin_build.targets import Machine, TargetError

        try:
            mid = self._target_store().save_machine(Machine(
                id=str(body.get("id") or "").strip(),
                label=str(body.get("label") or "").strip(),
                host=str(body.get("host") or "").strip(),
                port=int(body.get("port") or 22),
                username=str(body.get("username") or "sprixin").strip(),
                password=str(body.get("password") or ""),
                note=str(body.get("note") or "").strip(),
            ))
        except (TargetError, ValueError) as exc:
            return self._send_json({"error": str(exc)}, 400)
        self._send_json({"ok": True, "id": mid})

    def _api_machine_delete(self, body: dict) -> None:
        if self._require_auth() is None:
            return
        from sprixin_build.targets import TargetError

        try:
            self._target_store().delete_machine(str(body.get("id") or ""))
        except TargetError as exc:
            return self._send_json({"error": str(exc)}, 404)
        self._send_json({"ok": True})

    def _api_machine_bind(self, body: dict) -> None:
        """把某个目标系统绑定到一台机器（空 id 表示解绑）。"""
        if self._require_auth() is None:
            return
        from sprixin_build.targets import TargetError

        try:
            self._target_store().bind(
                str(body.get("target") or "").strip(),
                str(body.get("id") or "").strip(),
            )
        except TargetError as exc:
            return self._send_json({"error": str(exc)}, 409)
        self._send_json({"ok": True})

    def _api_machine_check(self, body: dict) -> None:
        if self._require_auth() is None:
            return
        from sprixin_build.targets import TargetError, check

        m = self._target_store().machine(str(body.get("id") or ""))
        if m is None:
            return self._send_json({"error": "没有这台机器"}, 404)

        # 若已绑定到某个目标系统，一并核对系统与 glibc 是否对得上
        target = str(body.get("target") or "").strip()
        expect_glibc = ""
        if target:
            arch = str(body.get("arch") or "x86_64").strip()
            for row in self._checklist().container_results(arch):
                if row.get("target") == target:
                    expect_glibc = row.get("glibc", "")
                    break
        try:
            self._send_json(check(m, expect_target=target, expect_glibc=expect_glibc))
        except TargetError as exc:
            return self._send_json({"error": str(exc)}, 502)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": f"探测失败: {exc}"}, 502)

    def _api_target_verify(self, body: dict, subject: str) -> None:
        """把包送到已绑定的机器上跑一遍完整验证，通过则自动勾上清单。"""
        target = str(body.get("target") or "").strip()
        arch = str(body.get("arch") or "x86_64").strip()

        m = self._target_store().for_target(target)
        if m is None:
            return self._send_json({"error": "该目标系统尚未绑定机器"}, 404)

        pkg = body.get("package")
        target_pkg = Path(pkg) if pkg else self._latest_artifact(arch)
        if target_pkg is None or not target_pkg.is_file():
            return self._send_json({"error": f"{arch} 尚无可用的候选产物"}, 404)

        ok, msg = self.server.runner.start_remote_verify(
            machine=m, target=target, package=target_pkg, arch=arch,
            operator=subject,
            script=REPO_ROOT / "compat" / "realmachine-verify.sh",
        )
        self._send_json({"ok": ok, "message": msg}, 200 if ok else 409)

    def _api_release(self, body: dict, subject: str) -> None:
        """把候选产物提升为正式版本。

        构建通过不等于可以发布 —— 还需在实机上验证。因此发布是一次显式
        操作，并要求填写测试说明，便于日后追溯这个版本凭什么发出去。
        """
        from sprixin_build.release import ReleaseError

        arch = str(body.get("arch") or "").strip()
        version = str(body.get("version") or "").strip()
        note = str(body.get("test_note") or "").strip()
        artifact = body.get("artifact")

        if not arch or not version:
            return self._send_json({"error": "需指定架构与版本号"}, 400)

        # 发布门槛：清单上每个系统都须经真机确认。这一关在服务端把，
        # 而不只靠界面把按钮置灰 —— 前端的禁用状态绕过去太容易了。
        target = Path(artifact) if artifact else self._latest_artifact(arch)
        if target is None:
            return self._send_json({"error": f"{arch} 尚无候选产物"}, 409)

        ck = self._checklist()
        st = ck.status(arch, target.name)
        force = bool(body.get("force"))

        # 容器验证失败的系统，强制也不放行 —— 那不是"来不及验"，
        # 而是"验过且没通过"，两者性质不同。
        if st["failed"]:
            return self._send_json({
                "error": f"存在容器验证未通过的系统，不可发布：{'、'.join(st['failed'])}",
                "checklist": st,
            }, 409)

        if not st["releasable"] and not force:
            return self._send_json({
                "error": f"尚未达到发布标准：{st['blocked_reason']}",
                "checklist": st, "can_force": True,
            }, 409)

        # 测试说明由清单如实汇总：验过哪些、跳过哪些，都写进去。
        # coverage_note 本身已交代未验证的系统与数量，此处不再复述一遍 ——
        # 同一件事说两遍只会让人怀疑是不是两回事。
        coverage = ck.coverage_note(st)
        if note:
            note = f"{note}\n\n{coverage}" if coverage else note
        else:
            note = coverage
        if force and st["pending"]:
            note = f"【强制发布】\n\n{note}"

        try:
            result = self._release_manager().publish(
                arch=arch,
                version=version,
                artifact=target,
                released_by=subject,
                test_note=note,
            )
        except ReleaseError as exc:
            return self._send_json({"error": str(exc)}, 409)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": f"发布失败: {exc}"}, 500)

        self._send_json({
            "ok": True,
            "version": result.version,
            "arch": result.arch,
            "sha256": result.sha256,
            "size": result.size,
            "message": f"{version} ({arch}) 已发布为正式版本，不可删除",
        })

    def _api_delete_artifact(self, body: dict) -> None:
        from sprixin_build.release import ReleaseError

        path = str(body.get("path") or "").strip()
        if not path:
            return self._send_json({"error": "需指定文件"}, 400)
        try:
            msg = self._release_manager().delete(path)
        except ReleaseError as exc:
            return self._send_json({"error": str(exc)}, 403)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": f"删除失败: {exc}"}, 500)
        self._send_json({"ok": True, "message": msg})

    # ── 下载 ────────────────────────────────────────────────────────

    def _resolve_artifact(self, raw: str) -> Path | None:
        """把请求中的路径解析为工作区内的真实文件。

        只允许 dist/ 与 releases/ 两处，且解析后须仍在其中 —— 否则
        `../` 之类的写法就能读到任意文件。
        """
        ws = Path(self.server.runner.workspace).resolve()
        allowed = [(ws / "dist").resolve(), (ws / "releases").resolve()]

        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = ws / raw
        try:
            candidate = candidate.resolve()
        except OSError:
            return None

        if not candidate.is_file():
            return None
        for root in allowed:
            if str(candidate).startswith(str(root) + os.sep):
                return candidate
        return None

    def _api_download_link(self) -> None:
        """签发下载链接，供复制到任意机器使用。"""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        raw = (params.get("path") or [""])[0]

        target = self._resolve_artifact(raw)
        if target is None:
            return self._send_json({"error": "文件不存在或不在允许的目录内"}, 404)

        sig, expiry = self.server.auth.sign_download(str(target))
        query = urllib.parse.urlencode(
            {"path": str(target), "exp": expiry, "sig": sig}
        )

        # 用请求时的 Host 拼出链接，这样在哪个地址访问控制台，
        # 拿到的就是哪个地址的链接，不必在服务端写死主机名
        host = self.headers.get("Host") or "localhost"
        self._send_json({
            "url": f"http://{host}/download?{query}",
            "filename": target.name,
            "size": target.stat().st_size,
            "expires_at": expiry,
            # 显式给出输出文件名。curl 的 -O 取的是 URL 末段，而这里的
            # 地址以 ?path=…&sig=… 结尾，-O 会把整串 query 当成文件名存下来，
            # 拿到手还得自己 mv 一次。-J 虽能读 Content-Disposition，但要
            # 额外开关且各版本行为不一，不如把名字写死来得干脆。
            "curl": f"curl -fL -o '{target.name}' 'http://{host}/download?{query}'",
            "wget": (
                f"wget --content-disposition -O '{target.name}' "
                f"'http://{host}/download?{query}'"
            ),
        })

    def _api_download(self) -> None:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        raw = (params.get("path") or [""])[0]
        expiry = (params.get("exp") or [""])[0]
        sig = (params.get("sig") or [""])[0]

        ok, why = self.server.auth.verify_download(raw, expiry, sig)
        if not ok:
            return self._send_json({"error": why}, 403)

        target = self._resolve_artifact(raw)
        if target is None:
            return self._send_json({"error": "文件不存在"}, 404)
        self._stream_file(target)

    def _stream_file(self, target: Path) -> None:
        """把文件发出去，支持断点续传。

        管理端与访客端共用：几百 MB 的包在弱网下断一次就得重来，
        两边都需要 Range 支持，没有理由各写一份。
        """
        size = target.stat().st_size
        start, end = 0, size - 1

        rng = self.headers.get("Range", "")
        partial = False
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m:
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        try:
            with target.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ── 容量 ────────────────────────────────────────────────────────

    def _api_capacity(self) -> None:
        """报告构建机容量与建议并发度。

        并发度不适合硬编码：这套系统既可能跑在 160 核的服务器上，也可能
        跑在开发机上。而且排查问题时往往需要退回串行，让日志不再交错。
        """
        from sprixin_build.capacity import detect

        cap = detect(str(self.server.runner.workspace))
        self._send_json({
            "cores": cap.cores,
            "mem_total_gb": round(cap.mem_total_gb, 1),
            "mem_available_gb": round(cap.mem_available_gb, 1),
            "disk_free_gb": round(cap.disk_free_gb, 1),
            "load1": round(cap.load1, 2),
            "suggest": {
                "build": cap.suggest("build"),
                "verify": cap.suggest("verify"),
            },
            "explain": {
                "build": cap.explain("build"),
                "verify": cap.explain("verify"),
            },
        })

    # ── 沿革 ────────────────────────────────────────────────────────

    def _api_history(self) -> None:
        store = self.server.store
        records = store.history(limit=50)
        out = []
        for rec in records:
            prev = store.previous_success(rec.id, rec.arch)
            out.append({
                "id": rec.id,
                "package_version": rec.package_version,
                "arch": rec.arch,
                "status": rec.status,
                "started_at": rec.started_at,
                "duration_s": rec.duration_s,
                "operator": rec.operator,
                "gate_passed": rec.gate_passed,
                "changes": rec.diff_against(prev),
                "artifacts": [
                    {"filename": a["filename"], "sha256": a["sha256"], "size": a["size"]}
                    for a in rec.artifacts
                ],
                "verifications": [
                    {"target_os": v["target_os"], "passed": bool(v["passed"])}
                    for v in rec.verifications
                ],
            })
        self._send_json({"builds": out})

    # ── 静态文件 ────────────────────────────────────────────────────

    def _serve_static(self, rel: str) -> None:
        # 防目录穿越
        target = (WEB_DIR / "static" / rel).resolve()
        static_root = (WEB_DIR / "static").resolve()
        if not str(target).startswith(str(static_root)) or not target.is_file():
            return self._send_json({"error": "未找到"}, 404)

        data = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        # 不缓存：控制台随构建系统一起升级，浏览器留着旧页面会与新接口
        # 对不上。登录框加用户名那次改动就是这么失效的 —— 缓存的旧页面
        # 提交时不带用户名，后端认不出是谁，看起来就像"密码错了"。
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="sprixin-build 构建控制台")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--workspace", default=os.environ.get("SPRIXIN_WORKSPACE", "/root/sprixin-build"))
    parser.add_argument("--config", default=str(REPO_ROOT / "components.yaml"))
    args = parser.parse_args()

    workspace = Path(args.workspace)
    secrets_dir = workspace / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.workspace = workspace
    httpd.auth = Authenticator(secrets_dir / "auth.json")
    httpd.store = BuildStore(workspace / "builds.db")
    httpd.runner = BuildRunner(REPO_ROOT, workspace, httpd.store)
    httpd.config_path = args.config

    state = "已绑定验证器" if httpd.auth.is_bound else "尚未绑定，首次访问将引导绑定"
    print(f"构建控制台已启动: http://{args.host}:{args.port}  （{state}）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
