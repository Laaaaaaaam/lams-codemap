"""提取器包 —— 多语言 AST 提取器。

按文件扩展名选择对应语言的提取器：
  .py → PythonExtractor
  .js → JavaScriptExtractor
  .ts → TypeScriptExtractor
  .go → GoExtractor
"""

from __future__ import annotations

import os
from typing import Any

from codemap.extractors.base import BaseExtractor
from codemap.extractors.python import PythonExtractor


# 语言 → 文件扩展名映射
_LANG_EXTENSIONS: dict[str, list[str]] = {
    "python": [".py"],
    "javascript": [".js", ".jsx", ".mjs", ".cjs"],
    "typescript": [".ts", ".tsx"],
    "go": [".go"],
}

# 扩展名 → 语言 反向映射
_EXT_TO_LANG: dict[str, str] = {}
for _lang, _exts in _LANG_EXTENSIONS.items():
    for _ext in _exts:
        _EXT_TO_LANG[_ext] = _lang


def detect_language(file_path: str) -> str | None:
    """根据文件扩展名检测语言。

    Returns:
        语言名（"python" / "javascript" / "typescript" / "go"），或 None。
    """
    _, ext = os.path.splitext(file_path)
    return _EXT_TO_LANG.get(ext.lower())


def get_extractor(file_path: str, source_code: str) -> BaseExtractor | None:
    """根据文件路径选择合适的提取器。

    Args:
        file_path: 文件相对路径（用于 ID 生成和位置记录）。
        source_code: 文件源码。

    Returns:
        对应语言的提取器实例，或 None（不支持的语言）。
    """
    lang = detect_language(file_path)
    if lang is None:
        return None

    if lang == "python":
        return PythonExtractor(file_path, source_code)

    # 延迟导入，避免未安装 tree-sitter 时影响 Python 提取
    try:
        if lang == "javascript":
            from codemap.extractors.javascript import JavaScriptExtractor
            return JavaScriptExtractor(file_path, source_code)
        elif lang == "typescript":
            from codemap.extractors.typescript import TypeScriptExtractor
            return TypeScriptExtractor(file_path, source_code)
        elif lang == "go":
            from codemap.extractors.go import GoExtractor
            return GoExtractor(file_path, source_code)
    except ImportError:
        return None

    return None


def supported_extensions() -> list[str]:
    """返回所有支持的文件扩展名。"""
    return list(_EXT_TO_LANG.keys())


__all__ = [
    "BaseExtractor",
    "PythonExtractor",
    "detect_language",
    "get_extractor",
    "supported_extensions",
]
