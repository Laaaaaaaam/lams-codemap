# Codemap 维护指南

## 项目定位

Codemap 是一个多语言代码网络图谱 CLI 工具，为 AI Agent 消费而设计。原则：**只建事实，不做判断**。

## 架构

```
codemap/
  __init__.py          # 公共 API 导出
  __main__.py          # python -m codemap 入口
  cli.py               # CLI 命令定义（click）
  build.py             # 构建编排（全量/增量、多语言分发）
  models.py            # 数据模型（Node/Edge/Transform，frozen dataclass）
  store.py             # SQLite 存储层（CRUD + 查询）
  normalizer.py        # 原始事实去重、补全
  resolver.py          # 跨文件引用解析、param-flow 边创建
  query.py             # 查询逻辑（trace/info/at/search/file/dead/impact/api/cycles/types）
  extractor.py         # 向后兼容 shim → extractors/python.py
  extractors/
    __init__.py         # 多语言提取器分发中心
    base.py             # BaseExtractor 协议
    python.py           # Python 提取器（基于 ast 模块）
    javascript.py       # JavaScript 提取器（基于 tree-sitter）
    typescript.py       # TypeScript 提取器（继承 JS，增加类型注解）
    go.py               # Go 提取器（基于 tree-sitter）
    treesitter_utils.py # tree-sitter 通用工具
```

## 构建流程

1. Extractor（按语言选择）→ 提取原始节点/边/变换
2. Normalizer → 去重、补全缺失节点
3. Store → 写入 SQLite
4. Resolver → 跨文件引用解析（imports → calls → param-flow）

## 添加新语言

1. 在 `extractors/` 下创建 `<lang>.py`，实现 `extract() -> (nodes, edges, transforms)`
2. 在 `extractors/__init__.py` 的 `_LANG_EXTENSIONS` 和 `get_extractor()` 中注册
3. 在 `resolver.py` 中添加该语言的 import/call 解析逻辑

## 测试

```bash
codemap build <repo> --full       # 构建图谱
codemap trace <symbol> --json     # 验证追踪
codemap dead --json               # 验证死代码
codemap impact <symbol> --json    # 验证影响面
```

验证仓库：`test_multiservice`（4 语言混合）、`test_repos/requests`（Python）、`test_repos/express`（JS）、`test_repos/gin`（Go）。

## 版本约定

- 遵循语义版本：`MAJOR.MINOR.PATCH`
- 同步更新 `pyproject.toml`、`codemap/__init__.py`、`DESIGN.md` 版本记录
