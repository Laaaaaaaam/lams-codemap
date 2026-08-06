# Codemap 设计文档

## 一、定位

**只建事实，不做判断。**

Codemap 是一个 CLI 工具，为多语言项目（Python/JavaScript/TypeScript/Go）构建代码网络图谱。输出 JSON 格式，面向 Agent 消费。
Agent 拿到图谱后自行推理，工具不替 Agent 做任何语义判断。

## 二、核心原则

1. **事实，不判断**：只从 AST 提取确定性事实，不推断职责/架构/建议
2. **每件事实都可定位**：节点和边都带 `file:line:col`
3. **存储大而全，查询按需投影**：存满所有 AST 可提取信息，输出按 `--detail` 档位投影
4. **输出最小化**：默认只给 `{symbol, at, appearances: [{code, at, scope}]}`，其余按需追问
5. **盲点诚实**：AST 看不到的标记为 `unknown`，不猜测

## 三、数据模型

### 3.1 节点类型

| 类型 | 说明 | 例子 |
|---|---|---|
| File | 文件/模块 | `src/auth/token.py` |
| Function | 函数/方法/lambda | `verify_token`, `handle_request` |
| Class | 类 | `UserCreated` |
| Variable | 变量/参数/字段/常量 | `token`, `data`, `MAX_RETRIES` |
| External | 外部符号 | `jwt.decode`, `requests.get` |

每个节点存储字段：
- `id`: 稳定 ID，格式 `文件路径#符号路径`
- `kind`: File | Function | Class | Variable | External
- `name`: 符号名
- `location`: 定义位置 (file:line:col)
- `end_location`: 定义结束位置
- `scope`: 作用域，格式 `文件路径:函数路径` 或 `文件路径`
- `type_annotation`: 类型注解（如有）
- `source_hash`: 源码 hash（用于增量）

### 3.2 边类型

**结构边：**
| 边类型 | 方向 | 说明 |
|---|---|---|
| defines | File → Node | 文件定义了节点 |
| imports | File → Variable/External | import 引入 |
| calls | Function → Function | 函数调用 |
| returns | Function → Variable | return 返回 |
| assigns | Function → Variable | 赋值语句 |
| reads | Function → Variable | 读取变量 |
| writes | Function → Variable | 写入变量 |
| attrs | Variable → Variable | 属性访问 (a.b) |

**数据流边（核心差异化）：**
| 边类型 | 说明 |
|---|---|
| param-flow | 实参流入形参（跨函数传递） |
| param-transform | 变换操作：输入经操作产出输出（通过 Transform 节点） |

### 3.3 Transform 节点

表示一次变换操作的中转节点，解决多对多关系表达。

字段：
- `id`: 稳定 ID
- `kind`: call | assign | unpack | attribute | subscript | operator | return | comprehension
- `op`: 操作描述（函数名/属性名/运算符）
- `op_node`: 如果是调用，指向被调函数节点 ID
- `location`: 源码位置
- `inputs`: 输入符号 ID 列表（有序）
- `outputs`: 输出符号 ID 列表（有序）
- `branch`: 条件分支标记（if/else/elif/None）
- `code`: 涉及的完整代码行/语句

## 四、存储

SQLite 单文件。表结构：

```sql
CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    location TEXT NOT NULL,
    end_location TEXT,
    scope TEXT NOT NULL,
    type_annotation TEXT,
    source_hash TEXT
);

CREATE TABLE edges (
    id TEXT PRIMARY KEY,
    edge_type TEXT NOT NULL,
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    location TEXT NOT NULL,
    code TEXT NOT NULL,
    metadata TEXT  -- JSON: 额外信息 (co_inputs, arg_index, branch, etc.)
);

CREATE INDEX idx_nodes_name ON nodes(name);
CREATE INDEX idx_nodes_scope ON nodes(scope);
CREATE INDEX idx_nodes_kind ON nodes(kind);
CREATE INDEX idx_edges_from ON edges(from_node);
CREATE INDEX idx_edges_to ON edges(to_node);
CREATE INDEX idx_edges_type ON edges(edge_type);
CREATE INDEX idx_edges_location ON edges(location);
```

metadata JSON 结构：
```json
{
  "co_inputs": [{"id": "V_002", "name": "B", "location": "foo.py:2:9"}],
  "arg_index": 0,
  "branch": "if",
  "op": "+",
  "transform_kind": "call"
}
```

## 五、查询体系

### 命令总览

```
codemap build <repo>              # 构建图谱（默认增量）
codemap build <repo> --full       # 强制全量
codemap trace <symbol>            # 正向追踪：符号出现在哪
codemap trace <symbol> --reverse  # 反向追踪：符号从哪来
codemap trace <symbol> --depth N  # N 层展开
codemap info <symbol>             # 符号定义详情
codemap at <file:line>            # 位置反查
codemap search <text>             # 全文搜索代码原文
codemap file <path>               # 文件级查询
codemap dead                      # 死代码列表
codemap impact <symbol|file:line> # 影响面查询
```

### 通用参数

| 参数 | 说明 |
|---|---|
| `--detail direct` | 最小输出（默认） |
| `--detail function` | direct + 涉及函数详情 |
| `--detail full` | 全部字段 |
| `--json` | JSON 输出 |
| `--scope <scope>` | 限定作用域 |

### trace 查询语法

```bash
codemap trace token               # 精确匹配符号名 "token"
codemap trace token --fuzzy       # 模糊匹配（包含 token 的符号名）
codemap trace .buffer             # 只查属性访问 .buffer
codemap trace state.buffer        # 查 state.buffer 这个链
```

属性前缀 `.` 用于区分属性访问和同名的变量：
- `trace buffer` → 返回所有名为 buffer 的符号（变量 + 属性访问）
- `trace .buffer` → 只返回属性访问

### direct 档位输出格式

```json
{
  "symbol": "token",
  "appearances": [
    {
      "code": "user = verify_token(token)",
      "at": "middleware.py:4:18",
      "scope": "middleware.py:auth_required"
    },
    {
      "code": "payload = jwt.decode(token)",
      "at": "token.py:12:18",
      "scope": "token.py:verify_token"
    }
  ]
}
```

跨函数参数传递时，depth_2 的 code 带函数签名行：
```json
{
  "depth_2": [
    {
      "code": "def verify_token(token):\n    return jwt.decode(token)",
      "at": "token.py:45:0",
      "scope": "token.py:verify_token"
    }
  ]
}
```

### function 档位额外字段

```json
{
  "functions": [
    {
      "name": "verify_token",
      "location": "token.py:45:0",
      "params": [{"name": "token", "index": 0, "type": "str"}],
      "returns": [{"type": "User"}]
    }
  ]
}
```

### full 档位额外字段

```json
{
  "edges": [
    {
      "id": "E_001",
      "edge_type": "param-flow",
      "from_node": "V_001",
      "to_node": "V_003",
      "co_inputs": [
        {"id": "V_002", "name": "request", "location": "middleware.py:4:10"}
      ],
      "branch": null,
      "transform_kind": "call"
    }
  ]
}
```

### info 输出格式

```json
{
  "symbol": "verify_token",
  "id": "F_001",
  "kind": "Function",
  "location": "token.py:45:0",
  "end_location": "token.py:58:0",
  "scope": "token.py",
  "params": [
    {"name": "token", "index": 0, "type": "str"}
  ],
  "returns": [{"type": "User"}],
  "decorators": [],
  "docstring": "验证 JWT 令牌签名与有效期"
}
```

### at 输出格式

```json
{
  "location": "token.py:52:18",
  "code": "payload = jwt.decode(token)",
  "symbols": [
    {"name": "jwt.decode", "id": "E_005", "kind": "External"},
    {"name": "payload", "id": "V_012", "kind": "Variable"},
    {"name": "token", "id": "V_003", "kind": "Variable"}
  ],
  "edges": [
    {"edge_type": "param-transform", "id": "T_008", "kind": "call", "op": "jwt.decode"}
  ]
}
```

### search 输出格式

```json
{
  "query": "UserCreated",
  "results": [
    {"code": "event = UserCreated(user_id=\"123\")", "at": "producer.py:1:0"},
    {"code": "def handle(event: UserCreated):", "at": "consumer.py:1:0"}
  ],
  "match_type": "text"  // 区别于 trace 的 symbol 匹配
}
```

### file 输出格式

```json
{
  "file": "auth/token.py",
  "defines": [
    {"symbol": "verify_token", "kind": "Function", "at": "token.py:45:0"},
    {"symbol": "TokenExpired", "kind": "Class", "at": "token.py:10:0"}
  ],
  "imports": [
    {"symbol": "jwt", "at": "token.py:1:0"},
    {"symbol": "time", "at": "token.py:2:0"}
  ],
  "imported_by": [
    {"file": "middleware.py", "symbols": ["verify_token"]}
  ]
}
```

### dead 输出格式

```json
{
  "dead_symbols": [
    {"symbol": "old_helper", "at": "utils.py:45:0", "kind": "Function", "scope": "utils.py"},
    {"symbol": "UNUSED_CONST", "at": "config.py:12:0", "kind": "Variable", "scope": "config.py"}
  ],
  "dead_chains": [
    {
      "root": "old_helper",
      "chain": ["old_helper", "_internal_parse", "_tokenize"],
      "reason": "old_helper 零入边，其下游只被 old_helper 引用"
    }
  ]
}
```

### impact 输出格式

```json
{
  "target": "verify_token",
  "affected": [
    {"code": "user = verify_token(token)", "at": "middleware.py:4:0", "scope": "middleware.py:auth_required"},
    {"code": "return handle_request(request)", "at": "middleware.py:6:0", "scope": "middleware.py:auth_required"},
    {"code": "request.current_user = user", "at": "middleware.py:5:0", "scope": "middleware.py:auth_required"}
  ]
}
```

## 六、Extractor 规范

### 6.1 能精确提取的 AST 事实

| 事实 | AST 节点 |
|---|---|
| 函数定义及形参 | `FunctionDef.args` |
| 类定义 | `ClassDef` |
| 赋值（含解包） | `Assign` / `AnnAssign` |
| 函数调用及实参 | `Call` |
| return 语句 | `Return` |
| 属性访问 | `Attribute` |
| 下标访问 | `Subscript` |
| 增量赋值 | `AugAssign` |
| with 语句的 as 绑定 | `With` |
| for 循环变量 | `For` |
| except 绑定 | `ExceptHandler` |
| 海象运算符 | `NamedExpr` |
| global/nonlocal 声明 | `Global` / `Nonlocal` |
| import 绑定 | `Import` / `ImportFrom` |
| 推导式 | `ListComp` / `DictComp` / `SetComp` / `GeneratorExp` |
| 装饰器 | 函数/类的 `decorator_list` |
| lambda | `Lambda` |
| 布尔运算符 | `BoolOp` / `IfExp` |
| 比较运算符 | `Compare` |

### 6.2 AST 看不到的（标记为 unknown）

- 动态分发：`getattr(obj, name)()` → `edge.metadata.unknown = true`
- monkey patch：`SomeClass.method = fn` → 按赋值处理，不推断
- `eval`/`exec` → 标记节点类型为 unknown
- C 扩展调用 → External 节点，不展开
- `__getattr__` 动态属性 → 标记
- 元类、`__init_subclass__` → 标记
- 事件总线运行时发布/订阅 → 不追踪跨事件边界

### 6.3 符号名提取规则

**搜索匹配的是符号名，不是代码文本。** 提取时以 AST Name 节点为准：

- `Name` 节点 → 变量/函数引用
- `FunctionDef.name` → 函数名
- `ClassDef.name` → 类名
- `Attribute.attr` → 属性名（记录为 `object.attr` 格式）
- `arg.arg` → 参数名
- `alias.name` → import 别名
- `keyword.arg` → 关键字参数名

**不纳入符号搜索的**：关键字（`if`/`for`/`while`/`import`/`return`/`def`/`class`）、内置常量（`True`/`False`/`None`）、运算符。

### 6.4 Scope 计算规则

```
模块级：文件路径
函数内：文件路径:函数路径（嵌套用 . 分隔，如 file.py:func_a.inner）
类内：文件路径:类名
类方法内：文件路径:类名.方法名
```

## 七、增量构建

- 基于文件 hash：比较每个文件的当前 hash 与上次构建记录的 hash
- 变更文件 → 删除该文件的所有节点和边 → 重新解析
- 受影响的依赖文件（import 该文件的文件）→ 选择性重解析
- 全量构建：`codemap build --full`

## 八、项目结构

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

## 九、版本记录

- v0.1: 初始设计，覆盖结构边 + 参数流边 + 完整查询体系
- v0.2: 多语言支持（Python + JavaScript + TypeScript + Go），基于 tree-sitter
- v0.3: 多语言调用解析（Go receiver 方法、JS obj.method 调用）、接口感知死代码检测
- v0.4: 新增 api/cycles/types 命令、TypeAlias 节点类型、入口点白名单、search 过滤分页、CommonJS exports 调用边

## 十、新增命令（v0.4）

### api — HTTP 边界软关联

```bash
codemap api                # 列出所有发现的 API 路径
codemap api /api/users     # 查询某路径的所有引用（跨语言）
```

基于代码中的路径字符串（如 `"/api/users"`）匹配，建立跨语言 API 调用关联。

### cycles — 循环依赖检测

```bash
codemap cycles             # 检测跨文件 import 循环
```

基于文件依赖图 DFS 检测环，输出循环链。

### types — 跨语言类型一致性检查

```bash
codemap types              # 列出所有跨语言同名类型
codemap types User         # 检查特定类型的一致性
```

查找同名的 Class 类型在不同语言/文件中的定义，比较字段是否一致，输出 mismatches。

## 十一、新增节点类型

| 类型 | 说明 | 例子 |
|---|---|---|
| TypeAlias | 类型别名（Go: `type X = Y`） | `HandlerFunc` |

## 十二、增强的查询参数

| 命令 | 新参数 | 说明 |
|---|---|---|
| trace | --kind | 过滤出现类型 (definition,call,reference,import) |
| trace | role | 每条 appearance 标注 role 字段 |
| impact | --scope | 消歧，与 trace/info 一致 |
| search | --file | 限定文件路径前缀 |
| search | --limit | 最大返回条数 |
| dead | reason | 判定原因 |
| dead | confidence | high/medium/low |
| dead | is_test | 是否在测试文件中 |
| info | fields | Class 节点返回字段列表 |