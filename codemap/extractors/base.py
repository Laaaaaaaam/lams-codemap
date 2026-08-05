"""提取器协议 —— 所有语言提取器实现此接口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from codemap.models import Node, Edge, Transform


@runtime_checkable
class BaseExtractor(Protocol):
    """提取器接口：从源文件提取图事实。

    每种语言实现此接口，build.py 按文件扩展名选择对应提取器。
    """

    def extract(self) -> tuple[list[Node], list[Edge], list[Transform]]:
        """解析源码并返回 (节点, 边, 变换)。

        Returns:
            nodes: 图中的节点列表。
            edges: 图中的边列表。
            transforms: 变换操作列表。
        """
        ...
