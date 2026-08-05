"""构建记录与版本沿革。

每一次构建都完整落库：谁在什么时候、把哪些组件从什么版本升到了什么版本、
产物的校验和是多少、ABI 门禁与目标系统验证是否通过。

这替代了此前手写 UPGRADE-REPORT.md 的做法 —— 那些报告里的
"相对 v13.1 对全部非 Nginx 程序文件做了哈希比较，结果一致"之类的结论，
本就应当由机器给出。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS builds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    package_version TEXT NOT NULL,
    arch          TEXT NOT NULL,
    status        TEXT NOT NULL,          -- running / success / failed
    operator      TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    duration_s    INTEGER,
    gate_passed   INTEGER,
    gate_summary  TEXT,
    log_path      TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS build_components (
    build_id      INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    component     TEXT NOT NULL,
    version       TEXT NOT NULL,
    build_kind    TEXT,
    sha256        TEXT,
    previous_sha  TEXT,
    changed       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (build_id, component)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id      INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    path          TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    size          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id      INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    target_os     TEXT NOT NULL,          -- 如 linxos-6.0.99-x86_64
    glibc         TEXT,
    passed        INTEGER NOT NULL,
    detail        TEXT,
    checked_at    TEXT NOT NULL
);

-- 正式版本。
--
-- 构建只产出候选，须经实机测试确认后才提升为正式版本。二者分开存放：
-- 候选可随时清理，正式版本一经发布即不可删除 —— 现场部署与回滚都依赖
-- 它，误删的代价远高于占用的磁盘。
CREATE TABLE IF NOT EXISTS releases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version       TEXT NOT NULL,
    arch          TEXT NOT NULL,
    filename      TEXT NOT NULL,
    path          TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    size          INTEGER NOT NULL,
    build_id      INTEGER REFERENCES builds(id),
    released_by   TEXT,
    released_at   TEXT NOT NULL,
    test_note     TEXT,
    UNIQUE (version, arch)
);

CREATE INDEX IF NOT EXISTS idx_builds_started ON builds(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_releases_at ON releases(released_at DESC);
"""


@dataclass
class ComponentRecord:
    component: str
    version: str
    build_kind: str = ""
    sha256: str = ""
    previous_sha: str = ""
    changed: bool = False


@dataclass
class BuildRecord:
    id: int
    package_version: str
    arch: str
    status: str
    started_at: str
    finished_at: str | None = None
    duration_s: int | None = None
    operator: str | None = None
    gate_passed: bool | None = None
    gate_summary: str | None = None
    components: list[ComponentRecord] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    verifications: list[dict[str, Any]] = field(default_factory=list)

    def diff_against(self, previous: "BuildRecord | None") -> list[str]:
        """相对上一次构建的组件版本变化，用于版本沿革展示。"""
        if previous is None:
            return [f"{c.component} {c.version}" for c in self.components]
        old = {c.component: c.version for c in previous.components}
        changes = []
        for c in self.components:
            before = old.get(c.component)
            if before is None:
                changes.append(f"{c.component} 新增 {c.version}")
            elif before != c.version:
                changes.append(f"{c.component} {before} → {c.version}")
        for name, version in old.items():
            if name not in {c.component for c in self.components}:
                changes.append(f"{name} 移除（原 {version}）")
        return changes


class BuildStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── 写入 ────────────────────────────────────────────────────────

    def start(
        self,
        *,
        package_version: str,
        arch: str,
        operator: str | None = None,
        log_path: str | None = None,
    ) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO builds (package_version, arch, status, operator,"
                " started_at, log_path) VALUES (?, ?, 'running', ?, ?, ?)",
                (package_version, arch, operator, now, log_path),
            )
            return int(cur.lastrowid)

    def record_component(self, build_id: int, rec: ComponentRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO build_components"
                " (build_id, component, version, build_kind, sha256, previous_sha, changed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    build_id,
                    rec.component,
                    rec.version,
                    rec.build_kind,
                    rec.sha256,
                    rec.previous_sha,
                    int(rec.changed),
                ),
            )

    def record_artifact(
        self, build_id: int, *, filename: str, path: str, sha256: str, size: int
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO artifacts (build_id, filename, path, sha256, size)"
                " VALUES (?, ?, ?, ?, ?)",
                (build_id, filename, path, sha256, size),
            )

    def record_verification(
        self,
        build_id: int,
        *,
        target_os: str,
        passed: bool,
        glibc: str = "",
        detail: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO verifications (build_id, target_os, glibc, passed, detail, checked_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    build_id,
                    target_os,
                    glibc,
                    int(passed),
                    detail,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    def finish(
        self,
        build_id: int,
        *,
        status: str,
        gate_passed: bool | None = None,
        gate_summary: str = "",
        note: str = "",
    ) -> None:
        now = datetime.now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT started_at FROM builds WHERE id = ?", (build_id,)
            ).fetchone()
            duration = None
            if row:
                try:
                    started = datetime.fromisoformat(row["started_at"])
                    duration = int((now - started).total_seconds())
                except ValueError:
                    duration = None
            conn.execute(
                "UPDATE builds SET status = ?, finished_at = ?, duration_s = ?,"
                " gate_passed = ?, gate_summary = ?, note = ? WHERE id = ?",
                (
                    status,
                    now.isoformat(timespec="seconds"),
                    duration,
                    None if gate_passed is None else int(gate_passed),
                    gate_summary,
                    note,
                    build_id,
                ),
            )

    # ── 读取 ────────────────────────────────────────────────────────

    def get(self, build_id: int) -> BuildRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
            if row is None:
                return None
            return self._hydrate(conn, row)

    def history(self, *, arch: str | None = None, limit: int = 50) -> list[BuildRecord]:
        sql = "SELECT * FROM builds"
        params: list[Any] = []
        if arch:
            sql += " WHERE arch = ?"
            params.append(arch)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._hydrate(conn, r) for r in rows]

    def previous_success(self, build_id: int, arch: str) -> BuildRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM builds WHERE id < ? AND arch = ? AND status = 'success'"
                " ORDER BY id DESC LIMIT 1",
                (build_id, arch),
            ).fetchone()
            return self._hydrate(conn, row) if row else None

    def _hydrate(self, conn: sqlite3.Connection, row: sqlite3.Row) -> BuildRecord:
        bid = row["id"]
        comps = [
            ComponentRecord(
                component=r["component"],
                version=r["version"],
                build_kind=r["build_kind"] or "",
                sha256=r["sha256"] or "",
                previous_sha=r["previous_sha"] or "",
                changed=bool(r["changed"]),
            )
            for r in conn.execute(
                "SELECT * FROM build_components WHERE build_id = ? ORDER BY component", (bid,)
            )
        ]
        arts = [
            dict(r)
            for r in conn.execute("SELECT * FROM artifacts WHERE build_id = ?", (bid,))
        ]
        vers = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM verifications WHERE build_id = ? ORDER BY target_os", (bid,)
            )
        ]
        return BuildRecord(
            id=bid,
            package_version=row["package_version"],
            arch=row["arch"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            duration_s=row["duration_s"],
            operator=row["operator"],
            gate_passed=None if row["gate_passed"] is None else bool(row["gate_passed"]),
            gate_summary=row["gate_summary"],
            components=comps,
            artifacts=arts,
            verifications=vers,
        )

    # ── 正式版本 ────────────────────────────────────────────────────

    def add_release(
        self,
        *,
        version: str,
        arch: str,
        filename: str,
        path: str,
        sha256: str,
        size: int,
        build_id: int | None = None,
        released_by: str = "",
        test_note: str = "",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO releases (version, arch, filename, path, sha256, size,"
                " build_id, released_by, released_at, test_note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version, arch, filename, path, sha256, size, build_id,
                    released_by, datetime.now().isoformat(timespec="seconds"), test_note,
                ),
            )
            return int(cur.lastrowid)

    def releases(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM releases ORDER BY id DESC LIMIT ?", (limit,)
                )
            ]

    def is_released(self, version: str, arch: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM releases WHERE version = ? AND arch = ?", (version, arch)
            ).fetchone()
            return row is not None

    def released_paths(self) -> set[str]:
        """全部正式版本的文件路径，用于删除前的保护性检查。"""
        with self._conn() as conn:
            return {r["path"] for r in conn.execute("SELECT path FROM releases")}

    def to_json(self, record: BuildRecord) -> str:
        return json.dumps(
            {
                "id": record.id,
                "package_version": record.package_version,
                "arch": record.arch,
                "status": record.status,
                "started_at": record.started_at,
                "duration_s": record.duration_s,
                "gate_passed": record.gate_passed,
                "components": [vars(c) for c in record.components],
                "artifacts": record.artifacts,
                "verifications": record.verifications,
            },
            ensure_ascii=False,
            indent=2,
        )
