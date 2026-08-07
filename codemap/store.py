"""SQLite 存储层。

存储大而全：所有 AST 提取事实都落库。
查询层按需投影，通过 query.py 对外暴露。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from codemap.models import (
    Node,
    Edge,
    Transform,
    Appearance,
    NodeKind,
    EdgeKind,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    location TEXT NOT NULL,
    end_location TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    type_annotation TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    is_param INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    edge_type TEXT NOT NULL,
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    location TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS transforms (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    op TEXT NOT NULL DEFAULT '',
    op_node TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    inputs TEXT NOT NULL DEFAULT '[]',
    outputs TEXT NOT NULL DEFAULT '[]',
    branch TEXT,
    code TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS file_hashes (
    path TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_scope ON nodes(scope);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_node);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_node);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
CREATE INDEX IF NOT EXISTS idx_edges_location ON edges(location);
CREATE INDEX IF NOT EXISTS idx_edges_code ON edges(code);
"""


class Store:
    """SQLite 持久化层。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    # ── connection management ────────────────────────────

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(SCHEMA_SQL)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── bulk write ───────────────────────────────────────

    def insert_nodes(self, nodes: list[Node]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO nodes (id, kind, name, location, end_location, scope, type_annotation, source_hash, is_param) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    n.id,
                    n.kind,
                    n.name,
                    n.location,
                    n.end_location,
                    n.scope,
                    n.type_annotation,
                    n.source_hash,
                    n.is_param,
                )
                for n in nodes
            ],
        )

    def insert_edges(self, edges: list[Edge]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO edges (id, edge_type, from_node, to_node, location, code, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    e.id,
                    e.edge_type,
                    e.from_node,
                    e.to_node,
                    e.location,
                    e.code,
                    json.dumps(e.metadata, ensure_ascii=False),
                )
                for e in edges
            ],
        )

    def insert_transforms(self, transforms: list[Transform]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO transforms (id, kind, op, op_node, location, inputs, outputs, branch, code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    t.id,
                    t.kind,
                    t.op,
                    t.op_node,
                    t.location,
                    json.dumps(t.inputs, ensure_ascii=False),
                    json.dumps(t.outputs, ensure_ascii=False),
                    t.branch,
                    t.code,
                )
                for t in transforms
            ],
        )

    def insert_file_hash(self, path: str, source_hash: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO file_hashes (path, source_hash) VALUES (?, ?)",
            (path, source_hash),
        )

    def clear_file(self, file_path: str) -> None:
        """删除某个文件的所有节点和关联边、transform。

        使用精确前缀匹配而非 LIKE 通配符，避免文件名含 % 或 _ 时误删。
        """
        c = self.conn
        # 文件节点的前缀: file_path#
        prefix = f"{file_path}#"

        # 找到该文件的所有节点 ID（精确匹配 id 以 prefix 开头）
        node_ids = [
            row[0]
            for row in c.execute(
                "SELECT id FROM nodes WHERE id = ? OR id >= ? AND id < ?",
                (prefix, prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)),
            ).fetchall()
        ]

        for nid in node_ids:
            c.execute("DELETE FROM edges WHERE from_node = ? OR to_node = ?", (nid, nid))
            c.execute("DELETE FROM transforms WHERE op_node = ?", (nid,))
        # 删除节点（同样使用精确前缀匹配）
        c.execute(
            "DELETE FROM nodes WHERE id = ? OR (id >= ? AND id < ?)",
            (prefix, prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)),
        )
        c.execute("DELETE FROM file_hashes WHERE path = ?", (file_path,))

    def commit(self) -> None:
        self.conn.commit()

    # ── single reads ─────────────────────────────────────

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return dict(row) if row else None

    def get_node_by_name_scope(
        self, name: str, scope: str | None = None
    ) -> list[dict[str, Any]]:
        if scope:
            rows = self.conn.execute(
                "SELECT * FROM nodes WHERE name = ? AND scope = ?", (name, scope)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM nodes WHERE name = ?", (name,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_node_id_by_name_scope(self, name: str, scope: str) -> str | None:
        row = self.conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND scope = ?", (name, scope)
        ).fetchone()
        return row["id"] if row else None

    def get_nodes_by_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """模糊匹配：name 包含 prefix。"""
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE name LIKE ?", (f"%{prefix}%",)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_file_nodes(self, file_path: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE id LIKE ?", (f"{file_path}#%",)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── edge queries ─────────────────────────────────────

    def get_edges_from(self, node_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM edges WHERE from_node = ?", (node_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_edges_to(self, node_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM edges WHERE to_node = ?", (node_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_edges_by_location(self, file_path: str, line: int) -> list[dict[str, Any]]:
        prefix = f"{file_path}:{line}:"
        rows = self.conn.execute(
            "SELECT * FROM edges WHERE location LIKE ?", (f"{prefix}%",)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_edges_by_type(self, edge_type: EdgeKind) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM edges WHERE edge_type = ?", (edge_type,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_nodes_at_location(self, file_path: str, line: int) -> list[dict[str, Any]]:
        prefix = f"{file_path}:{line}:"
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE location LIKE ?", (f"{prefix}%",)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── compound queries ─────────────────────────────────

    def get_imported_by(self, file_path: str) -> list[dict[str, Any]]:
        """查找哪些文件 import 了给定文件的符号。

        策略：
        1. 查 to_node LIKE 'file_path#%' 的 import 边（同文件内符号被 import）
        2. 查 to_node 为 #external: 且名字匹配该文件模块路径的 import 边
        """
        result: dict[str, list[str]] = {}

        # 策略 1：直接匹配文件路径
        rows = self.conn.execute(
            """
            SELECT DISTINCT e.from_node, e.code, e.location, e.to_node
            FROM edges e
            WHERE e.edge_type = 'imports'
              AND e.to_node LIKE ?
            """,
            (f"{file_path}#%",),
        ).fetchall()
        for r in rows:
            from_file = r["from_node"].split("#")[0]
            result.setdefault(from_file, []).append(r["code"])

        # 策略 2：匹配 external 节点名
        # file_path 如 "auth/token.py" → 模块路径 "auth.token"
        module_path = file_path.replace("/", ".").replace("\\", ".")
        if module_path.endswith(".py"):
            module_path = module_path[:-3]
        # 也尝试不带目录的文件名
        file_stem = file_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].replace(".py", "")

        ext_rows = self.conn.execute(
            """
            SELECT DISTINCT e.from_node, e.code, e.to_node
            FROM edges e
            WHERE e.edge_type = 'imports'
              AND e.to_node LIKE ?
            """,
            (f"#external:{module_path}%",),
        ).fetchall()
        for r in ext_rows:
            from_file = r["from_node"].split("#")[0]
            result.setdefault(from_file, []).append(r["code"])

        # 也匹配不带目录的
        ext_rows2 = self.conn.execute(
            """
            SELECT DISTINCT e.from_node, e.code, e.to_node
            FROM edges e
            WHERE e.edge_type = 'imports'
              AND e.to_node LIKE ?
            """,
            (f"#external:%{file_stem}%",),
        ).fetchall()
        for r in ext_rows2:
            from_file = r["from_node"].split("#")[0]
            if from_file != file_path:  # 排除自身
                result.setdefault(from_file, []).append(r["code"])

        return [
            {"file": f, "symbols": list(set(syms))} for f, syms in result.items()
        ]

    def get_symbol_appearances(self, node_id: str) -> list[Appearance]:
        """获取符号的外部出现点。

        对于 trace 查询，"出现"意味着其他代码引用/调用了这个符号。
        不包括该符号内部的操作（那是符号自己的行为，不是外部出现）。

        对于 Variable：其他函数/语句 reads/writes/calls 它 = 出现。
        对于 Function：其他函数 calls 它 = 出现。
        同时总是包含节点自己的定义位置（def/assign 处）。
        """
        appearances: dict[tuple[str, str], Appearance] = {}

        node = self.get_node(node_id)
        if node is None:
            return []

        # 1. 定义位置本身（总是出现）
        def_at = node["location"]
        def_code = ""  # 定义位置的代码从 edges 中找
        for row in self.conn.execute(
            "SELECT code FROM edges WHERE edge_type = 'defines' AND to_node = ?",
            (node_id,),
        ).fetchall():
            def_code = row["code"]
            break
        if not def_code:
            # 函数/类的定义直接拿 node 信息
            if node["kind"] in ("Function", "Class"):
                def_code = f"def {node['name']}" if node["kind"] == "Function" else f"class {node['name']}"
            else:
                def_code = node["name"]

        def_scope = node.get("scope", "")
        appearances[(def_code, def_at)] = Appearance(code=def_code, at=def_at, scope=def_scope)

        # 2. 外部引用：指向该节点的边
        if node["kind"] == "Function":
            # 函数：被谁调用 (calls edge to_node = this)
            for row in self.conn.execute(
                "SELECT DISTINCT code, location, from_node FROM edges WHERE edge_type = 'calls' AND to_node = ?",
                (node_id,),
            ).fetchall():
                key = (row["code"], row["location"])
                if key not in appearances:
                    scope = self._scope_from_from_node(row["from_node"])
                    appearances[key] = Appearance(code=row["code"], at=row["location"], scope=scope)

            # 函数：被谁作为 import 引入
            for row in self.conn.execute(
                "SELECT DISTINCT code, location, from_node FROM edges WHERE edge_type = 'imports' AND to_node = ?",
                (node_id,),
            ).fetchall():
                key = (row["code"], row["location"])
                if key not in appearances:
                    scope = self._scope_from_from_node(row["from_node"])
                    appearances[key] = Appearance(code=row["code"], at=row["location"], scope=scope)

        elif node["kind"] == "Variable":
            # 变量：被谁 reads / writes / calls（作为实参）
            for row in self.conn.execute(
                "SELECT DISTINCT code, location, from_node FROM edges WHERE to_node = ? AND edge_type IN ('reads', 'writes', 'assigns', 'param-flow')",
                (node_id,),
            ).fetchall():
                key = (row["code"], row["location"])
                if key not in appearances:
                    scope = self._scope_from_from_node(row["from_node"])
                    appearances[key] = Appearance(code=row["code"], at=row["location"], scope=scope)

        elif node["kind"] == "Class":
            # 类：被谁调用/实例化/继承
            for row in self.conn.execute(
                "SELECT DISTINCT code, location, from_node FROM edges WHERE to_node = ?",
                (node_id,),
            ).fetchall():
                key = (row["code"], row["location"])
                if key not in appearances:
                    scope = self._scope_from_from_node(row["from_node"])
                    appearances[key] = Appearance(code=row["code"], at=row["location"], scope=scope)

        return list(appearances.values())

    def _scope_from_from_node(self, from_node: str) -> str:
        """从 edge 的 from_node 推导 scope。

        from_node 格式:
          "file.py#"           → scope = "file.py"（模块级）
          "file.py#func"       → scope = "file.py:func"
          "file.py#Class.method" → scope = "file.py:Class.method"
        """
        if "#" not in from_node:
            return from_node
        file_part, sym_part = from_node.split("#", 1)
        if not sym_part:
            return file_part
        return f"{file_part}:{sym_part}"

    def get_symbol_reverse_appearances(self, node_id: str) -> list[Appearance]:
        """获取符号的来源（指向该节点的边，以及从这些边的 from_node 的上游）。"""
        result: dict[tuple[str, str], Appearance] = {}
        for r in self.conn.execute(
            "SELECT code, location, from_node FROM edges WHERE to_node = ?", (node_id,)
        ).fetchall():
            key = (r["code"], r["location"])
            if key not in result:
                scope = self._scope_from_from_node(r["from_node"])
                result[key] = Appearance(code=r["code"], at=r["location"], scope=scope)
        return list(result.values())

    def get_dead_nodes(self) -> list[dict[str, Any]]:
        """查找零入边的函数和类（死代码）。

        只查 Function 和 Class 的模块级符号。
        defines 入边不算"被使用"（那是声明，不是引用）。

        增加判定原因和置信度：
          - reason: 为何判定为死代码
          - confidence: high / medium / low
          - is_test: 是否在测试文件中（test_* / *_test.go 等）
        """
        rows = self.conn.execute(
            """
            SELECT n.id, n.kind, n.name, n.location, n.scope
            FROM nodes n
            WHERE n.kind IN ('Function', 'Class')
              AND n.scope NOT LIKE '%:%'
              AND n.id NOT IN (SELECT DISTINCT to_node FROM edges WHERE edge_type = 'calls')
              AND n.id NOT IN (SELECT DISTINCT to_node FROM edges WHERE edge_type = 'reads')
              AND n.id NOT IN (SELECT DISTINCT to_node FROM edges WHERE edge_type = 'imports')
              AND n.id NOT IN (SELECT DISTINCT to_node FROM edges WHERE edge_type = 'decorates')
              AND n.id NOT IN (SELECT DISTINCT to_node FROM edges WHERE edge_type = 'param-flow')
              -- 接口感知：如果 Class 定义了方法（通过 defines 边），不判死
              AND NOT (
                  n.kind = 'Class'
                  AND n.id IN (SELECT DISTINCT from_node FROM edges WHERE edge_type = 'defines')
              )
              -- 入口点白名单：以下符号即使零入边也不判死
              AND n.name NOT IN ('main', 'serve', 'run', 'init', 'start', 'handler')
              -- 有装饰器的函数不判死（Python @app.route / @click.command 等）
              AND n.id NOT IN (
                  SELECT from_node FROM edges WHERE edge_type = 'decorates'
              )
            """
        ).fetchall()

        result: list[dict[str, Any]] = []
        for r in rows:
            name = r["name"]
            location = r["location"]
            file_path = location.split(":")[0] if ":" in location else ""

            # 测试文件检测
            is_test = (
                file_path.startswith("test") or
                "/test" in file_path or
                "\\test" in file_path or
                file_path.endswith("_test.go") or
                ".test." in file_path or
                ".spec." in file_path
            )

            # 测试函数检测 (test_xxx, xxx_test, TestXxx)
            is_test_name = (
                name.startswith("test_") or
                name.startswith("Test") or
                name.startswith("test") or
                name.endswith("_test") or
                name.endswith("Test")
            )

            # 置信度
            if is_test or is_test_name:
                confidence = "low"
                reason = "可能是测试代码（框架动态发现）"
            elif name.startswith("_") or name.startswith("__"):
                confidence = "medium"
                reason = "零入边（私有/内部符号，可能被动态调用）"
            elif file_path.endswith(".go") and name[0].isupper():
                # Go: 首字母大写 = 导出符号（公开 API），可能被外部调用
                confidence = "medium"
                reason = "零入边（Go 导出符号，可能是公开 API 被外部调用）"
            elif file_path.endswith(".py") and self._module_is_imported(file_path):
                # Python: 模块被其他文件 import → 模块内顶层符号是命名空间公开 API
                confidence = "medium"
                reason = "零入边（所在模块被外部 import，可能是命名空间公开 API）"
            else:
                confidence = "high"
                reason = "零入边（无任何调用/引用/导入）"

            result.append({
                "id": r["id"],
                "kind": r["kind"],
                "name": name,
                "location": location,
                "scope": r["scope"],
                "reason": reason,
                "confidence": confidence,
                "is_test": is_test or is_test_name,
            })

        return result

    def _module_is_imported(self, file_path: str) -> bool:
        """检查 Python 模块是否被其他文件 import。

        如果模块被 import，其顶层符号可以通过模块命名空间（module.xxx）被外部访问，
        属于"命名空间公开 API"，不应判定为 high 置信度死代码。
        """
        # 该文件自身的 File 节点 id（如 utils.py#）
        file_node_id = f"{file_path}#"
        # 查找指向该文件的 imports 边（from_node 是其他文件）
        rows = self.conn.execute(
            """
            SELECT 1 FROM edges
            WHERE edge_type = 'imports'
              AND to_node LIKE ?
              AND from_node != ?
            LIMIT 1
            """,
            (f"%{file_path}", file_node_id),
        ).fetchall()
        if rows:
            return True
        # 也检查 from_node 是其他文件的模块级 import（to_node 是 #external:module.symbol）
        # 如 from .utils import foo → import 边 to_node 是 #external:utils.foo
        base_name = file_path.rsplit("/", 1)[-1].replace(".py", "")
        rows = self.conn.execute(
            """
            SELECT 1 FROM edges
            WHERE edge_type = 'imports'
              AND to_node LIKE ?
              AND from_node != ?
            LIMIT 1
            """,
            (f"#external:{base_name}.%", file_node_id),
        ).fetchall()
        return bool(rows)

    def get_text_search(self, pattern: str) -> list[Appearance]:
        """全文搜索：同时搜 edges 的 code 字段和 nodes 的 name 字段。"""
        results: dict[tuple[str, str], Appearance] = {}
        # 搜 edges 的 code
        for r in self.conn.execute(
            "SELECT DISTINCT code, location FROM edges WHERE code LIKE ?",
            (f"%{pattern}%",),
        ).fetchall():
            key = (r["code"], r["location"])
            if key not in results:
                results[key] = Appearance(code=r["code"], at=r["location"], scope="")
        # 搜 nodes 的 name
        for r in self.conn.execute(
            "SELECT name, location FROM nodes WHERE name LIKE ?",
            (f"%{pattern}%",),
        ).fetchall():
            key = (r["name"], r["location"])
            if key not in results:
                results[key] = Appearance(code=r["name"], at=r["location"], scope="")
        return list(results.values())

    def get_file_hashes(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT path, source_hash FROM file_hashes").fetchall()
        return {r["path"]: r["source_hash"] for r in rows}

    # ── stats ────────────────────────────────────────────

    def stats(self) -> dict[str, int]:
        c = self.conn
        return {
            "nodes": c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            "edges": c.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            "transforms": c.execute("SELECT COUNT(*) FROM transforms").fetchone()[0],
            "files": c.execute("SELECT COUNT(*) FROM file_hashes").fetchone()[0],
        }