"""查询层 —— 实现设计文档中的所有查询命令。

  每个查询都返回 dict（可直接 JSON 序列化），输出格式严格遵循设计文档。
  Agent 通过 CLI 调用这些函数，按 --detail 档位投影。

  查询命令：
    - trace: 符号出现在哪（正向/反向/N层展开）
    - info: 符号定义详情
    - at: 位置反查
    - search: 全文文本搜索
    - file: 文件级查询
    - dead: 死代码列表
    - impact: 影响面查询
"""

from __future__ import annotations

from typing import Any

from codemap.models import Appearance


def _appearance_from_row(code: str, at: str, scope: str = "") -> Appearance:
    return Appearance(code=code, at=at, scope=scope)


class Querier:
    """查询入口，封装 Store 提供面向 Agent 的查询接口。"""

    def __init__(self, store: Any) -> None:  # Store
        self.store = store

    # ── trace ────────────────────────────────────────────

    def trace(
        self,
        symbol: str,
        *,
        reverse: bool = False,
        depth: int = 1,
        fuzzy: bool = False,
        scope: str | None = None,
        limit: int = 50,
        kind_filter: str | None = None,
    ) -> dict[str, Any]:
        """追踪符号。

        Args:
            symbol: 符号名，支持 .attr 前缀表示属性访问。
            reverse: True = 反向（从哪来）。
            depth: 展开层数。
            fuzzy: 模糊匹配。
            scope: 限定作用域。
            limit: 最大返回条数（默认 50）。
        """
        is_attr = symbol.startswith(".")
        search_name = symbol[1:] if is_attr else symbol

        # 查找匹配的节点
        if fuzzy:
            node_records = self.store.get_nodes_by_prefix(search_name)
        else:
            node_records = self.store.get_node_by_name_scope(search_name, scope)

        if not node_records:
            return {"symbol": symbol, "appearances": []}

        result: dict[str, Any] = {
            "symbol": symbol,
            "appearances": [],
        }

        all_appearances: set[tuple[str, str, str]] = set()  # (code, at, scope)

        for nd in node_records:
            # 属性查询过滤
            if is_attr and nd["kind"] != "Variable":
                continue

            node_id = nd["id"]

            if reverse:
                apps = self.store.get_symbol_reverse_appearances(node_id)
            else:
                apps = self.store.get_symbol_appearances(node_id)

            for app in apps:
                key = (app.code, app.at, app.scope)
                if key not in all_appearances:
                    all_appearances.add(key)
                    # 推导 role（多语言适配）
                    role = self._derive_role(app.code)

                    # kind_filter 过滤
                    if kind_filter:
                        kinds = [k.strip() for k in kind_filter.split(",")]
                        if role not in kinds:
                            continue

                    result["appearances"].append({
                        "code": app.code,
                        "at": app.at,
                        "scope": app.scope,
                        "role": role,
                    })

        # 多层展开：基于图遍历，从当前节点参与的边找到关联节点
        if depth > 1 and not reverse:
            result["depth_layers"] = {}
            current_layer: set[str] = set(nd["id"] for nd in node_records)
            visited: set[str] = set(current_layer)

            for d in range(2, depth + 1):
                next_layer: set[str] = set()
                layer_appearances: list[dict[str, str]] = []
                layer_seen: set[tuple[str, str]] = set()

                for nid in current_layer:
                    # 从当前节点参与的边（无论 from 还是 to）找到关联节点
                    related_nodes: set[str] = set()
                    for e in self.store.get_edges_from(nid):
                        related_nodes.add(e["to_node"])
                    for e in self.store.get_edges_to(nid):
                        related_nodes.add(e["from_node"])

                    for to_id in related_nodes:
                        if to_id not in visited:
                            visited.add(to_id)
                            next_layer.add(to_id)
                        # 收集关联节点的 appearances
                        to_apps = self.store.get_symbol_appearances(to_id)
                        for a in to_apps:
                            key = (a.code, a.at)
                            if key not in layer_seen:
                                layer_seen.add(key)
                                layer_appearances.append({
                                    "code": a.code, "at": a.at, "scope": a.scope
                                })

                if layer_appearances:
                    result["depth_layers"][f"depth_{d}"] = layer_appearances
                current_layer = next_layer

        # limit 截断
        total = len(result["appearances"])
        if total > limit:
            result["appearances"] = result["appearances"][:limit]
            result["truncated"] = True
            result["total"] = total
            result["hint"] = f"共 {total} 条，已截断为 {limit} 条。用 --limit N 查看更多，或 --scope 限定作用域"

        return result

    def _derive_role(self, code: str) -> str:
        """从代码内容推导符号角色（多语言适配）。

        Returns:
            "definition" | "import" | "call" | "reference"
        """
        cs = code.lstrip()
        if (cs.startswith("def ") or
            cs.startswith("class ") or
            cs.startswith("type ") or  # Go: type X struct/interface
            cs.startswith("func ") or  # Go: func X() / func (r) X()
            cs.startswith("function ") or  # JS: function X()
            cs.startswith("async function ") or  # JS: async function X()
            " struct {" in cs[:50] or  # Go: X struct {
            " interface {" in cs[:50]):  # Go: X interface {
            return "definition"
        if (cs.startswith("from ") or
            cs.startswith("import ") or
            cs.startswith("var ") and "require" in cs or
            cs.startswith("const ") and "require" in cs or
            cs.startswith("let ") and "require" in cs):
            return "import"
        if "(" in code and "=" not in code.split("(")[0]:
            return "call"
        return "reference"

    # ── info ─────────────────────────────────────────────

    def info(self, symbol: str, scope: str | None = None) -> dict[str, Any]:
        """查询符号定义详情。

        如果有多个同名匹配且未指定 scope，返回消歧列表。
        """
        node_records = self.store.get_node_by_name_scope(symbol, scope)

        if not node_records:
            return {"symbol": symbol, "found": False}

        # 消歧：多个同名匹配且未指定 scope 时，返回列表
        if scope is None and len(node_records) > 1:
            # 按优先级排序：Function > Class > Variable
            kind_order = {"Function": 0, "Class": 1, "Variable": 2, "External": 3, "File": 4}
            sorted_records = sorted(node_records, key=lambda n: kind_order.get(n["kind"], 9))
            return {
                "symbol": symbol,
                "ambiguous": True,
                "matches": [
                    {"id": n["id"], "kind": n["kind"], "scope": n["scope"], "at": n["location"]}
                    for n in sorted_records
                ],
                "hint": "用 --scope <scope> 精确查询",
            }

        nd = node_records[0]

        result: dict[str, Any] = {
            "symbol": symbol,
            "found": True,
            "id": nd["id"],
            "kind": nd["kind"],
            "location": nd["location"],
            "end_location": nd.get("end_location", ""),
            "scope": nd.get("scope", ""),
        }

        if nd["kind"] == "Function":
            # 获取参数列表
            result["params"] = self._get_params(nd["id"])
            # 获取返回值信息
            result["returns"] = self._get_returns(nd["id"])
            # 返回类型注解
            if nd.get("type_annotation"):
                result["return_type"] = nd["type_annotation"]
            # 获取装饰器
            result["decorators"] = self._get_decorators(nd["id"])
        elif nd["kind"] == "Class":
            # 获取方法列表
            result["methods"] = self._get_methods(nd["id"])
            # 获取字段列表
            result["fields"] = self._get_fields(nd["id"])

        if nd.get("type_annotation"):
            result["type_annotation"] = nd["type_annotation"]

        return result

    def _get_params(self, func_node_id: str) -> list[dict[str, Any]]:
        nd = self.store.get_node(func_node_id)
        if not nd:
            return []
        params_scope = func_node_id.replace("#", ":")
        rows = self.store.conn.execute(
            "SELECT * FROM nodes WHERE kind = 'Variable' AND scope = ? AND is_param = 1",
            (params_scope,),
        ).fetchall()
        # 在 Python 中按 location 的行号排序，避免 SQL 字符串解析的脆弱性
        def _line_sort(r: dict[str, Any]) -> int:
            parts = r["location"].split(":")
            try:
                return int(parts[1])
            except (IndexError, ValueError):
                return 0
        rows_sorted = sorted(rows, key=_line_sort)
        return [
            {"name": r["name"], "type": r["type_annotation"] if r["type_annotation"] else "", "location": r["location"]}
            for r in rows_sorted
        ]

    def _get_returns(self, func_node_id: str) -> list[dict[str, str]]:
        edges = self.store.conn.execute(
            "SELECT * FROM edges WHERE edge_type = 'returns' AND from_node = ?",
            (func_node_id,),
        ).fetchall()
        result: list[dict[str, str]] = []
        for e in edges:
            to_node = self.store.get_node(e["to_node"])
            if to_node:
                result.append({
                    "symbol": to_node["name"],
                    "type": to_node["type_annotation"] if to_node["type_annotation"] else "",
                    "location": e["location"],
                })
        return result

    def _get_decorators(self, func_node_id: str) -> list[str]:
        """从 decorates 边提取装饰器名称。"""
        rows = self.store.conn.execute(
            "SELECT metadata FROM edges WHERE edge_type = 'decorates' AND from_node = ?",
            (func_node_id,),
        ).fetchall()
        import json as _json
        decorators: list[str] = []
        for r in rows:
            try:
                meta = _json.loads(r["metadata"]) if isinstance(r["metadata"], str) else r["metadata"]
                dec = meta.get("decorator", "")
                if dec:
                    decorators.append(dec)
            except (ValueError, TypeError):
                pass
        return decorators

    def _get_methods(self, class_node_id: str) -> list[dict[str, str]]:
        nd = self.store.get_node(class_node_id)
        if not nd:
            return []
        # 方法的 scope 格式是 "file.py:ClassName"（类名直接跟在文件路径后）
        method_scope = f"{nd['scope']}:{nd['name']}" if nd["scope"] else nd["name"]
        rows = self.store.conn.execute(
            "SELECT * FROM nodes WHERE kind = 'Function' AND scope = ?",
            (method_scope,),
        ).fetchall()
        return [
            {"name": r["name"], "location": r["location"]}
            for r in rows
        ]

    def _get_fields(self, class_node_id: str) -> list[dict[str, str]]:
        nd = self.store.get_node(class_node_id)
        if not nd:
            return []
        # 字段的 scope 格式是 "file:ClassName"（与方法相同）
        field_scope = f"{nd['scope']}:{nd['name']}" if nd["scope"] else nd["name"]
        rows = self.store.conn.execute(
            "SELECT * FROM nodes WHERE kind = 'Variable' AND scope = ?",
            (field_scope,),
        ).fetchall()
        return [
            {"name": r["name"], "type": r["type_annotation"] if r["type_annotation"] else "", "location": r["location"]}
            for r in rows
        ]

    # ── at ───────────────────────────────────────────────

    def at(self, location: str) -> dict[str, Any]:
        """位置反查：这行代码涉及什么符号和边。"""
        parts = location.rsplit(":", 2)
        if len(parts) < 2:
            return {"location": location, "error": "invalid format (expected file:line[:col])"}

        file_path = parts[0]
        try:
            line = int(parts[1])
        except ValueError:
            return {"location": location, "error": "invalid line number"}

        edges = self.store.get_edges_by_location(file_path, line)
        nodes_list = self.store.get_nodes_at_location(file_path, line)

        # 找匹配的 code：优先从 edges 取，edges 为空时从 nodes 的 defines 边取
        code = edges[0]["code"] if edges else ""
        if not code and nodes_list:
            # 从 defines 边取该行符号的定义代码
            for n in nodes_list:
                def_edges = self.store.conn.execute(
                    "SELECT code FROM edges WHERE edge_type = 'defines' AND to_node = ?",
                    (n["id"],),
                ).fetchall()
                if def_edges:
                    code = def_edges[0]["code"]
                    break

        return {
            "location": location,
            "code": code,
            "symbols": [
                {"name": n["name"], "id": n["id"], "kind": n["kind"]}
                for n in nodes_list
            ],
            "edges": [
                {
                    "edge_type": e["edge_type"],
                    "id": e["id"],
                    "from": e["from_node"],
                    "to": e["to_node"],
                }
                for e in edges
            ],
        }

    # ── search ───────────────────────────────────────────

    def search(
        self,
        text: str,
        *,
        file_filter: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """全文搜索代码原文。

        Args:
            text: 搜索文本。
            file_filter: 限定文件路径前缀（如 "src/" 或 "lib/"）。
            limit: 最大返回条数。
        """
        apps = self.store.get_text_search(text)
        seen: set[tuple[str, str]] = set()
        results: list[dict[str, str]] = []
        for app in apps:
            key = (app.code, app.at)
            if key not in seen:
                seen.add(key)
                # 文件过滤
                if file_filter:
                    file_path = app.at.split(":")[0] if ":" in app.at else ""
                    if not file_path.startswith(file_filter):
                        continue
                results.append({"code": app.code, "at": app.at})
                if len(results) >= limit:
                    break

        total = len(seen)
        result: dict[str, Any] = {
            "query": text,
            "results": results,
            "match_type": "text",
        }
        if total > limit:
            result["truncated"] = True
            result["total"] = total
        return result

    # ── file ─────────────────────────────────────────────

    def file(self, file_path: str) -> dict[str, Any]:
        """文件级查询。"""
        nodes = self.store.get_file_nodes(file_path)
        if not nodes:
            return {"file": file_path, "error": "not found in graph"}

        defines: list[dict[str, str]] = []
        imports: list[dict[str, str]] = []

        file_node_id = file_path + "#"

        # 先查出该文件所有 imports 边的 assigns to_node（import 别名）
        import_assign_targets: set[str] = set()
        for e in self.store.conn.execute(
            "SELECT to_node FROM edges WHERE edge_type = 'assigns' AND from_node = ?",
            (file_node_id,),
        ).fetchall():
            import_assign_targets.add(e["to_node"])

        for nd in nodes:
            if nd["id"] == file_node_id:
                continue
            # 跳过 import 别名（由 from xxx import yyy 创建的本地 Variable）
            if nd["id"] in import_assign_targets:
                continue
            # 只看顶层符号（scope 为空或等于文件路径本身）
            scope = nd.get("scope", "")
            is_top = scope == "" or scope == file_path
            if nd["kind"] in ("Function", "Class") and is_top:
                defines.append({
                    "symbol": nd["name"],
                    "kind": nd["kind"],
                    "at": nd["location"],
                })
            elif nd["kind"] == "Variable" and is_top:
                defines.append({
                    "symbol": nd["name"],
                    "kind": nd["kind"],
                    "at": nd["location"],
                })

        # import 关系
        import_edges = self.store.conn.execute(
            "SELECT * FROM edges WHERE edge_type = 'imports' AND from_node = ?",
            (file_node_id,),
        ).fetchall()
        for e in import_edges:
            to_node = self.store.get_node(e["to_node"])
            if to_node:
                imports.append({
                    "symbol": to_node["name"],
                    "at": e["location"],
                })

        # 被谁 import
        imported_by = self.store.get_imported_by(file_path)

        return {
            "file": file_path,
            "defines": defines,
            "imports": imports,
            "imported_by": imported_by,
        }

    # ── dead ─────────────────────────────────────────────

    def dead(self, scope: str | None = None) -> dict[str, Any]:
        """死代码查询。"""
        dead_rows = self.store.get_dead_nodes()

        dead_symbols: list[dict[str, Any]] = []
        for dr in dead_rows:
            if scope and not dr["scope"].startswith(scope):
                continue
            dead_symbols.append({
                "symbol": dr["name"],
                "at": dr["location"],
                "kind": dr["kind"],
                "scope": dr["scope"],
                "reason": dr.get("reason", ""),
                "confidence": dr.get("confidence", ""),
                "is_test": dr.get("is_test", False),
            })

        # 查找死代码链：看死代码的下游是否也只被死代码调用
        dead_ids = {self._node_id_from_row(d) for d in dead_rows}
        chains: list[dict[str, Any]] = self._find_dead_chains(dead_ids)

        return {
            "dead_symbols": dead_symbols,
            "dead_chains": chains,
        }

    def _node_id_from_row(self, row: dict[str, Any]) -> str:
        return row.get("id", "")

    def _find_dead_chains(self, dead_ids: set[str]) -> list[dict[str, Any]]:
        """查找死代码链：如果 A→B 且 B 的所有引用者都在 dead_ids 中，则 B 也在链上。"""
        chains: list[dict[str, Any]] = []
        visited_chain: set[str] = set()

        for did in dead_ids:
            if did in visited_chain:
                continue
            chain: list[str] = []
            self._dfs_dead_chain(did, dead_ids, visited_chain, chain)
            if len(chain) > 1:
                # 找到根节点信息
                root_node = self.store.get_node(chain[0])
                chains.append({
                    "root": root_node["name"] if root_node else chain[0],
                    "chain": chain,
                    "reason": "零入边或仅被死代码引用",
                })

        return chains

    def _dfs_dead_chain(
        self,
        node_id: str,
        dead_ids: set[str],
        visited: set[str],
        chain: list[str],
    ) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        chain.append(node_id)

        # 找到该节点的出边
        edges = self.store.get_edges_from(node_id)
        for e in edges:
            to_id = e["to_node"]
            # 检查 to_id 的入边是否全部来自 dead_ids
            to_edges = self.store.get_edges_to(to_id)
            all_dead = all(
                te["from_node"] in dead_ids
                for te in to_edges
                if te["edge_type"] in ("calls", "reads", "param-flow")
            )
            if all_dead and to_id in dead_ids:
                self._dfs_dead_chain(to_id, dead_ids, visited, chain)

    # ── impact ───────────────────────────────────────────

    def impact(self, target: str, scope: str | None = None) -> dict[str, Any]:
        """影响面查询，输出分层。

        target 可以是:
          - 符号名 → 查该符号影响的所有 appearance
          - file:line → 查该位置影响的所有 appearance

        Args:
            target: 符号名或 file:line 位置。
            scope: 限定作用域，用于消歧。

        输出分层：
          - direct_callers: 反向1跳，谁直接调用/引用了我
          - direct_callees: 正向1跳，我直接调用/引用了谁
          - transitive: 2跳及以上，间接依赖
        """
        # 确定起始节点
        start_ids: list[str] = []
        if ":" in target and not target.startswith("."):
            at_result = self.at(target)
            if "edges" in at_result:
                for e in at_result["edges"]:
                    start_ids.append(e["to"])
                    start_ids.append(e["from"])
        else:
            node_records = self.store.get_node_by_name_scope(target, scope)
            for nd in node_records:
                start_ids.append(nd["id"])

        if not start_ids:
            return {"target": target, "direct_callers": [], "direct_callees": [], "transitive": []}

        direct_callers: list[dict[str, str]] = []
        direct_callees: list[dict[str, str]] = []
        transitive: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def _add_to(list_dst: list[dict[str, str]], code: str, at: str, scope: str) -> None:
            dedup_key = (code, at)
            if dedup_key not in seen:
                seen.add(dedup_key)
                list_dst.append({"code": code, "at": at, "scope": scope})

        # 第 1 层：直接调用者（反向 1 跳）和直接被调用者（正向 1 跳）
        layer1_ids: set[str] = set()
        for sid in start_ids:
            if sid.startswith("#external:"):
                continue
            node = self.store.get_node(sid)
            if node and node["kind"] == "Variable":
                meaningful_types = ("reads", "assigns", "writes", "param-flow")
            else:
                meaningful_types = ("calls", "param-flow")

            # 正向：我调用了谁
            for e in self.store.get_edges_from(sid):
                if e["edge_type"] not in meaningful_types:
                    continue
                scope = self._scope_from_from_node(e["from_node"])
                _add_to(direct_callees, e["code"], e["location"], scope)
                layer1_ids.add(e["to_node"])

            # 反向：谁调用了我
            for e in self.store.get_edges_to(sid):
                if e["edge_type"] not in meaningful_types:
                    continue
                scope = self._scope_from_from_node(e["from_node"])
                _add_to(direct_callers, e["code"], e["location"], scope)
                layer1_ids.add(e["from_node"])

        # 第 2 层及以后：传递依赖
        visited: set[str] = set(start_ids)
        current_layer = layer1_ids - visited
        for lid in current_layer:
            visited.add(lid)

        depth = 2
        while current_layer and depth <= 3:
            next_layer: set[str] = set()
            for sid in current_layer:
                if sid.startswith("#external:"):
                    continue
                node = self.store.get_node(sid)
                if node and node["kind"] == "Variable":
                    meaningful_types = ("reads", "assigns", "writes", "param-flow")
                else:
                    meaningful_types = ("calls", "param-flow")

                for e in self.store.get_edges_from(sid):
                    if e["edge_type"] not in meaningful_types:
                        continue
                    scope = self._scope_from_from_node(e["from_node"])
                    _add_to(transitive, e["code"], e["location"], scope)
                    if e["to_node"] not in visited:
                        visited.add(e["to_node"])
                        next_layer.add(e["to_node"])

                for e in self.store.get_edges_to(sid):
                    if e["edge_type"] not in meaningful_types:
                        continue
                    scope = self._scope_from_from_node(e["from_node"])
                    _add_to(transitive, e["code"], e["location"], scope)
                    if e["from_node"] not in visited:
                        visited.add(e["from_node"])
                        next_layer.add(e["from_node"])

            current_layer = next_layer
            depth += 1

        return {
            "target": target,
            "direct_callers": direct_callers,
            "direct_callees": direct_callees,
            "transitive": transitive,
        }

    def _scope_from_from_node(self, from_node: str) -> str:
        """从 edge 的 from_node 推导 scope。"""
        if "#" not in from_node:
            return from_node
        file_part, sym_part = from_node.split("#", 1)
        if not sym_part:
            return file_part
        return f"{file_part}:{sym_part}"

    # ── api (HTTP boundary soft links) ──────────────────

    def api(self, path: str | None = None) -> dict[str, Any]:
        """HTTP 边界软关联：基于路径字符串匹配跨语言 API 调用。

        策略：搜索代码中的 API 路径字符串（如 "/api/users"），
        找到所有引用该路径的文件和符号，建立跨语言关联。

        Args:
            path: 限定 API 路径前缀，None 表示列出所有发现的路径。
        """
        import re

        # 从 edges 的 code 字段中提取 API 路径字符串
        all_code = self.store.conn.execute(
            "SELECT DISTINCT code, location, from_node FROM edges WHERE code LIKE '%/api/%'"
        ).fetchall()

        # 提取所有 API 路径
        path_pattern = re.compile(r'["\'`](/api/[\w/{}\-]+)["\'`]')

        # 路径 -> 出现位置列表
        path_locations: dict[str, list[dict[str, str]]] = {}

        for r in all_code:
            code = r["code"]
            matches = path_pattern.findall(code)
            for match in matches:
                if match not in path_locations:
                    path_locations[match] = []
                from_node = r["from_node"]
                file_path = from_node.split("#")[0] if "#" in from_node else from_node
                scope = self._scope_from_from_node(from_node)
                path_locations[match].append({
                    "code": r["code"][:200],
                    "at": r["location"],
                    "file": file_path,
                    "scope": scope,
                })

        # 如果指定了路径，返回该路径的所有引用
        if path:
            locations = path_locations.get(path, [])
            # 按语言/文件分组
            by_file: dict[str, list[dict[str, str]]] = {}
            for loc in locations:
                by_file.setdefault(loc["file"], []).append(loc)

            return {
                "path": path,
                "total_references": len(locations),
                "by_file": [
                    {"file": f, "references": refs}
                    for f, refs in by_file.items()
                ],
            }

        # 否则列出所有发现的路径
        return {
            "paths": [
                {"path": p, "references": len(locs)}
                for p, locs in sorted(path_locations.items(), key=lambda x: -len(x[1]))
            ],
            "total_paths": len(path_locations),
        }

    # ── cycles (circular dependency detection) ──────────

    def cycles(self) -> dict[str, Any]:
        """检测跨文件循环依赖。

        基于 imports 边构建文件依赖图，使用 DFS 检测环。
        """
        # 构建文件依赖图: file -> [imported_files]
        graph: dict[str, set[str]] = {}

        import_edges = self.store.conn.execute(
            "SELECT from_node, to_node FROM edges WHERE edge_type = 'imports'"
        ).fetchall()

        for e in import_edges:
            from_file = e["from_node"].split("#")[0] if "#" in e["from_node"] else e["from_node"]
            to_node = e["to_node"]

            # 尝试将 external 节点映射到项目内文件
            to_file = None
            if not to_node.startswith("#external:"):
                to_file = to_node.split("#")[0] if "#" in to_node else to_node
            else:
                # external:module.path → 尝试匹配项目内文件
                ext_name = to_node[len("#external:"):]
                # 尝试按模块路径匹配文件
                # 如 #external:auth → auth.py
                candidates = [
                    ext_name.replace(".", "/") + ".py",
                    ext_name.replace(".", "/") + ".js",
                    ext_name.replace(".", "/") + ".ts",
                    ext_name.replace(".", "/") + ".go",
                ]
                for c in candidates:
                    # 检查这个文件是否在 nodes 表中
                    exists = self.store.conn.execute(
                        "SELECT 1 FROM nodes WHERE id = ? LIMIT 1", (c + "#",)
                    ).fetchone()
                    if exists:
                        to_file = c
                        break
                # 也尝试只用最后一段匹配
                if not to_file:
                    parts = ext_name.split(".")
                    last = parts[-1]
                    for ext in [".py", ".js", ".ts", ".go"]:
                        # 模糊匹配文件名
                        rows = self.store.conn.execute(
                            "SELECT DISTINCT id FROM nodes WHERE id LIKE ?",
                            (f"%{last}{ext}#",),
                        ).fetchall()
                        if rows:
                            to_file = rows[0]["id"].split("#")[0]
                            break

            if to_file and to_file != from_file:
                graph.setdefault(from_file, set()).add(to_file)

        # DFS 检测环
        result_cycles: list[dict[str, Any]] = []
        visited: set[str] = set()
        rec_stack: list[str] = []

        def _dfs(node: str) -> None:
            if node in rec_stack:
                # 找到环
                cycle_start = rec_stack.index(node)
                cycle = rec_stack[cycle_start:]
                result_cycles.append({"chain": cycle[:]})
                return
            if node in visited:
                return
            visited.add(node)
            rec_stack.append(node)
            for neighbor in graph.get(node, []):
                _dfs(neighbor)
            rec_stack.pop()

        for node in graph:
            if node not in visited:
                _dfs(node)

        return {"cycles": result_cycles, "total": len(result_cycles)}

    # ── types (cross-language type consistency) ──────────

    def types(self, name: str | None = None) -> dict[str, Any]:
        """跨语言类型一致性检查。

        查找同名的 Class 类型在不同语言/文件中的定义，
        比较它们的字段是否一致。

        Args:
            name: 限定类型名，None 表示列出所有跨语言同名类型。
        """
        # 查找所有 Class 节点（排除 External）
        if name:
            rows = self.store.conn.execute(
                "SELECT id, name, kind, location, scope FROM nodes WHERE kind = 'Class' AND name = ?",
                (name,)
            ).fetchall()
        else:
            rows = self.store.conn.execute(
                "SELECT id, name, kind, location, scope FROM nodes WHERE kind = 'Class'"
            ).fetchall()

        # 按名称分组
        by_name: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            file_path = r["location"].split(":")[0] if ":" in r["location"] else r["location"]
            # 推断语言
            if file_path.endswith(".py"):
                lang = "python"
            elif file_path.endswith(".go"):
                lang = "go"
            elif file_path.endswith(".ts") or file_path.endswith(".tsx"):
                lang = "typescript"
            elif file_path.endswith(".js") or file_path.endswith(".jsx"):
                lang = "javascript"
            else:
                lang = "unknown"

            line = int(r["location"].split(":")[1]) if ":" in r["location"] and r["location"].split(":")[1].isdigit() else 0

            # 提取字段（通过 defines 边找到子 Variable 节点）
            fields: list[str] = []
            field_rows = self.store.conn.execute(
                "SELECT to_node FROM edges WHERE edge_type = 'defines' AND from_node = ?",
                (r["id"],)
            ).fetchall()
            for fr in field_rows:
                field_node = self.store.get_node(fr["to_node"])
                if field_node:
                    fields.append(field_node["name"])

            by_name.setdefault(r["name"], []).append({
                "id": r["id"],
                "file": file_path,
                "language": lang,
                "line": line,
                "scope": r["scope"],
                "fields": fields,
            })

        # 找出跨语言定义（同名且出现在不同文件中）
        type_groups: list[dict[str, Any]] = []
        for type_name, defs in by_name.items():
            # 只保留出现在多个文件中的类型
            files = set(d["file"] for d in defs)
            if len(files) < 2:
                continue

            # 检测字段不一致
            all_field_sets = [set(d["fields"]) for d in defs if d["fields"]]
            mismatches: list[str] = []
            if len(all_field_sets) >= 2:
                common = all_field_sets[0]
                for fs in all_field_sets[1:]:
                    missing = common - fs
                    extra = fs - common
                    if missing:
                        idx = all_field_sets.index(fs)
                        mismatches.append(f"{defs[idx]['file']} 缺少 {','.join(missing)}")
                    if extra:
                        idx = all_field_sets.index(fs)
                        mismatches.append(f"{defs[idx]['file']} 多出 {','.join(extra)}")

            type_groups.append({
                "name": type_name,
                "count": len(defs),
                "definitions": defs,
                "mismatches": mismatches,
            })

        return {"type_groups": type_groups, "total": len(type_groups)}