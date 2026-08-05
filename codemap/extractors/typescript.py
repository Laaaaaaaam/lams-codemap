"""TypeScript 提取器 —— 基于 tree-sitter 从 TS 源文件提取图事实。

继承 JavaScript 提取器，额外支持：
  - 类型注解 (type annotation)
  - interface 声明
  - type 别名
  - 泛型参数
  - 访问修饰符 (public/private/protected)
"""

from __future__ import annotations

from typing import Any

import tree_sitter as ts
import tree_sitter_typescript as tsts

from codemap.models import Node, Edge, Transform
from codemap.extractors.javascript import JavaScriptExtractor
from codemap.extractors.treesitter_utils import (
    ts_loc,
    ts_end_loc,
    ts_source,
    ts_node_text,
    get_identifier_text,
    find_child_by_type,
)


class TypeScriptExtractor(JavaScriptExtractor):
    """从 TypeScript 源文件提取图事实。"""

    def __init__(self, file_path: str, source_code: str) -> None:
        # 不调用 super().__init__ 因为需要用 TS 语言
        self.file = file_path
        self.source = source_code
        self.source_bytes = source_code.encode("utf-8", errors="replace")
        from hashlib import sha256
        self.source_hash = sha256(source_code.encode()).hexdigest()[:12]

        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self.transforms: list[Transform] = []

        self._seen_node_ids: set[str] = set()
        self._node_index: dict[str, int] = {}
        self._seen_edge_keys: set[tuple[str, str, str, str]] = set()
        self._counter: dict[str, int] = {}

        # 使用 TypeScript 语言（不是 TSX）
        self._lang = ts.Language(tsts.language_typescript())
        self._parser = ts.Parser(self._lang)
        self._tree: ts.Tree | None = None

    def _extract_ts_params(
        self, params_node: ts.Node, func_scope: tuple[str, ...]
    ) -> None:
        """提取 TypeScript 参数（带类型注解）。

        tree-sitter TypeScript 的参数节点:
          required_parameter: children=[identifier, type_annotation]
          optional_parameter: children=[identifier, type_annotation]
          但 child_by_field_name("name") 返回 None，需要手动查找。
        """
        for param in params_node.children:
            if not param.is_named:
                continue
            if param.type in ("required_parameter", "optional_parameter"):
                # 查找 identifier 子节点
                name_n = None
                ann = ""
                for child in param.children:
                    if child.type == "identifier":
                        name_n = child
                    elif child.type == "type_annotation":
                        ann = ts_source(self.source_bytes, child)
                if name_n:
                    self._add_node(
                        ts_node_text(name_n), "Variable", func_scope, name_n,
                        type_annotation=ann, is_param=1,
                    )
            elif param.type == "identifier":
                self._add_node(ts_node_text(param), "Variable", func_scope, param, is_param=1)
            elif param.type == "rest_pattern":
                # ...args: number[]
                for child in param.children:
                    if child.type == "identifier":
                        self._add_node(ts_node_text(child), "Variable", func_scope, child, is_param=1)

    def _visit_statement(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        t = node.type

        if t == "interface_declaration":
            self._visit_interface_declaration(node, scope, file_id)
        elif t == "type_alias_declaration":
            self._visit_type_alias(node, scope, file_id)
        elif t == "enum_declaration":
            self._visit_enum_declaration(node, scope, file_id)
        elif t == "abstract_class_declaration":
            # 与 class_declaration 处理一致
            self._visit_class_declaration(node, scope, file_id)
        else:
            super()._visit_statement(node, scope, file_id)

    def _visit_interface_declaration(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        """处理 interface 声明。"""
        name_node = node.child_by_field_name("name")
        iface_name = get_identifier_text(name_node) if name_node else "<anonymous>"
        iface_scope = (*scope, iface_name)

        self._add_node(iface_name, "Class", scope, node)  # interface 作为 Class 节点
        scope_id = self._node_id(*scope) if scope else file_id
        self._add_edge("defines", scope_id, self._node_id(*iface_scope), node)

        # 接口体中的属性签名
        body = node.child_by_field_name("body")
        if body:
            for child in body.children:
                if child.type == "property_signature":
                    name_n = child.child_by_field_name("name")
                    if name_n:
                        prop_name = ts_node_text(name_n)
                        self._add_node(prop_name, "Variable", iface_scope, name_n)
                        self._add_edge(
                            "defines",
                            self._node_id(*iface_scope),
                            self._node_id(*iface_scope, prop_name),
                            child,
                        )
                elif child.type == "method_signature":
                    name_n = child.child_by_field_name("name")
                    if name_n:
                        method_name = ts_node_text(name_n)
                        self._add_node(method_name, "Function", iface_scope, name_n)
                        self._add_edge(
                            "defines",
                            self._node_id(*iface_scope),
                            self._node_id(*iface_scope, method_name),
                            child,
                        )

    def _visit_type_alias(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        """处理 type 别名: type Foo = { ... }。"""
        name_node = node.child_by_field_name("name")
        if name_node:
            type_name = ts_node_text(name_node)
            type_ann = ""
            value = node.child_by_field_name("value")
            if value:
                type_ann = ts_source(self.source_bytes, value)
            self._add_node(type_name, "Class", scope, node, type_annotation=type_ann)
            scope_id = self._node_id(*scope) if scope else file_id
            self._add_edge("defines", scope_id, self._node_id(*scope, type_name), node)

    def _visit_enum_declaration(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        """处理 enum 声明。"""
        name_node = node.child_by_field_name("name")
        enum_name = ts_node_text(name_node) if name_node else "<anonymous>"
        enum_scope = (*scope, enum_name)

        self._add_node(enum_name, "Class", scope, node)
        scope_id = self._node_id(*scope) if scope else file_id
        self._add_edge("defines", scope_id, self._node_id(*enum_scope), node)

        body = node.child_by_field_name("body")
        if body:
            for child in body.children:
                if child.type == "enum_assignment":
                    name_n = child.child_by_field_name("name")
                    if name_n:
                        member_name = ts_node_text(name_n)
                        self._add_node(member_name, "Variable", enum_scope, name_n)
                        self._add_edge(
                            "defines",
                            self._node_id(*enum_scope),
                            self._node_id(*enum_scope, member_name),
                            child,
                        )

    def _visit_function_declaration(
        self, node: ts.Node, scope: tuple[str, ...], file_id: str
    ) -> None:
        """重写以支持 TypeScript 类型注解。"""
        name_node = node.child_by_field_name("name")
        func_name = get_identifier_text(name_node) if name_node else "<anonymous>"
        func_scope = (*scope, func_name)
        func_id = self._node_id(*func_scope)

        # 返回类型注解
        ret_type = ""
        ret_type_node = node.child_by_field_name("return_type")
        if ret_type_node:
            ret_type = ts_source(self.source_bytes, ret_type_node)

        self._add_node(func_name, "Function", scope, node, type_annotation=ret_type)
        scope_id = self._node_id(*scope) if scope else file_id
        self._add_edge("defines", scope_id, func_id, node)

        # 形参（TypeScript 参数有类型注解）
        params_node = node.child_by_field_name("parameters")
        if params_node:
            self._extract_ts_params(params_node, func_scope)

        # 函数体
        body = node.child_by_field_name("body")
        if body:
            self._visit_statement(body, func_scope, file_id)

    def _visit_method_definition(
        self, node: ts.Node, cls_scope: tuple[str, ...], file_id: str
    ) -> None:
        """重写以支持 TypeScript 方法类型注解。"""
        name_node = node.child_by_field_name("name")
        method_name = get_identifier_text(name_node) if name_node else "<anonymous>"
        method_scope = (*cls_scope, method_name)

        ret_type = ""
        ret_type_node = node.child_by_field_name("return_type")
        if ret_type_node:
            ret_type = ts_source(self.source_bytes, ret_type_node)

        self._add_node(method_name, "Function", cls_scope, node, type_annotation=ret_type)
        cls_id = self._node_id(*cls_scope)
        method_id = self._node_id(*method_scope)
        self._add_edge("defines", cls_id, method_id, node)

        params_node = node.child_by_field_name("parameters")
        if params_node:
            self._extract_ts_params(params_node, method_scope)

        body = node.child_by_field_name("body")
        if body:
            self._visit_statement(body, method_scope, file_id)
