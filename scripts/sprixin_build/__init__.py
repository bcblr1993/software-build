"""sprixin-build —— 跨操作系统离线安装包构建引擎。

命令行（scripts/build.py）与 Web 控制台（web/）共用本包，
确保两条入口执行的是同一套构建逻辑。
"""

from .config import Component, Config, ConfigError, VendoredLib
from .builder import Builder, BuildError, RecipeRun
from .fetch import Fetcher, FetchError, FetchResult, sha256_file
from .gate import GateResult, run_gate

__version__ = "0.1.0"

__all__ = [
    "Component",
    "Config",
    "ConfigError",
    "VendoredLib",
    "Builder",
    "BuildError",
    "RecipeRun",
    "Fetcher",
    "FetchError",
    "FetchResult",
    "sha256_file",
    "GateResult",
    "run_gate",
    "__version__",
]
