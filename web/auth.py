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


#: 单用户时代的账号名，迁移旧配置时沿用
LEGACY_NAME = "admin"

#: 用户名不存在时用来消耗等量计算，避免以响应快慢泄露成员是否存在
_DUMMY_SECRET = "A" * 32


def _normalize_name(name: str) -> str:
    """成员名归一：去空白、转小写。

    避免 "Admin" 与 "admin" 被当成两个人，也避免尾随空格造成登录失败
    却看不出原因。
    """
    return (name or "").strip().lower()


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Member:
    """一位已登记的使用者。

    失败计数与防重放计数器都按人存放，而非全局：全局存放会让一个人
    连续输错把所有人一起锁在门外，也会让两人在同一时间窗内先后登录时
    后者被误判为重放。
    """

    name: str = ""
    secret: str = ""
    bound: bool = False
    used_counters: list[int] = field(default_factory=list)
    failures: int = 0
    locked_until: float = 0.0
    added_at: str = ""
    added_by: str = ""


@dataclass
class AuthState:
    members: dict = field(default_factory=dict)


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

        # 旧格式为单个密钥。就地迁移成第一位成员，已绑好的验证器继续可用 ——
        # 让人为了这次改造重新扫码是不必要的打扰。
        if "members" not in raw and raw.get("secret"):
            legacy = Member(
                name=LEGACY_NAME,
                secret=raw.get("secret", ""),
                bound=bool(raw.get("bound", False)),
                used_counters=list(raw.get("used_counters", []))[-10:],
                failures=int(raw.get("failures", 0)),
                locked_until=float(raw.get("locked_until", 0.0)),
                added_at="(迁移自单用户配置)",
            )
            return AuthState(members={LEGACY_NAME: legacy})

        members: dict = {}
        for name, m in (raw.get("members") or {}).items():
            members[name] = Member(
                name=name,
                secret=m.get("secret", ""),
                bound=bool(m.get("bound", False)),
                used_counters=list(m.get("used_counters", []))[-10:],
                failures=int(m.get("failures", 0)),
                locked_until=float(m.get("locked_until", 0.0)),
                added_at=m.get("added_at", ""),
                added_by=m.get("added_by", ""),
            )
        return AuthState(members=members)

    def _save(self) -> None:
        payload = {
            "version": 2,
            "members": {
                name: {
                    "secret": m.secret,
                    "bound": m.bound,
                    "used_counters": m.used_counters[-10:],
                    "failures": m.failures,
                    "locked_until": m.locked_until,
                    "added_at": m.added_at,
                    "added_by": m.added_by,
                }
                for name, m in self.state.members.items()
            },
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
        """是否已有可用于登录的成员。"""
        return any(m.bound and m.secret for m in self.state.members.values())

    def members(self) -> list[dict]:
        return [
            {
                "name": m.name,
                "bound": m.bound,
                "added_at": m.added_at,
                "added_by": m.added_by,
                "locked": time.time() < m.locked_until,
            }
            for m in sorted(self.state.members.values(), key=lambda x: x.name)
        ]

    def begin_enrollment(self, account: str = LEGACY_NAME) -> tuple[str, str]:
        """为某人生成新密钥并返回 (密钥, otpauth 地址)。

        首位成员无需登录即可绑定 —— 系统刚装好时还没有人能登录。此后
        再添加成员须由已登录者发起，这一层由 server 的会话校验把关。

        已绑定的成员不可从网页端顶掉：重置须在构建机上编辑
        secrets/auth.json，也就要求先有 shell 访问权限。
        """
        name = _normalize_name(account)
        if not name:
            raise ValueError("成员名不能为空")

        exist = self.state.members.get(name)
        if exist is not None and exist.bound:
            raise PermissionError(
                f"{name} 已完成绑定；如需重置请在构建机上编辑 secrets/auth.json"
            )

        secret = generate_secret()
        self.state.members[name] = Member(
            name=name,
            secret=secret,
            bound=False,
            added_at=exist.added_at if exist else "",
            added_by=exist.added_by if exist else "",
        )
        self._save()
        return secret, provisioning_uri(secret, name, self.issuer)

    def confirm_enrollment(self, code: str, account: str = LEGACY_NAME,
                           added_by: str = "") -> bool:
        """校验一次动态码以确认某人的验证器绑定成功。"""
        name = _normalize_name(account)
        m = self.state.members.get(name)
        if m is None or not m.secret:
            raise ValueError("尚未开始绑定")
        if m.bound:
            raise PermissionError(f"{name} 已完成绑定")
        if not self._check_code(m, code):
            return False
        m.bound = True
        m.failures = 0
        m.added_at = m.added_at or _now_text()
        m.added_by = m.added_by or added_by
        self._save()
        return True

    def remove_member(self, name: str, operator: str = "") -> None:
        """移除成员。

        不允许移除最后一位 —— 那会让所有人都进不去，只能到构建机上手工
        修文件才能恢复。
        """
        name = _normalize_name(name)
        if name not in self.state.members:
            raise ValueError(f"没有名为 {name} 的成员")
        bound = [n for n, m in self.state.members.items() if m.bound]
        if bound == [name]:
            raise PermissionError("这是最后一位已绑定的成员，移除后将无人可登录")
        del self.state.members[name]
        self._save()

    # ── 校验 ────────────────────────────────────────────────────────

    def _check_code(self, member: Member, code: str) -> bool:
        code = (code or "").strip().replace(" ", "")
        if not code.isdigit() or len(code) != DIGITS:
            return False

        now = time.time()
        counter = int(now // PERIOD)

        # 前后各一个步长，容忍时钟漂移
        for delta in (0, -1, 1):
            candidate = counter + delta
            if candidate in member.used_counters:
                continue
            if hmac.compare_digest(_hotp(member.secret, candidate), code):
                member.used_counters.append(candidate)
                member.used_counters = member.used_counters[-10:]
                return True
        return False

    def verify(self, code: str, account: str = "") -> tuple[bool, str]:
        """校验某人的动态码。返回 (是否通过, 说明)。"""
        now = time.time()

        if not self.is_bound:
            return False, "尚未绑定验证器"

        name = _normalize_name(account)
        m = self.state.members.get(name)

        # 用户名不存在时，走完与存在时相同的时间开销并给出同样含糊的
        # 提示 —— 否则错误信息就成了枚举系统里有哪些人的探针。
        if m is None or not m.bound:
            _hotp(_DUMMY_SECRET, int(now // PERIOD))
            return False, "用户名或验证码不正确"

        if now < m.locked_until:
            wait = int(m.locked_until - now)
            return False, f"失败次数过多，请 {wait} 秒后重试"

        if self._check_code(m, code):
            m.failures = 0
            m.locked_until = 0.0
            self._save()
            return True, "验证通过"

        m.failures += 1
        if m.failures >= MAX_FAILURES:
            m.locked_until = now + LOCKOUT_SECONDS
            m.failures = 0
            self._save()
            return False, f"失败次数过多，已锁定 {LOCKOUT_SECONDS // 60} 分钟"

        self._save()
        remaining = MAX_FAILURES - m.failures
        return False, f"用户名或验证码不正确，还可尝试 {remaining} 次"

    # ── 会话 ────────────────────────────────────────────────────────

    def issue_session(self, subject: str = "admin", ttl: int = SESSION_TTL) -> str:
        """签发会话令牌：subject.expiry.signature"""
        expiry = int(time.time()) + ttl
        payload = f"{subject}.{expiry}"
        sig = hmac.new(self._session_key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    # ── 下载链接签名 ────────────────────────────────────────────────

    def sign_download(self, path: str, ttl: int = 86400) -> tuple[str, int]:
        """为某个文件签发下载凭证。

        产物动辄数百 MB，取走它的场合往往是另一台机器上的 wget/curl，
        带不了浏览器会话。因此改用签名链接：凭证与具体路径绑定，换一个
        路径签名即失效，从而既能直接下载，又不至于变成任意文件读取。
        """
        expiry = int(time.time()) + ttl
        payload = f"{path}|{expiry}"
        sig = hmac.new(self._session_key, payload.encode(), hashlib.sha256).hexdigest()
        return sig, expiry

    def verify_download(self, path: str, expiry: str | int, sig: str) -> tuple[bool, str]:
        try:
            expiry_i = int(expiry)
        except (TypeError, ValueError):
            return False, "凭证格式不正确"

        expected = hmac.new(
            self._session_key, f"{path}|{expiry_i}".encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, sig or ""):
            return False, "凭证无效"
        if expiry_i < time.time():
            return False, "链接已过期，请回控制台重新获取"
        return True, ""

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
