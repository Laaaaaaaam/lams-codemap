"""JavaScript 提取器 —— 基于 tree-sitter 从 JS 源文件提取图事实。

支持提取：
  - 函数声明 / 函数表达式 / 箭头函数
  - 类声明 / 类方法
  - 变量声明 (var/let/const)
  - import / require
  - 函数调用
  - 赋值 / 读取
  - 返回值
  - 对象属性访问

Node ID 格式与 Python 提取器一致：file_path#symbol_path
"""

from __future__ import annotations

import hashlib
from typing import Any

import tree_sitter as ts
import tree_sitter_javascript as tsjs

from codemap.models import Node, Edge, Transform
from codemap.extractors.treesitter_utils import (
    ts_loc,
    ts_end_loc,
    ts_source,
    ts_node_text,
    find_child_by_type,
    find_children_by_type,
    get_identifier_text,
)


def _hash_source(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:12]


class JavaScriptExtractor:
    """从 JavaScript 源文件提取图事实。"""

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

        # 解析 tree-sitter
        self._lang = ts.Language(tsjs.language())
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
        """解析源码并返回 (节点, 边, 变换)。"""
        try:
            self._tree = self._parser.parse(self.source_bytes)
        except Exception:
            self._add_file_node()
            return self.nodes, self.edges, self.transforms

        self._add_file_node()
        root = self._tree.root_node
        self._visit_program(root, ())
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
        """在作用域链中查找已定义的符号。"""
        for i in range(len(scope), 0, -1):
            node_id = self._node_id(*scope[:i], name)
            if node_id in self._seen_node_ids:
                return node_id
        node_id = self._node_id(name)
        if node_id in self._seen_node_ids:
            return node_id
        return None

    # ── Visitors ──────────────────────────────────────────

    def _visit_program(self, root: ts.Node, scope: tuple[str, ...]) -> None:
        file_id = self._node_id()
        for child in root.children:
            self._visit_statement(child, scope, file_id)

    def _visit_statement(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        t = node.type

        if t == "function_declaration":
            self._visit_function_declaration(node, scope, file_id)

        elif t == "class_declaration":
            self._visit_class_declaration(node, scope, file_id)

        elif t == "variable_declaration":
            self._visit_variable_declaration(node, scope, file_id)

        elif t in ("lexical_declaration",):
            self._visit_variable_declaration(node, scope, file_id)

        elif t == "import_statement":
            self._visit_import(node, scope, file_id)

        elif t == "export_statement":
            # export default function foo() {} / export { foo }
            for child in node.children:
                if child.type in ("function_declaration", "class_declaration", "lexical_declaration", "variable_declaration"):
                    self._visit_statement(child, scope, file_id)

        elif t == "expression_statement":
            self._visit_expression(node.child_by_field_name("expression") or node.children[0], scope, file_id)

        elif t == "return_statement":
            self._visit_return(node, scope, file_id)

        elif t == "if_statement":
            cond = node.child_by_field_name("condition")
            if cond:
                self._visit_expression(cond, scope, file_id)
            cons = node.child_by_field_name("consequence")
            if cons:
                self._visit_statement(cons, scope, file_id)
            alt = node.child_by_field_name("alternative")
            if alt:
                self._visit_statement(alt, scope, file_id)

        elif t in ("for_statement", "for_in_statement", "while_statement"):
            for child in node.children:
                if child.type in ("assignment_expression", "variable_declaration", "lexical_declaration"):
                    self._visit_statement(child, scope, file_id)
                elif child.is_named and child.type not in ("statement_block",):
                    self._visit_expression(child, scope, file_id)
            body = node.child_by_field_name("body")
            if body:
                self._visit_statement(body, scope, file_id)

        elif t == "statement_block":
            for child in node.children:
                if child.is_named and child.type != "comment":
                    self._visit_statement(child, scope, file_id)

        elif t == "throw_statement":
            for child in node.children:
                if child.is_named:
                    self._visit_expression(child, scope, file_id)

        elif t == "try_statement":
            body = node.child_by_field_name("body")
            if body:
                self._visit_statement(body, scope, file_id)
            handler = node.child_by_field_name("handler")
            if handler:
                handler_body = handler.child_by_field_name("body")
                if handler_body:
                    self._visit_statement(handler_body, scope, file_id)

        else:
            # 递归处理未识别的命名子节点
            for child in node.children:
                if child.is_named and child.type not in ("comment",):
                    self._visit_expression(child, scope, file_id)

    def _visit_function_declaration(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        name_node = node.child_by_field_name("name")
        func_name = get_identifier_text(name_node) if name_node else "<anonymous>"
        func_scope = (*scope, func_name)
        func_id = self._node_id(*func_scope)

        self._add_node(func_name, "Function", scope, node)
        self._add_edge("defines", file_id if not scope else self._node_id(*scope), func_id, node)

        # 形参
        params_node = node.child_by_field_name("parameters")
        if params_node:
            for param in params_node.children:
                if param.type in ("identifier", "shorthand_property_identifier"):
                    self._add_node(ts_node_text(param), "Variable", func_scope, param, is_param=1)

        # 函数体
        body = node.child_by_field_name("body")
        if body:
            self._visit_statement(body, func_scope, file_id)

    def _visit_class_declaration(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        name_node = node.child_by_field_name("name")
        class_name = get_identifier_text(name_node) if name_node else "<anonymous>"
        cls_scope = (*scope, class_name)
        cls_id = self._node_id(*cls_scope)

        self._add_node(class_name, "Class", scope, node)
        self._add_edge("defines", file_id if not scope else self._node_id(*scope), cls_id, node)

        # 基类
        heritage = node.child_by_field_name("heritage")
        if heritage:
            self._visit_expression(heritage, scope, file_id)

        # 类体
        body = node.child_by_field_name("body")
        if body:
            for child in body.children:
                if child.type == "method_definition":
                    self._visit_method_definition(child, cls_scope, file_id)
                elif child.type == "field_definition":
                    self._visit_field_definition(child, cls_scope, file_id)

    def _visit_method_definition(
        self, node: ts.Node, cls_scope: tuple[str, ...], file_id: str
    ) -> None:
        name_node = node.child_by_field_name("name")
        method_name = get_identifier_text(name_node) if name_node else "<anonymous>"
        method_scope = (*cls_scope, method_name)

        self._add_node(method_name, "Function", cls_scope, node)
        cls_id = self._node_id(*cls_scope)
        method_id = self._node_id(*method_scope)
        self._add_edge("defines", cls_id, method_id, node)

        # 形参
        params_node = node.child_by_field_name("parameters")
        if params_node:
            for param in params_node.children:
                if param.type in ("identifier", "shorthand_property_identifier"):
                    self._add_node(ts_node_text(param), "Variable", method_scope, param, is_param=1)

        # 方法体
        body = node.child_by_field_name("body")
        if body:
            self._visit_statement(body, method_scope, file_id)

    def _visit_field_definition(
        self, node: ts.Node, cls_scope: tuple[str, ...], file_id: str
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node and name_node.type == "property_identifier":
            field_name = ts_node_text(name_node)
            self._add_node(field_name, "Variable", cls_scope, node)
            cls_id = self._node_id(*cls_scope)
            self._add_edge("defines", cls_id, self._node_id(*cls_scope, field_name), node)
        value = node.child_by_field_name("value")
        if value:
            self._visit_expression(value, cls_scope, file_id)

    def _visit_variable_declaration(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        scope_id = self._node_id(*scope) if scope else file_id

        for declarator in node.children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            value_node = declarator.child_by_field_name("value")

            if name_node and name_node.type == "identifier":
                var_name = ts_node_text(name_node)
                self._add_node(var_name, "Variable", scope, name_node)
                self._add_edge("assigns", scope_id, self._node_id(*scope, var_name), node)

                # 检测 CommonJS require(): var x = require('module') 或 var x = require('mod').sub
                if value_node:
                    # 递归查找 require 调用（可能在 member_expression 内）
                    require_call = None
                    check_node = value_node
                    for _ in range(3):  # 最多 3 层嵌套
                        if check_node.type == "call_expression":
                            func_n = check_node.child_by_field_name("function")
                            if func_n and func_n.type == "identifier" and ts_node_text(func_n) == "require":
                                require_call = check_node
                                break
                        if check_node.child_count > 0:
                            found = False
                            for child in check_node.children:
                                if child.type in ("call_expression", "member_expression"):
                                    check_node = child
                                    found = True
                                    break
                            if not found:
                                break
                        else:
                            break

                    if require_call:
                        args_node = require_call.child_by_field_name("arguments")
                        if args_node:
                            for arg in args_node.children:
                                if arg.type == "string":
                                    module_name = ts_node_text(arg).strip("'\"")
                                    ext_id = f"#external:{module_name}"
                                    self._add_edge("imports", file_id, ext_id, node)
                                    continue

                # 处理函数表达式 / 箭头函数
                if value_node and value_node.type in ("function_expression", "arrow_function"):
                    func_scope = (*scope, var_name)
                    params_node = value_node.child_by_field_name("parameters")
                    if params_node:
                        for param in params_node.children:
                            if param.type in ("identifier", "shorthand_property_identifier"):
                                self._add_node(ts_node_text(param), "Variable", func_scope, param, is_param=1)
                    body = value_node.child_by_field_name("body")
                    if body:
                        self._visit_statement(body, func_scope, file_id)

                elif value_node:
                    self._visit_expression(value_node, scope, file_id)

            elif name_node and name_node.type == "array_pattern":
                # 解构: const [a, b] = ...
                for child in name_node.children:
                    if child.type == "identifier":
                        var_name = ts_node_text(child)
                        self._add_node(var_name, "Variable", scope, child)
                        self._add_edge("assigns", scope_id, self._node_id(*scope, var_name), node)
                if value_node:
                    self._visit_expression(value_node, scope, file_id)

            elif name_node and name_node.type == "object_pattern":
                # 解构: const { a, b } = ...
                for child in name_node.children:
                    if child.type == "shorthand_property_identifier":
                        var_name = ts_node_text(child)
                        self._add_node(var_name, "Variable", scope, child)
                        self._add_edge("assigns", scope_id, self._node_id(*scope, var_name), node)
                if value_node:
                    self._visit_expression(value_node, scope, file_id)

    def _visit_import(self, node: ts.Node, scope: tuple[str, ...], file_id: str) -> None:
        """处理 import 语句: import foo from 'module'; import { a, b } from 'module'。"""
        source_node = None
        for child in node.children:
            if child.type == "string":
                source_node = child
                break

        module_name = ts_node_text(source_node).strip("'\"") if source_node else ""

        # 处理 import 的绑定
        for child in node.children:
            if child.type == "import_clause":
                for ic in child.children:
                    if ic.type == "identifier":
                        # default import
                        local_name = ts_node_text(ic)
                        self._add_node(local_name, "Variable", scope, ic)
                        ext_id = f"#external:{module_name}.{local_name}"
                        self._add_edge("imports", file_id, ext_id, node)
                        self._add_edge("assigns", file_id, self._node_id(*scope, local_name), node)
                    elif ic.type == "named_imports":
                        for ni in ic.children:
                            if ni.type == "import_specifier":
                                name_n = ni.child_by_field_name("name")
                                if name_n:
                                    local_name = ts_node_text(name_n)
                                    self._add_node(local_name, "Variable", scope, name_n)
                                    ext_id = f"#external:{module_name}.{local_name}"
                                    self._add_edge("imports", file_id, ext_id, node)
                                    self._add_edge("assigns", file_id, self._node_id(*scope, local_name), node)
                    elif ic.type == "namespace_import":
                        # import * as foo from 'module'
                        for ni in ic.children:
                            if ni.type == "identifier":
                                local_name = ts_node_text(ni)
                                self._add_node(local_name, "Variable", scope, ni)
                                ext_id = f"#external:{module_name}"
                                self._add_edge("imports", file_id, ext_id, node)
                                self._add_edge("assigns", file_id, self._node_id(*scope, local_name), node)

    def _visit_return(self, node: ts.Node, scope: tuple[str, ...], file_id: str) -> None:
        scope_id = self._node_id(*scope) if scope else file_id
        for child in node.children:
            if child.is_named and child.type != "comment":
                self._visit_expression(child, scope, file_id)
                # 为返回的标识符创建 returns 边
                if child.type == "identifier":
                    var_name = ts_node_text(child)
                    var_id = self._resolve_name(var_name, scope)
                    if var_id:
                        self._add_edge("returns", scope_id, var_id, node)

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
            if not var_id:
                # 前向引用：定义可能在后面，先查文件级 scope
                var_id = self._resolve_name(var_name, ())
            if not var_id:
                # 前向引用：节点尚未创建，用文件级 node_id 占位
                var_id = self._node_id(var_name)
            if var_id:
                self._add_edge("reads", scope_id, var_id, node)

        elif t == "call_expression":
            # 函数调用
            func_node = node.child_by_field_name("function")
            self._visit_expression(func_node, scope, file_id)

            func_name = self._extract_callable_name(func_node)
            if func_name:
                callee_id = self._resolve_name(func_name, scope)
                if not callee_id:
                    callee_id = self._resolve_name(func_name, ())
                if callee_id:
                    args_meta = self._extract_call_args(node, scope)
                    self._add_edge("calls", scope_id, callee_id, node, metadata=args_meta)
                else:
                    ext_id = f"#external:{func_name}"
                    args_meta = self._extract_call_args(node, scope)
                    args_meta["unknown"] = True
                    self._add_edge("calls", scope_id, ext_id, node, metadata=args_meta)

            # 实参
            args_node = node.child_by_field_name("arguments")
            if args_node:
                for arg in args_node.children:
                    if arg.is_named:
                        self._visit_expression(arg, scope, file_id)

        elif t == "member_expression":
            # 属性访问 a.b
            obj = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            self._visit_expression(obj, scope, file_id)
            if obj and obj.type == "identifier":
                owner_id = self._resolve_name(ts_node_text(obj), scope)
                if owner_id:
                    self._add_edge("reads", scope_id, owner_id, node)

        elif t == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")

            # 检测 obj.method = function 模式（JS 最常见的原型方法挂载）
            handled = False
            if left and left.type == "member_expression":
                obj_node = left.child_by_field_name("object")
                prop_node = left.child_by_field_name("property")
                if obj_node and prop_node:
                    obj_name = ts_node_text(obj_node) if obj_node.type == "identifier" else ""
                    method_name = ts_node_text(prop_node)

                    if obj_name and method_name and right and right.type in ("function_expression", "arrow_function"):
                        # obj.method = function() {} → 创建 Function 节点
                        # owner_scope 决定方法的命名空间，避免与同名模块级变量冲突
                        owner_scope = scope
                        # 检查 obj_name 是否是当前 scope 的最后一个元素
                        if scope and scope[-1] == obj_name:
                            owner_scope = scope
                        else:
                            # 检查 obj_name 是否已定义为节点
                            owner_id = self._resolve_name(obj_name, scope)
                            if owner_id:
                                owner_node = self.nodes[self._node_index.get(owner_id, -1)] if owner_id in self._node_index else None
                                if owner_node and owner_node.kind == "Class":
                                    # obj 是 Class → 方法归属到 Class scope
                                    owner_scope = scope
                                else:
                                    # obj 是 Variable（如 res/app）→ 用 obj 名作为 scope 层
                                    # 避免方法 ID 与同名模块级变量冲突（如 var send = require('send') vs res.send = function）
                                    owner_scope = (*scope, obj_name)
                            else:
                                # 创建一个以 obj_name 为 scope 的新层
                                owner_scope = (*scope, obj_name)
                                self._add_node(obj_name, "Variable", scope, obj_node)

                        method_scope = (*owner_scope, method_name)
                        self._add_node(method_name, "Function", owner_scope, node)
                        scope_id_owner = self._node_id(*owner_scope) if owner_scope else file_id
                        self._add_edge("defines", scope_id_owner, self._node_id(*method_scope), node)

                        # 提取形参
                        params_node = right.child_by_field_name("parameters")
                        if params_node:
                            for param in params_node.children:
                                if param.type in ("identifier", "shorthand_property_identifier"):
                                    self._add_node(ts_node_text(param), "Variable", method_scope, param, is_param=1)

                        # 提取函数体
                        body = right.child_by_field_name("body")
                        if body:
                            if body.type == "statement_block":
                                self._visit_statement(body, method_scope, file_id)
                            else:
                                self._visit_expression(body, method_scope, file_id)

                        handled = True

                    elif obj_name == "module" and method_name == "exports" and right:
                        # module.exports = X → 为 X 创建 reads 边（导出符号有入边）
                        if right.type == "identifier":
                            exported_name = ts_node_text(right)
                            exported_id = self._resolve_name(exported_name, scope)
                            if not exported_id:
                                # 前向引用：定义可能在后面
                                exported_id = self._node_id(*scope, exported_name)
                            if exported_id:
                                self._add_edge("reads", scope_id, exported_id, node)
                        elif right.type == "call_expression":
                            # module.exports = require('module') → 创建 import 边
                            func_n = right.child_by_field_name("function")
                            if func_n and func_n.type == "identifier" and ts_node_text(func_n) == "require":
                                args_node = right.child_by_field_name("arguments")
                                if args_node:
                                    for arg in args_node.children:
                                        if arg.type == "string":
                                            module_name = ts_node_text(arg).strip("'\"")
                                            ext_id = f"#external:{module_name}"
                                            self._add_edge("imports", file_id, ext_id, node)
                        elif right.type == "function_expression":
                            # module.exports = function() {} → 直接作为函数定义
                            func_scope = (*scope, "exports")
                            self._add_node("exports", "Function", scope, node)
                            self._add_edge("defines", scope_id, self._node_id(*func_scope), node)
                            params_node = right.child_by_field_name("parameters")
                            if params_node:
                                for param in params_node.children:
                                    if param.type in ("identifier", "shorthand_property_identifier"):
                                        self._add_node(ts_node_text(param), "Variable", func_scope, param, is_param=1)
                            body = right.child_by_field_name("body")
                            if body:
                                if body.type == "statement_block":
                                    self._visit_statement(body, func_scope, file_id)
                                else:
                                    self._visit_expression(body, func_scope, file_id)
                        else:
                            self._visit_expression(right, scope, file_id)
                        handled = True

                    elif obj_name == "exports" and method_name and right:
                        # exports.X = Y → 为 Y 创建 reads 边（导出符号有入边）
                        if right.type == "identifier":
                            exported_name = ts_node_text(right)
                            exported_id = self._resolve_name(exported_name, scope)
                            if exported_id:
                                self._add_edge("reads", scope_id, exported_id, node)
                        elif right.type in ("function_expression", "arrow_function"):
                            # exports.X = function() {} → 创建 Function 节点
                            func_scope = (*scope, method_name)
                            self._add_node(method_name, "Function", scope, node)
                            self._add_edge("defines", scope_id, self._node_id(*func_scope), node)
                            params_node = right.child_by_field_name("parameters")
                            if params_node:
                                for param in params_node.children:
                                    if param.type in ("identifier", "shorthand_property_identifier"):
                                        self._add_node(ts_node_text(param), "Variable", func_scope, param, is_param=1)
                            body = right.child_by_field_name("body")
                            if body:
                                if body.type == "statement_block":
                                    self._visit_statement(body, func_scope, file_id)
                                else:
                                    self._visit_expression(body, func_scope, file_id)
                        else:
                            self._visit_expression(right, scope, file_id)
                        handled = True

                    elif obj_name and method_name and right:
                        # obj.prop = value (非函数) → writes 边
                        owner_id = self._resolve_name(obj_name, scope)
                        if owner_id:
                            self._add_edge("writes", scope_id, owner_id, node)
                        self._visit_expression(right, scope, file_id)
                        handled = True

                    # 检测 exports = X (链式赋值 exports = module.exports = X)
                    if not handled and left and left.type == "identifier":
                        left_name = ts_node_text(left)
                        if left_name == "exports" and right:
                            # 递归处理右侧（可能是嵌套的 module.exports = X）
                            if right.type == "identifier":
                                exported_name = ts_node_text(right)
                                exported_id = self._resolve_name(exported_name, scope)
                                if not exported_id:
                                    # 可能是前向引用（定义在后面），直接构造 node_id
                                    exported_id = self._node_id(*scope, exported_name)
                                if exported_id:
                                    self._add_edge("reads", scope_id, exported_id, node)
                                handled = True
                            elif right.type == "assignment_expression":
                                # 嵌套赋值：exports = module.exports = X
                                inner_right = right.child_by_field_name("right")
                                if inner_right and inner_right.type == "identifier":
                                    exported_name = ts_node_text(inner_right)
                                    exported_id = self._resolve_name(exported_name, scope)
                                    if not exported_id:
                                        exported_id = self._node_id(*scope, exported_name)
                                    if exported_id:
                                        self._add_edge("reads", scope_id, exported_id, node)
                                # 也递归处理内层
                                self._visit_expression(right, scope, file_id)
                                handled = True

            if not handled:
                if left and left.type == "identifier":
                    var_name = ts_node_text(left)
                    self._add_node(var_name, "Variable", scope, left)
                    self._add_edge("assigns", scope_id, self._node_id(*scope, var_name), node)
                elif left:
                    self._visit_expression(left, scope, file_id)
                if right:
                    self._visit_expression(right, scope, file_id)

        elif t == "binary_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            self._visit_expression(left, scope, file_id)
            self._visit_expression(right, scope, file_id)

        elif t == "unary_expression":
            arg = node.child_by_field_name("argument")
            self._visit_expression(arg, scope, file_id)

        elif t == "conditional_expression":
            self._visit_expression(node.child_by_field_name("condition"), scope, file_id)
            self._visit_expression(node.child_by_field_name("consequence"), scope, file_id)
            self._visit_expression(node.child_by_field_name("alternative"), scope, file_id)

        elif t == "array":
            for child in node.children:
                if child.is_named:
                    self._visit_expression(child, scope, file_id)

        elif t == "object":
            for child in node.children:
                if child.is_named and child.type == "pair":
                    val = child.child_by_field_name("value")
                    self._visit_expression(val, scope, file_id)

        elif t == "template_string":
            for child in node.children:
                if child.type == "template_substitution" and child.is_named:
                    self._visit_expression(child.child_by_field_name("value") or child.children[0] if child.child_count > 0 else None, scope, file_id)

        elif t == "new_expression":
            # new ClassName(args)
            ctor = node.child_by_field_name("constructor")
            self._visit_expression(ctor, scope, file_id)
            func_name = self._extract_callable_name(ctor)
            if func_name:
                callee_id = self._resolve_name(func_name, scope) or self._resolve_name(func_name, ())
                if callee_id:
                    self._add_edge("calls", scope_id, callee_id, node)
                else:
                    ext_id = f"#external:{func_name}"
                    self._add_edge("calls", scope_id, ext_id, node, metadata={"unknown": True})
            args_node = node.child_by_field_name("arguments")
            if args_node:
                for arg in args_node.children:
                    if arg.is_named:
                        self._visit_expression(arg, scope, file_id)

        elif t == "arrow_function":
            # 箭头函数赋值给变量时已在 variable_declaration 中处理
            params_node = node.child_by_field_name("parameters")
            if params_node:
                for param in params_node.children:
                    if param.type in ("identifier",):
                        self._add_node(ts_node_text(param), "Variable", scope, param, is_param=1)
            body = node.child_by_field_name("body")
            if body:
                if body.type == "statement_block":
                    self._visit_statement(body, scope, file_id)
                else:
                    self._visit_expression(body, scope, file_id)

        elif t == "subscript_expression":
            obj = node.child_by_field_name("object")
            self._visit_expression(obj, scope, file_id)
            index = node.child_by_field_name("index")
            self._visit_expression(index, scope, file_id)

        elif t == "await_expression":
            # await 表达式的子节点是位置子节点（无命名字段）
            for child in node.children:
                if child.is_named and child.type != "await":
                    self._visit_expression(child, scope, file_id)

        elif t == "sequence_expression":
            for child in node.children:
                if child.is_named:
                    self._visit_expression(child, scope, file_id)

        else:
            # 递归处理未识别的命名子节点
            for child in node.children:
                if child.is_named and child.type not in ("comment",):
                    self._visit_expression(child, scope, file_id)

    def _extract_callable_name(self, func_node: ts.Node | None) -> str | None:
        """从调用表达式提取被调函数名。"""
        if func_node is None:
            return None
        if func_node.type == "identifier":
            return ts_node_text(func_node)
        elif func_node.type == "member_expression":
            # a.b() → a.b
            obj = func_node.child_by_field_name("object")
            prop = func_node.child_by_field_name("property")
            if obj and prop:
                obj_name = ts_node_text(obj) if obj.type == "identifier" else self._extract_callable_name(obj)
                prop_name = ts_node_text(prop)
                if obj_name:
                    # .bind() / .call() / .apply() → 返回原始函数名（间接引用）
                    if prop_name in ("bind", "call", "apply"):
                        return obj_name
                    return f"{obj_name}.{prop_name}"
        return None

    def _extract_call_args(self, call_node: ts.Node, scope: tuple[str, ...]) -> dict[str, Any]:
        """提取实参信息。"""
        arg_infos: list[dict[str, Any]] = []
        args_node = call_node.child_by_field_name("arguments")
        if args_node:
            for i, arg in enumerate(args_node.children):
                if not arg.is_named:
                    continue
                if arg.type == "identifier":
                    arg_infos.append({"index": i, "name": ts_node_text(arg), "kind": "name"})
                elif arg.type == "member_expression":
                    obj = arg.child_by_field_name("object")
                    root_name = ts_node_text(obj) if obj and obj.type == "identifier" else ""
                    arg_infos.append({"index": i, "name": root_name, "kind": "attr"})
                else:
                    arg_infos.append({"index": i, "name": "", "kind": "other"})
        return {"args": arg_infos}
