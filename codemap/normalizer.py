"""规范化器 —— 将 Extractor 的原始事实清洗、去重、补全后注入 Store。

职责：
  1. 去重（同一 node/edge 多处提取的合并）
  2. 补全缺失的节点（如未定义的函数引用）
  3. 生成稳定的 scope 映射表，供 resolver 使用
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from codemap.models import Node, Edge, Transform, NodeKind


class Normalizer:
    """将 Extractor 输出的原始 node/edge/transform 列表清洗整理。"""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.transforms: list[dict[str, Any]] = []

    def process(
        self,
        raw_nodes: list[Node],
        raw_edges: list[Edge],
        raw_transforms: list[Transform],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """清洗并返回可直接写入 Store 的 dict 列表。"""
        self._merge_nodes(raw_nodes)
        self._deduplicate_edges(raw_edges)
        self._fill_missing_variables()
        self._deduplicate_transforms(raw_transforms)

        return (
            list(self.nodes.values()),
            self.edges,
            self.transforms,
        )

    def _merge_nodes(self, raw_nodes: list[Node]) -> None:
        """去重：同名同 scope 的 Variable 只保留一个，取其最早 definition + all annotations."""
        for node in raw_nodes:
            key = node.id
            if key in self.nodes:
                existing = self.nodes[key]
                # 合并 type_annotation
                if node.type_annotation and not existing["type_annotation"]:
                    existing["type_annotation"] = node.type_annotation
                # 合并 is_param（任一为 1 则为 1）
                if node.is_param and not existing.get("is_param", 0):
                    existing["is_param"] = node.is_param
                # 保留更早的定义位置
                if node.location < existing["location"]:
                    existing["location"] = node.location
            else:
                self.nodes[key] = {
                    "id": node.id,
                    "kind": node.kind,
                    "name": node.name,
                    "location": node.location,
                    "end_location": node.end_location,
                    "scope": node.scope,
                    "type_annotation": node.type_annotation,
                    "source_hash": node.source_hash,
                    "is_param": node.is_param,
                }

    def _deduplicate_edges(self, raw_edges: list[Edge]) -> None:
        """去重：相同 (from, to, edge_type, location) 只保留一条。"""
        seen: dict[tuple[str, str, str, str], int] = {}  # key → index in self.edges
        for e in raw_edges:
            key = (e.from_node, e.to_node, e.edge_type, e.location)
            if key not in seen:
                seen[key] = len(self.edges)
                self.edges.append({
                    "id": e.id,
                    "edge_type": e.edge_type,
                    "from_node": e.from_node,
                    "to_node": e.to_node,
                    "location": e.location,
                    "code": e.code,
                    "metadata": e.metadata,
                })
            else:
                # 合并 metadata：O(1) 查找
                self.edges[seen[key]]["metadata"].update(e.metadata)

    def _deduplicate_transforms(self, raw_transforms: list[Transform]) -> None:
        """去重：相同 (op, location) 的 transform 只保留一条，合并 inputs/outputs。"""
        seen: set[tuple[str, str]] = set()
        for t in raw_transforms:
            key = (t.op, t.location)
            if key not in seen:
                seen.add(key)
                self.transforms.append({
                    "id": t.id,
                    "kind": t.kind,
                    "op": t.op,
                    "op_node": t.op_node,
                    "location": t.location,
                    "inputs": t.inputs,
                    "outputs": t.outputs,
                    "branch": t.branch,
                    "code": t.code,
                })
            else:
                # 合并 inputs/outputs
                for existing in self.transforms:
                    if existing["op"] == t.op and existing["location"] == t.location:
                        existing["inputs"] = list(set(existing["inputs"] + t.inputs))
                        existing["outputs"] = list(set(existing["outputs"] + t.outputs))
                        break

    def _fill_missing_variables(self) -> None:
        """为 edges 中引用但不在 nodes 中出现的外部符号/未定义变量创建占位节点。

        例如: from_node 或 to_node 引用的 node_id 在 nodes dict 中不存在。
        这种情况常见于:
          1. 跨文件调用 → resolver 会处理
          2. 外部模块符号 → 创建 External 节点
          3. 作用域边界外的符号 → 暂不处理（resolver 负责）
        """
        all_ids: set[str] = set(self.nodes.keys())

        for e in self.edges:
            for node_id in (e["from_node"], e["to_node"]):
                if node_id not in all_ids:
                    # 判断是不是 external 占位符
                    if node_id.startswith("#external:"):
                        kind: NodeKind = "External"
                        name = node_id[len("#external:"):]
                    else:
                        # 跨文件或多跳引用，暂不创建占位，resolver 会处理
                        continue
                    self.nodes[node_id] = {
                        "id": node_id,
                        "kind": kind,
                        "name": name,
                        "location": e["location"],
                        "end_location": "",
                        "scope": "",
                        "type_annotation": "",
                        "source_hash": "",
                    }
                    all_ids.add(node_id)


def build_scope_index(nodes: dict[str, dict[str, Any]]) -> dict[tuple[str, str], str]:
    """构建 (name, scope_string) → node_id 的索引，用于 resolver 跨函数查找。"""
    index: dict[tuple[str, str], str] = {}
    for nid, nd in nodes.items():
        index[(nd["name"], nd["scope"])] = nid
    return index


def build_scope_hierarchy(nodes: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """构建 scope → [child_scope, ...] 的层次结构，用于作用域继承查找。"""
    hierarchy: dict[str, list[str]] = defaultdict(list)
    for nid, nd in nodes.items():
        if nd["scope"]:
            parent = nd["scope"].rsplit(":", 1)[0]
            if parent and parent != nd["scope"]:
                hierarchy[parent].append(nd["scope"])
    return dict(hierarchy)