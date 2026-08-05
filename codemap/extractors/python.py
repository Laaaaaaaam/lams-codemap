"""AST 提取器 —— 从 Python 源文件提取原始图事实。

约定：
  - Node.id 格式: 文件路径#符号路径
    模块/文件:  file.py#
    模块级函数:  file.py#func
    模块级类:    file.py#ClassName
    模块级变量:  file.py#var_name
    函数内变量:  file.py#func.var_name  (scope = file.py:func)
    函数形参:    file.py#func.param_name
    类方法:      file.py#ClassName.method
    方法内变量:  file.py#ClassName.method.var_name

  - 只提取 AST 确定性能看到的事实。动态分发、eval 等记录在 metadata.unknown。
"""

from __future__ import annotations

import ast
import hashlib
from typing import Any

from codemap.models import (
    Node,
    Edge,
    Transform,
)


def _hash_source(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:12]


def _loc(file: str, node: ast.AST) -> str:
    """格式: file:line:col"""
    return f"{file}:{node.lineno}:{node.col_offset}"


def _end_loc(file: str, node: ast.AST) -> str:
    """格式: file:line:col (结束位置)"""
    el = getattr(node, "end_lineno", node.lineno)
    ec = getattr(node, "end_col_offset", node.col_offset + 4)
    return f"{file}:{el}:{ec}"


def _get_source_segment(source: str, node: ast.AST) -> str:
    """获取 AST 节点对应的源码片段。"""
    try:
        seg = ast.get_source_segment(source, node)
    except Exception:
        seg = ""
    return seg or ""


def _get_full_statement(source: str, node: ast.AST) -> str:
    """获取节点所在完整语句源码（向上找到 stmt 级别）。"""
    current = node
    while current:
        if isinstance(
            current,
            (
                ast.stmt,
                ast.Module,
                ast.ExceptHandler,
            ),
        ):
            break
        current = getattr(current, "parent", None)  # type: ignore[assignment]
    if current is None:
        current = node
    return _get_source_segment(source, current)


def _ast_to_source(source: str, node: ast.AST) -> str:
    """获取节点所在完整语句源码（向上找到 stmt 级别）。"""
    # 优先取完整语句，因为 agent 需要上下文
    stmt_source = _get_full_statement(source, node)
    return stmt_source or _get_source_segment(source, node)


class PythonExtractor:
    """为单个 Python 文件提取所有图事实。

    实现 BaseExtractor 协议。"""

    def __init__(self, file_path: str, source_code: str) -> None:
        self.file = file_path
        self.source = source_code
        self.source_hash = _hash_source(source_code)

        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self.transforms: list[Transform] = []

        # 已创建节点 ID 集合，防止重复
        self._seen_node_ids: set[str] = set()
        # node_id → 在 self.nodes 中的索引，O(1) 查找
        self._node_index: dict[str, int] = {}
        # 已创建边 key 集合，防止重复
        self._seen_edge_keys: set[tuple[str, str, str, str]] = set()

        # 计数器，用于生成稳定 ID
        self._counter: dict[str, int] = {}

    def _gen_id(self, prefix: str) -> str:
        self._counter[prefix] = self._counter.get(prefix, 0) + 1
        # 包含文件路径前缀，保证跨文件全局唯一
        safe_file = self.file.replace("/", "_").replace("\\", "_").replace(".", "_")
        return f"{safe_file}_{prefix}_{self._counter[prefix]:04d}"

    # ── Scope stack helpers ──────────────────────────────

    def _node_id(self, *parts: str) -> str:
        """构建节点 ID: 文件路径#part1.part2..."""
        return f"{self.file}#{'.'.join(parts)}"

    def _scope_str(self, *parts: str) -> str:
        """构建 scope: 文件路径:part1.part2..."""
        if not parts:
            return self.file
        return f"{self.file}:{'.'.join(parts)}"

    # ── Main entry ───────────────────────────────────────

    def extract(self) -> tuple[list[Node], list[Edge], list[Transform]]:
        """解析源码并返回提取的 (节点, 边, 变换)。"""
        try:
            tree = ast.parse(self.source)
        except SyntaxError:
            # 语法错误：只记录文件节点
            self._add_file_node()
            return self.nodes, self.edges, self.transforms

        # 给每个 AST 节点挂 parent 引用，便于上溯
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child.parent = parent  # type: ignore[attr-defined]

        # 文件节点
        self._add_file_node()

        # 递归遍历
        self._visit_module(tree)

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
        return n

    def _add_node(
        self,
        name: str,
        kind: str,
        scope_parts: tuple[str, ...],
        node: ast.AST,
        type_annotation: str = "",
        is_param: int = 0,
    ) -> Node:
        nid = self._node_id(*scope_parts, name)
        # 去重：如果已存在，合并 type_annotation 后返回
        if nid in self._seen_node_ids:
            idx = self._node_index.get(nid)
            if idx is not None:
                n = self.nodes[idx]
                # 合并 type_annotation（新值优先）
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
            location=_loc(self.file, node),
            end_location=_end_loc(self.file, node),
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
        node: ast.AST,
        code: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Edge | None:
        loc = _loc(self.file, node)
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
            code=code or _ast_to_source(self.source, node),
            metadata=metadata or {},
        )
        self.edges.append(e)
        return e

    def _add_transform(
        self,
        kind: str,
        op: str,
        node: ast.AST,
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
            location=_loc(self.file, node),
            inputs=inputs,
            outputs=outputs,
            branch=branch,
            code=_ast_to_source(self.source, node),
        )
        self.transforms.append(t)
        return t

    # ── Visitors ──────────────────────────────────────────

    def _visit_module(self, tree: ast.Module) -> None:
        file_id = self._node_id()

        # 第一 pass: 只收集顶层定义（函数/类/变量名），不展开函数体
        func_stmts: list[tuple[ast.FunctionDef, tuple[str, ...]]] = []
        class_stmts: list[tuple[ast.ClassDef, tuple[str, ...]]] = []
        non_top_body: list[ast.stmt] = []  # assign / import 等非函数体语句
        
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._add_node(stmt.name, "Function", (), stmt)
                self._add_edge("defines", file_id, self._node_id(stmt.name), stmt)
                func_stmts.append((stmt, ()))
            elif isinstance(stmt, ast.ClassDef):
                self._add_node(stmt.name, "Class", (), stmt)
                self._add_edge("defines", file_id, self._node_id(stmt.name), stmt)
                class_stmts.append((stmt, ()))
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    for var_name in self._extract_names(target):
                        self._add_node(var_name, "Variable", (), target)
                        self._add_edge("defines", file_id, self._node_id(var_name), stmt)
                non_top_body.append(stmt)
            elif isinstance(stmt, ast.Import):
                self._visit_import(stmt, ())
            elif isinstance(stmt, ast.ImportFrom):
                self._visit_import_from(stmt, ())
            elif isinstance(stmt, ast.AnnAssign):
                if stmt.target and isinstance(stmt.target, ast.Name):
                    self._add_node(stmt.target.id, "Variable", (), stmt.target)
                    self._add_edge("defines", file_id, self._node_id(stmt.target.id), stmt)
                non_top_body.append(stmt)
            else:
                non_top_body.append(stmt)

        # 第二 pass: 处理非函数体语句的 RHS（变量赋值表达式）
        for stmt in non_top_body:
            if isinstance(stmt, ast.Assign):
                self._visit_expr(stmt.value, ())
                for target in stmt.targets:
                    outputs = [self._node_id(n) for n in self._extract_names(target)]
                    inputs = self._expr_inputs(stmt.value)
                    if inputs or isinstance(stmt.value, ast.Call):
                        self._handle_rhs_transform(stmt.value, stmt, inputs, outputs)

        # 第三 pass: 展开函数/类体（此时所有顶层符号已就绪）
        for func_stmt, scope in func_stmts:
            self._visit_func(func_stmt, scope)
        for class_stmt, scope in class_stmts:
            self._visit_class(class_stmt, scope)

    def _visit_func(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, parent_scope: tuple[str, ...]
    ) -> None:
        func_scope = (*parent_scope, node.name)
        func_id = self._node_id(*func_scope)

        # 函数节点（带返回类型注解）
        ret_ann = ""
        if node.returns:
            ret_ann = _get_source_segment(self.source, node.returns)
        self._add_node(node.name, "Function", parent_scope, node, type_annotation=ret_ann)

        # 形参：补全 posonlyargs / args / kwonlyargs / vararg / kwarg
        all_params = (
            list(node.args.posonlyargs) +
            list(node.args.args) +
            list(node.args.kwonlyargs)
        )
        if node.args.vararg:
            all_params.append(node.args.vararg)
        if node.args.kwarg:
            all_params.append(node.args.kwarg)

        for arg in all_params:
            ann = ""
            if arg.annotation:
                ann = _get_source_segment(self.source, arg.annotation)
            self._add_node(arg.arg, "Variable", func_scope, arg, type_annotation=ann, is_param=1)

        # 装饰器 → decorator reads + decorates 边
        for dec in node.decorator_list:
            # 提取装饰器名称
            dec_name = ""
            if isinstance(dec, ast.Name):
                dec_name = dec.id
            elif isinstance(dec, ast.Attribute):
                dec_name = _ast_to_source(self.source, dec)
            elif isinstance(dec, ast.Call):
                if isinstance(dec.func, ast.Name):
                    dec_name = dec.func.id
                elif isinstance(dec.func, ast.Attribute):
                    dec_name = _ast_to_source(self.source, dec.func)
            if dec_name:
                self._add_edge(
                    "decorates", func_id, func_id, node,
                    metadata={"decorator": dec_name},
                )
            self._visit_expr(dec, parent_scope)

        # 函数体
        for stmt in node.body:
            self._visit_stmt(stmt, func_scope)

    def _visit_class(self, node: ast.ClassDef, parent_scope: tuple[str, ...]) -> None:
        cls_scope = (*parent_scope, node.name)

        # 类节点
        self._add_node(node.name, "Class", parent_scope, node)

        # 基类引用
        for base in node.bases:
            self._visit_expr(base, parent_scope)

        # 类体
        cls_id = self._node_id(*cls_scope)
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._visit_func(stmt, cls_scope)
                self._add_edge("defines", cls_id, self._node_id(*cls_scope, stmt.name), stmt)
            elif isinstance(stmt, ast.ClassDef):
                self._visit_class(stmt, cls_scope)
                self._add_edge("defines", cls_id, self._node_id(*cls_scope, stmt.name), stmt)
            elif isinstance(stmt, ast.Assign):
                # 类变量
                for target in stmt.targets:
                    for var_name in self._extract_names(target):
                        self._add_node(var_name, "Variable", cls_scope, target)
                        self._add_edge("defines", cls_id, self._node_id(*cls_scope, var_name), stmt)
                self._visit_expr(stmt.value, cls_scope)
            else:
                self._visit_stmt(stmt, cls_scope)

    def _visit_stmt(self, stmt: ast.stmt, scope: tuple[str, ...]) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._visit_func(stmt, scope)
        elif isinstance(stmt, ast.ClassDef):
            self._visit_class(stmt, scope)
        elif isinstance(stmt, ast.Return):
            scope_id = self._node_id(*scope)
            if stmt.value:
                self._visit_expr(stmt.value, scope)
                # returns 边: enclosing function → returned variables
                for var_name in self._extract_names(stmt.value):
                    ret_id = self._node_id(*scope, var_name)
                    self._add_edge("returns", scope_id, ret_id, stmt)
                # Transform: return → outputs
                outputs = [
                    self._node_id(*scope, n)
                    for n in self._extract_names(stmt.value)
                ]
                inputs = self._expr_inputs(stmt.value)
                if (inputs or outputs) and isinstance(stmt.value, (ast.Call, ast.BinOp, ast.UnaryOp)):
                    self._add_transform(
                        "return",
                        "return",
                        stmt,
                        inputs,
                        outputs,
                    )
        elif isinstance(stmt, ast.Assign):
            self._visit_assign(stmt, scope)
        elif isinstance(stmt, ast.AnnAssign):
            self._visit_ann_assign(stmt, scope)
        elif isinstance(stmt, ast.AugAssign):
            self._visit_aug_assign(stmt, scope)
        elif isinstance(stmt, ast.Expr):
            self._visit_expr(stmt.value, scope)
        elif isinstance(stmt, (ast.If, ast.While)):
            # 条件分支
            branch = "if" if isinstance(stmt, ast.If) else "while"
            self._visit_expr(stmt.test, scope)
            for b in stmt.body:
                self._visit_stmt(b, scope)
            for b in stmt.orelse:
                self._visit_stmt(b, scope)
        elif isinstance(stmt, ast.For):
            self._visit_expr(stmt.iter, scope)
            # for 循环变量
            for var_name in self._extract_names(stmt.target):
                self._add_node(var_name, "Variable", scope, stmt.target)
                iter_inputs = self._expr_inputs(stmt.iter)
                self._add_transform(
                    "comprehension", "for", stmt,
                    inputs=iter_inputs,
                    outputs=[self._node_id(*scope, var_name)],
                )
            for b in stmt.body:
                self._visit_stmt(b, scope)
        elif isinstance(stmt, ast.With):
            for item in stmt.items:
                if item.optional_vars:
                    for var_name in self._extract_names(item.optional_vars):
                        self._add_node(var_name, "Variable", scope, item.optional_vars)
                self._visit_expr(item.context_expr, scope)
            for b in stmt.body:
                self._visit_stmt(b, scope)
        elif isinstance(stmt, ast.Try):
            for b in stmt.body:
                self._visit_stmt(b, scope)
            for h in stmt.handlers:
                if h.name:
                    self._add_node(h.name, "Variable", scope, h)
                for b in h.body:
                    self._visit_stmt(b, scope)
            for b in stmt.finalbody:
                self._visit_stmt(b, scope)
        elif isinstance(stmt, ast.Assert):
            self._visit_expr(stmt.test, scope)
        elif isinstance(stmt, ast.Raise):
            if stmt.exc:
                self._visit_expr(stmt.exc, scope)
        elif isinstance(stmt, ast.Global):
            for name in stmt.names:
                # global 声明：标记变量来自模块作用域
                self._add_node(name, "Variable", (), stmt)
        elif isinstance(stmt, ast.Import):
            self._visit_import(stmt, scope)
        elif isinstance(stmt, ast.ImportFrom):
            self._visit_import_from(stmt, scope)

    def _visit_assign(self, stmt: ast.Assign, scope: tuple[str, ...]) -> None:
        """处理赋值语句。"""
        scope_id = self._node_id(*scope)

        # RHS: 提取 input symbols
        inputs = self._expr_inputs(stmt.value)
        self._visit_expr(stmt.value, scope)

        # LHS: 创建 Variable 节点 + assigns 边
        for target in stmt.targets:
            for var_name in self._extract_names(target):
                var_id = self._node_id(*scope, var_name)
                # 新建变量（如果当前 scope 没有）
                self._add_node(var_name, "Variable", scope, target)
                self._add_edge("assigns", scope_id, var_id, stmt)

            # Transform: 如果有多个输出（解包）或 RHS 是 call/运算
            outputs = [self._node_id(*scope, n) for n in self._extract_names(target)]
            if len(outputs) > 1 or isinstance(stmt.value, (ast.Call, ast.BinOp, ast.UnaryOp)):
                self._handle_rhs_transform(stmt.value, stmt, inputs, outputs)

            # attr 访问在 LHS: state.buffer = x → writes state.buffer
            for attr_chain in self._extract_attr_chains(target):
                if len(attr_chain) > 1:
                    owner_id = self._resolve_name(attr_chain[0], scope)
                    if owner_id:
                        self._add_edge("writes", scope_id, owner_id, stmt)

    def _handle_rhs_transform(
        self,
        value: ast.expr,
        stmt: ast.stmt,
        inputs: list[str],
        outputs: list[str],
    ) -> None:
        """为 RHS 创建 transform 节点。"""
        if isinstance(value, ast.Call):
            op_name = _ast_to_source(self.source, value.func)
            # op_node 暂时留空，由 resolver 填充
            self._add_transform(
                "call", op_name, stmt,
                inputs=inputs, outputs=outputs,
            )
        elif isinstance(value, ast.BinOp):
            op_name = type(value.op).__name__
            self._add_transform(
                "operator", op_name, stmt,
                inputs=inputs, outputs=outputs,
            )
        elif isinstance(value, ast.UnaryOp):
            op_name = type(value.op).__name__
            self._add_transform(
                "operator", op_name, stmt,
                inputs=inputs, outputs=outputs,
            )
        elif isinstance(value, (ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
            self._add_transform(
                "comprehension", "comprehension", stmt,
                inputs=inputs, outputs=outputs,
            )

    def _visit_ann_assign(self, stmt: ast.AnnAssign, scope: tuple[str, ...]) -> None:
        scope_id = self._node_id(*scope)
        if stmt.value:
            self._visit_expr(stmt.value, scope)
        if stmt.target and isinstance(stmt.target, ast.Name):
            var_id = self._node_id(*scope, stmt.target.id)
            self._add_node(stmt.target.id, "Variable", scope, stmt.target)
            self._add_edge("assigns", scope_id, var_id, stmt)

    def _visit_aug_assign(self, stmt: ast.AugAssign, scope: tuple[str, ...]) -> None:
        scope_id = self._node_id(*scope)
        self._visit_expr(stmt.target, scope)
        self._visit_expr(stmt.value, scope)
        if isinstance(stmt.target, ast.Name):
            var_id = self._node_id(*scope, stmt.target.id)
            self._add_node(stmt.target.id, "Variable", scope, stmt.target)

    def _visit_import(self, stmt: ast.Import, scope: tuple[str, ...]) -> None:
        file_id = self._node_id()
        for alias in stmt.names:
            name = alias.asname or alias.name
            # 顶层模块作为 External
            ext_id = f"#external:{alias.name.split('.')[0]}"
            ext_node = Node(
                id=ext_id,
                kind="External",
                name=alias.name.split(".")[0],
                location=_loc(self.file, stmt),
                scope="",
            )
            self.nodes.append(ext_node)
            self._add_edge("imports", file_id, ext_id, stmt)

            # 如果用了 alias，创建本地 Variable
            if alias.asname:
                var_id = self._node_id(*scope, alias.asname)
                self._add_node(alias.asname, "Variable", scope, stmt)
                self._add_edge("assigns", file_id, var_id, stmt)

    def _visit_import_from(self, stmt: ast.ImportFrom, scope: tuple[str, ...]) -> None:
        file_id = self._node_id()
        module = stmt.module or ""
        for alias in stmt.names:
            name = alias.asname or alias.name
            full_name = f"{module}.{name}" if module else name
            if module:
                ext_id = f"#external:{module}.{name}"
            else:
                ext_id = f"#external:{name}"
            ext_node = Node(
                id=ext_id,
                kind="External",
                name=full_name,
                location=_loc(self.file, stmt),
                scope="",
            )
            self.nodes.append(ext_node)
            self._add_edge("imports", file_id, ext_id, stmt)

            # 本地符号
            local_name = alias.asname or alias.name
            local_id = self._node_id(*scope, local_name)
            self._add_node(local_name, "Variable", scope, stmt)
            self._add_edge("assigns", file_id, local_id, stmt)

    def _visit_expr(self, node: ast.expr, scope: tuple[str, ...]) -> None:
        """递归访问表达式，记录 reads / calls / attrs。"""
        scope_id = self._node_id(*scope)

        if isinstance(node, ast.Name):
            # 变量读取
            var_id = self._resolve_name(node.id, scope)
            if var_id:
                self._add_edge("reads", scope_id, var_id, node)

        elif isinstance(node, ast.Attribute):
            # 属性访问: a.b → attrs a → a.b, 标记 a 为 reads
            self._visit_expr(node.value, scope)
            # 只处理简单属性链: a.b
            if isinstance(node.value, ast.Name):
                owner_id = self._resolve_name(node.value.id, scope)
                if owner_id:
                    self._add_edge("reads", scope_id, owner_id, node)
            elif isinstance(node.value, ast.Attribute):
                # a.b.c: 递归处理
                # 提取完整链
                chain = self._extract_attr_chain(node)
                if len(chain) > 1:
                    owner_id = self._resolve_name(chain[0], scope)
                    if owner_id:
                        self._add_edge("reads", scope_id, owner_id, node)

        elif isinstance(node, ast.Call):
            # 函数调用
            self._visit_expr(node.func, scope)
            # calls 边: scope → called function
            func_name = self._extract_callable_name(node.func)
            if func_name:
                # 尝试在当前作用域内解析
                callee_id = self._resolve_name(func_name, scope)
                if not callee_id:
                    callee_id = self._resolve_name(func_name, ())
                if callee_id:
                    self._add_edge("calls", scope_id, callee_id, node, metadata=self._extract_call_args(node))
                else:
                    # 未解析的函数（外部或跨文件），创建调用边到占位符
                    ext_id = f"#external:{func_name}"
                    self._add_edge("calls", scope_id, ext_id, node, metadata={**self._extract_call_args(node), "unknown": True})

            # 实参: 记录 reads
            for i, arg in enumerate(node.args):
                self._visit_expr(arg, scope)

            # 关键字实参
            for kw in node.keywords:
                self._visit_expr(kw.value, scope)

        elif isinstance(node, ast.Subscript):
            self._visit_expr(node.value, scope)
            self._visit_expr(node.slice, scope)

        elif isinstance(node, ast.BinOp):
            self._visit_expr(node.left, scope)
            self._visit_expr(node.right, scope)

        elif isinstance(node, ast.UnaryOp):
            self._visit_expr(node.operand, scope)

        elif isinstance(node, ast.BoolOp):
            for v in node.values:
                self._visit_expr(v, scope)

        elif isinstance(node, ast.Compare):
            self._visit_expr(node.left, scope)
            for c in node.comparators:
                self._visit_expr(c, scope)

        elif isinstance(node, ast.IfExp):
            self._visit_expr(node.test, scope)
            self._visit_expr(node.body, scope)
            self._visit_expr(node.orelse, scope)

        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for elt in node.elts:
                self._visit_expr(elt, scope)

        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if k:
                    self._visit_expr(k, scope)
                self._visit_expr(v, scope)

        elif isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.FormattedValue):
                    self._visit_expr(v.value, scope)

        elif isinstance(node, ast.Lambda):
            # 创建匿名函数节点
            self._add_node("<lambda>", "Function", scope, node)
            for arg in node.args.args:
                self._add_node(arg.arg, "Variable", (*scope, "<lambda>"), arg)
            self._visit_expr(node.body, (*scope, "<lambda>"))

        elif isinstance(node, ast.ListComp):
            self._visit_expr(node.elt, scope)
            for gen in node.generators:
                self._visit_expr(gen.iter, scope)
                for if_clause in gen.ifs:
                    self._visit_expr(if_clause, scope)
                # 推导变量
                for var_name in self._extract_names(gen.target):
                    self._add_node(var_name, "Variable", scope, gen.target)

        elif isinstance(node, ast.SetComp):
            self._visit_expr(node.elt, scope)
            for gen in node.generators:
                self._visit_expr(gen.iter, scope)
                for if_clause in gen.ifs:
                    self._visit_expr(if_clause, scope)
                # 推导变量
                for var_name in self._extract_names(gen.target):
                    self._add_node(var_name, "Variable", scope, gen.target)

        elif isinstance(node, ast.GeneratorExp):
            self._visit_expr(node.elt, scope)
            for gen in node.generators:
                self._visit_expr(gen.iter, scope)
                for if_clause in gen.ifs:
                    self._visit_expr(if_clause, scope)
                # 推导变量
                for var_name in self._extract_names(gen.target):
                    self._add_node(var_name, "Variable", scope, gen.target)

        elif isinstance(node, ast.DictComp):
            self._visit_expr(node.key, scope)
            self._visit_expr(node.value, scope)
            for gen in node.generators:
                self._visit_expr(gen.iter, scope)
                for var_name in self._extract_names(gen.target):
                    self._add_node(var_name, "Variable", scope, gen.target)

        elif isinstance(node, ast.NamedExpr):
            self._visit_expr(node.target, scope)
            self._visit_expr(node.value, scope)

        elif isinstance(node, ast.Await):
            # await expr → 递归处理内部表达式（通常是 Call）
            self._visit_expr(node.value, scope)

        elif isinstance(node, ast.Starred):
            self._visit_expr(node.value, scope)

    def _extract_call_args(self, call_node: ast.Call) -> dict[str, Any]:
        """从 AST Call 节点提取实参信息，存入 calls 边的 metadata。

        返回 {"args": [{"index": int, "name": str, "kind": "name"|"attr"|"other"}, ...]}

        resolver 读取此信息创建 param-flow 边，不再反解析 code 字符串。
        """
        arg_infos: list[dict[str, Any]] = []
        for i, arg in enumerate(call_node.args):
            if isinstance(arg, ast.Name):
                arg_infos.append({"index": i, "name": arg.id, "kind": "name"})
            elif isinstance(arg, ast.Attribute):
                chain = self._extract_attr_chain(arg)
                root_name = chain[0] if chain else ""
                arg_infos.append({"index": i, "name": root_name, "kind": "attr", "full": ".".join(chain)})
            elif isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
                arg_infos.append({"index": i, "name": arg.value.id, "kind": "starred"})
            else:
                arg_infos.append({"index": i, "name": "", "kind": "other"})
        # 关键字实参
        for kw in call_node.keywords:
            if kw.arg and isinstance(kw.value, ast.Name):
                arg_infos.append({"index": -1, "name": kw.value.id, "kind": "kw", "kw": kw.arg})
            elif kw.arg:
                arg_infos.append({"index": -1, "name": "", "kind": "kw", "kw": kw.arg})
        return {"args": arg_infos}

    # ── Helpers ──────────────────────────────────────────

    def _extract_names(self, node: ast.expr) -> list[str]:
        """从赋值目标提取变量名列表。"""
        if isinstance(node, ast.Name):
            return [node.id]
        elif isinstance(node, (ast.Tuple, ast.List)):
            names: list[str] = []
            for elt in node.elts:
                if isinstance(elt, ast.Name):
                    names.append(elt.id)
                elif isinstance(elt, ast.Starred) and isinstance(elt.value, ast.Name):
                    names.append(elt.value.id)
            return names
        elif isinstance(node, ast.Attribute):
            # a.b = x → 只返回 "a"，表示写入的是 a 的属性
            if isinstance(node.value, ast.Name):
                return [node.value.id]
            return []
        elif isinstance(node, ast.Starred) and isinstance(node.value, ast.Name):
            return [node.value.id]
        elif isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name):
                return [node.value.id]
            return []
        return []

    def _extract_attr_chains(self, node: ast.expr) -> list[list[str]]:
        """提取属性链，如 a.b.c → [['a', 'b', 'c']]。"""
        chain: list[str] = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            chain.insert(0, current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            chain.insert(0, current.id)
        return [chain] if chain else []

    def _extract_attr_chain(self, node: ast.Attribute) -> list[str]:
        """提取属性链，如 a.b.c → ['a', 'b', 'c']。"""
        chain: list[str] = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            chain.insert(0, current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            chain.insert(0, current.id)
        return chain

    def _extract_callable_name(self, func: ast.expr) -> str | None:
        """从可调用表达式提取名字。"""
        if isinstance(func, ast.Name):
            return func.id
        elif isinstance(func, ast.Attribute):
            # a.b() → 尝试解析为 a.b
            chain = self._extract_attr_chain(func)
            return ".".join(chain) if chain else None
        return None

    def _resolve_name(self, name: str, scope: tuple[str, ...]) -> str | None:
        """按 Python 作用域规则解析符号名到 node ID。

        先查当前函数作用域，再查外层，最后查模块级。
        使用 _seen_node_ids set 进行 O(1) 查找。
        """
        # 当前及外层函数作用域
        for i in range(len(scope), 0, -1):
            scope_candidate = scope[:i]
            node_id = self._node_id(*scope_candidate, name)
            if node_id in self._seen_node_ids:
                return node_id
        # 模块级
        node_id = self._node_id(name)
        if node_id in self._seen_node_ids:
            return node_id
        return None

    def _expr_inputs(self, expr: ast.expr) -> list[str]:
        """递归提取表达式中的输入符号 ID（相对于模块作用域，由调用方提供 scope）。"""
        names: list[str] = []

        def _collect(e: ast.expr) -> None:
            if isinstance(e, ast.Name):
                names.append(e.id)
            elif isinstance(e, ast.Attribute):
                _collect(e.value)
            elif isinstance(e, ast.BinOp):
                _collect(e.left)
                _collect(e.right)
            elif isinstance(e, ast.UnaryOp):
                _collect(e.operand)
            elif isinstance(e, ast.Call):
                _collect(e.func)
                for a in e.args:
                    _collect(a)
                for kw in e.keywords:
                    _collect(kw.value)
            elif isinstance(e, ast.Subscript):
                _collect(e.value)
            elif isinstance(e, ast.BoolOp):
                for v in e.values:
                    _collect(v)
            elif isinstance(e, ast.Compare):
                _collect(e.left)
                for c in e.comparators:
                    _collect(c)

        _collect(expr)
        return names
