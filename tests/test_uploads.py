"""上传件的架构归属。

nacos 是纯 Java，两个架构本该用同一份；influxdb 与 jdk 是各自架构的
原生二进制，跨架构共用会打出一个装着 amd64 二进制的 ARM 包 —— 那种
包要到目标机器上启动时才会暴露，代价远高于在这里挡住。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))


def _load_build_module():
    spec = importlib.util.spec_from_file_location(
        "_build_under_test", REPO / "scripts" / "build.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_build_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Comp:
    def __init__(self, build: str, arch_independent: bool = False) -> None:
        self.build = build
        self.arch_independent = arch_independent


class _Cfg:
    def __init__(self, components: dict, architectures: list[str]) -> None:
        self.components = components
        self.architectures = architectures


class _Ws:
    def __init__(self, root: Path) -> None:
        self.root = root


class FindUploads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.build = _load_build_module()
        except ImportError as exc:  # pyyaml 等依赖缺失
            raise unittest.SkipTest(f"无法加载 build.py: {exc}")

    def _mk(self, files: dict[str, list[str]]):
        """files: {架构: [组件名, ...]}"""
        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        for arch, names in files.items():
            d = root / "uploads" / arch
            d.mkdir(parents=True, exist_ok=True)
            for n in names:
                (d / f"{n}.tar.gz").write_bytes(b"x")
        return _Ws(root)

    def _cfg(self):
        return _Cfg(
            components={
                "nacos": _Comp("repack", arch_independent=True),
                "influxdb": _Comp("repack", arch_independent=False),
                "nginx": _Comp("compile"),
            },
            architectures=["x86_64", "aarch64"],
        )

    def test_uses_own_arch_first(self):
        ws = self._mk({"x86_64": ["nacos"], "aarch64": ["nacos"]})
        got = self.build.find_uploads(ws, self._cfg(), "aarch64")
        self.assertEqual(got["nacos"].parent.name, "aarch64")

    def test_arch_independent_falls_back_to_other_arch(self):
        """只传了 x86 的 nacos，ARM 也该用它。

        否则同一个版本的两个包里，一个装着改过的 nacos，一个是上游原版。
        """
        ws = self._mk({"x86_64": ["nacos"]})
        got = self.build.find_uploads(ws, self._cfg(), "aarch64")
        self.assertIn("nacos", got)
        self.assertEqual(got["nacos"].parent.name, "x86_64")

    def test_arch_dependent_never_shared(self):
        """influxdb 是 Go 编译的原生二进制，绝不可跨架构取用。"""
        ws = self._mk({"x86_64": ["influxdb"]})
        got = self.build.find_uploads(ws, self._cfg(), "aarch64")
        self.assertNotIn("influxdb", got)

    def test_only_repack_components_considered(self):
        """需编译的组件必须走基线构建与 ABI 门禁，不接受上传件。"""
        ws = self._mk({"aarch64": ["nginx"]})
        got = self.build.find_uploads(ws, self._cfg(), "aarch64")
        self.assertNotIn("nginx", got)

    def test_missing_upload_dir_is_fine(self):
        ws = self._mk({})
        self.assertEqual(self.build.find_uploads(ws, self._cfg(), "aarch64"), {})


if __name__ == "__main__":
    unittest.main()
