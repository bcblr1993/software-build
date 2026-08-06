"""验证清单与发布时的改名。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_cl = _load("_checklist_under_test", "scripts/sprixin_build/checklist.py")
Checklist = _cl.Checklist

PKG = "sprixinSoft-x86_64-v14-2026-08-06.tar.gz"


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "verify-results").mkdir(parents=True)

    def _container(self, arch: str, rows: list[dict]):
        (self.ws / "verify-results" / f"{arch}.json").write_text(
            json.dumps(rows), encoding="utf-8"
        )

    def _rows(self, n=3, passed=True, package=PKG):
        return [
            {"target": f"os-{i}", "os_name": f"OS {i}", "glibc": "2.28",
             "passed": passed, "package": package}
            for i in range(n)
        ]


class ContainerBaseline(Base):
    def test_container_results_define_the_list(self):
        """容器验证确定「需要验哪些系统」，但它本身不构成发布资格。"""
        self._container("x86_64", self._rows(3))
        st = Checklist(self.ws).status("x86_64", PKG)
        self.assertEqual(st["total"], 3)
        self.assertEqual(st["container_passed"], 3)
        self.assertEqual(st["machine_checked"], 0)
        self.assertFalse(st["releasable"], "容器全过也不能直接发，实机未验")
        self.assertIn("尚未在真实机器上验证", st["blocked_reason"])

    def test_failed_container_blocks_release(self):
        rows = self._rows(2) + [
            {"target": "bad-os", "os_name": "Bad", "passed": False, "package": PKG}
        ]
        self._container("x86_64", rows)
        st = Checklist(self.ws).status("x86_64", PKG)
        self.assertFalse(st["releasable"])
        self.assertIn("bad-os", st["failed"])
        self.assertIn("bad-os", st["blocked_reason"])

    def test_no_verification_blocks_release(self):
        st = Checklist(self.ws).status("x86_64", PKG)
        self.assertFalse(st["releasable"])
        self.assertIn("尚无容器验证", st["blocked_reason"])

    def test_results_of_another_package_do_not_count(self):
        """换个包重新构建后，旧包的验证结果不能给新包背书。"""
        self._container("x86_64", self._rows(3, package="some-other-pkg.tar.gz"))
        st = Checklist(self.ws).status("x86_64", PKG)
        self.assertEqual(st["total"], 0)
        self.assertFalse(st["releasable"])


class ManualChecks(Base):
    def test_mark_and_unmark(self):
        self._container("x86_64", self._rows(2))
        c = Checklist(self.ws)
        c.mark(PKG, "os-0", passed=True, operator="chenxu", note="lab-host-1")

        st = c.status("x86_64", PKG)
        item = next(i for i in st["items"] if i["target"] == "os-0")
        self.assertTrue(item["machine_checked"])
        self.assertEqual(item["machine_note"], "lab-host-1")
        self.assertEqual(item["machine_by"], "chenxu")

        c.mark(PKG, "os-0", passed=False)
        st = c.status("x86_64", PKG)
        item = next(i for i in st["items"] if i["target"] == "os-0")
        self.assertFalse(item["machine_checked"])

    def test_manual_checks_survive_reload(self):
        self._container("x86_64", self._rows(1))
        Checklist(self.ws).mark(PKG, "os-0", passed=True, operator="a")
        self.assertTrue(
            Checklist(self.ws).status("x86_64", PKG)["items"][0]["machine_checked"]
        )

    def test_release_requires_every_system_machine_checked(self):
        """每一个系统都必须经真机确认，少一个都发不出去。"""
        self._container("x86_64", self._rows(3))
        c = Checklist(self.ws)

        c.mark(PKG, "os-0", passed=True, operator="chenxu")
        c.mark(PKG, "os-1", passed=True, operator="chenxu")
        st = c.status("x86_64", PKG)
        self.assertFalse(st["releasable"], "还差一个就不该放行")
        self.assertEqual(st["pending"], ["os-2"])

        c.mark(PKG, "os-2", passed=True, operator="chenxu")
        st = c.status("x86_64", PKG)
        self.assertTrue(st["releasable"])
        self.assertEqual(st["blocked_reason"], "")
        self.assertEqual(st["machine_checked"], 3)

    def test_unchecking_revokes_release_eligibility(self):
        """勾了又取消，发布资格随之收回。"""
        self._container("x86_64", self._rows(2))
        c = Checklist(self.ws)
        c.mark(PKG, "os-0", passed=True)
        c.mark(PKG, "os-1", passed=True)
        self.assertTrue(c.status("x86_64", PKG)["releasable"])

        c.mark(PKG, "os-1", passed=False)
        self.assertFalse(c.status("x86_64", PKG)["releasable"])

    def test_container_failure_blocks_even_if_machine_checked(self):
        """容器都过不了的系统，真机勾了也不放行。"""
        rows = self._rows(1) + [
            {"target": "bad-os", "os_name": "Bad", "passed": False, "package": PKG}
        ]
        self._container("x86_64", rows)
        c = Checklist(self.ws)
        c.mark(PKG, "os-0", passed=True)
        c.mark(PKG, "bad-os", passed=True)
        st = c.status("x86_64", PKG)
        self.assertFalse(st["releasable"])
        self.assertIn("bad-os", st["failed"])

    def test_checks_are_bound_to_package_not_version(self):
        """重新构建出的新包，不继承旧包的勾选。"""
        self._container("x86_64", self._rows(1))
        c = Checklist(self.ws)
        c.mark(PKG, "os-0", passed=True)
        other = "sprixinSoft-x86_64-v14-2026-08-07.tar.gz"
        self.assertEqual(c.manual_checks(other), {})

    def test_machine_only_target_still_listed(self):
        """实机上验过、但容器清单里没有的机型，也要出现在清单中。"""
        self._container("x86_64", self._rows(1))
        c = Checklist(self.ws)
        c.mark(PKG, "现场特有机型", passed=True, note="客户环境")
        st = c.status("x86_64", PKG)
        targets = [i["target"] for i in st["items"]]
        self.assertIn("现场特有机型", targets)
        extra = next(i for i in st["items"] if i["target"] == "现场特有机型")
        self.assertIsNone(extra["container_passed"])


class RenameForVersion(unittest.TestCase):
    """发布时包名要跟着版本号走。"""

    @classmethod
    def setUpClass(cls):
        try:
            import yaml  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("本机没有 pyyaml")
        from sprixin_build.release import rename_for_version
        cls.fn = staticmethod(rename_for_version)

    def test_replaces_version_segment(self):
        self.assertEqual(
            self.fn("sprixinSoft-x86_64-v14-2026-08-06.tar.gz", "v15"),
            "sprixinSoft-x86_64-v15-2026-08-06.tar.gz",
        )

    def test_keeps_arch_and_date(self):
        out = self.fn("sprixinSoft-aarch64-v14-2026-08-06.tar.gz", "v20")
        self.assertIn("aarch64", out)
        self.assertIn("2026-08-06", out)

    def test_same_version_is_untouched(self):
        name = "sprixinSoft-x86_64-v14-2026-08-06.tar.gz"
        self.assertEqual(self.fn(name, "v14"), name)

    def test_unrecognized_name_left_alone(self):
        """认不出格式时保持原名，宁可不改也不要猜错。"""
        self.assertEqual(self.fn("custom-package.tar.gz", "v15"), "custom-package.tar.gz")

    def test_empty_version_left_alone(self):
        name = "sprixinSoft-x86_64-v14-2026-08-06.tar.gz"
        self.assertEqual(self.fn(name, ""), name)

    def test_version_without_v_prefix(self):
        self.assertEqual(
            self.fn("sprixinSoft-x86_64-v14-2026-08-06.tar.gz", "2026.1"),
            "sprixinSoft-x86_64-2026.1-2026-08-06.tar.gz",
        )


if __name__ == "__main__":
    unittest.main()


class ForcePublishCoverage(Base):
    """强制发布时，发布说明必须如实交代哪些验了、哪些没验。"""

    def test_coverage_note_lists_both_sides(self):
        self._container("x86_64", self._rows(3))
        c = Checklist(self.ws)
        c.mark(PKG, "os-0", passed=True, note="lab-host-1")
        st = c.status("x86_64", PKG)
        note = c.coverage_note(st)
        self.assertIn("1 个目标系统的真实机器上验证通过", note)
        self.assertIn("lab-host-1", note)
        self.assertIn("2 个系统未经真实机器验证", note)
        self.assertIn("os-1", note)
        self.assertIn("os-2", note)

    def test_coverage_note_when_all_verified(self):
        self._container("x86_64", self._rows(2))
        c = Checklist(self.ws)
        c.mark(PKG, "os-0", passed=True)
        c.mark(PKG, "os-1", passed=True)
        note = c.coverage_note(c.status("x86_64", PKG))
        self.assertIn("2 个目标系统", note)
        self.assertNotIn("未经真实机器验证", note)

    def test_pending_list_drives_force_prompt(self):
        self._container("x86_64", self._rows(4))
        c = Checklist(self.ws)
        c.mark(PKG, "os-0", passed=True)
        st = c.status("x86_64", PKG)
        self.assertEqual(sorted(st["pending"]), ["os-1", "os-2", "os-3"])
        self.assertFalse(st["releasable"])
