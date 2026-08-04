"""基于 TOTP 的管理员认证（RFC 6238）。

不依赖第三方库：标准库的 hmac / hashlib / base64 已足够，而构建机所处
的内网出口带宽有限，少一个 pip 依赖就少一处安装失败的可能。

与主流验证器（Google Authenticator、Microsoft Authenticator、1Password
等）兼容：SHA-1、6 位、30 秒步长。

安全措施：
  · 校验窗口为前后各一个步长，容忍时钟漂移，但不放宽到更大范围
  · 同一动态码用过即作废，防止验证码在有效期内被重放
  · 失败次数超限后锁定，抵御在线暴力破解（6 位码搜索空间仅一百万）
  · 密钥文件权限 600，且存放于 .gitignore 覆盖的 secrets/ 目录
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

DIGITS = 6
PERIOD = 30
ALGORITHM = "SHA1"

# 在线暴力破解防护
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300

# 会话有效期
SESSION_TTL = 8 * 3600


def generate_secret(length: int = 20) -> str:
    """生成 Base32 编码的 TOTP 密钥（默认 160 位，符合 RFC 4226 建议）。"""
    return base64.b32encode(secrets.token_bytes(length)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    padding = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32.upper() + padding)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**DIGITS)).zfill(DIGITS)


def totp_at(secret_b32: str, timestamp: float | None = None) -> str:
    ts = time.time() if timestamp is None else timestamp
    return _hotp(secret_b32, int(ts // PERIOD))


def provisioning_uri(secret_b32: str, account: str, issuer: str) -> str:
    """生成验证器可识别的 otpauth:// 地址。"""
    label = quote(f"{issuer}:{account}")
    return (
        f"otpauth://totp/{label}?secret={secret_b32}"
        f"&issuer={quote(issuer)}&algorithm={ALGORITHM}"
        f"&digits={DIGITS}&period={PERIOD}"
    )


@dataclass
class AuthState:
    secret: str = ""
    bound: bool = False
    used_counters: list[int] = field(default_factory=list)
    failures: int = 0
    locked_until: float = 0.0


class Authenticator:
    """TOTP 认证与会话签发。

    状态持久化在 secrets/auth.json（权限 600）。
    """

    def __init__(self, state_path: str | Path, issuer: str = "sprixin-build") -> None:
        self.path = Path(state_path)
        self.issuer = issuer
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state = self._load()
        self._session_key = self._load_session_key()

    # ── 持久化 ──────────────────────────────────────────────────────

    def _load(self) -> AuthState:
        if not self.path.exists():
            return AuthState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return AuthState()
        return AuthState(
            secret=raw.get("secret", ""),
            bound=bool(raw.get("bound", False)),
            used_counters=list(raw.get("used_counters", []))[-10:],
            failures=int(raw.get("failures", 0)),
            locked_until=float(raw.get("locked_until", 0.0)),
        )

    def _save(self) -> None:
        payload = {
            "secret": self.state.secret,
            "bound": self.state.bound,
            "used_counters": self.state.used_counters[-10:],
            "failures": self.state.failures,
            "locked_until": self.state.locked_until,
        }
        tmp = self.path.with_suffix(".tmp")
        old_umask = os.umask(0o077)
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            self.path.chmod(0o600)
        finally:
            os.umask(old_umask)

    def _load_session_key(self) -> bytes:
        key_path = self.path.parent / "session.key"
        if key_path.exists():
            return key_path.read_bytes()
        key = secrets.token_bytes(32)
        old_umask = os.umask(0o077)
        try:
            key_path.write_bytes(key)
            key_path.chmod(0o600)
        finally:
            os.umask(old_umask)
        return key

    # ── 绑定 ────────────────────────────────────────────────────────

    @property
    def is_bound(self) -> bool:
        return self.state.bound and bool(self.state.secret)

    def begin_enrollment(self, account: str = "admin") -> tuple[str, str]:
        """生成新密钥并返回 (密钥, otpauth 地址)。

        仅在尚未绑定时可用。已绑定后若需重置，必须删除 secrets/auth.json，
        这要求对构建机有 shell 访问权限 —— 避免任何人从网页端顶掉现有绑定。
        """
        if self.is_bound:
            raise PermissionError("已完成绑定；如需重置请在构建机上删除 secrets/auth.json")
        self.state.secret = generate_secret()
        self.state.bound = False
        self._save()
        return self.state.secret, provisioning_uri(self.state.secret, account, self.issuer)

    def confirm_enrollment(self, code: str) -> bool:
        """校验一次动态码以确认验证器绑定成功。"""
        if self.is_bound:
            raise PermissionError("已完成绑定")
        if not self.state.secret:
            raise ValueError("尚未开始绑定")
        if not self._check_code(code):
            return False
        self.state.bound = True
        self.state.failures = 0
        self._save()
        return True

    # ── 校验 ────────────────────────────────────────────────────────

    def _check_code(self, code: str) -> bool:
        code = (code or "").strip().replace(" ", "")
        if not code.isdigit() or len(code) != DIGITS:
            return False

        now = time.time()
        counter = int(now // PERIOD)

        # 前后各一个步长，容忍时钟漂移
        for delta in (0, -1, 1):
            candidate = counter + delta
            if candidate in self.state.used_counters:
                continue
            if hmac.compare_digest(_hotp(self.state.secret, candidate), code):
                self.state.used_counters.append(candidate)
                self.state.used_counters = self.state.used_counters[-10:]
                return True
        return False

    def verify(self, code: str) -> tuple[bool, str]:
        """校验动态码。返回 (是否通过, 说明)。"""
        now = time.time()

        if not self.is_bound:
            return False, "尚未绑定验证器"

        if now < self.state.locked_until:
            wait = int(self.state.locked_until - now)
            return False, f"失败次数过多，请 {wait} 秒后重试"

        if self._check_code(code):
            self.state.failures = 0
            self.state.locked_until = 0.0
            self._save()
            return True, "验证通过"

        self.state.failures += 1
        if self.state.failures >= MAX_FAILURES:
            self.state.locked_until = now + LOCKOUT_SECONDS
            self.state.failures = 0
            self._save()
            return False, f"失败次数过多，已锁定 {LOCKOUT_SECONDS // 60} 分钟"

        self._save()
        remaining = MAX_FAILURES - self.state.failures
        return False, f"验证码不正确，还可尝试 {remaining} 次"

    # ── 会话 ────────────────────────────────────────────────────────

    def issue_session(self, subject: str = "admin", ttl: int = SESSION_TTL) -> str:
        """签发会话令牌：subject.expiry.signature"""
        expiry = int(time.time()) + ttl
        payload = f"{subject}.{expiry}"
        sig = hmac.new(self._session_key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def validate_session(self, token: str | None) -> str | None:
        """校验会话令牌，通过则返回 subject。"""
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        subject, expiry_raw, sig = parts
        payload = f"{subject}.{expiry_raw}"
        expected = hmac.new(self._session_key, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        try:
            if int(expiry_raw) < time.time():
                return None
        except ValueError:
            return None
        return subject
