"""构建编排 —— 将提取、规范、解析串联为一次构建流程。

支持全量构建和增量构建（基于文件 hash）。
支持多语言：Python / JavaScript / TypeScript / Go。
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from codemap.store import Store
from codemap.extractors import get_extractor, supported_extensions, detect_language
from codemap.normalizer import Normalizer
from codemap.resolver import Resolver
from codemap.models import Node, Edge, Transform


def _hash_file(path: str) -> str:
    """计算文件内容的 SHA256 hash。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except (OSError, PermissionError):
        return ""
    return h.hexdigest()


def _is_supported_file(path: str) -> bool:
    """检查是否为支持的源文件。"""
    _, ext = os.path.splitext(path)
    return ext.lower() in supported_extensions()


def _collect_files(root: str, lang: str | None = None) -> list[str]:
    """递归收集目录下的所有源文件，排除常见的无关目录。

    Args:
        root: 项目根目录。
        lang: 限定语言（"python"/"javascript"/"typescript"/"go"），None 表示全部。
    """
    exclude_dirs = {
        ".git", ".svn", "__pycache__", ".mypy_cache",
        ".tox", ".venv", "venv", ".env", "env",
        "node_modules", "build", "dist", ".eggs",
        ".codemap", ".codex",
    }
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 排除无关目录
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs and not d.startswith(".")]
        for fname in filenames:
            if _is_supported_file(fname):
                if lang is not None:
                    detected = detect_language(fname)
                    if detected != lang:
                        continue
                files.append(os.path.join(dirpath, fname))
    return files


def _make_relative(file_path: str, root: str) -> str:
    """将绝对路径转为相对路径（用于存储）。"""
    try:
        return os.path.relpath(file_path, root).replace("\\", "/")
    except ValueError:
        return file_path.replace("\\", "/")


class BuildContext:
    """构建上下文，持有 store 引用并管理构建流程。"""

    def __init__(self, root: str, db_path: str) -> None:
        self.root = os.path.abspath(root)
        self.db_path = db_path
        self.store = Store(db_path)

    def build(self, full: bool = False, lang: str | None = None) -> dict[str, Any]:
        """执行构建。

        Args:
            full: True = 强制全量构建，False = 增量（比较文件 hash）。
            lang: 限定语言，None 表示自动检测全部支持的语言。

        Returns:
            构建统计信息。
        """
        files = _collect_files(self.root, lang=lang)
        if not files:
            return {"status": "empty", "files": 0}

        with self.store:
            try:
                previous_hashes = self.store.get_file_hashes() if not full else {}
                changed_files: list[str] = []

                # 计算所有文件的当前 hash
                current_hashes: dict[str, str] = {}
                for f in files:
                    rel = _make_relative(f, self.root)
                    current_hashes[rel] = _hash_file(f)

                if full:
                    changed_files = files
                    # 清空旧数据
                    for rel in previous_hashes:
                        self.store.clear_file(rel)
                    self.store.commit()
                else:
                    # 找出变更（新增/修改）的文件
                    for f in files:
                        rel = _make_relative(f, self.root)
                        if rel not in previous_hashes or previous_hashes[rel] != current_hashes[rel]:
                            changed_files.append(f)

                    # 删除已移除的文件
                    for rel in previous_hashes:
                        abs_path = os.path.join(self.root, rel)
                        if not os.path.exists(abs_path):
                            self.store.clear_file(rel)

                    # 清除变更文件的旧数据
                    for f in changed_files:
                        rel = _make_relative(f, self.root)
                        self.store.clear_file(rel)

                    self.store.commit()

                # 逐个文件提取
                all_nodes: list[Node] = []
                all_edges: list[Edge] = []
                all_transforms: list[Transform] = []

                for f in changed_files:
                    rel = _make_relative(f, self.root)
                    try:
                        with open(f, "r", encoding="utf-8") as fh:
                            source = fh.read()
                    except (OSError, UnicodeDecodeError):
                        continue

                    extractor = get_extractor(rel, source)
                    if extractor is None:
                        continue
                    nodes, edges, transforms = extractor.extract()
                    all_nodes.extend(nodes)
                    all_edges.extend(edges)
                    all_transforms.extend(transforms)

                    # 记录 hash
                    self.store.insert_file_hash(rel, current_hashes[rel])

                # 规范化：去重、补全
                normalizer = Normalizer()
                norm_nodes, norm_edges, norm_transforms = normalizer.process(
                    all_nodes, all_edges, all_transforms
                )

                # 写入 Store
                node_objects = [
                    Node(**n) for n in norm_nodes  # type: ignore[arg-type]
                ]
                edge_objects = [
                    Edge(**e) for e in norm_edges  # type: ignore[arg-type]
                ]
                self.store.insert_nodes(node_objects)
                self.store.insert_edges(edge_objects)

                # Transforms 存储
                from codemap.models import Transform as TModel
                transform_objects = [
                    TModel(**t) for t in norm_transforms  # type: ignore[arg-type]
                ]
                self.store.insert_transforms(transform_objects)

                self.store.commit()

                # 解析：跨文件引用
                resolver = Resolver(self.store)
                resolver.resolve()
                self.store.commit()

            except Exception:
                self.store.conn.rollback()
                raise

        stats = self.store.stats()
        stats["status"] = "ok"
        stats["changed_files"] = len(changed_files)
        stats["total_files"] = len(files)
        return stats


def build(root: str, db_path: str | None = None, full: bool = False, lang: str | None = None) -> dict[str, Any]:
    """快速构建入口。

    Args:
        root: 项目根目录。
        db_path: SQLite 数据库路径（默认 root/.codemap/codemap.db）。
        full: 是否强制全量。
        lang: 限定语言，None 表示自动检测全部。
    """
    if db_path is None:
        codemap_dir = os.path.join(root, ".codemap")
        os.makedirs(codemap_dir, exist_ok=True)
        db_path = os.path.join(codemap_dir, "codemap.db")

    ctx = BuildContext(root, db_path)
    return ctx.build(full=full, lang=lang)
