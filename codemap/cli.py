"""CLI 入口 —— codemap 命令行工具。

命令:
    build    构建图谱
    trace    追踪符号
    info     符号详情
    at       位置反查
    search   文本搜索
    file     文件查询
    dead     死代码
    impact   影响面
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import click

from codemap.build import build as do_build
from codemap.query import Querier
from codemap.store import Store


# ── helpers ──────────────────────────────────────────────

def _get_store() -> Store:
    """在当前目录下查找 .codemap/codemap.db。"""
    cwd = os.getcwd()
    db_path = os.path.join(cwd, ".codemap", "codemap.db")
    if not os.path.exists(db_path):
        click.echo("错误: 未找到图谱数据库，请先运行 `codemap build`。", err=True)
        sys.exit(1)
    return Store(db_path)


def _output_json(data: dict[str, Any]) -> None:
    """JSON 输出。"""
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _output_trace(result: dict[str, Any]) -> None:
    """人类可读的 trace 输出。"""
    symbol = result.get("symbol", "?")
    apps = result.get("appearances", [])
    layers = result.get("depth_layers", {})

    click.echo(f"追踪：{symbol}")
    click.echo("=" * 60)

    if not apps and not layers:
        click.echo("  (无结果)")
        return

    for app in apps:
        click.echo(f"  {app['at']}  [{app.get('scope', '')}]")
        click.echo(f"    {app['code']}")

    for layer_name, layer_apps in layers.items():
        click.echo(f"\n  {layer_name}:")
        for app in layer_apps:
            click.echo(f"    {app['at']}  [{app.get('scope', '')}]")
            click.echo(f"      {app['code']}")


def _output_info(result: dict[str, Any]) -> None:
    """人类可读的 info 输出。"""
    if not result.get("found"):
        click.echo(f"符号 {result['symbol']} 未找到")
        return

    click.echo(f"符号：{result['symbol']}")
    click.echo(f"类型：{result['kind']}")
    click.echo(f"ID：{result['id']}")
    click.echo(f"位置：{result['location']}")
    if result.get("scope"):
        click.echo(f"作用域：{result['scope']}")
    if result.get("type_annotation"):
        click.echo(f"类型注解：{result['type_annotation']}")

    params = result.get("params", [])
    if params:
        click.echo("参数：")
        for p in params:
            type_str = f": {p['type']}" if p.get("type") else ""
            click.echo(f"  {p['name']}{type_str}")

    returns = result.get("returns", [])
    if returns:
        click.echo("返回值：")
        for r in returns:
            click.echo(f"  {r['symbol']}: {r.get('type', '')}  (at {r['location']})")

    methods = result.get("methods", [])
    if methods:
        click.echo("方法：")
        for m in methods:
            click.echo(f"  {m['name']}  ({m['location']})")

    decorators = result.get("decorators", [])
    if decorators:
        click.echo("装饰器：")
        for d in decorators:
            click.echo(f"  @{d}")


# ── CLI group ────────────────────────────────────────────

@click.group()
def cli() -> None:
    """Codemap —— 多语言代码网络图谱工具（Python/JS/TS/Go）。"""
    pass


@cli.command()
@click.argument("root", type=click.Path(exists=True, file_okay=False))
@click.option("--full", is_flag=True, help="强制全量构建")
@click.option("--lang", default=None, help="限定语言 (python/javascript/typescript/go)")
def build(root: str, full: bool, lang: str | None) -> None:
    """构建代码图谱。

    ROOT: 项目根目录。

    自动检测文件语言（Python/JS/TS/Go），可用 --lang 限定。
    """
    click.echo(f"构建图谱: {root}" + (f" (语言: {lang})" if lang else ""))
    result = do_build(root, full=full, lang=lang)
    if result.get("status") == "empty":
        click.echo("  未找到源文件")
        return
    click.echo(f"  完成: {result.get('total_files', 0)} 文件, "
               f"变更 {result.get('changed_files', 0)}, "
               f"{result.get('nodes', 0)} 节点, "
               f"{result.get('edges', 0)} 边")


@cli.command()
@click.argument("symbol")
@click.option("--reverse", is_flag=True, help="反向追踪（从哪来）")
@click.option("--depth", "-n", type=int, default=1, help="展开层数 (默认 1)")
@click.option("--fuzzy", is_flag=True, help="模糊匹配")
@click.option("--scope", default=None, help="限定作用域")
@click.option("--kind", default=None, help="过滤出现类型 (definition,call,reference,import)")
@click.option("--limit", "-l", type=int, default=50, help="最大返回条数 (默认 50)")
@click.option("--json", "as_json", is_flag=True, help="JSON 输出")
def trace(
    symbol: str,
    reverse: bool,
    depth: int,
    fuzzy: bool,
    scope: str | None,
    kind: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """追踪符号的出现位置。

    SYMBOL: 符号名。支持 .attr 前缀表示属性访问。
    """
    store = _get_store()
    querier = Querier(store)
    result = querier.trace(
        symbol,
        reverse=reverse,
        depth=depth,
        fuzzy=fuzzy,
        scope=scope,
        limit=limit,
        kind_filter=kind,
    )
    if as_json:
        _output_json(result)
    else:
        _output_trace(result)


@cli.command()
@click.argument("symbol")
@click.option("--scope", default=None, help="限定作用域")
@click.option("--json", "as_json", is_flag=True, help="JSON 输出")
def info(symbol: str, scope: str | None, as_json: bool) -> None:
    """查看符号的定义详情。"""
    store = _get_store()
    querier = Querier(store)
    result = querier.info(symbol, scope=scope)
    if as_json:
        _output_json(result)
    else:
        _output_info(result)


@cli.command()
@click.argument("location")
@click.option("--json", "as_json", is_flag=True, help="JSON 输出")
def at(location: str, as_json: bool) -> None:
    """位置反查：这行代码在图谱里有什么。

    LOCATION: file:line[:col] 格式。
    """
    store = _get_store()
    querier = Querier(store)
    result = querier.at(location)
    if as_json:
        _output_json(result)
    else:
        if "error" in result:
            click.echo(f"错误: {result['error']}")
            return
        click.echo(f"位置: {result['location']}")
        click.echo(f"代码: {result['code']}")
        if result["symbols"]:
            click.echo("符号:")
            for s in result["symbols"]:
                click.echo(f"  {s['name']} ({s['kind']})")
        if result["edges"]:
            click.echo("边:")
            for e in result["edges"]:
                click.echo(f"  {e['edge_type']}: {e['from']} → {e['to']}")


@cli.command()
@click.argument("text")
@click.option("--file", "file_filter", default=None, help="限定文件路径前缀")
@click.option("--limit", "-l", type=int, default=50, help="最大返回条数 (默认 50)")
@click.option("--json", "as_json", is_flag=True, help="JSON 输出")
def search(text: str, file_filter: str | None, limit: int, as_json: bool) -> None:
    """全文搜索代码原文。"""
    store = _get_store()
    querier = Querier(store)
    result = querier.search(text, file_filter=file_filter, limit=limit)
    if as_json:
        _output_json(result)
    else:
        click.echo(f"搜索: {result['query']} ({len(result['results'])} 结果)")
        if result.get("truncated"):
            click.echo(f"  (共 {result['total']} 条，已截断为 {limit})")
        for r in result["results"]:
            click.echo(f"  {r['at']}")
            click.echo(f"    {r['code']}")


@cli.command()
@click.argument("path")
@click.option("--json", "as_json", is_flag=True, help="JSON 输出")
def file(path: str, as_json: bool) -> None:
    """文件级查询。"""
    store = _get_store()
    querier = Querier(store)
    result = querier.file(path)
    if as_json:
        _output_json(result)
    else:
        if "error" in result:
            click.echo(f"错误: {result['error']}")
            return
        click.echo(f"文件: {result['file']}")
        click.echo(f"\n定义了:")
        for d in result.get("defines", []):
            click.echo(f"  {d['symbol']} ({d['kind']}) — {d['at']}")
        if result.get("imports"):
            click.echo(f"\n引入了:")
            for i in result["imports"]:
                click.echo(f"  {i['symbol']} — {i['at']}")
        if result.get("imported_by"):
            click.echo(f"\n被引入于:")
            for ib in result["imported_by"]:
                syms = ", ".join(ib["symbols"])
                click.echo(f"  {ib['file']}: {syms}")


@cli.command()
@click.option("--scope", default=None, help="限定目录/作用域")
@click.option("--json", "as_json", is_flag=True, help="JSON 输出")
def dead(scope: str | None, as_json: bool) -> None:
    """查找死代码（零入边的函数/变量/类）。"""
    store = _get_store()
    querier = Querier(store)
    result = querier.dead(scope=scope)
    if as_json:
        _output_json(result)
    else:
        dead_symbols = result.get("dead_symbols", [])
        click.echo(f"死代码: {len(dead_symbols)} 个")
        for ds in dead_symbols:
            click.echo(f"  {ds['kind']} {ds['symbol']} — {ds['at']} [{ds['scope']}]")
        chains = result.get("dead_chains", [])
        if chains:
            click.echo("\n死代码链:")
            for c in chains:
                chain_str = " → ".join(c["chain"])
                click.echo(f"  {c['root']}: {chain_str}")


@cli.command()
@click.argument("target")
@click.option("--scope", default=None, help="限定作用域，用于消歧")
@click.option("--depth", default=3, type=int, help="传递影响最大深度（默认 3）")
@click.option("--json", "as_json", is_flag=True, help="JSON 输出")
def impact(target: str, scope: str | None, depth: int, as_json: bool) -> None:
    """查看改动影响面。

    TARGET: 符号名或 file:line 位置。
    """
    store = _get_store()
    querier = Querier(store)
    result = querier.impact(target, scope=scope, depth=depth)
    if as_json:
        _output_json(result)
    else:
        click.echo(f"影响面: {result['target']}")

        callers = result.get("direct_callers", [])
        click.echo(f"\n直接调用者 ({len(callers)}):")
        for a in callers:
            click.echo(f"  {a['at']} [{a.get('scope', '')}]")
            click.echo(f"    {a['code']}")

        callees = result.get("direct_callees", [])
        click.echo(f"\n直接被调用者 ({len(callees)}):")
        for a in callees:
            click.echo(f"  {a['at']} [{a.get('scope', '')}]")
            click.echo(f"    {a['code']}")

        transitive = result.get("transitive", [])
        click.echo(f"\n传递依赖 ({len(transitive)}):")
        for a in transitive[:20]:
            click.echo(f"  {a['at']} [{a.get('scope', '')}]")
            click.echo(f"    {a['code']}")
        if len(transitive) > 20:
            click.echo(f"  ... 还有 {len(transitive) - 20} 条（用 --json 查看全部）")


@cli.command()
@click.argument("path", required=False)
@click.option("--json", "as_json", is_flag=True, help="JSON 输出")
def api(path: str | None, as_json: bool) -> None:
    """HTTP 边界软关联：列出或查询跨语言 API 路径。

    PATH: 可选的 API 路径（如 /api/users），省略则列出所有发现的路径。
    """
    store = _get_store()
    querier = Querier(store)
    result = querier.api(path=path)
    if as_json:
        _output_json(result)
    else:
        if path:
            click.echo(f"API 路径: {result['path']}")
            click.echo(f"总引用: {result['total_references']}")
            for f in result.get("by_file", []):
                click.echo(f"\n  {f['file']}:")
                for ref in f["references"]:
                    click.echo(f"    {ref['at']} [{ref.get('scope', '')}]")
                    click.echo(f"      {ref['code'][:80]}")
        else:
            paths = result.get("paths", [])
            click.echo(f"发现 {result.get('total_paths', 0)} 个 API 路径:")
            for p in paths:
                click.echo(f"  {p['path']}  ({p['references']} 引用)")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="JSON 输出")
def cycles(as_json: bool) -> None:
    """检测循环依赖（跨文件 import 循环）。

    基于 file_hashes 中的文件路径和 imports 边构建文件依赖图，
    使用 DFS 检测循环。
    """
    store = _get_store()
    querier = Querier(store)
    result = querier.cycles()
    if as_json:
        _output_json(result)
    else:
        cycles_list = result.get("cycles", [])
        if not cycles_list:
            click.echo("未检测到循环依赖")
        else:
            click.echo(f"检测到 {len(cycles_list)} 个循环依赖:")
            for i, c in enumerate(cycles_list, 1):
                chain_str = " -> ".join(c["chain"] + [c["chain"][0]])
                click.echo(f"  [{i}] {chain_str}")


@cli.command(name="types")
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True, help="JSON 输出")
def types(name: str | None, as_json: bool) -> None:
    """跨语言类型一致性检查。

    NAME: 可选的类型名（如 User），省略则列出所有跨语言同名类型。
    """
    store = _get_store()
    querier = Querier(store)
    result = querier.types(name=name)
    if as_json:
        _output_json(result)
    else:
        groups = result.get("type_groups", [])
        if not groups:
            click.echo("未发现跨语言同名类型")
        else:
            click.echo(f"发现 {len(groups)} 个跨语言同名类型:")
            for g in groups:
                click.echo(f"\n  {g['name']} (在 {g['count']} 个语言/文件中定义):")
                for d in g["definitions"]:
                    click.echo(f"    [{d['language']}] {d['file']}:{d['line']} | fields: {', '.join(d.get('fields', []))}")
                if g.get("mismatches"):
                    click.echo(f"    不一致: {', '.join(g['mismatches'])}")


@cli.command()
@click.argument("fmt", type=click.Choice(["graphml", "dot"]))
@click.option("-o", "--output", "output", type=click.Path(), default=None, help="输出文件路径（默认 stdout）")
@click.option("--include-external", is_flag=True, help="包含 External 占位符节点")
def export(fmt: str, output: str | None, include_external: bool) -> None:
    """导出图谱为 GraphML 或 DOT 格式（用于可视化）。

    FMT: graphml（Gephi/yEd）或 dot（Graphviz）。
    """
    from codemap.export import export as do_export

    store = _get_store()
    try:
        result = do_export(store, fmt, include_external=include_external)
    except ValueError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(result)
        click.echo(f"已导出到 {output}")
    else:
        click.echo(result)


if __name__ == "__main__":
    cli()