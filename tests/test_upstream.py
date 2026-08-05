"""上游登记与校验的单元测试。

只覆盖不依赖网络的部分 —— 模板填充、登记项校验、指纹归一化。
真正的验签与哈希比对需要连上游，由 scripts/build.py fetch 在构建时
实地完成，另有正反两面的实测记录在案。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

# 直接按文件路径加载，绕开包的 __init__ —— 它会连带导入 config 进而
# 要求 pyyaml。本模块的纯逻辑不需要 yaml，测试也就不该被它挡住。
_spec = importlib.util.spec_from_file_location(
    "_upstream_under_test", REPO / "scripts" / "sprixin_build" / "upstream.py"
)
_up = importlib.util.module_from_spec(_spec)
# 必须先登记再执行：dataclass 装饰器会回查 sys.modules 取所属模块的
# 命名空间，缺了这一步会在类定义阶段抛 AttributeError。
sys.modules["_upstream_under_test"] = _up
_spec.loader.exec_module(_up)

UpstreamError = _up.UpstreamError
UpstreamSpec = _up.UpstreamSpec
_fill = _up._fill
_normalize_fpr = _up._normalize_fpr


class FillTemplate(unittest.TestCase):
    def test_replaces_version(self):
        self.assertEqual(
            _fill("https://x/nginx-{version}.tar.gz", "1.31.3"),
            "https://x/nginx-1.31.3.tar.gz",
        )

    def test_keeps_regex_quantifiers(self):
        """回归：正则量词 {64} 不可被当成占位符。

        曾用 str.format 实现，哈希清单的匹配式里含 [0-9a-f]{64}，
        format 会把 {64} 当占位符而抛 KeyError，redis 那条登记直接不可用。
        """
        pattern = r"^hash\s+redis-{version}\.tar\.gz\s+sha256\s+([0-9a-f]{64})"
        out = _fill(pattern, "8.8.0")
        self.assertIn("redis-8.8.0", out)
        self.assertIn("{64}", out)

    def test_no_placeholder_is_noop(self):
        self.assertEqual(_fill("https://x/fixed.tar.gz", "9"), "https://x/fixed.tar.gz")


class NormalizeFingerprint(unittest.TestCase):
    def test_strips_spaces_and_uppercases(self):
        self.assertEqual(
            _normalize_fpr("4338 7825 ddb1 bb97"), "43387825DDB1BB97"
        )


class SpecValidation(unittest.TestCase):
    def _pgp(self, **over):
        raw = {
            "tarball": "https://x/f-{version}.tar.gz",
            "verify": {
                "method": "pgp",
                "signature": "{tarball}.asc",
                "keyring": "keys/x.asc",
                "fingerprints": ["ABCD"],
                **over,
            },
        }
        return UpstreamSpec.from_dict("x", raw)

    def test_pgp_requires_fingerprints(self):
        """没有白名单的 pgp 登记必须被拒。

        只看 gpg 退出码等于没验：任何人都能生成密钥自签一个归档，
        导入后验签照样通过。
        """
        with self.assertRaises(UpstreamError) as ctx:
            self._pgp(fingerprints=[])
        self.assertIn("fingerprints", str(ctx.exception))

    def test_pgp_requires_keyring(self):
        with self.assertRaises(UpstreamError):
            self._pgp(keyring="")

    def test_unknown_method_rejected(self):
        with self.assertRaises(UpstreamError):
            UpstreamSpec.from_dict(
                "x", {"tarball": "u", "verify": {"method": "trust-me"}}
            )

    def test_missing_tarball_rejected(self):
        with self.assertRaises(UpstreamError):
            UpstreamSpec.from_dict("x", {"verify": {"method": "pgp"}})

    def test_hash_index_requires_index_and_pattern(self):
        with self.assertRaises(UpstreamError):
            UpstreamSpec.from_dict(
                "x",
                {"tarball": "u", "verify": {"method": "hash-index", "index": "i"}},
            )

    def test_cross_source_requires_mirror(self):
        with self.assertRaises(UpstreamError):
            UpstreamSpec.from_dict(
                "x", {"tarball": "u", "verify": {"method": "cross-source"}}
            )


class UrlComposition(unittest.TestCase):
    def test_signature_expands_tarball_reference(self):
        spec = UpstreamSpec.from_dict(
            "nginx",
            {
                "tarball": "https://nginx.org/download/nginx-{version}.tar.gz",
                "verify": {
                    "method": "pgp",
                    "signature": "{tarball}.asc",
                    "keyring": "keys/nginx.asc",
                    "fingerprints": ["AB"],
                },
            },
        )
        self.assertEqual(
            spec.signature_url("1.31.3"),
            "https://nginx.org/download/nginx-1.31.3.tar.gz.asc",
        )
        self.assertEqual(
            spec.tarball_url("1.31.3"),
            "https://nginx.org/download/nginx-1.31.3.tar.gz",
        )


class ManifestIntegration(unittest.TestCase):
    """清单里已登记的条目应当都是合法的。"""

    def test_repo_manifest_loads(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("本机没有 pyyaml")

        from sprixin_build.config import Config

        cfg = Config.load(Path(__file__).resolve().parents[1] / "components.yaml")
        self.assertIn("nginx", cfg.upstreams)
        self.assertIn("redis", cfg.upstreams)
        for name, spec in cfg.upstreams.items():
            self.assertTrue(spec.tarball, f"{name} 缺少 tarball")
            if spec.method == "pgp":
                keyring = Path(__file__).resolve().parents[1] / spec.keyring
                self.assertTrue(keyring.is_file(), f"{name} 的公钥文件缺失 {keyring}")


if __name__ == "__main__":
    unittest.main()
