# 第 03 课：核心工具链

## 🎯 本节目标

为 Agent 构建基础操作系统。实现核心工具链（文件读取、写入、精细编辑与命令行执行），确保编辑操作具备幻觉安全（精确匹配且唯一匹配），并具备防御性的结果截断机制，防止 Token 爆表。

---

## 🏆 最终效果

完成本节后，运行 Agent 并向其提问，它将具备浏览当前目录、读取文件内容、自动编辑修改代码以及运行测试或 shell 命令的能力：

**运行测试**：
```bash
python __main__.py "把 tools.py 中的 MAX_RESULT_CHARS 修改为 60000"
```

你将看到 Agent 能够自主进行链式操作：
1. 调用 `read_file` 确认 `tools.py` 里的现有常量定义。
2. 检索到具体字符串后，精确调用 `edit_file` 进行修改，并输出替换后的局部 diff。
3. 退出循环并汇报任务完成。

---

## 🛠️ 本节任务

1. **实现读取与截断**：实现 `read_file` 并构建头尾截断函数 `_truncate_result`。
2. **实现创建与写入**：实现 `write_file`，支持自动创建目录。
3. **构建精确编辑工具**：实现 `edit_file`，内置“唯一精确匹配”校验和“直弯引号容错”。
4. **构建命令执行工具**：实现 `run_shell`，支持超时捕获与标准错误输出合并。
5. **更新工具定义与分发逻辑**：注册新工具并重构 `execute_tool`。

---

## 📦 涉及文件

修改：
- `tools.py`

---

## 🚀 开始实现

### 步骤 1：文件读取 `read_file` 与结果截断保护

#### 为什么做

LLM 在阅读大型输出（如几百行的文件或超长的测试日志）时，容易因过量无用 Token 导致上下文窗口耗尽并产生巨大的 API 账单。
我们需要：
1. 读取文件时自动附带行号，方便 LLM 精准定位。
2. 实现头尾截断（保留前段和尾段，裁剪中段），因为报错信息通常在尾部，导入声明和入口通常在头部。

#### 做什么

修改 `tools.py`，导入必要包，实现 `_read_file` 和 `_truncate_result`：

```python
# tools.py

from __future__ import annotations

import re
import subprocess
from pathlib import Path

MAX_RESULT_CHARS = 50000  # 限制单次工具返回最大字符数——防止撑爆上下文窗口


def _read_file(inp: dict) -> str:
    """读取文件内容并附加行号，方便 LLM 精准定位"""
    try:
        content = Path(inp["file_path"]).read_text(encoding="utf-8")
        lines = content.split("\n")
        # 加上行号，方便 LLM 查找并定位代码
        return "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(lines))
    except Exception as e:
        return f"Error reading file: {e}"  # 返回错误字符串而非抛异常，让 LLM 自行重试


def _truncate_result(result: str) -> str:
    """头尾截断：保留文件开头和末尾（报错通常在尾部），裁剪中间部分"""
    if len(result) <= MAX_RESULT_CHARS:
        return result
    # 预留 60 字符给截断提示信息，其余平均分配给头尾
    keep = (MAX_RESULT_CHARS - 60) // 2
    return (
        result[:keep]
        + f"\n\n[... truncated {len(result) - keep * 2} chars ...]\n\n"
        + result[-keep:]
    )
```

#### 注意什么

- 必须对大结果进行物理截断，否则一次错误的 Shell 输出（如输出上万行日志）就能彻底废掉当前会话。

---

### 步骤 2：自动创建父目录的写入工具 `write_file`

#### 为什么做

Agent 经常需要创建新代码文件。如果直接调用底层的 Python 写入，一旦目标路径中的某些父目录不存在，就会报错失败。我们应该让写入工具具备自动创建缺失路径（`mkdir -p`）的能力，减少 Agent 对 Shell 命令的依赖。

#### 做什么

在 `tools.py` 中编写 `_write_file` 逻辑：

```python
# tools.py（续）


def _write_file(inp: dict) -> str:
    """写入文件，自动创建不存在的父目录"""
    try:
        path = Path(inp["file_path"])
        # 自动创建任意不存在的父级目录——减少 Agent 对 Shell mkdir 的依赖
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inp["content"], encoding="utf-8")

        # 返回前 30 行预览，让 Agent 确认写入格式正确，无需再调 read_file
        lines = inp["content"].split("\n")
        preview = "\n".join(f"{i+1:4d} | {l}" for i, l in enumerate(lines[:30]))
        trunc = f"\n  ... ({len(lines)} lines total)" if len(lines) > 30 else ""
        return f"Successfully wrote to {inp['file_path']} ({len(lines)} lines):\n\n{preview}{trunc}"
    except Exception as e:
        return f"Error writing file: {e}"
```

#### 注意什么

- 工具返回结果中包含了前 30 行的预览，使 Agent 能够确认写入内容格式正确，无需立即调用 `read_file` 进行再次读取。

---

### 步骤 3：具备引号容错和唯一匹配校验的 `edit_file`

#### 为什么做

这是 Coding Agent 中**最核心也是技术点最多**的工具。
1. **精确替换（Search-and-Replace）**：通过要求 LLM 提供原文中唯一的 `old_string` 和准备替换的 `new_string` 实施修改。相较于“Unified diff”和“行号编辑”，这种方式对 LLM 极其友好且不容易因格式错乱损坏文件。
2. **唯一性校验**：必须验证 `old_string` 在文件中仅出现 **1 次**。若匹配多次，必须拒绝，否则可能导致错误替换别处的代码。若为 0 次，说明模型记忆产生了幻觉。
3. **引号容错（Quote Normalization）**：LLM 在处理 Token 时经常将直引号（`"`）和弯引号（`“`）混淆，导致匹配失败。我们必须在后台对其做容错对齐，匹配成功后仍换回文件原样。

#### 做什么

在 `tools.py` 中实现引号标准化、轻量级 diff 生成及 `edit_file` 替换方法：

```python
# tools.py（续）


def _normalize_quotes(s: str) -> str:
    """将弯引号和特殊引号统一为直引号——LLM 常混淆这些字符"""
    s = re.sub("[‘’′]", "'", s)
    s = re.sub('[“”″]', '"', s)
    return s


def _find_actual_string(file_content: str, search_string: str) -> str | None:
    """在文件中查找目标字符串，支持引号容错"""
    # 先尝试精确匹配——优先使用原始字符串
    if search_string in file_content:
        return search_string
    # 精确匹配失败，转为直引号后再次匹配——处理 LLM 的引号混淆问题
    norm_search = _normalize_quotes(search_string)
    norm_file = _normalize_quotes(file_content)
    idx = norm_file.find(norm_search)
    if idx != -1:
        # 返回文件里的原样字符串，保持代码本身的风格
        return file_content[idx : idx + len(search_string)]
    return None  # 完全未匹配——LLM 可能产生了幻觉


def _generate_diff(old_content: str, old_string: str, new_string: str) -> str:
    """生成轻量级 diff 输出，方便 Agent 确认修改内容"""
    # 根据 old_string 在前文中出现的 \n 计算起始行号
    line_num = old_content.split(old_string)[0].count("\n") + 1
    old_lines = old_string.split("\n")
    new_lines = new_string.split("\n")
    parts = [f"@@ -{line_num},{len(old_lines)} +{line_num},{len(new_lines)} @@"]
    parts.extend(f"- {l}" for l in old_lines)
    parts.extend(f"+ {l}" for l in new_lines)
    return "\n".join(parts)


def _edit_file(inp: dict) -> str:
    """精确编辑：通过唯一匹配的 old_string 替换为 new_string"""
    try:
        path = Path(inp["file_path"])
        content = path.read_text(encoding="utf-8")

        # 带引号容错的查找
        actual = _find_actual_string(content, inp["old_string"])
        if not actual:
            return f"Error: old_string not found in {inp['file_path']}"

        # 唯一性校验防止误替换——匹配多次时拒绝执行，由 LLM 调整 old_string 重试
        count = content.count(actual)
        if count > 1:
            return f"Error: old_string found {count} times in {inp['file_path']}. Must be unique."

        # replace 第三个参数 1 表示只替换首个匹配——即使校验通过也做防御
        new_content = content.replace(actual, inp["new_string"], 1)
        path.write_text(new_content, encoding="utf-8")

        diff = _generate_diff(content, actual, inp["new_string"])
        # 若实际匹配的字符串与请求不同，说明经历了引号标准化
        note = " (matched via quote normalization)" if actual != inp["old_string"] else ""
        return f"Successfully edited {inp['file_path']}{note}\n\n{diff}"
    except Exception as e:
        return f"Error editing file: {e}"
```

#### 注意什么

- 当匹配到多次或未匹配时，要向模型返回清晰的报错，模型会自动读取文件重新尝试。**错误是数据，不是程序崩溃。**

---

### 步骤 4：超时捕获与合并输出的 `run_shell`

#### 为什么做

Agent 常运行测试或构建任务。我们需要：
1. **合并捕获 stdout 和 stderr**：程序报错通常在 stderr 中，两者缺一不可。
2. **超时机制**：防止运行像 `ping` 这种永远不会自动停止的命令导致整个 Agent 彻底挂起。

#### 做什么

在 `tools.py` 中编写命令执行逻辑：

```python
# tools.py（续）

import subprocess


def _run_shell(inp: dict) -> str:
    """执行 Shell 命令，合并 stdout 和 stderr，支持超时保护"""
    try:
        timeout_ms = inp.get("timeout", 30000)  # 默认 30 秒超时
        timeout_s = timeout_ms / 1000
        result = subprocess.run(
            inp["command"],
            shell=True,
            capture_output=True,  # 同时捕获 stdout 和 stderr
            text=True,
            timeout=timeout_s,
        )
        stderr = f"\nStderr:\n{result.stderr}" if result.stderr else ""
        if result.returncode != 0:
            # 非零退出码——将 stderr 一起返回，错误信息通常在 stderr 中
            return f"Command failed (exit code {result.returncode}){stderr}"
        return result.stdout or "(command succeeded with no output)"
    except subprocess.TimeoutExpired:
        return f"Command timed out after {inp.get('timeout', 30000)}ms"
    except Exception as e:
        return f"Error: {e}"
```

#### 注意什么

- 当命令成功但无输出（例如 `mkdir`）时，要返回明确的说明（如 `(command succeeded with no output)`），避免 Agent 认为发生了异常或无休止重试。

---

### 步骤 5：注册新工具并重构工具分发器 `execute_tool`

#### 为什么做

最后，我们将所有实现的新工具声明加入 `tool_definitions` 数组中，并在 `execute_tool` 分发函数里配置相应的路由，同时应用大结果自动截断。

#### 做什么

更新 `tools.py` 中的静态声明及 `execute_tool` 分发方法：

```python
# tools.py（续）

# 添加工具定义（合并入已有定义中）
tool_definitions: list[dict] = [
    # list_files 的定义（保留自第 1 课）
    {
        "name": "list_files",
        "description": "List files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {"type": "string", "description": "Base directory"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file. Returns the file content with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to read"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to write"},
                "content": {"type": "string", "description": "The content to write to the file"},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Edit a file by replacing an exact string match with new content. The old_string must match exactly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to edit"},
                "old_string": {"type": "string", "description": "The exact string to find and replace"},
                "new_string": {"type": "string", "description": "The string to replace it with"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "run_shell",
        "description": "Execute a shell command and return its output.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "timeout": {"type": "number", "description": "Timeout in milliseconds (default: 30000)"},
            },
            "required": ["command"],
        },
    },
]


def get_tool_definitions() -> list[dict]:
    """返回所有工具的定义，供 LLM API 调用时传入"""
    return tool_definitions


# read_file_state: mtime 防护——记录文件最后读取时间，防止并发读写冲突
async def execute_tool(name: str, inp: dict, read_file_state: dict[str, float] | None = None) -> str:
    """工具分发器：根据名称路由到具体实现函数，并自动截断大结果"""
    handlers = {
        "list_files": _list_files,  # 第一课实现
        "read_file": _read_file,
        "write_file": _write_file,
        "edit_file": _edit_file,
        "run_shell": _run_shell,
    }
    handler = handlers.get(name)
    if not handler:
        return f"Unknown tool: {name}"  # 返回字符串而非抛异常，让 LLM 自行修正工具名

    # 执行对应函数并自动拦截大结果输出
    result = handler(inp)
    return _truncate_result(result)


# _list_files 的实现（第一课已写好，此处省略）
```

#### 注意什么

- 分发器没有抛出 Python 异常，而是选择返回 `"Unknown tool: {name}"` 的字符串。这有助于让模型自主理解并修复其拼写错误的“工具幻觉”。

---

## 工具执行的副作用管理原则

在大模型工具系统设计中，**副作用（Side Effects）的管理**是保障系统可预测性与简洁性的重要原则。

### 什么是工具的副作用？
工具执行除了向大模型返回文本结果外，对系统状态或外部世界产生的额外改变。例如：
- 写入文件（直接副作用）
- 自动更新项目记忆索引 `MEMORY.md`（间接/级联副作用）

### 副作用隐藏的危害
在早期设计中，我们可能会倾向于在具体工具的实现函数（如 `_write_file`）内部隐式调用这些副作用（如直接调用 `_auto_update_memory_index`）。这种做法被称为**副作用隐藏**，它会导致：
1. **控制流模糊**：副作用在底层深处发生，高层调度器（`execute_tool`）无法感知和控制。
2. **测试困难**：在对 `_write_file` 进行单元测试时，必须同时模拟或处理记忆系统的副作用，导致测试严重耦合。
3. **并发隐患**：当在流式或多任务并发场景下，底层的隐式副作用可能导致意料之外的读写冲突。

### 显式副作用管理原则
为了消除这一隐患，我们应当将副作用提升到**分发器层（`execute_tool`）显式调用**，使底层工具实现函数保持“纯粹（Pure）”或仅产生最直接的副作用。

- **不好的做法**：
  ```python
  def _write_file(inp: dict) -> str:
      # ... 写入逻辑 ...
      # ❌ 隐藏的间接副作用——高层调度器无法感知，测试时必须模拟
      _auto_update_memory_index(inp["file_path"])
  ```
- **推荐的做法**：
  ```python
  def _write_file(inp: dict) -> str:
      # ... 仅限直接写入逻辑 ...
      return "Success"

  async def execute_tool(name: str, inp: dict) -> str:
      """分发器：集中管理副作用，保持底层函数纯粹"""
      # ... 分发执行 ...
      result = handler(inp)

      # ✅ 在高层显式处理级联副作用——清晰、可控、易测试
      if name == "write_file" and not result.startswith("Error"):
          _auto_update_memory_index(inp["file_path"])

      return result
  ```

这种显式设计提升了代码的教学价值，使工具的执行逻辑更加透明、独立且易于测试。

---

## ⚖️ 设计权衡

### 字符串精细替换（Search-and-Replace） vs Unified diff

- **方案 A**：**字符串精细替换**（我们所用）
  - 要求提供唯一的原文片段并给入新替换片段。
  - **优点**：LLM 几乎不会犯错，即使由于引号混淆也有容错拦截；失败时极具“幻觉安全性”（不匹配直接报错失败，绝不猜）。
  - **缺点**：如果大文件内存在多个一模一样的多行片段，则必须扩展定位上下文才能做唯一匹配。
- **方案 B**：**Unified diff 补丁文件**
  - 使用 Linux 的 `patch` 格式或提供包含修改位置的补丁文件。
  - **优点**：支持多处同时替换。
  - **缺点**：LLM 在精准输出 hunk header（例如 `@@ -12,4 +12,6 @@`）和 `+`/`-` 符号前缀时的格式控制极差，极易破坏补丁。

**结论**：在 Agent 应用中，精确字符串匹配替换是最稳定、幻觉安全率最高的方案。

---

## ⚠️ 常见陷阱

### 1. `edit_file` 未限制唯一匹配导致多处误改

```python
# ❌ 错误：这会导致只要找到匹配项就替换全部，或者静默改变别处代码
new_content = content.replace(inp["old_string"], inp["new_string"])
```

**后果**：程序可能会把项目里其他完全不相关的方法一并修改，造成非常隐蔽的 Bug。
**修正**：先使用 `.count()` 检测匹配计数是否精确为 1，如非 1 必须返回 Error。且使用 `.replace(..., ..., 1)` 确保即使发生了多次也只限制首个以保证受控。

---

### 2. 弯引号造成编辑大量报错

大语言模型常输出中英文弯双引号（`“”`）或弯单引号（`‘’`）。如果不做后台统一化，会让 30% 以上的 `edit_file` 修改直接抛出 `old_string not found`。

**修正**：运行 `_find_actual_string()` 过滤器，若匹配失败，则在后台通过正则将所有弯引号和特殊符号一并标准化对齐，再行检索文件内容。

---

## ✅ 验收点

### 输入

创建一个用于测试编辑的 `test_edit.txt` 文件，写入：
```
hello world
"msg" = "hello"
goodbye world
```

然后通过 Agent 将中间的直引号编辑为弯引号（如果能实现，说明编辑及引号容错工作完全正常）：
```bash
python __main__.py "把 test_edit.txt 文件中的直引号 '\"msg\" = \"hello\"' 替换为 '“msg” = “hello”'"
```

### 预期结果

Agent 正确调用 `edit_file`，修改生效。再次查看该文件，内容已被正确更新。且终端输出成功渲染如下形式的局部 diff：
```
Successfully edited test_edit.txt

@@ -2,1 +2,1 @@
- "msg" = "hello"
+ “msg” = “hello”
```

---

## 🧠 思考题

1. **为什么在 `read_file` 中输出的行号我们用 ` | ` 隔开，而在 `edit_file` 替换时却不能把行号作为 `old_string` 的一部分匹配？**
   *(提示：行号是我们的工具在读取时动态拼装上去提供给 LLM 参考的虚拟数据，文件物理磁盘上并没有这些行号。)*
2. **在 `run_shell` 超时设计中，如果超时时间设置得太大或没有超时（例如 30s 变成无穷大），一旦 Agent 运行了持续监听进程（例如跑一个前端 `npm run dev` 服务器），系统会发生什么？**
   *(提示：Agent 将在 `run_shell` 调用中永久死锁，直到父进程被硬杀。)*
3. **为什么 `_truncate_result` 采用"头尾保留、中间截断"的策略，而不是只保留开头或只保留结尾？**
   *(提示：文件开头通常是 import 语句和类定义，结尾通常是报错信息或测试结果——这两部分对 LLM 理解上下文最关键。中间的样板代码往往是冗余的。如果只保留开头，LLM 看不到错误；如果只保留结尾，LLM 缺少上下文。)*

---

## 📦 本节收获

1. **精确替换机制**：掌握了精确匹配替换算法和幻觉拦截设计。
2. **防过载截断**：实现了头尾截断方案，拦截大文件或日志对会话的撑爆。
3. **鲁棒性边缘设计**：懂得了如何通过引号标准化提高工具对自然语言大模型的包容度。

---

> **下一章**：工具定义了 Agent 的物理能力边界，而 System Prompt 则定义了它在面对这些工具时的行为准则。

> **进阶特性预览**：完整源码中 `execute_tool` 还包含 `read_file_state` 参数（mtime 防护：确保文件先读后写）、工具延迟加载（`deferred` 标记）、以及权限系统（`check_permission`）。这些特性将在后续课程中逐一展开。
