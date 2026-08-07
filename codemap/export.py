"""图谱导出 —— 将 SQLite 图谱导出为 GraphML / DOT 格式。

用于可视化（Gephi、yEd、Graphviz 等）或外部工具消费。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from codemap.store import Store


def export_graphml(store: Store) -> str:
    """导出 GraphML 格式。

    节点属性: id, kind, name, scope
    边属性: edge_type, code
    """
    # 构建 XML（GraphML 使用命名空间）
    ns = {
        "graphml": "http://graphml.graphdrawing.org/xmlns",
        "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    }
    root = ET.Element(
        "graphml",
        {
            "xmlns": ns["graphml"],
            "xmlns:xsi": ns["xsi"],
        },
    )

    # 定义 key
    ET.SubElement(root, "key", id="kind", for_="node", attr_name="kind", attr_type="string")
    ET.SubElement(root, "key", id="name", for_="node", attr_name="name", attr_type="string")
    ET.SubElement(root, "key", id="scope", for_="node", attr_name="scope", attr_type="string")
    ET.SubElement(root, "key", id="edge_type", for_="edge", attr_name="edge_type", attr_type="string")

    graph = ET.SubElement(root, "graph", id="G", edgedefault="directed")

    # 节点
    nodes = store.conn.execute(
        "SELECT id, name, kind, scope FROM nodes WHERE kind != 'External'"
    ).fetchall()
    for n in nodes:
        node_el = ET.SubElement(graph, "node", id=n["id"])
        for key, val in (("kind", n["kind"]), ("name", n["name"]), ("scope", n["scope"])):
            if val:
                data = ET.SubElement(node_el, "data", key=key)
                data.text = str(val)

    # 边
    edges = store.conn.execute(
        "SELECT from_node, to_node, edge_type FROM edges WHERE edge_type IN ('calls', 'reads', 'imports', 'defines')"
    ).fetchall()
    for i, e in enumerate(edges):
        edge_el = ET.SubElement(graph, "edge", id=f"e{i}", source=e["from_node"], target=e["to_node"])
        data = ET.SubElement(edge_el, "data", key="edge_type")
        data.text = e["edge_type"]

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def export_dot(store: Store, include_external: bool = False) -> str:
    """导出 Graphviz DOT 格式。

    节点: 方框，按 kind 着色
    边: 有向，按 edge_type 着色
    """
    kind_colors = {
        "Function": "#4CAF50",
        "Class": "#2196F3",
        "Variable": "#FFC107",
        "File": "#9E9E9E",
        "TypeAlias": "#9C27B0",
        "External": "#FF5722",
    }
    edge_colors = {
        "calls": "#E91E63",
        "reads": "#3F51B5",
        "imports": "#009688",
        "defines": "#795548",
    }

    lines: list[str] = []
    lines.append("digraph codemap {")
    lines.append("  rankdir=LR;")
    lines.append("  node [shape=box, style=filled, fontname=\"Arial\"];")

    # 节点
    if include_external:
        nodes = store.conn.execute("SELECT id, name, kind, scope FROM nodes").fetchall()
    else:
        nodes = store.conn.execute(
            "SELECT id, name, kind, scope FROM nodes WHERE kind != 'External'"
        ).fetchall()
    for n in nodes:
        nid = n["id"].replace('"', '\\"')
        color = kind_colors.get(n["kind"], "#FFFFFF")
        label = n["name"] if n["name"] else nid
        lines.append(f'  "{nid}" [label="{label}", fillcolor="{color}"];')

    # 边
    edges = store.conn.execute(
        "SELECT from_node, to_node, edge_type FROM edges WHERE edge_type IN ('calls', 'reads', 'imports', 'defines')"
    ).fetchall()
    for e in edges:
        src = e["from_node"].replace('"', '\\"')
        dst = e["to_node"].replace('"', '\\"')
        color = edge_colors.get(e["edge_type"], "#999999")
        lines.append(f'  "{src}" -> "{dst}" [color="{color}", label="{e["edge_type"]}"];')

    lines.append("}")
    return "\n".join(lines)


def export(store: Store, fmt: str, include_external: bool = False) -> str:
    """导出图谱为指定格式。"""
    if fmt == "graphml":
        return export_graphml(store)
    elif fmt == "dot":
        return export_dot(store, include_external=include_external)
    raise ValueError(f"不支持的导出格式: {fmt}（支持 graphml / dot）")
