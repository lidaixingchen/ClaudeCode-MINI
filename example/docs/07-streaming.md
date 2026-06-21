# 第 07 课：文本流式输出与 API 重试

## 🎯 本节目标

为 Agent 构建流畅的流式字符输出界面和稳健的 API 网络容错机制。实现 Anthropic 与 OpenAI 两套后端的文本流渲染（逐字显示回复），滤除大模型的 Extended Thinking 冗余 Token 以节省上下文，并为 API 请求注入带随机抖动的“指数退避”重试保护。

> **💡 本课与第 06 课的关系**
>
> - **第 06 课**（Anthropic 专属）：利用 Anthropic 流式 API 的 `content_block_stop` 事件实现**实时抢跑**——工具参数一生成完就开始执行
> - **第 07 课**（双后端）：实现文本流式输出和 OpenAI 流式工具调用的**增量拼装**
>
> 两课的核心区别在于 API 协议设计不同：
>
> - **Anthropic**：有明确的”块结束”信号，支持实时抢跑
> - **OpenAI**：工具参数通过多个 delta 片段累加，需要流结束后才能组装完整调用，使用 `asyncio.gather` 并行执行

---

## 🏆 最终效果

完成本节后，运行 Agent 时你将看到：
- **逐字打字机效果**：大模型的回答不再是沉默等待数十秒后一次性砸向屏幕，而是字符如瀑布般顺畅流出，首字响应时间降至数百毫秒。
- **思维隐藏**：大模型的 Extended Thinking（思考链）Token 仅在流式生成期间显示，完成后自动被过滤，防止撑爆消息历史。
- **网络容错**：当遇到服务临时过载（429 报错）或网络瞬断时，终端会自动打印类似 `↻ Retry 1/3: HTTP 429` 的重试信息，自动指数退避延时后继续请求，确保执行不会意外中断。

---

## ⚠️ 前置准备：移除临时输出代码

从第 1 课到第 6 课，我们一直在 `_chat_anthropic` 和 `_chat_openai` 循环末尾使用临时代码输出回复。本课实现流式输出后，文本会在 API 响应过程中逐字实时打印，循环结束后不再需要额外输出。请在开始本课前，**删除两处临时输出代码**：

**1. 删除 `_chat_anthropic` 末尾的 Anthropic 风格输出：**

```python
# 临时方案——本课将移除
for block in response.content:
    if block.type == "text":
        print(block.text)
```

**2. 删除 `_chat_openai` 末尾的 OpenAI 风格输出：**

```python
# 临时方案——本课将移除
if message.content:
    print(message.content)
```

---

## 🛠️ 本节任务

1. **实现流式字符渲染方法**：实现 `_emit_text`，处理流式文本输出以及子代理输出缓冲。
2. **实现 Anthropic 流式文本与思考过滤**：在 `_call_anthropic_stream` 中实现流式输出并过滤掉 `thinking` 块。
3. **实现 OpenAI 增量分块参数重建**：在 `_call_openai_stream` 中手动拼装增量分片的 `arguments` 并流式渲染正文。
4. **编写指数退避与抖动重试封装**：实现 `_is_retryable` 与 `_with_retry`，保护两套流式接口不受瞬时网络故障影响。

---

## 📦 涉及文件

修改：
- `agent.py`

---

## 🚀 开始实现

### 步骤 0：实现 thinking 模式检测与输出 Token 限制

#### 为什么做

在实现流式输出之前，我们需要先确定两个关键的辅助函数：
1. **thinking 模式检测**：判断当前模型是否支持 Extended Thinking（思考链）功能，以及是否支持自适应思考模式。这决定了我们在调用 API 时是否启用 thinking 参数。
2. **输出 Token 限制**：根据不同的模型版本，设置合理的最大输出 Token 数，避免超出模型的上下文窗口限制。

#### 做什么

在 `agent.py` 中实现模型能力检测函数和 Token 限制函数：

```python
# agent.py 中的修改

import os

# 已知支持推理/思考的第三方模型关键词（小写）
_THINKING_MODEL_KEYWORDS = ("deepseek-r1", "qwq", "grok-3", "reasoning", "think")


def _model_supports_thinking(model: str) -> bool:
    """判断模型是否支持 Extended Thinking（思考链）功能。

    检测优先级：
    1. Claude 模型硬编码（已知型号自动识别）
    2. THINKING_MODE 环境变量显式覆盖（第三方模型推荐方式）
    3. 关键词启发式匹配（deepseek-r1、qwq 等）
    4. 默认返回 False（安全回退）
    """
    m = model.lower()
    # 1. Claude 3 系列明确不支持 thinking
    if "claude-3-" in m or "3-5-" in m or "3-7-" in m:
        return False
    # 2. Claude 4+ 支持
    if "claude" in m and any(x in m for x in ("opus", "sonnet", "haiku")):
        return True
    # 3. 第三方模型：环境变量显式覆盖（最高优先级）
    env_override = os.environ.get("THINKING_MODE", "").lower()
    if env_override in ("enabled", "adaptive"):
        return True
    if env_override == "disabled":
        return False
    # 4. 第三方模型：关键词启发式匹配
    return any(kw in m for kw in _THINKING_MODEL_KEYWORDS)


def _model_supports_adaptive_thinking(model: str) -> bool:
    """判断模型是否支持自适应思考模式（可动态调整思考深度）。

    仅 Claude opus-4-6 / sonnet-4-6 原生支持。
    第三方模型需显式设置 THINKING_MODE=adaptive 才会启用。
    """
    m = model.lower()
    # Claude 已知型号
    if "opus-4-6" in m or "sonnet-4-6" in m:
        return True
    # 第三方：环境变量显式指定 adaptive 时信任用户
    return os.environ.get("THINKING_MODE", "").lower() == "adaptive"


def _get_max_output_tokens(model: str) -> int:
    """根据模型版本返回最大输出 Token 数。

    Claude 模型使用硬编码值；第三方模型通过 MAX_OUTPUT_TOKENS 环境变量覆盖。
    """
    m = model.lower()
    # Claude 系列硬编码
    if "opus-4-6" in m:
        return 64000  # 最新旗舰模型有更大输出空间
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    # 第三方模型：环境变量覆盖（如 MAX_OUTPUT_TOKENS=65536）
    env_val = os.environ.get("MAX_OUTPUT_TOKENS")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    return 16384  # 默认回退值


# 思考强度级别 → 占 max_output 的比例
_THINKING_EFFORT_RATIOS: dict[str, float] = {
    "low": 0.10,       # 10% — 快速浅层推理，适合简单任务
    "medium": 0.30,    # 30% — 中等深度推理
    "high": 0.60,      # 60% — 深度推理，适合复杂任务
    "max": 1.00,       # 100% — 最大推理深度，几乎全部 token 留给思考
}


def _get_thinking_budget(model: str, max_output: int) -> int:
    """获取思考链的 token 预算。

    优先级：
    1. THINKING_EFFORT 环境变量（语义化级别：low / medium / high / max）
    2. THINKING_BUDGET 环境变量（原始 token 数，保留向后兼容）
    3. 默认 max（全部 token 留给思考）
    预算值会被钳制到 [1024, max_output - 1] 范围内。
    """
    # 1. 语义化思考强度（优先）
    effort = os.environ.get("THINKING_EFFORT", "").lower()
    if effort in _THINKING_EFFORT_RATIOS:
        ratio = _THINKING_EFFORT_RATIOS[effort]
        budget = int(max_output * ratio)
        return max(1024, min(budget, max_output - 1))

    # 2. 原始 token 数（向后兼容）
    env_val = os.environ.get("THINKING_BUDGET")
    if env_val:
        try:
            requested = int(env_val)
            return max(1024, min(requested, max_output - 1))
        except ValueError:
            pass

    # 3. 默认最大值
    return max_output - 1
```

#### 注意什么

- **模型版本识别**：`_model_supports_thinking` 函数通过字符串匹配来识别模型版本。Claude 3 系列（如 claude-3-opus、claude-3.5-sonnet）不支持 thinking，而更新的版本（如 claude-4-opus、claude-4-sonnet）则支持。
- **自适应思考**：只有最新的 opus-4-6 和 sonnet-4-6 版本支持自适应思考模式，这种模式可以动态调整思考深度。
- **Token 限制策略**：不同模型的上下文窗口大小不同，因此需要根据模型版本设置合理的输出 Token 上限，避免请求失败。
- **思考强度（Thinking Effort）**：`_get_thinking_budget` 函数控制思考链的 token 预算。支持两种调节方式：
  - **`THINKING_EFFORT`**（推荐）：语义化级别，可选 `low`（10%）、`medium`（30%）、`high`（60%）、`max`（100%），直观易用
  - **`THINKING_BUDGET`**（高级）：直接指定 token 数，保留向后兼容
  - 预算值会被钳制到 `[1024, max_output - 1]` 范围内。对于第三方推理模型，使用 `low` 或 `medium` 可以降低延迟和成本
- **第三方模型支持**：OpenAI 兼容后端的模型名是无限开放的，不可能穷举所有型号。因此我们采用**三层检测策略**：
  1. **Claude 硬编码**（自动识别）：已知的 Claude 型号直接匹配
  2. **环境变量覆盖**（推荐方式）：通过 `THINKING_MODE`、`THINKING_EFFORT`、`MAX_OUTPUT_TOKENS`、`CONTEXT_WINDOW` 等环境变量，用户显式声明第三方模型的能力
  3. **关键词启发式匹配**（兜底）：对 `deepseek-r1`、`qwq`、`grok-3` 等已知推理模型做关键词匹配
- **第三方模型使用示例**：

  ```bash
  # DeepSeek R1 推理模型（最大思考深度）
  export THINKING_MODE=enabled
  export THINKING_EFFORT=max
  export MAX_OUTPUT_TOKENS=32768
  python __main__.py --model deepseek-r1 --api-base https://api.deepseek.com/v1

  # QwQ 推理模型（浅层思考，降低延迟和成本）
  export THINKING_MODE=enabled
  export THINKING_EFFORT=low
  python __main__.py --model qwq-32b --api-base https://api.openai.com/v1
  ```

---

### 步骤 1：实现流式字符渲染接口

#### 为什么做

由于 Python 默认的 `print()` 会自动换行，且标准输出（stdout）默认有缓冲区，在不换行打印时常常不会即时显示。我们需要通过调用底层 `sys.stdout.write` 并强行刷新（`sys.stdout.flush()`）来实现实时逐字打印。此外，子代理（Sub-agent）运行时，其输出需要被静默缓冲，不能直接打在主终端上。

#### 做什么

修改 `agent.py`，实现 `_emit_text` 方法分流控制：

```python
# agent.py 中的修改

import json
import sys
from ui import print_assistant_text, stop_spinner  # UI 库封装了 sys.stdout.write/flush


class Agent:
    # ... 在 __init__ 中定义 self._output_buffer: list[str] | None = None

    def _emit_text(self, text: str) -> None:
        """流式文本输出：区分主代理直接打印 vs 子代理缓冲收集。"""
        # 子代理运行时缓冲输出，避免干扰主终端显示
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        else:
            # 主代理直接调用 UI 层进行格式化输出
            print_assistant_text(text)


#### 注意什么

- **状态存储区分**：在本节的教学简化版中我们直接将 `self._output_buffer` 定义在实例上。但在实际完整 codebase 的架构中，为了统一管理运行状态，我们将其保存在状态容器 `self.state.output_buffer` 中。
- **UI 模块结合**：这里调用的 `print_assistant_text()` 是第 5 课所创建的 `ui.py` 中定义的函数，它可以确保流式字符的输出格式整齐。
```

---

### 步骤 2：实现 Anthropic 后端文本流与思考过滤

#### 为什么做

Anthropic API 的流式响应会混杂输出 `text` 块和 `thinking` 块。
1. `thinking` 块包含模型的中间思考步骤，通常极其庞大（数千 Token），直接存入消息历史会导致后续上下文极速膨胀。我们必须在响应完全接收后过滤掉它们。
2. `text` 块是返回给用户的自然语言，需捕获它并调用 `_emit_text` 实时输出。

#### 做什么

在 `agent.py` 的 `_call_anthropic_stream` 中实现流监听及思考链清洗逻辑：

```python
# agent.py（续）

    async def _call_anthropic_stream(self, on_tool_block_complete=None) -> Any:
        """Anthropic 后端流式调用：处理 SSE 事件流，实时渲染文本并过滤思考链。"""
        async def _do():
            max_output = _get_max_output_tokens(self.backend.model)
            create_params: dict[str, Any] = {
                "model": self.backend.model,
                # thinking 模式下需要更大输出空间，禁用时回退到默认值
                "max_tokens": max_output if self.state.thinking_mode != "disabled" else 16384,
                "system": self._system_prompt,
                "tools": get_tool_definitions(),
                "messages": self.history.anthropic_messages,
            }

            # 根据 thinking_mode 决定是否启用 Extended Thinking
            if self.state.thinking_mode in ("adaptive", "enabled"):
                # 使用 _get_thinking_budget 获取预算，支持 THINKING_BUDGET 环境变量调节
                budget = _get_thinking_budget(self.backend.model, max_output)
                create_params["thinking"] = {"type": "enabled", "budget_tokens": budget}

            tool_blocks_by_index: dict[int, dict] = {}  # 按索引跟踪工具块的累积状态
            first_text = True  # 标记是否为首个有效文本，用于控制 spinner 停止时机

            # 启动 API 监听流
            async with self._client.messages.stream(**create_params) as stream:
                async for event in stream:
                    if not hasattr(event, 'type'):
                        continue

                    # 工具调用块开始：初始化该工具的参数累积器
                    if event.type == "content_block_start":
                        cb = getattr(event, 'content_block', None)
                        if cb and getattr(cb, 'type', None) == "tool_use":
                            tool_blocks_by_index[event.index] = {
                                "id": cb.id,
                                "name": cb.name,
                                "input_json": "",  # 逐步累积 JSON 参数片段
                            }
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        # 捕获并清洗思考链（thinking）以及普通文本并实时流式渲染
                        if hasattr(delta, 'text'):
                            # 首个文本到达时停止 spinner，切换到打字机模式
                            if first_text:
                                stop_spinner()
                                self._emit_text("\n")  # 首字输出前先换行
                                first_text = False
                            self._emit_text(delta.text)
                        elif hasattr(delta, 'thinking'):
                            # 思考链也实时显示，但标记为 [thinking]
                            if first_text:
                                stop_spinner()
                                self._emit_text("\n  [thinking] ")
                                first_text = False
                            self._emit_text(delta.thinking)
                        elif hasattr(delta, 'partial_json'):
                            # 累积工具调用的 JSON 参数片段
                            tb = tool_blocks_by_index.get(event.index)
                            if tb:
                                tb["input_json"] += delta.partial_json
                                
                    elif event.type == "content_block_stop":
                        # 工具块结束：解析完整 JSON 并触发回调（用于流式工具抢跑执行）
                        tb = tool_blocks_by_index.pop(event.index, None)
                        if tb and on_tool_block_complete:
                            try:
                                parsed = json.loads(tb["input_json"] or "{}")
                            except Exception:
                                parsed = {}  # JSON 解析失败时回退到空字典
                            on_tool_block_complete({
                                "type": "tool_use", "id": tb["id"],
                                "name": tb["name"], "input": parsed,
                            })

                final_message = await stream.get_final_message()

            # 【核心过滤】移除 thinking 块，防止其占用上下文窗口空间
            final_message.content = [b for b in final_message.content if b.type != "thinking"]
            return final_message

        # 使用步骤 4 实现的重试方法进行包裹，处理瞬时网络故障
        return await _with_retry(_do)


#### 注意什么

- **思考链与打字机 Spinner**：在 Anthropic 流式读取时，模型可能会先返回 `thinking` 类型的数据块进行思考。为了保证用户体验，我们必须在收到首个有效字符（无论是普通文本还是思考文本）时立即调用 `stop_spinner()` 来停止加载动画。
- **工具定义获取**：使用 `get_tool_definitions()` 获取工具定义列表。第 12 课引入延迟工具后，会改用 `get_active_tool_definitions()` 过滤未激活的延迟工具。
```

---

### 步骤 3：实现 OpenAI 增量分块参数重建与流式输出

#### 为什么做

OpenAI 的流式格式和 Anthropic 大相径庭：
1. **工具调用切片到达**：OpenAI 的 `tool_calls` 不是完整的 JSON 块，而是打碎成极小的 `delta` 块分片推送。比如 `arguments` 属性可能会每次推送 `{"fi`、`le_`、`pa` 这样几个字符。我们必须通过 `choices[0].delta.tool_calls` 的 `index` 识别出属于第几个工具，手动累加参数字符串，最后在流结束时进行拼装。
2. **正文流输出**：捕获 `delta.content` 并进行流式打字渲染。

#### 做什么

在 `agent.py` 中实现 `_call_openai_stream` 的增量装配器：

```python
# agent.py（续）


async def _call_openai_stream(self) -> dict:
    """OpenAI 后端流式调用：处理增量分片并实时渲染文本。"""
    async def _do():
        # 构建请求参数
        create_params: dict[str, Any] = {
            "model": self.backend.model,
            "tools": _to_openai_tools(get_tool_definitions()),
            "messages": self.history.openai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},  # 要求返回 token 用量统计
        }

        # 思考模式：通过 extra_body 传递 thinking 参数，通过 reasoning_effort 控制强度
        if self.state.thinking_mode != "disabled":
            create_params["reasoning_effort"] = os.environ.get(
                "THINKING_EFFORT", "high"
            ).lower()
            create_params["extra_body"] = {"thinking": {"type": "enabled"}}

        # 启动 OpenAI 兼容端流式生成
        stream = await self._client.chat.completions.create(**create_params)

        content = ""  # 累积完整的回复文本
        reasoning_content = ""  # DeepSeek 等模型的思维链内容
        first_text = True  # 标记首个文本到达，用于控制 spinner 停止
        reasoning_started = False  # 标记是否已开始输出思维链
        tool_calls: dict[int, dict] = {}  # 按索引累积工具调用参数
        finish_reason = ""
        usage = None  # 用于记录最后一个 chunk 返回的 token 用量

        async for chunk in stream:
            # 捕获 usage 信息（通常在最后一个 chunk 中出现）
            if chunk.usage:
                usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                }

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # 1. 处理思维链内容（reasoning_content）
            # DeepSeek 等模型通过 delta.reasoning_content 逐步推送思维链
            if delta and hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                if not reasoning_started:
                    stop_spinner()
                    self._emit_text("\n  [thinking] ")
                    reasoning_started = True
                    first_text = False  # 思考开始后，后续 text 不再触发首字逻辑
                self._emit_text(delta.reasoning_content)
                reasoning_content += delta.reasoning_content

            # 2. 处理正文输出文本，并进行流式刷新
            if delta and delta.content:
                if first_text:
                    stop_spinner()
                    self._emit_text("\n")  # 首字输出前先换行
                    first_text = False
                self._emit_text(delta.content)
                content += delta.content  # 累积完整文本用于历史记录

            # 3. 收集与累加工具调用参数分片
            # OpenAI 的 tool_calls 被打碎成极小的 delta 片段，需要手动拼装
            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    existing = tool_calls.get(tc.index)
                    if existing:
                        # 已有该工具块，累加参数字符串片段
                        if tc.function and tc.function.arguments:
                            existing["arguments"] += tc.function.arguments
                    else:
                        # 初始化首个参数块（可能是 id/name/arguments 的任一片段先到）
                        tool_calls[tc.index] = {
                            "id": tc.id or "",
                            "name": (tc.function.name if tc.function else "") or "",
                            "arguments": (tc.function.arguments if tc.function else "") or "",
                        }

            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason

        # 3. 按索引排序后拼装成标准 OpenAI 格式的工具对象结构
        # 排序确保即使流式传输乱序，最终结构也严格对应
        assembled = (
            [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for _, tc in sorted(tool_calls.items())
            ]
            if tool_calls
            else None
        )

        # 构建返回消息体，包含 reasoning_content（如有）
        message: dict[str, Any] = {
            "role": "assistant",
            "content": content or None,
            "tool_calls": assembled,
        }
        if reasoning_content:
            message["reasoning_content"] = reasoning_content

        # 返回统一的数据包供外层主循环更新历史
        return {
            "choices": [
                {
                    "message": message,
                    "finish_reason": finish_reason or "stop",
                }
            ],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
        }

    return await _with_retry(_do)


#### 注意什么

- **消息历史与 OpenAI 格式规范**：在流式输出结束后，我们需要使用 `MessageHistory` 来统一更新历史记录。
  1. 对于 **Anthropic 后端**：在 `_call_anthropic_stream` 结束后，通过 `self.history.append_assistant_message()` 添加。
  2. 对于 **OpenAI 后端**：在 `_call_openai_stream` 收集完文本和 `tool_calls` 后，通过 `self.history.append_assistant_message()` 添加带有 `tool_calls` 的回复。如果是字典格式，抽象层会直接推入，防止将其包装在多余的 `assistant` 属性中导致 OpenAI API 抛出 400 Bad Request。
- **模块级函数调用**：注意 `_to_openai_tools` 是一个模块级工具函数，调用时不需要加上 `self.` 前缀。
- **思考模式 API 参数**：对于 OpenAI 兼容后端（如 DeepSeek），思考模式通过两个参数传递：
  - `reasoning_effort`：控制思考强度，可选 `low` / `medium` / `high` / `max`，通过 `THINKING_EFFORT` 环境变量设置，默认 `high`
  - `extra_body={"thinking": {"type": "enabled"}}`：启用思考模式的开关
  - 注意：这两个参数需要通过 `extra_body` 传递，因为 OpenAI SDK 的标准参数中没有 `thinking` 字段
- **思维链内容处理**：DeepSeek 等模型通过 `delta.reasoning_content` 逐步推送思维链内容，与 `delta.content`（正文）同级。我们在流式渲染时先显示 `[thinking]` 标记，再逐字输出思维链，最后输出正文
```

---

### 步骤 4：编写指数退避与抖动重试封装

#### 为什么做

网络请求常会遇到服务器偶尔过载（HTTP 429）、服务器维护临时不可达（HTTP 503/529）或连接被外部重置（`ECONNRESET`）。
- 我们应当只重试此类“可恢复的错误”（不应重试参数错误 400 或认证失败 401 ）。
- 指数退避（每次重试等待时长翻倍，如 $1\text{s} \to 2\text{s} \to 4\text{s}$）能让下游服务器在大负荷时有喘息之机。
- 随机抖动（Jitter）则能防止多台机器在同一时间同步重试，避免形成“重试风暴”。

#### 做什么

在 `agent.py` 文件中，编写重试拦截装饰逻辑：

```python
# agent.py（续）

import asyncio
import time
from ui import print_retry  # 导入重试渲染函数


def _is_retryable(error: Exception) -> bool:
    """判断错误是否可重试（瞬时网络故障 vs 永久性配置错误）。"""
    # 提取错误状态码（兼容不同 SDK 的属性命名）
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    # 429: 限流, 503: 服务不可用, 529: Anthropic 过载
    if status in (429, 503, 529):
        return True
    # 通过错误消息匹配网络层异常（不做 .lower()，源码直接匹配大写关键字）
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    return False


async def _with_retry(fn, max_retries: int = 3):
    """带指数退避和随机抖动的重试封装，防止重试风暴。"""
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as error:
            # 如果重试次数耗尽，或者错误不可恢复，直接向上抛出
            if attempt >= max_retries or not _is_retryable(error):
                raise
            
            # 指数退避计算：min(30s, 1s * 2^attempt) + 随机抖动时间
            # 随机抖动能防止多客户端在同一时间点重试形成"重试风暴"
            delay = min(1.0 * (2 ** attempt), 30.0) + (hash(str(time.time())) % 1000) / 1000
            
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else "network error"
            print_retry(attempt + 1, max_retries, reason)
            
            await asyncio.sleep(delay)


#### 注意什么

- **避免惊群效应（Thundering Herd）**：在重试机制中加入随机抖动（Jitter）至关重要。当大批客户端同时因云端 API 限流（如 429）或网络瞬断而请求失败时，如果它们都采用整秒指数退避（例如 1s, 2s, 4s），它们会在相同的秒数切片处再次并发轰炸网关。加上 0 到 1 秒之间的随机抖动值 `jitter`，可以有效错开各客户端的实际重试时点，平滑流量波峰。
```

---

## ⚖️ 设计权衡

### 思考链历史保存 vs 丢弃（Filtering Thinking Tokens）

- **方案 A**：**从消息历史中过滤**（我们所用）
  - 流式结束时，从 API 返回结果的消息体（`content` 数组）中剔除 `thinking` 类型的块，只保留 `text` 块存入 `self._messages`。
  - **优点**：大幅节约上下文窗口空间，避免多轮对话时被思考文本占满，减缓大模型的生成成本。
  - **缺点**：大模型在下一轮对话中无法看到自己上一轮具体的“心路历程”（只看得到自己的最终结论和工具输出），但实践证明其决策影响极小。
- **方案 B**：**完全完整保存**
  - 不做任何过滤，将 `thinking` 和 `text` 原封不动发回。
  - **优点**：模型记忆完全一致。
  - **缺点**：上下文 Token 消耗会呈数倍爆发，很快就会逼近极限，在真实工程环境中不推荐。

**结论**：过滤丢弃是高频交互 Agent 保持 Token 经济型的最重要前置策略。

---

## ⚠️ 常见陷阱

### 1. 弯单引号引起 OpenAI JSON 反序列化失败

OpenAI 在推送 `tool_calls` 的 `arguments` 切片时，模型有时会误输出非标准的 JSON 字符串。如果在收集完毕时没有容错机制，直接使用 `json.loads()` 会发生解析崩溃。

**修正**：在 `__main__.py` 或 `execute_tool` 中，我们必须提供解析的容错处理，如发生 `JSONDecodeError` 则退回默认空字典，防止抛出未处理异常。

---

### 2. 重试风暴（Retry Storm）中漏掉抖动因子

```python
# ❌ 错误：如果只使用纯粹的指数退避，没有引入随机抖动
delay = 1.0 * (2 ** attempt)
```

**后果**：若并发客户端很多，一旦网络闪断，全部客户端都会在完全相同的物理时间点（比如第 1.0 秒、2.0 秒、4.0 秒）向 API 网关发起海量冲击重试，导致刚刚恢复的网关由于被瞬间压垮而再次挂掉。

---

## ✅ 验收点

### 输入与验证

1. 启动 Agent 进入 REPL 终端：
   ```bash
   python __main__.py
   ```
2. 输入一个需要较长文本回复的复杂查询（例如让模型写一段 100 行 of 算法）。
3. **观察流式效果**：仔细核对字词是否是一个一个跳出来，在输出过程中，能否通过 `Ctrl+C` 中断输出流并成功返回 `> ` 提示符。
4. **模拟重试测试**：可以通过暂时掐断网线或提供一个极低重载限流的模拟接口 base_url，验证终端是否能正确捕获网络异常并成功打印出 `↻ Retry 1/3: ...` 的提示。

### 失败时如何排查

1. **终端加载动画（Spinner）无法停止**：检查在解析 `content_block_delta` 事件时，是否遗漏了在首个 `text` 或 `thinking` 块到达时调用 `stop_spinner()`。
2. **OpenAI 模式下提示 `400 Bad Request`**：检查在流式结束后向 `MessageHistory` 回填 assistant 消息时，是否错误地将已经拼接包装好的 `choice.message` 字典又做了一次冗余的 `role` 和 `content` 包装。
3. **未定义的函数报错**：确保 `tools.py` 导出的方法名称在 `agent.py` 顶部的 import 列表中拼写正确（使用 `get_tool_definitions`）。

---

## 🧠 思考题

1. **为什么在 `_call_openai_stream` 中，我们需要使用 `sorted(tool_calls.items())` 对累加后的工具列表进行排序？**
   *(提示：大模型流式发回切片时，即使是多个工具的 delta 包，它们也有可能会由于网络传输因素发生乱序。在拼接时按照 index 序号对其重新排序，可以保证构建出的 arguments 结构严格对应。)*
2. **在 `_is_retryable` 判断中，我们为什么要主动忽略 HTTP 401（未授权）和 HTTP 400（请求不合法）错误而不进行重试？**
   *(提示：因为这些错误由于参数或配置写死，是无法通过“等待一段时间重新发送”来自动解决的。盲目重试只会徒增 API 等待时间和算力浪费。)*
3. **为什么指数退避重试要加“随机抖动”（jitter）？直接用固定的 `2^attempt` 延迟不行吗？**
   *(提示：如果有 100 个 Agent 实例同时遇到 429 错误，固定延迟会导致它们在完全相同的时间点重试，形成“惊群效应”（Thundering Herd），再次把服务器打爆。随机抖动让每个实例的重试时间错开，分散压力。这是分布式系统中的经典设计。)*

---

## 📦 本节收获

1. **SSE 打字机交互**：掌握了利用 Server-Sent Events 事件机制渲染实时字符的终端交互技术。
2. **切片数据流累加**：掌握了还原多路并行乱序切片参数包（OpenAI 格式）的数据组装算法。
3. **退避防风暴设计**：理解了指数退避加随机抖动的算法在提高云端 API 交互可用性上的重大工程作用。

---

> **下一章**：现在 Agent 既能高速操作又能实时流式沟通。但一个能运行任意 Shell 命令的 Agent 是极其危险的，我们需要构筑防卫线——权限与安全系统。
