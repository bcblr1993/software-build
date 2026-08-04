"""基线容器的编排。

所有编译都在基线容器内进行，宿主只负责调度。这样做的直接结果是：
构建不再需要与目标操作系统相同的机器，一台 x86 服务器即可产出两种
架构的产物 —— aarch64 通过 binfmt_misc + qemu-user-static 执行。

容器挂载约定：
    /cache    上游源码归档（只读）
    /recipes  编译配方（只读）
    /base     基准包中该组件已解压的目录（只读，可选）
    /out      产物输出
"""

from __future__ import annotations

import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

LogSink = Callable[[str], None]


class BuildError(Exception):
    pass


@dataclass
class RecipeRun:
    component: str
    arch: str
    returncode: int
    log_path: Path

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Builder:
    def __init__(
        self,
        *,
        repo_root: Path,
        cache_dir: Path,
        out_dir: Path,
        log_dir: Path,
        log: LogSink = print,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.cache = Path(cache_dir)
        self.out = Path(out_dir)
        self.log_dir = Path(log_dir)
        self.log = log
        for d in (self.cache, self.out, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ── 基线镜像 ────────────────────────────────────────────────────

    def ensure_baseline(self, arch: str, image: str, vault_url: str) -> None:
        """确保基线镜像存在，不存在则从 rootfs 归档构建。"""
        if self._image_exists(image):
            self.log(f"基线镜像已存在: {image}")
            return

        raw = f"{image}-raw"
        if not self._image_exists(raw):
            archive = self.cache / f"centos-7-{arch}-docker.tar.xz"
            if not archive.exists():
                raise BuildError(
                    f"缺少 {arch} 的基础 rootfs: {archive}\n"
                    f"该归档来自 CentOS 官方 sig-cloud-instance-images 仓库"
                )
            self.log(f"导入基础 rootfs: {archive.name}")
            self._run(["docker", "import", str(archive), raw], check=True)

        self.log(f"构建基线镜像: {image}")
        cmd = [
            "docker", "build",
            "-t", image,
            "--build-arg", f"ARCH={arch}",
            "--build-arg", f"VAULT_URL={vault_url}",
            "-f", str(self.repo_root / "baseline" / "Dockerfile"),
            str(self.repo_root),
        ]
        proc = self._run(cmd)
        if proc.returncode != 0:
            raise BuildError(f"基线镜像构建失败: {image}")

    def _image_exists(self, image: str) -> bool:
        return (
            subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
            ).returncode
            == 0
        )

    # ── 配方执行 ────────────────────────────────────────────────────

    def run_recipe(
        self,
        *,
        component: str,
        arch: str,
        image: str,
        recipe: str,
        env: dict[str, str],
        base_package: Path | None = None,
        sysroot_volume: str | None = None,
    ) -> RecipeRun:
        """在基线容器内执行一个配方。

        sysroot_volume 用于在同一架构的多个配方之间共享 /opt/sysroot：
        随包依赖库只编译一次，后续组件直接链接。
        """
        recipe_path = self.repo_root / "scripts" / "recipes" / recipe
        if not recipe_path.exists():
            raise BuildError(f"找不到配方: {recipe_path}")

        arch_out = self.out / arch
        arch_out.mkdir(parents=True, exist_ok=True)

        cmd = [
            "docker", "run", "--rm",
            "-v", f"{self.cache}:/cache:ro",
            "-v", f"{self.repo_root / 'scripts' / 'recipes'}:/recipes:ro",
            "-v", f"{arch_out}:/out",
        ]
        if sysroot_volume:
            cmd += ["-v", f"{sysroot_volume}:/opt/sysroot"]
        if base_package is not None:
            cmd += ["-v", f"{base_package}:/base:ro"]
            env = {**env, "BASE_PACKAGE": "/base"}

        for key, value in env.items():
            cmd += ["-e", f"{key}={value}"]

        cmd += [image, "bash", f"/recipes/{recipe}"]

        log_path = self.log_dir / f"{arch}-{component}.log"
        self.log(f"构建 {component} ({arch})")
        rc = self._stream_to_file(cmd, log_path)

        if rc != 0:
            tail = self._tail(log_path, 25)
            self.log(f"  失败，日志末尾：\n{tail}")

        return RecipeRun(component=component, arch=arch, returncode=rc, log_path=log_path)

    # ── sysroot 卷 ──────────────────────────────────────────────────

    def sysroot_volume_name(self, arch: str) -> str:
        return f"sprixin-sysroot-{arch}"

    def reset_sysroot(self, arch: str) -> str:
        """重建该架构的 sysroot 卷。

        每轮构建都从干净的 sysroot 开始，避免上一次残留的旧版本库
        被悄悄链接进产物 —— 这类污染在离线交付里极难排查。
        """
        name = self.sysroot_volume_name(arch)
        subprocess.run(["docker", "volume", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "volume", "create", name], capture_output=True, check=False)
        return name

    # ── 底层 ────────────────────────────────────────────────────────

    def _run(self, cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            self.log((proc.stderr or proc.stdout).strip()[:500])
            if check:
                raise BuildError(f"命令失败: {' '.join(cmd[:3])}…")
        return proc

    def _stream_to_file(self, cmd: list[str], log_path: Path) -> int:
        """执行命令，日志同时写文件并回传给日志接收方。

        逐行回传是为了让 Web 控制台能实时展示构建过程，而不是等到结束
        才吐出一大块输出。
        """
        with log_path.open("w", encoding="utf-8") as fh:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                fh.write(line)
                self.log(line.rstrip("\n"))
            proc.wait()
            return proc.returncode

    @staticmethod
    def _tail(path: Path, n: int) -> str:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "(无日志)"
        return "\n".join("    " + l for l in lines[-n:])


def docker_available() -> bool:
    return shutil.which("docker") is not None
