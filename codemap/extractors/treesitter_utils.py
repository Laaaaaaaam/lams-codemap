"""tree-sitter 通用工具 —— 为多语言提取器提供共享基础设施。

所有 tree-sitter 提取器共享的逻辑：
  - 节点位置格式化
  - 源码片段提取
  - 通用遍历框架
"""

from __future__ import annotations

from typing import Any

import tree_sitter as ts


def ts_loc(file_path: str, node: ts.Node) -> str:
    """格式: file:line:col（基于 0-indexed，转换为 1-indexed 行号）。"""
    return f"{file_path}:{node.start_point[0] + 1}:{node.start_point[1]}"


def ts_end_loc(file_path: str, node: ts.Node) -> str:
    """结束位置: file:line:col。"""
    return f"{file_path}:{node.end_point[0] + 1}:{node.end_point[1]}"


def ts_source(source: bytes, node: ts.Node) -> str:
    """获取节点对应的源码片段。"""
    try:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
    except Exception:
        return ""


def ts_node_text(node: ts.Node) -> str:
    """获取节点的纯文本内容。"""
    try:
        return node.text.decode("utf-8", errors="replace")
    except Exception:
        return ""


def find_child_by_type(node: ts.Node, node_type: str) -> ts.Node | None:
    """查找第一个指定类型的子节点。"""
    for child in node.children:
        if child.type == node_type:
            return child
    return None


def find_children_by_type(node: ts.Node, node_type: str) -> list[ts.Node]:
    """查找所有指定类型的子节点。"""
    return [child for child in node.children if child.type == node_type]


def find_descendants_by_type(node: ts.Node, node_type: str) -> list[ts.Node]:
    """查找所有指定类型的后代节点（递归）。"""
    result: list[ts.Node] = []
    def _walk(n: ts.Node) -> None:
        if n.type == node_type:
            result.append(n)
        for child in n.children:
            _walk(child)
    _walk(node)
    return result


def get_identifier_text(node: ts.Node | None) -> str:
    """从 identifier 节点提取文本。"""
    if node is None:
        return ""
    if node.type == "identifier" or node.type == "property_identifier":
        return ts_node_text(node)
    return ts_node_text(node)
