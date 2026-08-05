"""Codemap 数据模型 —— 纯 dataclass，不依赖任何外部库。

所有模型都是 frozen dataclass，不可变、可哈希。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ─── 节点类型 ──────────────────────────────────────────────

NodeKind = Literal["File", "Function", "Class", "Variable", "External", "TypeAlias"]


@dataclass(frozen=True, slots=True)
class Node:
    """图中的节点。

    Attributes:
        id: 稳定 ID，格式 `文件路径#符号路径` 或 `文件路径#`（文件节点）。
        kind: 节点类型。
        name: 用户友好的名称（函数名、变量名等）。
        location: 定义位置，格式 `file:line:col`。
        end_location: 定义结束位置，格式 `file:line:col`。
        scope: 作用域，格式 `file` 或 `file:func` 或 `file:Class.method`。
        type_annotation: Python 类型注解原文。
        source_hash: 源码片段 hash（增量判断用）。
        is_param: 是否是函数形参（1=是，0=否）。
    """

    id: str
    kind: NodeKind
    name: str
    location: str          # file:line:col
    end_location: str = "" # file:line:col
    scope: str = ""        # file:func_path
    type_annotation: str = ""
    source_hash: str = ""
    is_param: int = 0


# ─── 边类型 ────────────────────────────────────────────────

EdgeKind = Literal[
    "defines",
    "imports",
    "calls",
    "returns",
    "assigns",
    "reads",
    "writes",
    "attrs",
    "decorates",
    "param-flow",
    "param-transform",
]


@dataclass(frozen=True, slots=True)
class Edge:
    """图中的边。

    Attributes:
        id: 稳定 ID。
        edge_type: 边类型。
        from_node: 起点节点 ID。
        to_node: 终点节点 ID。
        location: 边发生的位置 `file:line:col`。
        code: 触发这条边的源码行（完整语句）。
        metadata: 额外字典。
            - co_inputs: [{"id","name","location"}, ...]
            - arg_index: int
            - branch: if|else|elif|None
            - op: str
            - transform_kind: call|assign|unpack|attribute|subscript|operator|return|comprehension
            - unknown: bool
    """

    id: str
    edge_type: EdgeKind
    from_node: str        # Node.id
    to_node: str          # Node.id
    location: str         # file:line:col
    code: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ─── Transform 节点 ────────────────────────────────────────

TransformKind = Literal[
    "call", "assign", "unpack", "attribute",
    "subscript", "operator", "return", "comprehension",
]


@dataclass(frozen=True, slots=True)
class Transform:
    """一次变换操作（解决多对多关系）。

    不直接存储在 nodes 表，而是附加在 param-transform 边的 metadata 中。

    Attributes:
        id: 稳定 ID。
        kind: 变换类型。
        op: 操作描述（函数名/属性名/运算符）。
        op_node: 如果是调用，指向被调函数节点 ID。
        location: 源码位置。
        inputs: 输入符号 ID 列表（有序）。
        outputs: 输出符号 ID 列表（有序）。
        branch: 条件分支标记。
        code: 涉及的完整代码语句。
    """

    id: str
    kind: TransformKind
    op: str
    op_node: str = ""      # Node.id
    location: str = ""
    inputs: list[str] = field(default_factory=list)   # Node.id 列表
    outputs: list[str] = field(default_factory=list)  # Node.id 列表
    branch: str | None = None
    code: str = ""


# ─── 查询响应模型 ──────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Appearance:
    """一个符号出现点。

    Attributes:
        code: 源码行/语句。
        at: 位置 `file:line:col`。
        scope: 作用域。
    """

    code: str
    at: str
    scope: str


@dataclass(frozen=True, slots=True)
class TraceResult:
    """trace 查询结果（direct 档位）。

    Attributes:
        symbol: 被查询的符号名。
        appearances: 正向 appearance 列表（符号出现在哪）。
        depth_N: 第 N 层展开的 appearance 列表，键为 "depth_2", "depth_3" 等。
    """

    symbol: str
    appearances: list[Appearance] = field(default_factory=list)
    depth_layers: dict[str, list[Appearance]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InfoResult:
    """info 查询结果（节点详情）。"""

    symbol: str
    id: str
    kind: NodeKind
    location: str
    end_location: str
    scope: str
    params: list[dict[str, Any]] = field(default_factory=list)
    returns: list[dict[str, str]] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    docstring: str = ""


@dataclass(frozen=True, slots=True)
class AtResult:
    """at 查询结果（位置反查）。"""

    location: str
    code: str
    symbols: list[dict[str, str]] = field(default_factory=list)  # [{name, id, kind}]
    edges: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SearchResult:
    """search 查询结果（全文搜索）。"""

    query: str
    results: list[Appearance] = field(default_factory=list)
    match_type: str = "text"


@dataclass(frozen=True, slots=True)
class FileResult:
    """file 查询结果。"""

    file: str
    defines: list[dict[str, str]] = field(default_factory=list)  # [{symbol, kind, at}]
    imports: list[dict[str, str]] = field(default_factory=list)   # [{symbol, at}]
    imported_by: list[dict[str, Any]] = field(default_factory=list)  # [{file, symbols}]


@dataclass(frozen=True, slots=True)
class DeadResult:
    """dead 查询结果（死代码）。"""

    dead_symbols: list[dict[str, str]] = field(default_factory=list)  # [{symbol, at, kind, scope}]
    dead_chains: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ImpactResult:
    """impact 查询结果（影响面）。"""

    target: str
    affected: list[Appearance] = field(default_factory=list)
