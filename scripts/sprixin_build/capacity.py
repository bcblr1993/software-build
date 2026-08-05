"""构建机容量探测与并发度建议。

并发度不应硬编码：构建机可能是 160 核的服务器，也可能是开发者的笔记本。
但也不宜简单地取核数 —— 两类任务的真正约束并不是 CPU：

    容器构建   瓶颈在 rpm 解包的磁盘 IO 与依赖闭包的多轮 readelf 扫描，
               并发过高只会让磁盘排队，反而更慢
    端到端验证 每个实例要跑起 nacos(512M 堆)、rabbitmq(Erlang)、redis、
               influxdb、nginx 五个服务，约需 2GB 内存与 2GB 磁盘，
               内存才是硬约束

因此按各自的约束分别给出建议，并留出余量给系统本身与已有负载。

用法（供 shell 脚本取值）：
    python3 -m sprixin_build.capacity --for verify
    python3 -m sprixin_build.capacity --report
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

# 单个端到端验证实例的资源占用（实测值，留有余量）
VERIFY_MEM_GB = 2.0
VERIFY_DISK_GB = 2.5

# 单个容器构建的磁盘占用（解包 + 镜像导入）
BUILD_DISK_GB = 3.0

# 上限：再高也不会更快，只会让排错变难
MAX_BUILD = 6
MAX_VERIFY = 8


@dataclass
class Capacity:
    cores: int
    mem_total_gb: float
    mem_available_gb: float
    disk_free_gb: float
    load1: float

    @property
    def load_ratio(self) -> float:
        return self.load1 / self.cores if self.cores else 1.0

    def suggest(self, task: str) -> int:
        """给出建议并发度。task 取 build 或 verify。"""
        if task == "build":
            by_cpu = self.cores // 16
            by_disk = int(self.disk_free_gb // BUILD_DISK_GB)
            limit = MAX_BUILD
        else:
            by_cpu = self.cores // 8
            by_disk = int(self.disk_free_gb // VERIFY_DISK_GB)
            limit = MAX_VERIFY

        by_mem = int(self.mem_available_gb // VERIFY_MEM_GB) if task == "verify" else limit

        n = min(by_cpu or 1, by_mem or 1, by_disk or 1, limit)

        # 机器已经很忙时收敛，避免把现有业务拖垮
        if self.load_ratio > 0.8:
            n = max(1, n // 2)

        return max(1, n)

    def explain(self, task: str) -> str:
        n = self.suggest(task)
        parts = [
            f"{self.cores} 核",
            f"内存 {self.mem_available_gb:.0f}/{self.mem_total_gb:.0f} GB 可用",
            f"磁盘 {self.disk_free_gb:.0f} GB 可用",
            f"负载 {self.load1:.1f}",
        ]
        reason = "内存" if task == "verify" and self.mem_available_gb // VERIFY_MEM_GB <= self.cores // 8 else "CPU"
        if self.load_ratio > 0.8:
            reason += "（当前负载偏高，已减半）"
        return f"{'、'.join(parts)} → 建议并发 {n}（受限于{reason}）"


def detect(path: str = "/") -> Capacity:
    cores = os.cpu_count() or 1

    mem_total = mem_avail = 0.0
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key == "MemTotal":
                    mem_total = int(rest.split()[0]) / 1024 / 1024
                elif key == "MemAvailable":
                    mem_avail = int(rest.split()[0]) / 1024 / 1024
    except OSError:
        # 非 Linux（如开发机上的 macOS）：退化为按核数估算
        mem_total = mem_avail = cores * 2.0

    if mem_avail <= 0:
        mem_avail = mem_total

    try:
        disk_free = shutil.disk_usage(path).free / 1024**3
    except OSError:
        disk_free = 0.0

    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        load1 = 0.0

    return Capacity(
        cores=cores,
        mem_total_gb=mem_total,
        mem_available_gb=mem_avail,
        disk_free_gb=disk_free,
        load1=load1,
    )


def resolve(value: str | int | None, task: str, path: str = "/") -> int:
    """把用户给的并发度设置解析成具体数字。

    接受 auto / serial / 具体数字；空值按 auto 处理。
    """
    if value is None or value == "":
        value = "auto"
    text = str(value).strip().lower()

    if text in ("serial", "1", "串行"):
        return 1
    if text in ("auto", "自动"):
        return detect(path).suggest(task)
    try:
        n = int(text)
    except ValueError:
        return detect(path).suggest(task)
    return max(1, n)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    task = "verify"
    path = "/"
    report = False

    i = 0
    while i < len(args):
        if args[i] == "--for" and i + 1 < len(args):
            task = args[i + 1]
            i += 2
        elif args[i] == "--path" and i + 1 < len(args):
            path = args[i + 1]
            i += 2
        elif args[i] == "--report":
            report = True
            i += 1
        else:
            i += 1

    cap = detect(path)
    if report:
        print(f"构建机容量：{cap.cores} 核 / 内存 {cap.mem_available_gb:.0f} GB 可用"
              f" / 磁盘 {cap.disk_free_gb:.0f} GB 可用 / 负载 {cap.load1:.1f}")
        for t in ("build", "verify"):
            label = {"build": "容器构建", "verify": "端到端验证"}[t]
            print(f"  {label}: {cap.explain(t)}")
        return 0

    print(cap.suggest(task))
    return 0


if __name__ == "__main__":
    sys.exit(main())
