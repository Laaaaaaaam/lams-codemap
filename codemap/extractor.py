"""向后兼容模块 —— 实际提取器已迁移到 codemap.extractors.python。

保留此文件以兼容现有代码中 `from codemap.extractor import Extractor` 的引用。
"""

from __future__ import annotations

from codemap.extractors.python import PythonExtractor as Extractor

__all__ = ["Extractor"]
