# 第 02 课：双后端架构

## 🎯 本节目标

实现 Agent 的双后端支持。允许 Agent 既可以使用 Anthropic API，也可以使用 OpenAI 兼容的 API（如 DeepSeek、Ollama 本地模型或 GPT-4 等），且两者共享相同的工具定义和执行流程。

---

## 🏆 最终效果

完成本节后，用户可以通过修改 `.env` 文件，在不修改代码的情况下，无缝在 Anthropic 和 OpenAI 兼容的后端之间进行切换。

在 `.env` 文件中配置 OpenAI 兼容后端（以使用本地 Ollama 运行的 `qwen2.5-coder` 或在线的 DeepSeek API 为例）：

```bash
# .env
OPENAI_BASE_URL=http://localhost:11434/v1  # 或在线 API 接口
OPENAI_API_KEY=ollama                      # 在线 API 请填写实际 Key
MODEL=qwen2.5-coder                        # 模型名称
```

然后运行：

```bash
cd python
python -m mini_claude "列出当前目录下所有 .py 文件"
```

运行后你将看到与第一课相同的工具调用流程，但整个对话和决策都是由 OpenAI 兼容接口的模型驱动的：

```
🔧 list_files {"pattern": "*.py"}
  mini_claude/__init__.py
  mini_claude/__main__.py
  mini_claude/agent.py
  mini_claude/tools.py

当前目录下有以下 .py 文件：...
```

---

## 🛠️ 本节任务

1. **扩展客户端初始化**：增加 `api_base` 与 `api_key` 参数，实例化 `openai.AsyncOpenAI` 客户端。
2. **编写工具定义转换器**：实现 `_to_openai_tools`，将 Anthropic 格式的工具 Schema 自动转换为 OpenAI 格式。
3. **实现 OpenAI 专属的聊天循环 `_chat_openai`**：独立处理 OpenAI 消息列表格式及 `tool_calls` / `tool` 结果的推入。
4. **重构 `_chat` 入口**：根据客户端配置，自动分发到 `_chat_anthropic`（原 `_chat`）或新实现的 `_chat_openai`。
5. **更新 `__main__.py` 入口**：读取环境变量以支持外部参数传递。

---

## 📦 涉及文件

修改：
- `agent.py`
- `__main__.py`

---

## 🚀 开始实现

### 步骤 1：引入 `BackendConfig` 工厂模式

#### 为什么做

为了支持两套 API，我们需要一个统一的配置层来封装后端选择逻辑。`BackendConfig` 负责"连哪个 API"，`AgentConfig` 负责"Agent 怎么跑"——两者各司其职。

#### 做什么

修改 `agent.py`，新增 `BackendConfig` 类，导入 `openai`，并重构 `Agent.__init__`：

```python
# agent.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
import anthropic
import openai
from .tools import execute_tool, get_tool_definitions


@dataclass
class BackendConfig:
    """后端配置——封装后端选择逻辑，提供工厂方法"""
    provider: Literal["anthropic", "openai"]
    api_key: str
    base_url: str | None = None
    model: str = "claude-sonnet-4-6"

    @classmethod
    def from_env(cls, model: str | None = None, api_base_override: str | None = None) -> "BackendConfig":
        """从环境变量自动检测后端类型并返回配置实例"""
        if api_base_override:
            api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError("--api-base 需要设置 OPENAI_API_KEY 或 ANTHROPIC_API_KEY")
            return cls(provider="openai", api_key=api_key, base_url=api_base_override, model=model or "gpt-4o")
        if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL"):
            return cls(provider="openai", api_key=os.environ["OPENAI_API_KEY"],
                       base_url=os.environ["OPENAI_BASE_URL"], model=model or "gpt-4o")
        if os.environ.get("ANTHROPIC_API_KEY"):
            return cls(provider="anthropic", api_key=os.environ["ANTHROPIC_API_KEY"],
                       base_url=os.environ.get("ANTHROPIC_BASE_URL"), model=model or "claude-sonnet-4-6")
        if os.environ.get("OPENAI_API_KEY"):
            return cls(provider="openai", api_key=os.environ["OPENAI_API_KEY"],
                       base_url=os.environ.get("OPENAI_BASE_URL"), model=model or "gpt-4o")
        raise ValueError("未找到 API Key。请设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY + OPENAI_BASE_URL")

    def create_client(self) -> Any:
        """工厂方法：根据 provider 创建对应的异步 SDK 客户端"""
        if self.provider == "openai":
            return openai.AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return anthropic.AsyncAnthropic(**kwargs)

    @property
    def is_openai(self) -> bool:
        return self.provider == "openai"


@dataclass
class AgentConfig:
    """Agent 行为配置（不含后端信息）"""
    pass


@dataclass
class AgentState:
    """Agent 的运行时状态"""
    pass


class MessageHistory:
    """统一 Anthropic/OpenAI 消息格式的抽象层"""

    def __init__(self, use_openai: bool, system_prompt: str):
        self.use_openai = use_openai
        self.system_prompt = system_prompt
        self._anthropic_messages: list[dict] = []
        self._openai_messages: list[dict] = []
        # OpenAI 协议要求 system prompt 作为消息列表的首条
        if use_openai:
            self._openai_messages.append({"role": "system", "content": system_prompt})

    @property
    def messages(self) -> list[dict]:
        """根据当前后端返回对应的消息列表"""
        return self._openai_messages if self.use_openai else self._anthropic_messages

    @property
    def anthropic_messages(self) -> list[dict]:
        return self._anthropic_messages

    @property
    def openai_messages(self) -> list[dict]:
        return self._openai_messages

    def append_user_message(self, content: str | list) -> None:
        """添加用户消息——两种协议格式相同，无需区分"""
        self.messages.append({"role": "user", "content": content})

    def append_assistant_message(self, content: Any) -> None:
        """添加助手回复。OpenAI 模式下保留 tool_calls 结构"""
        if self.use_openai and isinstance(content, dict) and "role" in content:
            self.messages.append(content)
        else:
            self.messages.append({"role": "assistant", "content": content})

    def append_tool_results(self, results: list[dict]) -> None:
        """添加工具执行结果。两种协议的消息格式不同"""
        if self.use_openai:
            for r in results:
                self.messages.append(r)
        else:
            self.messages.append({"role": "user", "content": results})


class Agent:
    def __init__(self, backend: BackendConfig):
        self.backend = backend
        self.config = AgentConfig()
        self.state = AgentState()
        self.use_openai = backend.is_openai

        system_prompt = "You are a helpful coding assistant with access to tools."
        self.history = MessageHistory(use_openai=self.use_openai, system_prompt=system_prompt)

        # 通过 BackendConfig 工厂方法创建客户端——一行搞定
        self._client = backend.create_client()
```

#### 注意什么

- `BackendConfig.from_env()` 自动从环境变量检测后端类型，`api_base_override` 参数支持命令行 `--api-base` 覆盖。
- `BackendConfig.create_client()` 是工厂方法，Agent 不需要知道如何创建客户端，只管调用。
- `BackendConfig.is_openai` 属性用于判断当前后端类型。
- 需要确保在 Python 虚拟环境中已安装 `openai` 库（可使用 `pip install openai` 安装）。

---

### 步骤 2：实现工具格式转换器 `_to_openai_tools`

#### 为什么做

Anthropic 与 OpenAI 对工具的定义 Schema 包装格式不同。Anthropic 直接接受输入 Schema 数组，而 OpenAI 要求外层包裹 `{"type": "function", "function": {...}}`，并将参数指定在 `parameters` 属性中。

#### 做什么

在 `Agent` 类之前添加模块级函数 `_to_openai_tools` 进行工具定义转换（它不属于任何类，因为与特定实例无关）：

```python
# agent.py（在 Agent 类定义之前）

def _to_openai_tools(tools: list[dict]) -> list[dict]:
    """将 Anthropic 格式的工具定义转换为 OpenAI 格式

    Anthropic: {"name": ..., "description": ..., "input_schema": ...}
    OpenAI:    {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],  # 底层 JSON Schema 一致，只是外层包装不同
            },
        }
        for t in tools
    ]
```

#### 注意什么

- OpenAI 的参数定义在 `parameters` 下，这对应 Anthropic 的 `input_schema`。两者的底层 JSON Schema 字段本身是一致的。

---

### 步骤 3：实现 OpenAI 专属的聊天循环 `_chat_openai`

#### 为什么做

OpenAI API 的消息流与 Anthropic 存在两个关键协议差异：
1. **System Prompt**：OpenAI 不支持顶层的 `system` 参数，必须作为 `{"role": "system", "content": "..."}` 消息插在消息历史 `messages` 列表的开头。
2. **工具调用与结果**：模型工具调用通过 `tool_calls` 返回；执行结果必须通过 `role: "tool"` 消息返回，且必须附带 `tool_call_id`。

#### 做什么

在 `Agent` 类中实现 `_chat_openai` 循环：

```python
# agent.py（续）

    async def _chat_openai(self, user_message: str) -> None:
        """OpenAI 兼容后端的 Agent Loop"""
        # 1. 用户消息推入历史
        self.history.append_user_message(user_message)

        while True:
            # 2. 调用 OpenAI 兼容 API
            response = await self._client.chat.completions.create(
                model=self.backend.model,
                messages=self.history.openai_messages,
                tools=self._to_openai_tools(get_tool_definitions()),
            )
            message = response.choices[0].message

            # 3. 构造 assistant 回复——必须保留 tool_calls 结构，否则后续 tool 消息会报协议错误
            msg_dict = {"role": "assistant", "content": message.content}
            if message.tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ]
            self.history.append_assistant_message(msg_dict)

            # 4. 检查是否有工具调用——循环终止条件与 Anthropic 后端一致
            if not message.tool_calls:
                break

            # 5. 执行工具并将结果（role: "tool"）推入历史
            import json
            tool_results = []
            for tc in message.tool_calls:
                # arguments 是 JSON 字符串，需要解析为 dict
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = {}  # 解析失败时用空 dict，让工具自行处理缺失参数

                result = await execute_tool(tc.function.name, args)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,  # 必须与 assistant 消息中的 tool_calls.id 对应
                    "name": tc.function.name,
                    "content": result,
                })
            self.history.append_tool_results(tool_results)

        # 输出最终回复
        if message.content:
            print(message.content)
```

#### 注意什么

- 保存 `assistant` 回复到 `_messages` 时，务必保留 `tool_calls` 结构。否则，下一次循环将 `tool` 结果发回时，API 会报协议错误（因为 `tool` 结果必须紧跟在包含对应 `tool_calls` 的 `assistant` 回复之后）。
- 每一个 `tool` 消息都必须包含正确的 `tool_call_id`。

---

### 步骤 4：重构 `_chat` 入口进行分发

#### 为什么做

外部调用者不需要关心底层是哪种 API，只需调用统一 of `_chat()` 入口。我们需要根据当前 Agent 的后端配置进行路由转发。

#### 做什么

重构 `agent.py` 中的 `_chat`，将原本的 Anthropic 逻辑提取到 `_chat_anthropic` 中，然后用 `_chat` 进行分发：

```python
# agent.py（续）

    async def _chat(self, user_message: str) -> None:
        """统一入口：根据后端配置自动分发到对应的聊天循环"""
        if self.use_openai:
            await self._chat_openai(user_message)
        else:
            await self._chat_anthropic(user_message)

    async def _chat_anthropic(self, user_message: str) -> None:
        """Anthropic 后端的 Agent Loop——与第 1 课逻辑一致，使用 history 抽象层"""
        self.history.append_user_message(user_message)

        while True:
            response = await self._client.messages.create(
                model=self.backend.model,
                max_tokens=4096,
                system="You are a helpful coding assistant with access to tools.",
                tools=get_tool_definitions(),
                messages=self.history.anthropic_messages,
            )

            # 把 SDK 对象转为 dict 后存入历史
            self.history.append_assistant_message(
                [self._block_to_dict(b) for b in response.content]
            )

            # 检查是否有工具调用——唯一的循环终止判断
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break

            tool_results = []
            for tu in tool_uses:
                result = await execute_tool(tu.name, dict(tu.input))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,  # 关联回对应的 tool_use 调用
                    "content": result,
                })
            # Anthropic 协议：tool_result 用 role: "user" 包裹
            self.history.append_tool_results(tool_results)

        # 输出最终回复
        for block in response.content:
            if block.type == "text":
                print(block.text)

    @staticmethod
    def _block_to_dict(block) -> dict:
        """将 Anthropic SDK 对象转为普通 dict"""
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": dict(block.input) if hasattr(block.input, "items") else block.input,
            }
        return {"type": block.type}
```

#### 注意什么

- `_chat_anthropic` 和 `_chat_openai` 都通过 `self._client` 调用 API。
- 模型名称通过 `self.backend.model` 获取。

---

### 步骤 5：更新 `__main__.py` 入口

#### 为什么做

我们需要在程序入口读取环境变量，让最终用户可以通过配置 `OPENAI_BASE_URL` 轻松测试 OpenAI 兼容后端，而无需手动修改代码。`BackendConfig.from_env()` 封装了环境变量检测逻辑。

#### 做什么

修改 `__main__.py`，用 `BackendConfig.from_env()` 替代手动检测：

```python
# __main__.py

import os
import sys
import asyncio
from dotenv import load_dotenv
from .agent import Agent, BackendConfig

# 加载 .env 文件中的环境变量
load_dotenv()


async def main():
    """程序入口：读取环境变量，自动选择后端并启动 Agent"""
    query = sys.argv[1] if len(sys.argv) > 1 else "列出当前目录下所有 .py 文件"

    model = os.environ.get("MODEL") or "claude-sonnet-4-6"
    backend = BackendConfig.from_env(model=model)
    agent = Agent(backend=backend)
    await agent._chat(query)


if __name__ == "__main__":
    asyncio.run(main())
```

#### 注意什么

- 优先检测 `OPENAI_API_KEY` + `OPENAI_BASE_URL` 组合，其次 `ANTHROPIC_API_KEY`，最后兜底到单独的 `OPENAI_API_KEY`。
- `api_base` 只传给 OpenAI 后端，`anthropic_base_url` 只传给 Anthropic 后端，两者互斥。
- 如果未配置任何 API Key，程序会报错退出。

---

## 消息历史抽象层

为了避免 Anthropic/OpenAI 双后端的代码重复，我们引入了 `MessageHistory` 类：

```python
class MessageHistory:
    def __init__(self, use_openai: bool, system_prompt: str): ...
    def append_user_message(self, content: str | list) -> None: ...
    def append_assistant_message(self, content: Any) -> None: ...
    def append_tool_results(self, results: list[dict]) -> None: ...
    def append_openai_tool_message(self, tool_call_id: str, content: str) -> None: ...
    def get_last_user_message(self) -> dict | None: ...
    def update_last_user_content(self, suffix: str) -> None: ...
    def clear(self, keep_system: bool = True) -> None: ...
    def update_system_prompt(self, prompt: str) -> None: ...
    def message_count(self) -> int: ...
    def to_dict(self) -> dict: ...
    def restore(self, data: dict) -> None: ...
    def replace_anthropic_messages(self, messages: list[dict]) -> None: ...
    def replace_openai_messages(self, messages: list[dict]) -> None: ...
```

核心方法说明：

- **`append_user_message`** -- 添加用户消息，Anthropic 格式直接追加，OpenAI 格式同理。
- **`append_assistant_message`** -- 添加助手回复。OpenAI 模式下若传入含 `role` 的 dict 则直接追加（保留 `tool_calls` 结构），否则统一包裹为 `{"role": "assistant", ...}`。
- **`append_tool_results`** -- 添加工具执行结果。OpenAI 格式逐条追加 `role: "tool"` 消息；Anthropic 格式则包裹在 `role: "user"` 的数组中。
- **`append_openai_tool_message`** -- 专门为 OpenAI 后端追加单条 `role: "tool"` 消息（附带 `tool_call_id`），用于工具并发执行后统一收集结果。
- **`get_last_user_message`** -- 从消息历史末尾反向查找最后一条用户消息，供记忆注入等场景使用。
- **`update_last_user_content`** -- 向最后一条用户消息追加文本（如注入记忆上下文），支持字符串和列表两种 content 格式。
- **`clear`** -- 清空消息历史。若 `keep_system=True` 则保留 OpenAI 模式下首条系统提示消息。
- **`update_system_prompt`** -- 更新系统提示词（如切换 Plan Mode 时），同时更新内部保存的 prompt 和 OpenAI 消息列表的首条消息。
- **`message_count`** -- 返回当前活跃消息列表的消息总数，用于会话元数据记录。
- **`to_dict`** -- 序列化当前消息历史为字典，供会话持久化存储。
- **`restore`** -- 从字典恢复消息历史，供会话加载时使用。
- **`replace_anthropic_messages`** -- 整体替换 Anthropic 消息列表，用于上下文压缩（compact）后的批量更新。
- **`replace_openai_messages`** -- 整体替换 OpenAI 消息列表，用于上下文压缩（compact）后的批量更新。

这个抽象层使得：
1. **工具执行循环只需写一次**：统一处理双后端的 `messages` 数组增长。
2. **消息格式转换逻辑集中管理**：屏蔽 Anthropic 与 OpenAI 格式（如 `tool_use`/`tool_result` 与 `tool_calls`/`tool` 消息）的底层协议差异。
3. **序列化/反序列化逻辑统一**：在持久化保存会话时，无需分头处理，统一调用 `to_dict()` 与 `restore()`。
4. **运行时查询与修改便捷**：`get_last_user_message`、`update_last_user_content`、`update_system_prompt` 等方法支持记忆注入、Plan Mode 切换等高级场景。
5. **上下文压缩支持**：`replace_anthropic_messages` / `replace_openai_messages` 允许压缩后整体替换消息列表，无需暴露内部实现细节。

---

## 🔌 第三方兼容 API 接入

双后端架构不仅支持 Anthropic 官方 API 和 OpenAI 官方 API，还支持各种第三方兼容 API。

### Anthropic 兼容格式（如 OpenRouter、中转代理）

许多第三方服务提供 Anthropic 兼容的 API 接口（如 OpenRouter、各类中转代理）。只需设置 `ANTHROPIC_BASE_URL` 环境变量，无需修改代码：

```bash
# .env
ANTHROPIC_API_KEY=your-api-key
ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1  # 或你的代理地址
```

`BackendConfig.from_env()` 会自动读取 `ANTHROPIC_BASE_URL` 并传给 `anthropic.AsyncAnthropic(base_url=...)`：

```python
# __main__.py — 无需手动处理，from_env() 自动检测
backend = BackendConfig.from_env(model=model)
agent = Agent(backend=backend)
```

`BackendConfig.create_client()` 内部处理了 `base_url` 的透传：

```python
# BackendConfig.create_client() 内部逻辑
def create_client(self):
    if self.provider == "openai":
        return openai.AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)
    kwargs = {"api_key": self.api_key}
    if self.base_url:
        kwargs["base_url"] = self.base_url
    return anthropic.AsyncAnthropic(**kwargs)
```

### OpenAI 兼容格式（如 DeepSeek、Ollama、vLLM）

前面已详细说明，通过设置 `OPENAI_BASE_URL` 即可接入任意 OpenAI 兼容的 API。

### 环境变量速查表

| 后端类型 | 必填变量 | 可选变量 | 典型场景 |
| --- | --- | --- | --- |
| Anthropic 官方 | `ANTHROPIC_API_KEY` | — | 直连 Claude API |
| Anthropic 兼容 | `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL` | — | OpenRouter、中转代理 |
| OpenAI 兼容 | `OPENAI_API_KEY` + `OPENAI_BASE_URL` | `MODEL` | DeepSeek、Ollama、vLLM |

> 💡 当同时配置了 `OPENAI_API_KEY` + `OPENAI_BASE_URL` 和 `ANTHROPIC_API_KEY` 时，`BackendConfig.from_env()` 优先检测 OpenAI 组合（优先级 1），其次 Anthropic（优先级 2）。

---

## ⚖️ 设计权衡

### 方案 A：共用同一个 `_messages` 列表

- **优点**：代码极其简洁，Agent 内部只维护一个统一的历史记录，在单一会话中无需复杂的跨后端转换。
- **缺点**：如果在单次会话中动态切换后端（这在实际应用中几乎不会发生），不同格式的历史消息混合会导致 API 报错。

### 方案 B：独立维护 `_anthropic_messages` 和 `_openai_messages` 两个列表（我们所用的）

- **优点**：后端解耦更彻底，可以避免任何格式混淆的问题，且容易支持运行中动态热切换后端。两种格式的历史消息完全隔离，不会互相干扰。
- **缺点**：初始化、持久化和状态重构的代码量会增加，但通过 `MessageHistory` 类封装后，复杂度被有效屏蔽。

**结论**：本项目选择方案 B。`MessageHistory` 类通过 `use_openai` 标志维护两个独立的列表，对外提供统一的 `append_user_message` / `append_assistant_message` / `append_tool_results` 等接口，调用者无需关心底层格式差异。这一设计也使得会话序列化（`to_dict` / `restore`）和上下文压缩（`compact`）等高级功能更容易实现。

---

## ⚠️ 常见陷阱

### 1. 遗漏 `tool_calls` 的回复内容

在推入 `assistant` 消息到 `_messages` 时，不能只保存文本内容（`content`），必须把包含工具调用的 `tool_calls` 结构一起存入：

```python
# ❌ 错误：这会导致下一次发送消息时 API 报错，因为 tool 消息找不到关联的 tool_calls
self._messages.append({"role": "assistant", "content": message.content})
```

**后果**：OpenAI 协议强制要求，所有的 `role: "tool"` 消息之前，必须紧跟一条带有对应 `tool_calls` 列表的 `assistant` 消息。

---

### 2. 系统提示词（System Prompt）的位置错误

OpenAI 协议中不支持顶层的 `system` 参数，必须作为 `{"role": "system", "content": ...}` 消息插在消息历史的最前面。

```python
# ❌ 错误：OpenAI completions 接口不支持顶层 system 参数
response = await self._client.chat.completions.create(
    model=self.backend.model,
    system="...",
    messages=self._messages
)
```

**修正**：在请求时动态将系统提示词拼装在 `messages` 列表 the 第 0 位：`messages=[system_prompt] + self._messages`。

---

## ✅ 验收点

### 输入

在终端中设置 OpenAI 兼容后端（此处以使用本地 Ollama 运行的 `qwen2.5-coder` 模型为例）：

**macOS / Linux**:
```bash
cd python
export OPENAI_BASE_URL="http://localhost:11434/v1"
export OPENAI_API_KEY="ollama"
export MODEL="qwen2.5-coder"
python -m mini_claude "列出当前目录下所有 .py 文件"
```

**Windows (PowerShell)**:
```powershell
cd python
$env:OPENAI_BASE_URL="http://localhost:11434/v1"
$env:OPENAI_API_KEY="ollama"
$env:MODEL="qwen2.5-coder"
python -m mini_claude "列出当前目录下所有 .py 文件"
```

### 预期结果

程序使用指定的本地模型启动，成功调用 `list_files` 工具，读取文件列表并最终以中文回复文件列表，过程与使用 Claude 效果相同。

### 失败时如何排查

| 症状 | 可能原因 | 排除方法 |
|---|---|---|
| `NotFoundError` / `InvalidURL` | `OPENAI_BASE_URL` 格式错误 | 检查接口地址，特别注意末尾是否漏掉 `/v1`。如 Ollama 默认为 `http://localhost:11434/v1`。 |
| `AuthenticationError` | API Key 错误 | 检查环境变量是否成功注入。如果是本地 Ollama，Key 可填写任意字符串。 |
| `BadRequestError: 'messages' must follow...` | 消息推入顺序不对 | 检查 `_chat_openai` 中保存 `assistant` 回复时，是否正确包裹了 `tool_calls`。 |

---

## 🧠 思考题

1. **为什么 OpenAI 协议中每个工具结果（`role: "tool"`）必须单独作为一个消息发送，而 Anthropic 可以在一个 `user` 消息的数组里放多个工具结果？**
   *(提示：OpenAI 要求 `role: "tool"` 消息必须单独存在，并使用 `tool_call_id` 与具体的调用进行平级关联。)*

2. **如果在单轮循环中模型决定同时调用 3 个工具，OpenAI 协议的历史中会增加多少条消息？**
   *(提示：1 条带 3 个 tool_calls 的 assistant 消息 + 3 条单独 the tool 消息 = 共 4 条消息。)*

---

## 📦 本节收获

1. **双后端架构的设计**：理解了如何通过抽象公共方法，同时支持 Anthropic 与 OpenAI 兼容协议。
2. **多协议适配的差异性**：掌握了 OpenAI 中 System Prompt、`tool_calls` 与 `role: "tool"` 的协议特殊要求。
3. **环境适应力**：使 Agent 摆脱单一闭源 API 的限制，具备了接入任意本地模型（Ollama/vLLM）或更具性价比的模型（如 DeepSeek/Qwen）的能力。

---

> **下一章**：有了双后端支持，下一步是让 Agent 具备真正的操作能力——工具系统。
