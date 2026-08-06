"""发布前的验证清单。

把「这个包在哪些系统上验过」从一句自由文本变成逐项可勾选的清单：
容器验证的结果自动打底，实机验证由人逐台勾选。发布说明据此生成，
不必再人工誊抄。

两类验证的分量并不相同，故分开记录：

  容器验证  由 compat/e2e-test.sh 在最小 rootfs 容器中自动完成，覆盖
            全部目标系统。它能查出动态链接、符号版本、依赖缺失这类
            装不上或起不来的问题 —— 恰是跨系统适配最常翻车的地方。
            这里用它来确定「需要验哪些系统」，并作为第一道拦截。
  实机验证  在真实机器上装一遍跑一遍，由人逐台确认后勾选。容器毕竟
            共用宿主内核，也没有现场的网络与外围。

发布门槛是两道都过：容器验证不得有失败项，且清单上每一个系统都必须
经人工在真实机器上确认。少勾一个就发不出去 —— 这是刻意的，验证覆盖
面若能被"我觉得差不多了"绕过，清单也就失去了意义。
"""

from __future__ import annotations

import json
from pathlib import Path


class ChecklistError(Exception):
    pass


class Checklist:
    """某个工作区内，各安装包的实机验证记录。

    以「包文件名 → 系统标识 → 记录」组织。绑到包名而非版本号，是因为
    重新构建会产出同版本号的新包，而旧包上的勾选不该顺延过来 —— 那等于
    把没验过的东西当成验过的。
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.path = self.workspace / "verify-results" / "manual-checks.json"

    # ── 持久化 ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    # ── 容器验证（自动打底）────────────────────────────────────────

    def container_results(self, arch: str) -> list[dict]:
        """读取该架构的容器验证结果。"""
        f = self.workspace / "verify-results" / f"{arch}.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    # ── 实机验证（人工勾选）────────────────────────────────────────

    def mark(
        self, package: str, target: str, *, passed: bool,
        operator: str = "", note: str = "",
    ) -> None:
        data = self._load()
        entry = data.setdefault(package, {})
        if passed:
            import time

            entry[target] = {
                "by": operator,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": note.strip(),
            }
        else:
            entry.pop(target, None)
            if not entry:
                data.pop(package, None)
        self._save(data)

    def manual_checks(self, package: str) -> dict:
        return self._load().get(package, {})

    # ── 汇总 ────────────────────────────────────────────────────────

    def coverage_note(self, status: dict) -> str:
        """如实说明本版本在哪些系统上验过、哪些没验。

        强制发布时尤其要紧：跳过了哪几个系统必须白纸黑字写进发布说明，
        否则几个月后没人说得清这个版本到底验到什么程度。
        """
        items = status.get("items") or []
        done = [i for i in items if i["machine_checked"]]
        skip = [i for i in items if not i["machine_checked"]]

        # 用 target 而非 os_name：同一发行版的不同镜像 PRETTY_NAME 可能完全
        # 一致（凝思 6.0.80 的两个镜像即如此），只写 os_name 会在发布说明里
        # 出现两行看不出区别的条目。
        def label(i: dict) -> str:
            osn = i.get("os_name") or ""
            extra = f"（{osn}）" if osn and osn != i["target"] else ""
            glibc = f" glibc {i['glibc']}" if i.get("glibc") else ""
            return f"{i['target']}{extra}{glibc}"

        lines = []
        if done:
            lines.append(f"已在 {len(done)} 个目标系统的真实机器上验证通过：")
            for i in done:
                mark = f" — {i['machine_note']}" if i["machine_note"] else ""
                lines.append(f"  · {label(i)}{mark}")
        if skip:
            lines.append("")
            lines.append(f"以下 {len(skip)} 个系统未经真实机器验证，仅通过容器验证：")
            for i in skip:
                lines.append(f"  · {label(i)}")
            lines.append("")
            lines.append("上述系统请在部署前自行确认。")
        return "\n".join(lines)

    def summarize(self, package: str, status: dict | None = None) -> str:
        """把勾选结果汇成一句测试说明，省去人工誊抄。

        发布说明里需要一句「凭什么发出去」的交代。逐台勾选的信息已经
        在清单里，再让人复述一遍既费事又容易与事实脱节。
        """
        checked = [i for i in (status or {}).get("items", []) if i["machine_checked"]]
        if not checked:
            return ""
        notes = [i["machine_note"] for i in checked if i["machine_note"]]
        text = f"已在 {len(checked)} 个目标系统的真实机器上逐台验证通过"
        if notes:
            text += f"（{'；'.join(notes[:6])}{'…' if len(notes) > 6 else ''}）"
        return text

    def status(self, arch: str, package: str) -> dict:
        """给出该包在该架构下的完整验证状况。

        返回结构直接供界面渲染，也供发布门槛判定。
        """
        container = self.container_results(arch)
        manual = self.manual_checks(package)

        # 容器验证记录的 package 字段若与当前包不一致，说明那批结果属于
        # 另一个包 —— 不能拿来给这个包背书。
        applicable = [c for c in container if c.get("package") in ("", None, package)]

        items = []
        for c in applicable:
            target = c.get("target") or c.get("os_name") or "?"
            m = manual.get(target)
            items.append({
                "target": target,
                "os_name": c.get("os_name") or target,
                "glibc": c.get("glibc", ""),
                "container_passed": bool(c.get("passed")),
                "machine_checked": m is not None,
                "machine_by": (m or {}).get("by", ""),
                "machine_at": (m or {}).get("at", ""),
                "machine_note": (m or {}).get("note", ""),
            })

        # 实机勾了、但容器结果里没有的系统（例如现场特有的机型），一并列出
        known = {i["target"] for i in items}
        for target, m in manual.items():
            if target in known:
                continue
            items.append({
                "target": target,
                "os_name": target,
                "glibc": "",
                "container_passed": None,
                "machine_checked": True,
                "machine_by": m.get("by", ""),
                "machine_at": m.get("at", ""),
                "machine_note": m.get("note", ""),
            })

        items.sort(key=lambda x: x["target"])
        failed = [i["target"] for i in items if i["container_passed"] is False]
        pending = [i["target"] for i in items if not i["machine_checked"]]

        if not items:
            reason = "尚无容器验证结果，请先执行验证"
        elif failed:
            reason = f"以下系统容器验证未通过：{'、'.join(failed)}"
        elif pending:
            head = "、".join(pending[:3])
            more = f" 等 {len(pending)} 个" if len(pending) > 3 else ""
            reason = f"尚未在真实机器上验证：{head}{more}"
        else:
            reason = ""

        return {
            "package": package,
            "arch": arch,
            "items": items,
            "total": len(items),
            "container_passed": sum(1 for i in items if i["container_passed"]),
            "machine_checked": sum(1 for i in items if i["machine_checked"]),
            "failed": failed,
            "pending": pending,
            # 两道门槛都过才放行：容器无失败，且每个系统都经人工实机确认
            "releasable": bool(items) and not failed and not pending,
            "blocked_reason": reason,
        }
