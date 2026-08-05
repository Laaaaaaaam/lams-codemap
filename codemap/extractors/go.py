"""Go 提取器 —— 基于 tree-sitter 从 Go 源文件提取图事实。

支持提取：
  - 包声明 (package)
  - 函数声明 (func)
  - 结构体声明 (type struct)
  - 接口声明 (type interface)
  - 方法声明 (func (r Receiver) Method())
  - 变量声明 (var / const / :=)
  - import
  - 函数调用
  - 返回值
  - 属性访问

Node ID 格式与 Python 提取器一致：file_path#symbol_path
"""

from __future__ import annotations

import hashlib
from typing import Any

import tree_sitter as ts
import tree_sitter_go as tsgo

from codemap.models import Node, Edge, Transform
from codemap.extractors.treesitter_utils import (
    ts_loc,
    ts_end_loc,
    ts_source,
    ts_node_text,
    get_identifier_text,
    find_child_by_type,
    find_children_by_type,
)


def _hash_source(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:12]


class GoExtractor:
    """从 Go 源文件提取图事实。"""

    def __init__(self, file_path: str, source_code: str) -> None:
        self.file = file_path
        self.source = source_code
        self.source_bytes = source_code.encode("utf-8", errors="replace")
        self.source_hash = _hash_source(source_code)

        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self.transforms: list[Transform] = []

        self._seen_node_ids: set[str] = set()
        self._node_index: dict[str, int] = {}
        self._seen_edge_keys: set[tuple[str, str, str, str]] = set()
        self._counter: dict[str, int] = {}

        self._lang = ts.Language(tsgo.language())
        self._parser = ts.Parser(self._lang)
        self._tree: ts.Tree | None = None

    def _gen_id(self, prefix: str) -> str:
        self._counter[prefix] = self._counter.get(prefix, 0) + 1
        safe_file = self.file.replace("/", "_").replace("\\", "_").replace(".", "_")
        return f"{safe_file}_{prefix}_{self._counter[prefix]:04d}"

    def _node_id(self, *parts: str) -> str:
        return f"{self.file}#{'.'.join(parts)}"

    def _scope_str(self, *parts: str) -> str:
        if not parts:
            return self.file
        return f"{self.file}:{'.'.join(parts)}"

    # ── Main entry ───────────────────────────────────────

    def extract(self) -> tuple[list[Node], list[Edge], list[Transform]]:
        try:
            self._tree = self._parser.parse(self.source_bytes)
        except Exception:
            self._add_file_node()
            return self.nodes, self.edges, self.transforms

        self._add_file_node()
        root = self._tree.root_node
        self._visit_source_file(root)
        return self.nodes, self.edges, self.transforms

    # ── Node / Edge factory ──────────────────────────────

    def _add_file_node(self) -> Node:
        n = Node(
            id=self._node_id(),
            kind="File",
            name=self.file,
            location=f"{self.file}:1:0",
            scope=self.file,
            source_hash=self.source_hash,
        )
        self.nodes.append(n)
        self._seen_node_ids.add(n.id)
        self._node_index[n.id] = len(self.nodes) - 1
        return n

    def _add_node(
        self,
        name: str,
        kind: str,
        scope_parts: tuple[str, ...],
        tsnode: ts.Node,
        type_annotation: str = "",
        is_param: int = 0,
    ) -> Node:
        nid = self._node_id(*scope_parts, name)
        if nid in self._seen_node_ids:
            idx = self._node_index.get(nid)
            if idx is not None:
                n = self.nodes[idx]
                if type_annotation and not n.type_annotation:
                    self.nodes[idx] = Node(
                        id=n.id, kind=n.kind, name=n.name,
                        location=n.location, end_location=n.end_location,
                        scope=n.scope, type_annotation=type_annotation,
                        source_hash=n.source_hash, is_param=n.is_param or is_param,
                    )
                    return self.nodes[idx]
                return n
        self._seen_node_ids.add(nid)
        n = Node(
            id=nid,
            kind=kind,  # type: ignore[arg-type]
            name=name,
            location=ts_loc(self.file, tsnode),
            end_location=ts_end_loc(self.file, tsnode),
            scope=self._scope_str(*scope_parts),
            type_annotation=type_annotation,
            source_hash=self.source_hash,
            is_param=is_param,
        )
        self._node_index[nid] = len(self.nodes)
        self.nodes.append(n)
        return n

    def _add_edge(
        self,
        edge_type: str,
        from_id: str,
        to_id: str,
        tsnode: ts.Node,
        code: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Edge | None:
        loc = ts_loc(self.file, tsnode)
        key = (edge_type, from_id, to_id, loc)
        if key in self._seen_edge_keys:
            return None
        self._seen_edge_keys.add(key)
        e = Edge(
            id=self._gen_id("E"),
            edge_type=edge_type,  # type: ignore[arg-type]
            from_node=from_id,
            to_node=to_id,
            location=loc,
            code=code or ts_source(self.source_bytes, tsnode),
            metadata=metadata or {},
        )
        self.edges.append(e)
        return e

    def _add_transform(
        self,
        kind: str,
        op: str,
        tsnode: ts.Node,
        inputs: list[str],
        outputs: list[str],
        op_node: str = "",
        branch: str | None = None,
    ) -> Transform:
        t = Transform(
            id=self._gen_id("T"),
            kind=kind,  # type: ignore[arg-type]
            op=op,
            op_node=op_node,
            location=ts_loc(self.file, tsnode),
            inputs=inputs,
            outputs=outputs,
            branch=branch,
            code=ts_source(self.source_bytes, tsnode),
        )
        self.transforms.append(t)
        return t

    def _resolve_name(self, name: str, scope: tuple[str, ...]) -> str | None:
        for i in range(len(scope), 0, -1):
            node_id = self._node_id(*scope[:i], name)
            if node_id in self._seen_node_ids:
                return node_id
        node_id = self._node_id(name)
        if node_id in self._seen_node_ids:
            return node_id
        return None

    # ── Visitors ──────────────────────────────────────────

    def _visit_source_file(self, root: ts.Node) -> None:
        file_id = self._node_id()
        for child in root.children:
            t = child.type
            if t == "package_clause":
                continue  # 包名不作为节点
            elif t == "import_declaration":
                self._visit_import(child, (), file_id)
            elif t == "function_declaration":
                self._visit_function_declaration(child, (), file_id)
            elif t == "method_declaration":
                self._visit_method_declaration(child, (), file_id)
            elif t == "type_declaration":
                self._visit_type_declaration(child, (), file_id)
            elif t == "var_declaration":
                self._visit_var_declaration(child, (), file_id)
            elif t == "const_declaration":
                self._visit_var_declaration(child, (), file_id)
            elif t == "short_var_declaration":
                self._visit_short_var_declaration(child, (), file_id)
            else:
                # 表达式语句等
                for c in child.children:
                    if c.is_named:
                        self._visit_expression(c, (), file_id)

    def _visit_import(self, node: ts.Node, scope: tuple[str, ...], file_id: str) -> None:
        """处理 import: import "fmt" 或 import ( "fmt"; f "os" )。

        分组 import 中 import_spec 嵌套在 import_spec_list 下，需要递归查找。
        """
        from codemap.extractors.treesitter_utils import find_descendants_by_type
        # 递归查找所有 import_spec（直接子节点或嵌套在 import_spec_list 中）
        specs = find_descendants_by_type(node, "import_spec")
        for spec in specs:
            path_node = find_child_by_type(spec, "interpreted_string_literal")
            if path_node:
                module = ts_node_text(path_node).strip('"')
                # import 别名
                alias = ""
                name_node = spec.child_by_field_name("name")
                if name_node:
                    alias = ts_node_text(name_node)
                local_name = alias or module.split("/")[-1]
                self._add_node(local_name, "Variable", scope, spec)
                ext_id = f"#external:{module}"
                self._add_edge("imports", file_id, ext_id, spec)
                self._add_edge("assigns", file_id, self._node_id(*scope, local_name), spec)

    def _visit_function_declaration(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        name_node = node.child_by_field_name("name")
        func_name = get_identifier_text(name_node) if name_node else "<anonymous>"
        func_scope = (*scope, func_name)
        func_id = self._node_id(*func_scope)

        # 返回类型
        ret_type = ""
        result = node.child_by_field_name("result")
        if result:
            ret_type = ts_source(self.source_bytes, result)

        self._add_node(func_name, "Function", scope, node, type_annotation=ret_type)
        scope_id = self._node_id(*scope) if scope else file_id
        self._add_edge("defines", scope_id, func_id, node)

        # 形参
        params_node = node.child_by_field_name("parameters")
        if params_node:
            self._visit_params(params_node, func_scope)

        # 函数体
        body = node.child_by_field_name("body")
        if body:
            self._visit_block(body, func_scope, file_id)

    def _visit_method_declaration(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        """Go 方法: func (r Receiver) Method(args) {}。"""
        # 接收者
        receiver_node = node.child_by_field_name("receiver")
        receiver_type = ""
        receiver_name = ""
        if receiver_node:
            # receiver 是 parameter_list，包含一个 parameter_declaration
            for param in receiver_node.children:
                if param.type == "parameter_declaration":
                    type_n = param.child_by_field_name("type")
                    if type_n:
                        receiver_type = ts_node_text(type_n).lstrip("*")
                    name_n = param.child_by_field_name("name")
                    if name_n and name_n.type == "identifier":
                        receiver_name = ts_node_text(name_n)

        name_node = node.child_by_field_name("name")
        method_name = get_identifier_text(name_node) if name_node else "<anonymous>"

        # 方法的 scope: ReceiverType.method_name
        if receiver_type:
            method_scope = (*scope, receiver_type, method_name)
            # 确保 receiver 类型节点存在
            if self._node_id(*scope, receiver_type) not in self._seen_node_ids:
                self._add_node(receiver_type, "Class", scope, node)
        else:
            method_scope = (*scope, method_name)

        ret_type = ""
        result = node.child_by_field_name("result")
        if result:
            ret_type = ts_source(self.source_bytes, result)

        self._add_node(method_name, "Function", (*scope, receiver_type) if receiver_type else scope, node, type_annotation=ret_type)
        if receiver_type:
            cls_id = self._node_id(*scope, receiver_type)
            method_id = self._node_id(*scope, receiver_type, method_name)
            self._add_edge("defines", cls_id, method_id, node)
        else:
            scope_id = self._node_id(*scope) if scope else file_id
            self._add_edge("defines", scope_id, self._node_id(*method_scope), node)

        # 形参
        params_node = node.child_by_field_name("parameters")
        if params_node:
            self._visit_params(params_node, method_scope)

        # Receiver 参数创建为节点（如 s in func (s *Server) Start()）
        if receiver_name and receiver_type:
            # receiver 变量存放在方法 scope 下，带类型注解
            self._add_node(
                receiver_name, "Variable", method_scope,
                receiver_node, type_annotation=receiver_type, is_param=1,
            )

        # 函数体
        body = node.child_by_field_name("body")
        if body:
            self._visit_block(body, method_scope, file_id)

    def _visit_params(self, params_node: ts.Node, func_scope: tuple[str, ...]) -> None:
        """处理参数列表。"""
        for param in params_node.children:
            if param.type == "parameter_declaration":
                name_n = param.child_by_field_name("name")
                type_n = param.child_by_field_name("type")
                ann = ts_source(self.source_bytes, type_n) if type_n else ""
                if name_n and name_n.type == "identifier":
                    self._add_node(
                        ts_node_text(name_n), "Variable", func_scope, name_n,
                        type_annotation=ann, is_param=1,
                    )
            elif param.type == "variadic_parameter_declaration":
                name_n = param.child_by_field_name("name")
                type_n = param.child_by_field_name("type")
                ann = ts_source(self.source_bytes, type_n) if type_n else ""
                if name_n and name_n.type == "identifier":
                    self._add_node(
                        ts_node_text(name_n), "Variable", func_scope, name_n,
                        type_annotation="..." + ann, is_param=1,
                    )

    def _visit_type_declaration(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        """处理 type 声明: type Foo struct {...} / type Bar interface {...} / type Alias = int。"""
        for spec in node.children:
            if spec.type != "type_spec":
                continue
            name_node = spec.child_by_field_name("name")
            if not name_node:
                continue
            type_name = ts_node_text(name_node)
            type_value = spec.child_by_field_name("type")
            if not type_value:
                continue

            if type_value.type == "struct_type":
                # 结构体
                self._add_node(type_name, "Class", scope, spec)
                scope_id = self._node_id(*scope) if scope else file_id
                type_scope = (*scope, type_name)
                self._add_edge("defines", scope_id, self._node_id(*type_scope), spec)

                # 结构体字段
                field_list = find_child_by_type(type_value, "field_declaration_list")
                if field_list:
                    for field in field_list.children:
                        if field.type == "field_declaration":
                            # Go struct 字段名是 field_identifier 类型子节点
                            field_name = ""
                            name_node = None
                            for child in field.children:
                                if child.type == "field_identifier":
                                    name_node = child
                                    field_name = ts_node_text(child)
                                    break
                            if field_name:
                                ann = ""
                                type_n = field.child_by_field_name("type")
                                if not type_n:
                                    # 备选：查找类型子节点
                                    for child in field.children:
                                        if child.type in ("type_identifier", "pointer_type", "qualified_type", "slice_type", "array_type", "map_type", "channel_type", "interface_type", "func_type", "struct_type"):
                                            type_n = child
                                            break
                                if type_n:
                                    ann = ts_source(self.source_bytes, type_n)
                                self._add_node(field_name, "Variable", type_scope, name_node or field, type_annotation=ann)
                                self._add_edge("defines", self._node_id(*type_scope), self._node_id(*type_scope, field_name), field)

            elif type_value.type == "interface_type":
                # 接口
                self._add_node(type_name, "Class", scope, spec)
                scope_id = self._node_id(*scope) if scope else file_id
                type_scope = (*scope, type_name)
                self._add_edge("defines", scope_id, self._node_id(*type_scope), spec)

                # 接口方法 (tree-sitter go: method_elem 节点，直接在 interface_type 下)
                for method in type_value.children:
                    if method.type in ("method_elem", "method_spec"):
                        mname = method.child_by_field_name("name")
                        if mname:
                            method_name = ts_node_text(mname)
                            self._add_node(method_name, "Function", type_scope, mname)
                            self._add_edge("defines", self._node_id(*type_scope), self._node_id(*type_scope, method_name), method)
            else:
                # 类型别名 (type X = Y 或 type X SomeType)
                ann = ts_source(self.source_bytes, type_value)
                self._add_node(type_name, "TypeAlias", scope, spec, type_annotation=ann)
                scope_id = self._node_id(*scope) if scope else file_id
                self._add_edge("defines", scope_id, self._node_id(*scope, type_name), spec)

    def _visit_var_declaration(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        """var x int = 1 / var ( x = 1; y = 2 )。"""
        scope_id = self._node_id(*scope) if scope else file_id
        for spec in node.children:
            if spec.type != "var_spec" and spec.type != "const_spec":
                continue
            name_node = spec.child_by_field_name("name")
            value_node = spec.child_by_field_name("value")
            type_n = spec.child_by_field_name("type")
            ann = ts_source(self.source_bytes, type_n) if type_n else ""

            if name_node and name_node.type == "identifier_list":
                for n in name_node.children:
                    if n.type == "identifier":
                        var_name = ts_node_text(n)
                        self._add_node(var_name, "Variable", scope, n, type_annotation=ann)
                        self._add_edge("assigns", scope_id, self._node_id(*scope, var_name), spec)
            elif name_node and name_node.type == "identifier":
                var_name = ts_node_text(name_node)
                self._add_node(var_name, "Variable", scope, name_node, type_annotation=ann)
                self._add_edge("assigns", scope_id, self._node_id(*scope, var_name), spec)

            if value_node:
                self._visit_expression(value_node, scope, file_id)

    def _visit_short_var_declaration(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        """x := expr。"""
        scope_id = self._node_id(*scope) if scope else file_id
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left and left.type == "expression_list":
            for n in left.children:
                if n.type == "identifier":
                    var_name = ts_node_text(n)
                    self._add_node(var_name, "Variable", scope, n)
                    self._add_edge("assigns", scope_id, self._node_id(*scope, var_name), node)
        if right:
            self._visit_expression(right, scope, file_id)

    def _visit_block(self, node: ts.Node, scope: tuple[str, ...], file_id: str) -> None:
        """处理函数体 block。"""
        for child in node.children:
            if not child.is_named or child.type == "comment":
                continue
            self._visit_stmt(child, scope, file_id)

    def _visit_stmt(self, node: ts.Node, scope: tuple[str, ...], file_id: str) -> None:
        t = node.type
        scope_id = self._node_id(*scope) if scope else file_id

        if t == "return_statement":
            for child in node.children:
                if child.is_named:
                    self._visit_expression(child, scope, file_id)
                    if child.type == "identifier":
                        var_name = ts_node_text(child)
                        var_id = self._resolve_name(var_name, scope)
                        if var_id:
                            self._add_edge("returns", scope_id, var_id, node)

        elif t == "short_var_declaration":
            self._visit_short_var_declaration(node, scope, file_id)

        elif t == "var_declaration" or t == "const_declaration":
            self._visit_var_declaration(node, scope, file_id)

        elif t == "assignment_statement":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left:
                self._visit_expression(left, scope, file_id)
            if right:
                self._visit_expression(right, scope, file_id)

        elif t == "if_statement":
            for child in node.children:
                if child.is_named:
                    if child.type == "block":
                        self._visit_block(child, scope, file_id)
                    elif child.type == "if_statement":
                        self._visit_stmt(child, scope, file_id)
                    else:
                        self._visit_expression(child, scope, file_id)

        elif t == "for_statement":
            for child in node.children:
                if child.is_named:
                    if child.type == "block":
                        self._visit_block(child, scope, file_id)
                    else:
                        self._visit_stmt(child, scope, file_id) if child.type in ("short_var_declaration", "assignment_statement") else self._visit_expression(child, scope, file_id)

        elif t == "expression_statement":
            for child in node.children:
                if child.is_named:
                    self._visit_expression(child, scope, file_id)

        elif t == "block":
            self._visit_block(node, scope, file_id)

        elif t == "switch_statement":
            for child in node.children:
                if child.is_named and child.type == "block":
                    self._visit_block(child, scope, file_id)

        else:
            for child in node.children:
                if child.is_named and child.type not in ("comment",):
                    self._visit_expression(child, scope, file_id)

    def _visit_expression(
        self, node: ts.Node | None, scope: tuple[str, ...], file_id: str
    ) -> None:
        if node is None:
            return
        t = node.type
        scope_id = self._node_id(*scope) if scope else file_id

        if t == "identifier":
            var_name = ts_node_text(node)
            var_id = self._resolve_name(var_name, scope)
            if var_id:
                self._add_edge("reads", scope_id, var_id, node)

        elif t == "call_expression":
            func_node = node.child_by_field_name("function")
            self._visit_expression(func_node, scope, file_id)
            func_name = self._extract_callable_name(func_node)
            if func_name:
                callee_id = self._resolve_name(func_name, scope)
                if not callee_id:
                    callee_id = self._resolve_name(func_name, ())
                if callee_id:
                    self._add_edge("calls", scope_id, callee_id, node, metadata=self._extract_call_args(node))
                else:
                    ext_id = f"#external:{func_name}"
                    meta = self._extract_call_args(node)
                    meta["unknown"] = True
                    self._add_edge("calls", scope_id, ext_id, node, metadata=meta)
            args_node = node.child_by_field_name("arguments")
            if args_node:
                for arg in args_node.children:
                    if arg.is_named:
                        self._visit_expression(arg, scope, file_id)

        elif t == "selector_expression":
            # a.b
            operand = node.child_by_field_name("operand")
            field = node.child_by_field_name("field")
            self._visit_expression(operand, scope, file_id)
            if operand and operand.type == "identifier":
                owner_id = self._resolve_name(ts_node_text(operand), scope)
                if owner_id:
                    self._add_edge("reads", scope_id, owner_id, node)

        elif t == "binary_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            self._visit_expression(left, scope, file_id)
            self._visit_expression(right, scope, file_id)

        elif t == "unary_expression":
            operand = node.child_by_field_name("operand")
            self._visit_expression(operand, scope, file_id)

        elif t == "composite_literal":
            # Foo{field: value}
            type_n = node.child_by_field_name("type")
            if type_n:
                type_name = ts_node_text(type_n)
                callee_id = self._resolve_name(type_name, scope) or self._resolve_name(type_name, ())
                if callee_id:
                    self._add_edge("calls", scope_id, callee_id, node)
            for child in node.children:
                if child.type == "keyed_element":
                    val = child.child_by_field_name("value")
                    self._visit_expression(val, scope, file_id)

        elif t == "index_expression":
            self._visit_expression(node.child_by_field_name("operand"), scope, file_id)
            self._visit_expression(node.child_by_field_name("index"), scope, file_id)

        elif t in ("parenthesized_expression",):
            for child in node.children:
                if child.is_named:
                    self._visit_expression(child, scope, file_id)

        elif t == "function_literal":
            # func() {} 匿名函数
            params_node = node.child_by_field_name("parameters")
            if params_node:
                self._visit_params(params_node, scope)
            body = node.child_by_field_name("body")
            if body:
                self._visit_block(body, scope, file_id)

        else:
            for child in node.children:
                if child.is_named and child.type not in ("comment",):
                    self._visit_expression(child, scope, file_id)

    def _extract_callable_name(self, func_node: ts.Node | None) -> str | None:
        if func_node is None:
            return None
        if func_node.type == "identifier":
            return ts_node_text(func_node)
        elif func_node.type == "selector_expression":
            # pkg.Func → pkg.Func
            operand = func_node.child_by_field_name("operand")
            field = func_node.child_by_field_name("field")
            if operand and field:
                if operand.type == "identifier":
                    return f"{ts_node_text(operand)}.{ts_node_text(field)}"
                else:
                    inner = self._extract_callable_name(operand)
                    if inner:
                        return f"{inner}.{ts_node_text(field)}"
        elif func_node.type == "parenthesized_expression":
            for child in func_node.children:
                if child.is_named:
                    return self._extract_callable_name(child)
        return None

    def _extract_call_args(self, call_node: ts.Node) -> dict[str, Any]:
        arg_infos: list[dict[str, Any]] = []
        args_node = call_node.child_by_field_name("arguments")
        if args_node:
            for i, arg in enumerate(args_node.children):
                if not arg.is_named:
                    continue
                if arg.type == "identifier":
                    arg_infos.append({"index": i, "name": ts_node_text(arg), "kind": "name"})
                elif arg.type == "selector_expression":
                    operand = arg.child_by_field_name("operand")
                    root_name = ts_node_text(operand) if operand and operand.type == "identifier" else ""
                    arg_infos.append({"index": i, "name": root_name, "kind": "attr"})
                else:
                    arg_infos.append({"index": i, "name": "", "kind": "other"})
        return {"args": arg_infos}
