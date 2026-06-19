# 第 01 课：最小 Agent Loop

## 🎯 本节目标

实现 coding agent 的心脏：一个 while 循环，让 LLM 能调用工具、拿到结果、继续思考，直到任务完成。

---

## 🏆 最终效果

完成本节后，运行以下命令：

```bash
cd python
python -m mini_claude "列出当前目录下所有 .py 文件"
```

你会看到终端输出类似：

```
🔧 list_files {"pattern": "*.py"}
  mini_claude/__init__.py
  mini_claude/__main__.py
  mini_claude/agent.py
  mini_claude/tools.py
  ...

当前目录下有以下 .py 文件：...
```

Agent 自动调用了 `list_files` 工具，拿到结果后组织语言回复你——**整个过程由 LLM 决策驱动，而不是你写死的 if/else**。

> 💡 **说明**：本课实现的是单轮对话——输入一条指令，Agent 自动完成任务后退出。多轮交互式对话将在第 02 课末尾引入。

---

## 🛠️ 本节任务

1. 创建 `Agent` 类骨架：消息数组 + API 客户端初始化
2. 实现 `_chat()` 核心循环：while → 调用 LLM → 检查 tool_use → 执行 → 继续
3. 注册一个最简工具 `list_files`，让 LLM 有能力操作外部世界
4. 验收：输入自然语言，Agent 自动调用工具并返回结果

---

## 📦 涉及文件

创建：
- `agent.py`
- `tools.py`
- `__main__.py`
- `__init__.py`

---

## 🚀 开始实现

### 步骤 1：消息数组——Agent 的记忆

#### 为什么做

LLM API 是无状态的——每次调用不会记住上一次说了什么。要让 Agent 能"连续对话"，必须把**完整历史消息**每次都发给 API。

消息数组就是 Agent 的"工作记忆"。

#### 做什么

创建 `agent.py`，定义 `AgentConfig` 和 `AgentState`，以及 `Agent` 类的骨架，并从 `tools.py` 导入工具定义与执行函数：

```python
# agent.py

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import cast
import anthropic
from anthropic.types import MessageParam, ToolUseBlockParam, TextBlockParam, ToolResultBlockParam
from anthropic.types.tool_param import ToolParam
from dotenv import load_dotenv
from .tools import execute_tool, get_tool_definitions

# 加载 .env 文件中的环境变量（如 ANTHROPIC_API_KEY）
load_dotenv()


@dataclass
class AgentConfig:
    """Agent 的静态配置，运行期间保持不变"""
    model: str = "claude-sonnet-4-6"  # 默认使用的 LLM 模型


@dataclass
class AgentState:
    """Agent 的运行时状态，随会话进行动态变化"""
    pass


class Agent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.state = AgentState()
        self._client = anthropic.AsyncAnthropic()  # 异步 Anthropic API 客户端
        self._messages: list[MessageParam] = []  # 消息历史：Agent 的工作记忆
```

#### 配置 API 密钥

`anthropic.AsyncAnthropic()` 初始化时会自动读取环境变量 `ANTHROPIC_API_KEY`。我们使用 `python-dotenv` 库从 `.env` 文件加载环境变量，避免在终端历史中暴露密钥。

首先安装依赖：

```bash
pip install python-dotenv
```

然后在项目根目录创建 `.env` 文件：

```bash
# .env（Anthropic 官方 API）
ANTHROPIC_API_KEY=sk-ant-api03-xxx  # 替换成你的真实密钥
```

如果使用第三方兼容 API（如 DeepSeek），需要同时配置 `ANTHROPIC_BASE_URL`：

```bash
# .env（DeepSeek Anthropic 兼容接口）
ANTHROPIC_API_KEY=sk-xxx           # 替换成你的真实密钥
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
```

同时需要将 `AgentConfig` 中的默认模型改为 API 支持的模型名称（如 `deepseek-v4-flash`）。

> ⚠️ **安全提示**：`.env` 文件包含敏感信息，务必将其加入 `.gitignore`，不要提交到版本控制。

`_messages` 是核心数据结构。它的增长方式：

```
用户说"列出 .py 文件"后：
  messages = [
    { role: "user",      content: "列出 .py 文件" }
  ]

LLM 返回 tool_use 后：
  messages = [
    { role: "user",      content: "列出 .py 文件" },
    { role: "assistant", content: [text + tool_use(list_files)] },
    { role: "user",      content: [tool_result("文件列表...")] },
  ]

LLM 认为任务完成后：
  messages = [
    ...前 3 条,
    { role: "assistant", content: [text("当前目录有以下文件...")] }
  ]
```

**每轮循环增长两条**：一条 assistant（LLM 的回复），一条 user（工具结果）。模型每次都能看到完整历史，这是它能"记住"之前做过什么的原因。

#### 注意什么

- 工具结果用 `role: "user"` 推入——这是 Anthropic API 的协议要求，结果必须通过 `tool_use_id` 关联回对应的调用
- 消息数组只增不减，复杂任务跑几十轮后会撑爆上下文窗口——这个问题留到第 7 章解决

---

### 步骤 2：核心循环——while + tool_use 检查

#### 为什么做

Agent 和普通聊天机器人的区别：**Agent 会主动做事**。

普通聊天：用户问 → LLM 答 → 结束。
Agent：用户问 → LLM 决定调用工具 → 执行工具 → 结果喂回 LLM → LLM 继续思考 → 可能再调工具 → ……直到 LLM 认为任务完成。

这个循环就是 Agent Loop。

#### 做什么

在 `Agent` 类中添加 `_chat()` 方法：

```python
# agent.py（续）

    async def _chat(self, user_message: str) -> None:
        """Agent Loop 核心：循环调用 LLM 直到任务完成"""
        # 1. 把用户消息推入历史
        self._messages.append({"role": "user", "content": user_message})

        while True:
            # 2. 调用 LLM
            response = await self._client.messages.create(
                model=self.config.model,
                max_tokens=4096,
                system="You are a helpful coding assistant with access to tools.",
                tools=cast(list[ToolParam], get_tool_definitions()),  # 从 tools.py 导入
                messages=self._messages,
            )

            # 3. 把 LLM 的回复推入历史——必须在检查 tool_use 之前，否则上下文断裂
            self._messages.append({
                "role": "assistant",
                "content": [self._block_to_dict(b) for b in response.content],
            })

            # 4. 检查是否有 tool_use——这是循环终止的唯一判断条件
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break  # 没有工具调用 → 任务完成，退出循环

            # 5. 执行工具，把结果推入历史
            tool_results = []
            for tu in tool_uses:
                result = await execute_tool(tu.name, dict(tu.input))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,  # 必须关联回对应的 tool_use 调用
                    "content": result,
                })
            # 工具结果用 role: "user" 推入——这是 Anthropic API 的协议要求
            self._messages.append({"role": "user", "content": tool_results})

        # 输出最终回复——当前用 print 直接输出，到第 7 课会替换为流式字符渲染（_emit_text）
        for block in response.content:
            if block.type == "text":
                print(block.text)

    @staticmethod
    def _block_to_dict(block) -> dict:
        """将 Anthropic SDK 对象转为普通 dict，因为消息数组只能存 dict"""
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                # block.input 可能是 dict 或自定义对象，需要统一处理
                "input": dict(block.input) if hasattr(block.input, "items") else block.input,
            }
        return {"type": block.type}
```

循环逻辑只有 **5 步**，核心判断只有一个：**`tool_uses` 是否为空**。

- 有 tool_use → 执行工具 → 结果推入消息 → 继续循环
- 没有 tool_use → LLM 认为任务完成 → break

**是 LLM，而不是代码逻辑，决定任务何时完成。**

#### 注意什么

- `_block_to_dict()` 把 Anthropic SDK 的对象转成普通 dict——因为消息数组里只能存 dict，不能存 SDK 对象
- `tool_use_id` 必须原样传回，它是 LLM 关联"调用"和"结果"的唯一标识
- 所有工具调用是**串行**的（一个 for 循环逐个执行）。Claude Code 用 `StreamingToolExecutor` 在流式响应期间并行执行，但那是第 5 章的内容

---

### 步骤 3：第一个工具——list_files

#### 为什么做

没有工具，LLM 只是一个聊天机器人。工具是 Agent 和真实世界交互的桥梁。

LLM 本身不能读文件、不能跑命令——但我们可以**在 System Prompt 里告诉它有哪些工具**，它返回结构化的工具调用请求，我们执行后把结果喂回去。

#### 做什么

创建 `tools.py`，定义工具和执行逻辑：

```python
# tools.py

from __future__ import annotations

import os
from pathlib import Path


# 工具定义：告诉 LLM 有哪些工具可用
tool_definitions: list[dict] = [
    {
        "name": "list_files",
        "description": "List files matching a glob pattern. Returns matching file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": 'Glob pattern to match files (e.g., "**/*.py", "src/**/*")',
                },
                "path": {
                    "type": "string",
                    "description": "Base directory to search from. Defaults to current directory.",
                },
            },
            "required": ["pattern"],
        },
    },
]


def get_tool_definitions() -> list[dict]:
    """返回所有工具的定义，供 LLM API 调用时传入"""
    return tool_definitions


# 单次列文件的最大条目数——防止超长输出撑爆上下文
MAX_LIST_FILES = 200


# 工具执行：根据名称分发到具体实现
async def execute_tool(name: str, inp: dict) -> str:
    if name == "list_files":
        return _list_files(inp)
    return f"Unknown tool: {name}"  # 返回字符串而非抛异常，让 LLM 自行修正


def _list_files(inp: dict) -> str:
    """列出目录下匹配 glob 模式的文件"""
    base = Path(inp.get("path") or ".")
    pattern = inp["pattern"]
    files = []
    for p in base.glob(pattern):
        if p.is_file():
            rel = str(p.relative_to(base) if base != Path(".") else p)
            # 跳过 node_modules 和 .git，避免返回垃圾结果
            if "node_modules" in rel or ".git" in rel.split(os.sep):
                continue
            files.append(rel)
            if len(files) >= MAX_LIST_FILES:
                break  # 达到上限立即停止，避免不必要的遍历
    if not files:
        return "No files found matching the pattern."
    return "\n".join(files[:MAX_LIST_FILES])
```

工具定义的结构是 **Anthropic Tool Use 协议**要求的格式：

- `name`：工具名，LLM 返回的 `tool_use` 块里会引用这个名字
- `description`：告诉 LLM 这个工具做什么、什么时候该用它
- `input_schema`：JSON Schema，定义工具接受哪些参数

#### 注意什么

- `description` 的质量直接影响 LLM 是否正确使用工具——写得太模糊它会乱调，写得太窄它会漏调
- `glob()` 会自动跳过 `node_modules` 和 `.git`，避免返回垃圾结果
- 结果限制 `MAX_LIST_FILES`（200）个文件，防止超长输出撑爆上下文

---

### 步骤 4：组装入口

#### 为什么做

Agent 和工具就绪后，需要一个入口把它们串起来。

#### 做什么

首先，在 `python/mini_claude` 目录下新建一个空白的 `__init__.py` 文件（若未创建）使其被识别为 Python 包。

然后在 `__main__.py` 中添加入口，支持从命令行参数读取查询：

```python
# __main__.py

import sys
import asyncio
from .agent import Agent, AgentConfig


async def main():
    """程序入口：从命令行参数读取查询并启动 Agent"""
    # 读取命令行参数作为用户查询，默认为列出 .py 文件
    query = sys.argv[1] if len(sys.argv) > 1 else "列出当前目录下所有 .py 文件"
    agent = Agent(config=AgentConfig())
    await agent._chat(query)  # 暂用 _chat，后续章节会扩展为完整的 chat 方法


if __name__ == "__main__":
    asyncio.run(main())
```

#### 注意什么

- 这里用 `_chat()` 而不是 `chat()`——暂时不处理会话管理、中断、成本统计等，后续章节逐步添加
- 需要设置环境变量 `ANTHROPIC_API_KEY` 才能运行

---

## 配置与状态分离

在设计复杂的 Agent 系统时，随着功能的增加，`Agent` 类会引入大量的属性。如果将配置（如模型名称、安全模式）与运行时状态（如 Token 统计、轮数计数）混杂在一起，会导致状态管理混乱、测试困难。

为此，我们将属性明确拆分为两个数据类（Dataclass）：

### AgentConfig（配置）
- **职责**：定义 Agent 的静态或相对稳定的配置。
- **特点**：在创建时确定，运行期间基本保持不变，容易序列化与比较。
- **包含属性**：`permission_mode`, `max_cost_usd`, `max_turns` 等。

### AgentState（状态）
- **职责**：维护 Agent 运行过程中的动态状态。
- **特点**：在运行期间频繁变化，用于流程控制和指标统计。
- **包含属性**：`total_input_tokens`, `current_turns`, `aborted`, `confirmed_paths` 等。

这种职责分离带来了诸多工程优势：
1. **结构清晰**：配置与状态分明，避免“上帝类”的属性膨胀。
2. **易于测试**：测试时可以独立实例化配置或状态，方便模拟边界情况。
3. **便于持久化**：仅序列化 `AgentConfig` 即可保存配置；保存/恢复会话时只需序列化 `AgentState` 和消息历史。

---

## 错误处理最佳实践

在 Agent 系统中，我们会与网络 API、本地文件系统、Shell 子进程等多个充满不确定性的外部环境进行交互。过于宽泛的错误处理（如 `except Exception: pass`）是 Agent 系统的重大隐患，这会导致：
- **静默失败**：Bug 被掩盖，Agent 误以为操作成功而做出错误决策。
- **调试困难**：日志缺失，无法回溯错误原因。

因此，我们必须遵守以下错误处理最佳实践：
1. **捕获具体异常**：严禁无目的的 `pass`。若需捕获，应锁定特定异常类（如 `OSError`、`json.JSONDecodeError`、`asyncio.CancelledError` 等）。
2. **合理使用日志记录**：引入 Python 标准 `logging` 模块，在 `except` 块中通过 `logger.warning` 或 `logger.debug` 记录异常堆栈，便于开发定位。
3. **将错误作为数据喂回**：对于工具执行（如读写文件失败），不要让程序直接崩溃退出，而应当将错误信息作为 `tool_result` 文本（带 `Error:` 前缀）返回给大模型。让大模型知晓发生了什么错误，并自主尝试修复或改用其他工具。

---

## ⚖️ 设计权衡

### 消息数组 vs 状态机

方案 A：**消息数组**（我们用的）
- 每轮把完整历史发给 LLM
- 优点：LLM 能看到完整上下文，实现简单
- 缺点：消息越来越长，最终撑爆上下文窗口

方案 B：**状态机 + 摘要**
- 每轮只发最近 N 条消息 + 之前的摘要
- 优点：上下文占用可控
- 缺点：摘要会丢失细节，实现复杂

结论：先用消息数组，撑爆了再压缩（第 7 章）。

### 串行工具执行 vs 并行

方案 A：**串行**（我们用的）
- 一个 for 循环逐个执行工具
- 优点：简单、不会出并发问题
- 缺点：多个独立工具时浪费时间

方案 B：**并行执行**
- 用 `asyncio.gather()` 并发执行所有工具
- 优点：多个只读工具可以同时跑
- 缺点：有副作用的工具（如写文件）并行执行可能冲突

结论：先串行，第 5 章加流式输出时再引入并行。

---

## ⚠️ 常见陷阱

### 1. 忘记把 tool_result 推入消息数组

```python
# ❌ 错误：执行了工具但没把结果推入消息
for tu in tool_uses:
    result = await execute_tool(tu.name, dict(tu.input))
    # result 丢了！LLM 看不到工具结果
```

结果：LLM 不知道工具返回了什么，会反复调用同一个工具，**死循环**。

修正：执行完工具后，必须把 `tool_result` 推入 `self._messages`。

### 2. tool_use_id 没有关联上

```python
# ❌ 错误：tool_result 里没有 tool_use_id
tool_results.append({"type": "tool_result", "content": result})
```

结果：API 报错，因为 LLM 不知道这个结果对应哪次调用。

修正：每个 `tool_result` 必须包含 `tool_use_id`，值来自对应 `tool_use` 块的 `id`。

### 3. 忘记把 assistant 回复推入历史

```python
# ❌ 错误：只推了 tool_result，没推 assistant 的回复
if not tool_uses:
    break
# 漏了 self._messages.append({"role": "assistant", ...})
```

结果：下一轮 LLM 看不到自己上一轮说了什么，上下文断裂。

修正：在检查 tool_uses **之前**，先把 assistant 的完整回复推入消息数组。

---

## ✅ 验收点

### 输入

确保已在项目根目录的 `.env` 文件中配置了 `ANTHROPIC_API_KEY`（参见步骤 1），然后运行：

```bash
cd python
python -m mini_claude "列出当前目录下所有 .py 文件"
```

> 💡 如果不想使用 `.env` 文件，也可以通过环境变量临时设置：`ANTHROPIC_API_KEY=sk-ant-xxx python -m mini_claude "..."`

### 预期结果

终端输出应包含：
1. LLM 返回的 `tool_use` 块（显示 `list_files` 被调用）
2. 工具执行结果（文件列表）
3. LLM 根据文件列表组织的自然语言回复

### 失败时如何排查

| 症状 | 可能原因 |
|------|---------|
| `AuthenticationError` | API Key 未设置或已过期 |
| `tool_use` 块没有出现 | LLM 认为不需要工具（prompt 太模糊） |
| 工具返回空结果 | glob pattern 不匹配，检查当前目录 |
| 死循环：反复调用同一工具 | tool_result 没推入消息数组 |

---

## 🧠 思考题

1. **如果 LLM 一次返回多个 `tool_use` 块，当前实现会怎样处理？** 这种情况下 LLM 认为什么？（提示：想想"同时需要读两个文件"的场景）

2. **为什么工具结果要用 `role: "user"` 推入，而不是 `role: "tool"`？** 这是 Anthropic API 的设计选择——如果不这样做会有什么问题？（提示：API 要求 user/assistant 严格交替）

3. **当前实现在什么情况下会"死循环"？** 思考：如果 LLM 每次都返回 tool_use 但工具每次都返回同样的结果，循环会怎样退出？（提示：不会退出——这就是为什么后续需要预算控制）

---

## 📦 本节收获

1. **Agent Loop 的本质**：`while True` + `tool_use` 检查。有工具调用就执行并继续，没有就退出——是 LLM 决定任务何时完成。

2. **消息数组是 Agent 的"记忆"**：每轮增长两条（assistant + tool_result），模型每次都能看到完整历史。

3. **工具是 LLM 和真实世界的桥梁**：在 System Prompt 里描述工具，LLM 返回结构化调用请求，代码执行后把结果喂回去。

4. **一个最简 Agent 只需要 ~60 行代码**：消息数组 + while 循环 + 工具执行 + 结果回填——复杂性来自后续的工程优化，不是核心逻辑。

---
> **下一章**：当前 Agent 只支持 Anthropic 后端。第二课将引入 `BackendConfig` 工厂模式，实现双后端支持——让 Agent 能同时使用 Anthropic 和 OpenAI 兼容的 API。
