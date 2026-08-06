"""Codemap —— 多语言代码网络图谱工具（Python/JS/TS/Go）。

面向 Agent 消费，提供代码网络图谱的构建与查询。
"""

from codemap.models import (
    Node,
    Edge,
    Transform,
    Appearance,
    TraceResult,
    InfoResult,
    AtResult,
    SearchResult,
    FileResult,
    DeadResult,
    ImpactResult,
    NodeKind,
    EdgeKind,
    TransformKind,
)
from codemap.store import Store
from codemap.extractors.python import PythonExtractor as Extractor
from codemap.extractors.javascript import JavaScriptExtractor
from codemap.extractors.typescript import TypeScriptExtractor
from codemap.extractors.go import GoExtractor
from codemap.extractors import get_extractor, detect_language
from codemap.normalizer import Normalizer
from codemap.resolver import Resolver
from codemap.build import build
from codemap.query import Querier

__version__ = "0.4.0"
__all__ = [
    "Node",
    "Edge",
    "Transform",
    "Appearance",
    "TraceResult",
    "InfoResult",
    "AtResult",
    "SearchResult",
    "FileResult",
    "DeadResult",
    "ImpactResult",
    "NodeKind",
    "EdgeKind",
    "TransformKind",
    "Store",
    "Extractor",
    "PythonExtractor",
    "JavaScriptExtractor",
    "TypeScriptExtractor",
    "GoExtractor",
    "get_extractor",
    "detect_language",
    "Normalizer",
    "Resolver",
    "build",
    "Querier",
    "__version__",
]