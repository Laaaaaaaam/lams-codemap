"""Codemap 核心功能测试套件。

覆盖：模型、提取器（Python/JS/Go）、构建、查询、解析器。
运行：pytest tests/test_basic.py -v
"""

from __future__ import annotations

import os
import tempfile

import pytest

from codemap.models import Node, Edge, Transform
from codemap.extractors.python import PythonExtractor
from codemap.extractors.javascript import JavaScriptExtractor
from codemap.extractors.go import GoExtractor
from codemap.extractors import detect_language, get_extractor
from codemap.build import build
from codemap.query import Querier
from codemap.store import Store


# ── Fixtures ────────────────────────────────────────────

@pytest.fixture
def tmp_repo(tmp_path):
    """创建临时项目目录。"""
    (tmp_path / "main.py").write_text(
        'def greet(name):\n    return f"Hello, {name}"\n\n'
        'class User:\n    def __init__(self, name):\n        self.name = name\n'
    )
    return tmp_path


@pytest.fixture
def built_repo(tmp_repo):
    """构建临时项目并返回 db 路径。"""
    result = build(str(tmp_repo), full=True)
    assert result["status"] == "ok"
    db_path = os.path.join(str(tmp_repo), ".codemap", "codemap.db")
    return db_path


# ── 模型测试 ────────────────────────────────────────────

class TestModels:
    def test_node_frozen(self):
        n = Node(id="a.py#foo", kind="Function", name="foo", location="a.py:1:0")
        with pytest.raises(AttributeError):
            n.name = "bar"  # frozen dataclass

    def test_edge_metadata_default(self):
        e = Edge(id="E1", edge_type="calls", from_node="a", to_node="b", location="a:1:0", code="foo()")
        assert e.metadata == {}

    def test_transform_lists(self):
        t = Transform(id="T1", kind="call", op="foo", inputs=["a"], outputs=["b"])
        assert t.inputs == ["a"]
        assert t.outputs == ["b"]


# ── 提取器测试 ──────────────────────────────────────────

class TestPythonExtractor:
    def test_extract_function(self):
        code = "def foo(a, b):\n    return a + b\n"
        ext = PythonExtractor("test.py", code)
        nodes, edges, _ = ext.extract()
        names = {n.name for n in nodes}
        assert "foo" in names
        assert "a" in names
        assert "b" in names

    def test_extract_class(self):
        code = "class Foo:\n    def bar(self):\n        pass\n"
        ext = PythonExtractor("test.py", code)
        nodes, edges, _ = ext.extract()
        kinds = {(n.name, n.kind) for n in nodes}
        assert ("Foo", "Class") in kinds

    def test_async_function(self):
        code = "async def fetch(url):\n    return await get(url)\n"
        ext = PythonExtractor("test.py", code)
        nodes, _, _ = ext.extract()
        assert any(n.name == "fetch" and n.kind == "Function" for n in nodes)

    def test_syntax_error(self):
        ext = PythonExtractor("bad.py", "def (\n")
        nodes, _, _ = ext.extract()
        assert any(n.kind == "File" for n in nodes)


class TestJavaScriptExtractor:
    def test_extract_function(self):
        code = "function foo(a, b) {\n return a + b;\n}\n"
        ext = JavaScriptExtractor("test.js", code)
        nodes, _, _ = ext.extract()
        assert any(n.name == "foo" and n.kind == "Function" for n in nodes)

    def test_require_import(self):
        code = 'var http = require("http");\n'
        ext = JavaScriptExtractor("test.js", code)
        _, edges, _ = ext.extract()
        assert any(e.edge_type == "imports" for e in edges)

    def test_module_exports(self):
        code = 'function foo() {}\nmodule.exports = foo;\n'
        ext = JavaScriptExtractor("test.js", code)
        _, edges, _ = ext.extract()
        assert any(e.edge_type == "reads" for e in edges)

    def test_obj_method_definition(self):
        code = "app.handle = function(req, res) { return res; };\n"
        ext = JavaScriptExtractor("test.js", code)
        nodes, _, _ = ext.extract()
        assert any(n.name == "handle" and n.kind == "Function" for n in nodes)


class TestGoExtractor:
    def test_extract_function(self):
        code = 'package main\n\nfunc foo(a int, b int) int {\n return a + b\n}\n'
        ext = GoExtractor("test.go", code)
        nodes, _, _ = ext.extract()
        assert any(n.name == "foo" and n.kind == "Function" for n in nodes)

    def test_extract_struct(self):
        code = 'package main\n\ntype User struct {\n Name string\n Age int\n}\n'
        ext = GoExtractor("test.go", code)
        nodes, _, _ = ext.extract()
        assert any(n.name == "User" and n.kind == "Class" for n in nodes)
        assert any(n.name == "Name" for n in nodes)

    def test_extract_interface(self):
        code = 'package main\n\ntype Handler interface {\n Serve() error\n}\n'
        ext = GoExtractor("test.go", code)
        nodes, _, _ = ext.extract()
        assert any(n.name == "Handler" and n.kind == "Class" for n in nodes)
        assert any(n.name == "Serve" for n in nodes)

    def test_extract_method(self):
        code = 'package main\n\ntype Server struct {}\n\nfunc (s *Server) Start() error {\n return nil\n}\n'
        ext = GoExtractor("test.go", code)
        nodes, _, _ = ext.extract()
        assert any(n.name == "Start" and n.kind == "Function" for n in nodes)

    def test_import_location(self):
        code = 'package main\n\nimport (\n "fmt"\n "os"\n)\n'
        ext = GoExtractor("test.go", code)
        _, edges, _ = ext.extract()
        import_edges = [e for e in edges if e.edge_type == "imports"]
        assert len(import_edges) == 2
        # 每个 import 应有不同位置
        locations = {e.location for e in import_edges}
        assert len(locations) == 2


# ── 语言检测测试 ────────────────────────────────────────

class TestLanguageDetection:
    def test_python(self):
        assert detect_language("foo.py") == "python"

    def test_javascript(self):
        assert detect_language("foo.js") == "javascript"

    def test_typescript(self):
        assert detect_language("foo.ts") == "typescript"

    def test_go(self):
        assert detect_language("foo.go") == "go"

    def test_unknown(self):
        assert detect_language("foo.txt") is None


# ── 构建测试 ────────────────────────────────────────────

class TestBuild:
    def test_build_python(self, tmp_repo):
        result = build(str(tmp_repo), full=True)
        assert result["status"] == "ok"
        assert result["total_files"] == 1
        assert result["nodes"] > 0

    def test_build_empty(self, tmp_path):
        (tmp_path / "readme.txt").write_text("hello")
        result = build(str(tmp_path), full=True)
        assert result["status"] == "empty"

    def test_incremental(self, tmp_repo):
        build(str(tmp_repo), full=True)
        result = build(str(tmp_repo), full=False)
        assert result["changed_files"] == 0


# ── 查询测试 ────────────────────────────────────────────

class TestQuery:
    def test_trace(self, built_repo):
        store = Store(built_repo)
        with store:
            q = Querier(store)
            result = q.trace("greet")
            assert len(result["appearances"]) >= 1

    def test_info(self, built_repo):
        store = Store(built_repo)
        with store:
            q = Querier(store)
            result = q.info("greet")
            assert result["found"] is True
            assert result["kind"] == "Function"

    def test_info_not_found(self, built_repo):
        store = Store(built_repo)
        with store:
            q = Querier(store)
            result = q.info("nonexistent")
            assert result["found"] is False

    def test_search(self, built_repo):
        store = Store(built_repo)
        with store:
            q = Querier(store)
            result = q.search("Hello")
            assert len(result["results"]) >= 1

    def test_file(self, built_repo):
        store = Store(built_repo)
        with store:
            q = Querier(store)
            result = q.file("main.py")
            assert "defines" in result
            assert len(result["defines"]) >= 2

    def test_dead(self, built_repo):
        store = Store(built_repo)
        with store:
            q = Querier(store)
            result = q.dead()
            assert "dead_symbols" in result

    def test_trace_role(self, built_repo):
        store = Store(built_repo)
        with store:
            q = Querier(store)
            result = q.trace("greet")
            for app in result["appearances"]:
                assert "role" in app

    def test_derive_role_definition(self):
        q = Querier.__new__(Querier)
        assert q._derive_role("def foo(): pass") == "definition"
        assert q._derive_role("class Foo: pass") == "definition"
        assert q._derive_role("func main() {}") == "definition"
        assert q._derive_role("function foo() {}") == "definition"

    def test_derive_role_call(self):
        q = Querier.__new__(Querier)
        assert q._derive_role("foo(bar)") == "call"

    def test_derive_role_reference(self):
        q = Querier.__new__(Querier)
        assert q._derive_role("bar = 1") == "reference"


# ── 公开 API 识别测试 ──────────────────────────────────

class TestPublicAPI:
    def test_all_export_reads(self, tmp_path):
        """__all__ 中的符号建立 reads 边。"""
        (tmp_path / "mymod.py").write_text(
            'def foo():\n    pass\n\n'
            'def bar():\n    pass\n\n'
            '__all__ = ("foo", "bar")\n'
        )
        build(str(tmp_path), full=True)
        store = Store(os.path.join(str(tmp_path), ".codemap", "codemap.db"))
        with store:
            # foo 应该有 reads 边（来自 __all__）
            rows = store.conn.execute(
                "SELECT COUNT(*) FROM edges WHERE edge_type='reads' AND to_node=?", ("mymod.py#foo",)
            ).fetchone()[0]
            assert rows >= 1

    def test_module_imported_medium(self, tmp_path):
        """模块被 import → 其顶层符号判为 medium 而非 high。"""
        (tmp_path / "utils.py").write_text(
            'def helper():\n    return 1\n'
        )
        (tmp_path / "main.py").write_text(
            'from .utils import helper\n'
            'print(helper())\n'
        )
        build(str(tmp_path), full=True)
        store = Store(os.path.join(str(tmp_path), ".codemap", "codemap.db"))
        with store:
            q = Querier(store)
            result = q.dead()
            # helper 被 main.py import 使用，不应出现在死代码中
            dead_scopes = {s["scope"] for s in result["dead_symbols"]}
            assert "utils.py" not in dead_scopes

    def test_unimported_module_high(self, tmp_path):
        """模块未被 import → 其顶层符号判为 high。"""
        (tmp_path / "solo.py").write_text(
            'def lonely():\n    return 1\n'
        )
        build(str(tmp_path), full=True)
        store = Store(os.path.join(str(tmp_path), ".codemap", "codemap.db"))
        with store:
            q = Querier(store)
            result = q.dead()
            # lonely 无任何引用且模块未被 import → high 死代码
            lonely = [s for s in result["dead_symbols"] if s["symbol"] == "lonely"]
            assert lonely and lonely[0]["confidence"] == "high"

    def test_go_same_package_resolution(self, tmp_path):
        """Go 同包跨文件函数解析（gin.go 与 *_test.go 同包）。"""
        (tmp_path / "server.go").write_text(
            'package main\n\nfunc New() *Server {\n\treturn &Server{}\n}\n'
        )
        (tmp_path / "server_test.go").write_text(
            'package main\n\nfunc TestNew() {\n\t_ = New()\n}\n'
        )
        build(str(tmp_path), full=True)
        store = Store(os.path.join(str(tmp_path), ".codemap", "codemap.db"))
        with store:
            # New 的调用应解析到 server.go#New（同包跨文件）
            rows = store.conn.execute(
                "SELECT COUNT(*) FROM edges WHERE edge_type='calls' AND to_node=?",
                ("server.go#New",),
            ).fetchone()[0]
            assert rows >= 1
