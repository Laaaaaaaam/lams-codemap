# Lam's Codemap

> 多语言代码网络图谱 CLI 工具，为 AI Agent 消费而设计。
>
> 原则：**只建事实，不做判断。**

## 概述

Codemap 将代码库解析为 **节点（Node）+ 边（Edge）** 的网络图谱，存储在 SQLite 中。它不是一个 LSP，不追求精确的语义类型推导——它建立的是**代码事实**：谁定义了什么、谁调用了什么、谁导入了什么。Agent 在这些事实上做语义推理。

支持 **4 种语言**：Python、JavaScript、TypeScript、Go。

## 安装

```bash
pip install -e .
```

依赖：`click`、`tree-sitter`（+ 3 个语言 grammar 包）。

## 快速开始

```bash
# 构建图谱
codemap build ./my-project --full

# 追踪符号
codemap trace myFunction --json

# 查看符号详情
codemap info MyClass --json

# 死代码检测
codemap dead --json

# 影响面分析
codemap impact myFunction --json

# 循环依赖检测
codemap cycles --json

# 跨语言类型一致性检查
codemap types --json

# HTTP 边界软关联
codemap api --json
```

## CLI 命令一览

| 命令 | 说明 |
|---|---|
| `build <repo>` | 构建图谱（全量/增量，多语言自动检测） |
| `trace <symbol>` | 符号追踪（含 role 标注：definition/call/reference/import） |
| `info <symbol>` | 符号详情（含字段列表、方法列表、参数、返回类型） |
| `at <file:line>` | 位置反查：这行代码涉及什么符号和边 |
| `search <text>` | 全文搜索（支持 `--file` 过滤、`--limit` 分页） |
| `file <path>` | 文件级查询：定义、导入、被引用 |
| `dead` | 死代码检测（三级置信度：high/medium/low + 入口点白名单） |
| `impact <target>` | 影响面分析（谁会被这个符号的变更影响） |
| `api [path]` | HTTP 边界软关联：发现跨语言 API 路径 |
| `cycles` | 循环依赖检测 |
| `types [name]` | 跨语言类型一致性检查 |

所有命令支持 `--json` 输出，方便 Agent 程序化消费。

## 架构

```
codemap/
  __init__.py          # 公共 API 导出
  cli.py               # CLI 命令定义（click）
  build.py             # 构建编排（全量/增量、多语言分发）
  models.py            # 数据模型（Node/Edge/Transform，frozen dataclass）
  store.py             # SQLite 存储层
  normalizer.py        # 原始事实去重、补全
  resolver.py          # 跨文件引用解析
  query.py             # 查询逻辑
  extractors/
    python.py           # Python 提取器（ast）
    javascript.py       # JavaScript 提取器（tree-sitter）
    typescript.py       # TypeScript 提取器（继承 JS）
    go.py               # Go 提取器（tree-sitter）
```

### 构建流程

1. **Extractor** → 按语言提取原始节点/边/变换
2. **Normalizer** → 去重、补全缺失节点
3. **Store** → 写入 SQLite
4. **Resolver** → 跨文件引用解析（imports → calls → param-flow）

## 验证数据

| 仓库 | 语言 | 文件 | 节点 | 边 | 调用解析率 |
|---|---|---|---|---|---|
| test_multiservice | Py+Go+JS+TS | 7 | 289 | 546 | 24% |
| requests | Python | 37 | 4,809 | 11,270 | 48% |
| express | JavaScript | 141 | 1,584 | 13,731 | 50% |
| gin | Go | 99 | 5,610 | 22,067 | 36% |

> 调用解析率未达 100% 是理论正常的——语言内置函数、标准库、第三方依赖和动态分发在静态分析中不可解析。

## 特色能力

- **跨语言符号追踪**：`trace login` 同时找到 JS 和 Python 中的定义
- **跨语言类型一致性检查**：发现 `User` 类型在 TS（`username`）和 Go（`Username`）中的字段命名不一致
- **HTTP 边界软关联**：基于路径字符串匹配发现前端→网关→后端的 API 调用链
- **循环依赖检测**：发现 `gateway.py ↔ proxy.py` 的 import 循环
- **死代码检测**：三级置信度 + 入口点白名单（`main`/`serve` 等不误判）

## 测试

```bash
python -m pytest tests/test_basic.py -v
# 34 passed in 0.92s
```

覆盖：模型、提取器（Python/JS/Go）、构建、查询、解析器。

## 版本

v0.4.0 — 详见 [DESIGN.md](DESIGN.md) 版本记录。

## 许可证

MIT
