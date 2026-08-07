"""访客视图的数据组装。

访客页面是整套系统里唯一无需登录即可访问的入口，因此这里承担的是
安全边界，而不只是取数：凡对外的字段都在此处过一遍，服务器路径、
目标机凭据、成员名单、构建日志一概不出现。

组装成独立模块而非写在 server 里，是为了让"访客能看到什么"集中在
一处可通读、可审查 —— 散落在各个 handler 里的话，日后谁多返回一个
字段都不容易被发现。
"""

from __future__ import annotations

import json
from pathlib import Path


#: 候选产物的公开名单。只记文件名，不记路径。
PUBLIC_LIST = "public-artifacts.json"


class PublicView:
    def __init__(self, workspace: Path, store, checklist_cls=None) -> None:
        self.workspace = Path(workspace)
        self.store = store
        self._checklist_cls = checklist_cls

    # ── 候选产物的公开名单 ──────────────────────────────────────────

    @property
    def _list_path(self) -> Path:
        return self.workspace / PUBLIC_LIST

    def published_names(self) -> set[str]:
        try:
            data = json.loads(self._list_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        return set(data) if isinstance(data, list) else set()

    def set_published(self, filename: str, public: bool) -> None:
        """公开／取消公开某个候选产物。

        以文件名为键而非版本号：重新构建会产出同名的新包，若按版本号记，
        旧包上的公开标记会顺延到新包 —— 而访客下到的将是一个你没打算
        放出去的东西。文件名一致时内容虽也可能变，但至少改名即失效。
        """
        names = self.published_names()
        if public:
            names.add(filename)
        else:
            names.discard(filename)
        self._list_path.write_text(
            json.dumps(sorted(names), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── 对外数据 ────────────────────────────────────────────────────

    def releases(self) -> list[dict]:
        """已发布的正式版本。全部默认可见。"""
        out = []
        for r in self.store.releases(limit=50):
            path = Path(r.get("path") or "")
            if not path.is_file():
                continue
            try:
                comps = json.loads(r.get("components") or "{}")
            except (TypeError, ValueError):
                comps = {}
            out.append({
                "version": r.get("version", ""),
                "arch": r.get("arch", ""),
                "filename": r.get("filename", ""),
                "size": r.get("size", 0),
                "sha256": r.get("sha256", ""),
                "released_at": (r.get("released_at") or "")[:19].replace("T", " "),
                "components": comps,
                "verification": self._verification_brief(r.get("test_note") or ""),
                # path 刻意不返回：它会暴露构建机的目录结构，
                # 且下载链接一律由服务端签发，访客不需要也不应该拿到它。
            })
        return out

    def artifacts(self) -> list[dict]:
        """已公开的候选产物。"""
        names = self.published_names()
        if not names:
            return []
        out = []
        dist = self.workspace / "dist"
        if not dist.is_dir():
            return []
        for arch_dir in sorted(dist.iterdir()):
            if not arch_dir.is_dir():
                continue
            for f in sorted(arch_dir.glob("*.tar.gz")):
                if f.name not in names:
                    continue
                out.append({
                    "filename": f.name,
                    "arch": arch_dir.name,
                    "size": f.stat().st_size,
                    "built_at": _mtime_text(f),
                })
        return out

    def _verification_brief(self, test_note: str) -> str:
        """把发布说明压成一句话。

        完整说明里有逐台机器的备注（含内网地址），不适合对外，
        故只保留数量层面的结论。
        """
        import re

        done = re.search(r"已在 (\d+) 个目标系统的真实机器上验证通过", test_note)
        skip = re.search(r"以下 (\d+) 个系统未经真实机器验证", test_note)
        parts = []
        if done:
            parts.append(f"{done.group(1)} 个系统真机验证通过")
        if skip:
            parts.append(f"{skip.group(1)} 个系统仅通过容器验证")
        return "，".join(parts)


def _mtime_text(p: Path) -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
