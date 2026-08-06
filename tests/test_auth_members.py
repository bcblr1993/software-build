"""多成员验证器。

重点验证三件事：旧的单用户配置能平滑迁移（已绑好的验证器不必重扫）、
失败锁定与防重放按人独立（否则一人输错会把所有人挡在门外）、
以及不会把最后一位成员删掉导致无人可登录。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "_auth_under_test", REPO / "web" / "auth.py"
)
_auth = importlib.util.module_from_spec(_spec)
sys.modules["_auth_under_test"] = _auth
_spec.loader.exec_module(_auth)

Authenticator = _auth.Authenticator


def _code_for(secret: str, when: float | None = None) -> str:
    counter = int((when or time.time()) // _auth.PERIOD)
    return _auth._hotp(secret, counter)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "auth.json"

    def _auth(self):
        return Authenticator(self.path)

    def _enroll(self, a, name):
        # 绑定用上一个时间窗的码（校验容忍 ±1 个步长），好让当前时间窗
        # 留给后续的登录断言 —— 否则绑定本身就把它消耗掉，紧接着的登录
        # 会被防重放正确地挡下，测出来的是测试写法的问题而非实现的问题。
        secret, _ = a.begin_enrollment(name)
        past = time.time() - _auth.PERIOD
        self.assertTrue(a.confirm_enrollment(_code_for(secret, past), account=name))
        return secret


class Migration(Base):
    def test_legacy_single_user_config_still_works(self):
        """旧格式必须能直接用，不能要求人重新扫码绑定。"""
        secret = _auth.generate_secret()
        self.path.write_text(json.dumps({
            "secret": secret, "bound": True,
            "used_counters": [], "failures": 0, "locked_until": 0.0,
        }))

        a = self._auth()
        self.assertTrue(a.is_bound)
        names = [m["name"] for m in a.members()]
        self.assertEqual(names, [_auth.LEGACY_NAME])

        ok, _ = a.verify(_code_for(secret), account=_auth.LEGACY_NAME)
        self.assertTrue(ok)

    def test_migrated_state_is_rewritten_in_new_format(self):
        secret = _auth.generate_secret()
        self.path.write_text(json.dumps({"secret": secret, "bound": True}))
        a = self._auth()
        a.verify(_code_for(secret), account=_auth.LEGACY_NAME)
        raw = json.loads(self.path.read_text())
        self.assertIn("members", raw)
        self.assertEqual(raw.get("version"), 2)


class MultipleMembers(Base):
    def test_two_members_each_login_with_own_code(self):
        a = self._auth()
        s1 = self._enroll(a, "chenxu")
        s2 = self._enroll(a, "zhangsan")

        ok, _ = a.verify(_code_for(s1), account="chenxu")
        self.assertTrue(ok)
        ok, _ = a.verify(_code_for(s2), account="zhangsan")
        self.assertTrue(ok)

    def test_code_of_one_member_rejected_for_another(self):
        a = self._auth()
        s1 = self._enroll(a, "chenxu")
        self._enroll(a, "zhangsan")
        ok, _ = a.verify(_code_for(s1), account="zhangsan")
        self.assertFalse(ok)

    def test_lockout_is_per_member(self):
        """一个人连续输错，不应把别人一起锁在门外。"""
        a = self._auth()
        self._enroll(a, "chenxu")
        s2 = self._enroll(a, "zhangsan")

        for _ in range(_auth.MAX_FAILURES + 1):
            a.verify("000000", account="chenxu")

        ok, msg = a.verify(_code_for(s2), account="zhangsan")
        self.assertTrue(ok, f"另一位成员被误锁: {msg}")

    def test_replay_counter_is_per_member(self):
        """同一时间窗内两人先后登录，后者不该被当成重放。"""
        a = self._auth()
        s1 = self._enroll(a, "chenxu")
        s2 = self._enroll(a, "zhangsan")
        now = time.time()

        self.assertTrue(a.verify(_code_for(s1, now), account="chenxu")[0])
        self.assertTrue(a.verify(_code_for(s2, now), account="zhangsan")[0])

    def test_same_code_cannot_be_replayed(self):
        a = self._auth()
        s = self._enroll(a, "chenxu")
        code = _code_for(s)
        self.assertTrue(a.verify(code, account="chenxu")[0])
        self.assertFalse(a.verify(code, account="chenxu")[0])

    def test_unknown_name_does_not_reveal_membership(self):
        """错误提示不能成为枚举成员的探针。"""
        a = self._auth()
        s = self._enroll(a, "chenxu")
        _, msg_unknown = a.verify("000000", account="someone-else")
        _, msg_known = a.verify("000000", account="chenxu")
        self.assertIn("不正确", msg_unknown)
        self.assertIn("不正确", msg_known)

    def test_name_is_case_insensitive(self):
        a = self._auth()
        s = self._enroll(a, "ChenXu")
        self.assertTrue(a.verify(_code_for(s), account="chenxu")[0])


class Removal(Base):
    def test_cannot_remove_last_bound_member(self):
        """删到没人能登录，就只能去构建机上手工修文件了。"""
        a = self._auth()
        self._enroll(a, "chenxu")
        with self.assertRaises(PermissionError):
            a.remove_member("chenxu")

    def test_can_remove_when_others_remain(self):
        a = self._auth()
        self._enroll(a, "chenxu")
        self._enroll(a, "zhangsan")
        a.remove_member("zhangsan")
        self.assertEqual([m["name"] for m in a.members()], ["chenxu"])

    def test_removed_member_cannot_login(self):
        a = self._auth()
        self._enroll(a, "chenxu")
        s2 = self._enroll(a, "zhangsan")
        a.remove_member("zhangsan")
        self.assertFalse(a.verify(_code_for(s2), account="zhangsan")[0])

    def test_removing_unknown_member_raises(self):
        a = self._auth()
        self._enroll(a, "chenxu")
        with self.assertRaises(ValueError):
            a.remove_member("nobody")


class Enrollment(Base):
    def test_cannot_rebind_existing_member_from_web(self):
        """已绑定者不可从网页端顶掉，否则等于绕过了验证器。"""
        a = self._auth()
        self._enroll(a, "chenxu")
        with self.assertRaises(PermissionError):
            a.begin_enrollment("chenxu")

    def test_state_survives_reload(self):
        a = self._auth()
        s = self._enroll(a, "chenxu")
        b = self._auth()          # 重新从磁盘加载
        self.assertTrue(b.is_bound)
        self.assertTrue(b.verify(_code_for(s), account="chenxu")[0])

    def test_empty_name_rejected(self):
        a = self._auth()
        with self.assertRaises(ValueError):
            a.begin_enrollment("   ")


if __name__ == "__main__":
    unittest.main()
