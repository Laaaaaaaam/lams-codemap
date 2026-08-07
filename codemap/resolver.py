"""解析器 —— 跨文件引用解析和 param-flow 边创建。

职责：
  1. 解析 calls 边中跨文件/跨 scope 的函数调用 → 连接到正确的 Function 节点
  2. 根据实参-形参映射创建 param-flow 边
  3. 处理 import 引用 → 连接本地符号到被 import 的 File/External 节点
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from codemap.models import Node, Edge


class Resolver:
    """跨文件引用解析器。

    需要所有文件的 node 和 edge 都已载入 store 之后运行。
    """

    def __init__(self, store: Any) -> None:  # Store
        self.store = store

    def resolve(self) -> None:
        """执行所有跨文件解析。"""
        self._resolve_imports()
        self._resolve_calls()
        self._resolve_param_flows()

    # ── Import resolution ────────────────────────────────

    def _resolve_imports(self) -> None:
        """解析 imports 边：将 #external: 占位符连接到真实定义节点。

        对于每条 imports 边 (file# → #external:module.symbol)：
        1. 从 ext 名提取模块路径和符号名
        2. 按模块路径找文件（模糊匹配文件名末段）
        3. 在文件中查找同名 Function/Class 节点
        4. 更新 to_node 为真实节点
        """
        import_edges = self.store.conn.execute(
            "SELECT * FROM edges WHERE edge_type = 'imports'"
        ).fetchall()
        updates: list[tuple[str, str]] = []  # (edge_id, new_to_node)

        for e in import_edges:
            to_id = e["to_node"]
            if not to_id.startswith("#external:"):
                continue

            ext_name = to_id[len("#external:"):]
            parts = ext_name.split(".")
            symbol_name = parts[-1]
            module_parts = parts[:-1]

            # 查找同名 Function/Class 节点
            rows = self.store.conn.execute(
                "SELECT id FROM nodes WHERE name = ? AND kind IN ('Function', 'Class')",
                (symbol_name,),
            ).fetchall()

            for r in rows:
                file_part = r["id"].split("#")[0]
                file_stem = file_part.rsplit("/", 1)[-1].replace(".py", "")

                # 模块路径末段匹配文件名
                if module_parts:
                    if file_stem == module_parts[-1] or "/".join(module_parts) in file_part:
                        updates.append((e["id"], r["id"]))
                        break
                else:
                    # 无模块路径，直接匹配
                    updates.append((e["id"], r["id"]))
                    break

        for edge_id, new_to in updates:
            self.store.conn.execute(
                "UPDATE edges SET to_node = ? WHERE id = ?",
                (new_to, edge_id),
            )

    # ── Call resolution ──────────────────────────────────

    def _resolve_calls(self) -> None:
        """解析 calls 边：将 calls 边连接到正确的目标 Function 节点。

        三种情况：
        1. to_node 是 Function/Class → 已正确，跳过
        2. to_node 是 Variable（import 别名）→ 通过 import 链找到真实 Function
        3. to_node 是 #external: 占位符 → 尝试解析，失败则标记 unknown
        """
        call_edges = self.store.get_edges_by_type("calls")
        updates: list[tuple[str, str, str, dict[str, Any]]] = []

        for e in call_edges:
            to_id = e["to_node"]
            node = self.store.get_node(to_id)

            if not node:
                continue

            # 情况 1：已经是 Function/Class，跳过
            if node["kind"] in ("Function", "Class"):
                continue

            # 情况 2：Variable（import 别名）→ 查找真实 Function
            if node["kind"] == "Variable":
                real_id = self._resolve_imported_variable(to_id, e["from_node"])
                if real_id:
                    updates.append((e["id"], to_id, real_id, {}))
                continue

            # 情况 3：External 占位符
            if to_id.startswith("#external:"):
                func_name = to_id[len("#external:"):]

                # 先尝试多语言方法调用解析 (receiver.method / obj.method)
                resolved = self._resolve_method_call(e, func_name)
                if not resolved:
                    # 尝试包级函数解析（Go/JS: 同文件中的同名顶层函数）
                    if "." not in func_name:
                        from_file = e["from_node"].split("#")[0] if "#" in e["from_node"] else e["from_node"]
                        resolved = self._find_real_node_in_file(func_name, from_file)
                if not resolved:
                    # 再走原有的 import 链解析
                    resolved = self._resolve_import_call(e, func_name)
                if resolved:
                    updates.append((e["id"], to_id, resolved, {}))
                else:
                    # metadata 是 JSON 字符串，先解析再更新
                    try:
                        meta_dict = json.loads(e["metadata"]) if isinstance(e["metadata"], str) else dict(e["metadata"])
                    except (json.JSONDecodeError, TypeError):
                        meta_dict = {}
                    meta_dict["unknown"] = True
                    updates.append((e["id"], to_id, to_id, meta_dict))

        # 批量更新
        for edge_id, old_to, new_to, new_meta in updates:
            meta = json.dumps(new_meta, ensure_ascii=False)
            self.store.conn.execute(
                "UPDATE edges SET to_node = ?, metadata = ? WHERE id = ?",
                (new_to, meta, edge_id),
            )

    def _resolve_method_call(self, edge: dict[str, Any], func_name: str) -> str | None:
        """解析多语言的方法调用：receiver.method() → Type.method()。

        适用于：
        - Go: engine.handleHTTPRequest() → gin.go#Engine.handleHTTPRequest
        - JS: app.handle(req, res) → file#handle (如果 handle 已定义为 Function)
        - Go: fmt.Printf() → 通过 import 链的 fmt 模块

        策略：
        1. 如果 func_name 含 "."，拆分为 owner.method
        2. 在 from_file 中查找 owner 变量的定义
        3. 如果 owner 是 import 别名（Variable 有 import 边），走 import 解析
        4. 如果 owner 是函数参数或局部变量，尝试：
           a. 查找同文件下所有 Class 节点的同名方法 (file#ClassName.method)
           b. 查找同文件下的同名顶层 Function (file#method)
        5. 如果 owner 名恰好是一个 Class 名，直接查 file#Owner.method
        """
        if "." not in func_name:
            return None

        parts = func_name.split(".", 1)
        owner_name = parts[0]
        method_name = parts[1]
        from_node = edge["from_node"]
        from_file = from_node.split("#")[0] if "#" in from_node else from_node

        # 策略 this: this.method() → 在 from_scope 的 Class 中查找方法
        # JS 类内部方法调用 this.handle() → 当前类的 handle 方法
        if owner_name == "this":
            from_scope = self._scope_from_node(from_node)
            if ":" in from_scope:
                scope_after_file = from_scope.split(":", 1)[1]
                # scope 可能是 "ClassName" 或 "ClassName.method"
                potential_class = scope_after_file.split(".")[0]
                method_id = self.store.get_node_id_by_name_scope(
                    method_name, f"{from_file}:{potential_class}"
                )
                if method_id:
                    return method_id
            # 也尝试 obj.method scope（JS: this.handle → file#res.handle 或 file#handle）
            all_funcs = self.store.conn.execute(
                "SELECT id FROM nodes WHERE name = ? AND kind = 'Function' LIMIT 10",
                (method_name,),
            ).fetchall()
            if all_funcs:
                # 优先返回同文件的
                for r in all_funcs:
                    if r["id"].split("#")[0] == from_file:
                        return r["id"]
                return all_funcs[0]["id"]

        # 策略 5: owner 恰好是一个 Class 名 → file#Owner.method
        cls_id = self.store.get_node_id_by_name_scope(owner_name, from_file)
        if cls_id:
            method_id = self.store.get_node_id_by_name_scope(method_name, f"{from_file}:{owner_name}")
            if method_id:
                return method_id

        # 策略 1-3: owner 是 import 别名 → 走 import 链
        owner_var_id = self.store.get_node_id_by_name_scope(owner_name, from_file)
        if owner_var_id:
            owner_node = self.store.get_node(owner_var_id)
            if owner_node and owner_node["kind"] == "Variable":
                # 检查是否有 import 边（JS: var utils = require('./support/utils')）
                # import 边的 to_node 是 #external:./support/utils（模块路径，非别名）
                file_node_id = from_file + "#"
                import_edges = self.store.conn.execute(
                    "SELECT to_node FROM edges WHERE edge_type='imports' AND from_node=?",
                    (file_node_id,),
                ).fetchall()
                for imp in import_edges:
                    imp_to = imp["to_node"]
                    # 尝试从 import 模块解析 method（utils.shouldHaveBody → 模块内 shouldHaveBody）
                    if imp_to.startswith("#external:"):
                        module_path = imp_to[len("#external:"):]
                        # 从模块路径找到对应文件（如 ./support/utils → test/support/utils.js）
                        target = self._resolve_method_in_module(module_path, method_name)
                        if target:
                            return target

        # 策略 4: owner 是参数/局部变量 → 查找所有 Class 的同名方法
        # 检查 owner 是否是当前文件的某个函数的参数
        from_scope = self._scope_from_node(from_node)

        # 策略 4a: 从 from_scope 提取类名（Go receiver 模式）
        # from_scope 可能是 "gin.go:Engine.ServeHTTP" 或 "gin.go:Engine"
        # 后者是因为 Go 方法的 scope 存储为 "file:ClassName" 而非 "file:ClassName.method"
        if ":" in from_scope:
            scope_after_file = from_scope.split(":", 1)[1]
            scope_parts = scope_after_file.split(".")
            # 尝试每个可能的类名前缀
            for i in range(len(scope_parts)):
                potential_class = scope_parts[i]
                method_id = self.store.get_node_id_by_name_scope(
                    method_name, f"{from_file}:{potential_class}"
                )
                if method_id:
                    return method_id

        owner_in_scope = self.store.get_node_id_by_name_scope(owner_name, from_scope)
        if owner_in_scope:
            # owner 是局部变量/参数，尝试在当前文件的所有 Class 中查找方法
            class_nodes = self.store.conn.execute(
                "SELECT name, id FROM nodes WHERE kind='Class' AND scope=?",
                (from_file,),
            ).fetchall()
            for cls in class_nodes:
                method_id = self.store.get_node_id_by_name_scope(
                    method_name, f"{from_file}:{cls['name']}"
                )
                if method_id:
                    return method_id

            # 也尝试查找同文件的顶层函数（JS 模式: app.handle → handle）
            top_func = self.store.get_node_id_by_name_scope(method_name, from_file)
            if top_func:
                target_node = self.store.get_node(top_func)
                if target_node and target_node["kind"] == "Function":
                    return top_func

        # 策略 4a2: 链式调用 c.Request.Header.Set → 查找 Set 方法
        # 多级属性链只取最后一段方法名
        if "." in method_name:
            last_method = method_name.rsplit(".", 1)[-1]
            from_scope = self._scope_from_node(from_node)
            if ":" in from_scope:
                scope_after_file = from_scope.split(":", 1)[1]
                for potential_class in scope_after_file.split("."):
                    method_id = self.store.get_node_id_by_name_scope(
                        last_method, f"{from_file}:{potential_class}"
                    )
                    if method_id:
                        return method_id

        # 策略 4b: 跨文件查找同名顶层 Function
        # 适用于 JS obj.method = function 模式，调用者在其他文件中
        # 以及 owner 是局部变量但方法定义在其他文件的情况
        all_funcs = self.store.conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND kind = 'Function' AND scope NOT LIKE '%:%' LIMIT 10",
            (method_name,),
        ).fetchall()
        for r in all_funcs:
            candidate = r["id"]
            candidate_file = candidate.split("#")[0] if "#" in candidate else candidate
            # 优先返回非当前文件的（当前文件的已在上面查过）
            if candidate_file != from_file:
                return candidate
        # 如果只有当前文件的，也返回
        if all_funcs:
            return all_funcs[0]["id"]

        # 策略 4c: 跨文件查找 Class 方法（Go: c.String() → context.go#Context.String）
        # 查找所有 scope 为 file:ClassName 格式的同名 Function
        method_funcs = self.store.conn.execute(
            "SELECT id, scope FROM nodes WHERE name = ? AND kind = 'Function' AND scope LIKE '%:%' LIMIT 20",
            (method_name,),
        ).fetchall()
        for r in method_funcs:
            candidate = r["id"]
            candidate_file = candidate.split("#")[0] if "#" in candidate else candidate
            if candidate_file != from_file:
                return candidate
        # 如果只有当前文件的，也返回
        if method_funcs:
            return method_funcs[0]["id"]

        return None

    def _resolve_imported_variable(self, var_node_id: str, from_node_id: str) -> str | None:
        """解析 import 别名 Variable 到真实 Function 节点。

        var_node_id 如 "middleware.py#verify_token"（由 from auth.token import verify_token 创建）
        查找该 Variable 的 import 边，找到对应的 External 节点名，
        然后在所有文件中查找同名 Function。
        """
        var_node = self.store.get_node(var_node_id)
        if not var_node:
            return None

        var_name = var_node["name"]

        # 查找该 Variable 的 import 边（assigns 边 from file_node → this variable）
        file_node_id = var_node_id.rsplit("#", 1)[0] + "#"
        import_edges = self.store.conn.execute(
            "SELECT * FROM edges WHERE edge_type = 'imports' AND from_node = ?",
            (file_node_id,),
        ).fetchall()

        # 找到匹配的 import：import 的 External 节点名包含 var_name
        for imp in import_edges:
            ext_node = self.store.get_node(imp["to_node"])
            if not ext_node:
                continue
            # ext_node name 如 "auth.token.verify_token" 或 "verify_token"
            ext_name = ext_node["name"]
            # 检查 import 的符号名是否匹配
            if ext_name == var_name or ext_name.endswith("." + var_name):
                # 在所有文件中查找同名 Function 节点
                # 按文件路径匹配模块路径
                module_parts = ext_name.split(".")[:-1]  # 去掉最后的符号名
                symbol_name = ext_name.split(".")[-1]

                # 尝试直接按名字查找 Function 节点
                rows = self.store.conn.execute(
                    "SELECT id FROM nodes WHERE name = ? AND kind = 'Function'",
                    (symbol_name,),
                ).fetchall()
                for r in rows:
                    # 检查文件路径是否匹配模块路径
                    file_part = r["id"].split("#")[0]
                    file_stem = file_part.rsplit("/", 1)[-1].replace(".py", "")
                    # 模块路径最后一段匹配文件名
                    if module_parts:
                        if file_stem == module_parts[-1] or "/".join(module_parts) in file_part:
                            return r["id"]
                    else:
                        return r["id"]

            # JS 解构导入：var x = require('./mod').x 只记录模块级 import（#external:./mod）
            # 用模块路径 + var_name 在模块文件中查找
            if ext_name.startswith("./") or ext_name.startswith("../"):
                target = self._resolve_method_in_module(ext_name, var_name)
                if target:
                    return target

        return None

    def _resolve_method_in_module(self, module_path: str, method_name: str) -> str | None:
        """从模块路径解析方法调用目标。

        JS: utils.shouldHaveBody → module_path='./support/utils' → test/support/utils.js#shouldHaveBody
        适用于 var utils = require('./support/utils'); utils.shouldHaveBody(...) 模式。

        Args:
            module_path: import 边中的模块路径（如 ./support/utils、./application）
            method_name: 要查找的方法名（如 shouldHaveBody）

        Returns:
            目标节点 ID，找不到返回 None
        """
        # 将模块路径转成候选文件路径
        # ./support/utils → support/utils.js / support/utils/index.js
        clean = module_path.lstrip("./")
        # 相对路径：从 from_file 目录推导（调用方已传 module_path，这里做全局模糊匹配）
        candidates = [
            clean + ".js",
            clean + ".ts",
            clean + ".go",
            clean + ".py",
            clean + "/index.js",
            clean + "/index.ts",
        ]
        for cand in candidates:
            # 在 nodes 表中查找文件节点（以文件路径开头的 Function）
            rows = self.store.conn.execute(
                "SELECT id FROM nodes WHERE id LIKE ? AND name = ? AND kind = 'Function'",
                (f"%{cand}#%", method_name),
            ).fetchall()
            if rows:
                return rows[0]["id"]

        # 备选：按文件名末段模糊匹配
        # ./support/utils → 匹配任意以 utils.js 结尾的文件
        last_seg = clean.rsplit("/", 1)[-1]
        rows = self.store.conn.execute(
            "SELECT id FROM nodes WHERE id LIKE ? AND name = ? AND kind = 'Function' LIMIT 5",
            (f"%{last_seg}.js#%", method_name),
        ).fetchall()
        if rows:
            return rows[0]["id"]
        rows = self.store.conn.execute(
            "SELECT id FROM nodes WHERE id LIKE ? AND name = ? AND kind = 'Function' LIMIT 5",
            (f"%{last_seg}.ts#%", method_name),
        ).fetchall()
        if rows:
            return rows[0]["id"]
        # 更宽松：模块路径的完整路径匹配（含相对目录前缀）
        # ./support/utils → 匹配 test/support/utils.js（相对 test/ 目录解析）
        rows = self.store.conn.execute(
            "SELECT id FROM nodes WHERE id LIKE ? AND name = ? AND kind = 'Function' LIMIT 5",
            (f"%{clean}#%", method_name),
        ).fetchall()
        if rows:
            return rows[0]["id"]
        return None

    def _resolve_import_call(self, edge: dict[str, Any], func_name: str) -> str | None:
        """通过 import 链解析函数调用目标。

        func_name 可能是:
          - "jwt.decode" → 查找 from jwt import decode 或 import jwt
          - "decode" → 查找 from xxx import decode
        """
        from_file = edge["from_node"].split("#")[0]

        # 查找 to_file 的 import 边
        imports = self.store.conn.execute(
            "SELECT * FROM edges WHERE edge_type = 'imports' AND from_node = ?",
            (f"{from_file}#",),
        ).fetchall()

        # 精确匹配 import 的符号
        parts = func_name.split(".")
        for imp in imports:
            imp_node = self.store.get_node(imp["to_node"])
            if not imp_node:
                continue
            if imp_node["name"] == func_name:
                # 检查 import 的目标是不是一个实际函数
                target = self._find_real_node_in_file(func_name, imp_node.get("scope", ""))
                if target:
                    return target

        # 尝试将 func_name 的首部分匹配 import 模块，然后查找模块内的函数
        if parts:
            module_name = parts[0]
            for imp in imports:
                imp_node = self.store.get_node(imp["to_node"])
                if not imp_node:
                    continue
                if imp_node["name"] == module_name or imp_node["name"].startswith(module_name + "."):
                    # import jwt → jwt.decode → 查找 jwt 模块内定义的 decode
                    target = self._find_node_in_module(imp["to_node"], ".".join(parts[1:]) or func_name)
                    if target:
                        return target

        return None

    def _find_real_node(self, name: str, scope: str) -> str | None:
        """按名字+scope 查找真实节点。"""
        node_id = self.store.get_node_id_by_name_scope(name, scope)
        if node_id:
            return node_id
        # 全局 scope
        if ":" in scope:
            file_path = scope.split(":")[0]
            global_scope = file_path
            node_id = self.store.get_node_id_by_name_scope(name, global_scope)
            if node_id:
                return node_id
        return None

    def _find_real_node_in_file(self, name: str, file_path: str) -> str | None:
        """在指定文件中查找指定名称的 Function 节点。"""
        rows = self.store.conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND kind IN ('Function', 'Class') AND id LIKE ?",
            (name, f"{file_path}#%"),
        ).fetchall()
        return rows[0]["id"] if rows else None

    def _find_node_in_module(self, ext_id: str, name: str) -> str | None:
        """在 external 模块对应代码仓库的文件中查找子符号。

        例如: ext_id = "#external:auth.token.verify_token"
              检查所有 import 了 auth.token 的文件，然后查找该文件是否有 verify_token。
        """
        # 更简单直接的方案：ext_id 格式 "#external:module.symbol"
        # 从 ext_id 提取模块路径，在 nodes 表中查找 Function 节点
        parts = ext_id[len("#external:"):].split(".")
        for i in range(len(parts), 0, -1):
            module_name = ".".join(parts[:i])
            symbol_name = ".".join(parts[i:]) if i < len(parts) else ""
            # 查找包含此模块的文件
            rows = self.store.conn.execute(
                "SELECT id FROM nodes WHERE name = ? AND kind IN ('Function', 'Class')",
                (module_name,),
            ).fetchall()
            for r in rows:
                # 检查 node 的文件路径是否匹配
                file_part = r["id"].split("#")[0]
                file_stem = file_part.rsplit("/", 1)[-1].replace(".py", "")
                # 模糊匹配
                if file_stem == module_name.split(".")[-1] or module_name.replace(".", "/") in file_part:
                    if symbol_name:
                        # 在文件中查找子符号
                        found = self._find_real_node_in_file(symbol_name, file_part)
                        if found:
                            return found
                    else:
                        return r["id"]
        return None

    def _scope_from_node(self, node_id: str) -> str:
        """从 node_id 推断 scope 字符串。"""
        node = self.store.get_node(node_id)
        if node:
            return node["scope"]
        parts = node_id.split("#", 1)
        if len(parts) == 2:
            file_part = parts[0]
            sym_parts = parts[1].split(".")
            if len(sym_parts) >= 2:
                return f"{file_part}:{'.'.join(sym_parts[:-1])}"
            return file_part
        return ""

    # ── Param-flow resolution ────────────────────────────

    def _resolve_param_flows(self) -> None:
        """根据 calls 边的 metadata 创建 param-flow 边。

        extractor 在 calls 边的 metadata["args"] 里存了实参信息：
        [{"index": 0, "name": "token", "kind": "name"}, ...]

        对于每条 calls(from_fn → to_fn) 边：
          1. 读 metadata["args"] 获取实参列表
          2. 找到 to_fn 的形参列表（按 is_param=1 查询）
          3. 为每条实参创建 param-flow 边: arg_variable → param_variable
        """
        call_edges = self.store.conn.execute(
            "SELECT * FROM edges WHERE edge_type = 'calls'"
        ).fetchall()

        new_edges: list[Edge] = []

        for ce in call_edges:
            to_node = self.store.get_node(ce["to_node"])
            if not to_node or to_node["kind"] != "Function":
                continue

            # 从 metadata 读取实参信息（不再反解析 code）
            meta_raw = ce["metadata"]
            try:
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except (json.JSONDecodeError, TypeError):
                meta = {}
            arg_infos = meta.get("args", [])
            if not arg_infos:
                continue

            # 获取目标函数的形参（按 is_param=1 查询，按 location 排序）
            params = self._get_function_params(ce["to_node"])
            if not params:
                continue

            from_loc = ce["from_node"]
            from_scope = self._scope_from_node(from_loc)
            from_file = from_loc.split("#")[0] if "#" in from_loc else from_loc

            for arg_info in arg_infos:
                arg_name = arg_info.get("name", "")
                if not arg_name:
                    continue  # 跳过字面量/复杂表达式

                arg_index = arg_info.get("index", -1)
                arg_kind = arg_info.get("kind", "")

                # 关键字参数：按名字匹配形参
                if arg_kind == "kw":
                    kw_name = arg_info.get("kw", "")
                    if kw_name in params:
                        param_name = kw_name
                    else:
                        continue
                elif arg_index >= 0 and arg_index < len(params):
                    param_name = params[arg_index]
                else:
                    continue

                # 查找实参变量节点
                arg_node_id = f"{from_file}#{self._scoped_name(from_scope, arg_name)}"
                arg_node = self.store.get_node(arg_node_id)
                if not arg_node:
                    arg_node_id = self.store.get_node_id_by_name_scope(arg_name, from_scope)
                    if not arg_node_id:
                        continue

                # 查找形参节点
                param_node_id = self.store.get_node_id_by_name_scope(
                    param_name, to_node["scope"]
                )
                if not param_node_id:
                    continue

                # 创建 param-flow 边
                edge_id = self._make_edge_id(ce["id"], arg_index if arg_index >= 0 else hash(arg_info.get("kw", "")))
                edge = Edge(
                    id=str(edge_id),
                    edge_type="param-flow",
                    from_node=arg_node_id,
                    to_node=param_node_id,
                    location=ce["location"],
                    code=ce["code"],
                    metadata={
                        "arg_index": arg_index,
                        "arg_kind": arg_kind,
                        "call_edge": ce["id"],
                    },
                )
                new_edges.append(edge)

        # 批量写入
        if new_edges:
            self.store.insert_edges(new_edges)

    def _get_function_params(self, func_node_id: str) -> list[str]:
        """获取函数节点的形参名列表（按 is_param=1 查询，按 location 排序）。"""
        func_scope = self._scope_from_node(func_node_id)
        param_nodes = self.store.conn.execute(
            "SELECT * FROM nodes WHERE kind = 'Variable' AND scope = ? AND is_param = 1 ORDER BY location",
            (func_scope,),
        ).fetchall()
        return [p["name"] for p in param_nodes]

    def _scoped_name(self, scope: str, name: str) -> str:
        """将 scope 和 name 组合为 node id 的后半部分。

        scope = "file.py:func" → "func.name"
        scope = "file.py" → "name"
        """
        if ":" in scope:
            func_path = scope.split(":", 1)[1]
            return f"{func_path}.{name}"
        return name

    def _make_edge_id(self, call_edge_id: str, arg_index: int) -> str:
        return f"{call_edge_id}_pf_{arg_index}"
