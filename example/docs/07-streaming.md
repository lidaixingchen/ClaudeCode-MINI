# 第 07 课：文本流式输出与 API 重试

## 🎯 本节目标

为 Agent 构建流畅的流式字符输出界面和稳健的 API 网络容错机制。实现 Anthropic 与 OpenAI 两套后端的文本流渲染（逐字显示回复），滤除大模型的 Extended Thinking 冗余 Token 以节省上下文，并为 API 请求注入带随机抖动的“指数退避”重试保护。

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


# 判断模型是否支持 Extended Thinking（思考链）功能
def _model_supports_thinking(model: str) -> bool:
    m = model.lower()
    # Claude 3 系列不支持 thinking，只有更新的 4 系列才支持
    if "claude-3-" in m or "3-5-" in m or "3-7-" in m:
        return False
    if "claude" in m and any(x in m for x in ("opus", "sonnet", "haiku")):
        return True
    return False


# 判断模型是否支持自适应思考模式（可动态调整思考深度）
def _model_supports_adaptive_thinking(model: str) -> bool:
    m = model.lower()
    # 仅 opus-4-6 和 sonnet-4-6 支持自适应思考
    return "opus-4-6" in m or "sonnet-4-6" in m


# 根据模型版本返回最大输出 Token 数，避免超出上下文窗口限制
def _get_max_output_tokens(model: str) -> int:
    m = model.lower()
    if "opus-4-6" in m:
        return 64000  # 最新旗舰模型有更大输出空间
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    return 16384  # 默认回退值


#### 注意什么

- **模型版本识别**：`_model_supports_thinking` 函数通过字符串匹配来识别模型版本。Claude 3 系列（如 claude-3-opus、claude-3.5-sonnet）不支持 thinking，而更新的版本（如 claude-4-opus、claude-4-sonnet）则支持。
- **自适应思考**：只有最新的 opus-4-6 和 sonnet-4-6 版本支持自适应思考模式，这种模式可以动态调整思考深度。
- **Token 限制策略**：不同模型的上下文窗口大小不同，因此需要根据模型版本设置合理的输出 Token 上限，避免请求失败。

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
from .ui import print_assistant_text, stop_spinner  # UI 库封装了 sys.stdout.write/flush


class Agent:
    # ... 在 __init__ 中定义 self._output_buffer: list[str] | None = None

    # 流式文本输出：区分主代理直接打印 vs 子代理缓冲收集
    def _emit_text(self, text: str) -> None:
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

    # Anthropic 后端流式调用：处理 SSE 事件流，实时渲染文本并过滤思考链
    async def _call_anthropic_stream(self, on_tool_block_complete=None) -> Any:
        async def _do():
            max_output = _get_max_output_tokens(self.config.model)
            create_params: dict[str, Any] = {
                "model": self.config.model,
                # thinking 模式下需要更大输出空间，禁用时回退到默认值
                "max_tokens": max_output if self.state.thinking_mode != "disabled" else 16384,
                "system": self._system_prompt,
                "tools": get_active_tool_definitions(self.tools),
                "messages": self.history.anthropic_messages,
            }

            # 根据 thinking_mode 决定是否启用 Extended Thinking
            if self.state.thinking_mode in ("adaptive", "enabled"):
                # 预留一个 token 差值给思考块，避免超出限制
                create_params["thinking"] = {"type": "enabled", "budget_tokens": max_output - 1}

            tool_blocks_by_index: dict[int, dict] = {}  # 按索引跟踪工具块的累积状态
            first_text = True  # 标记是否为首个有效文本，用于控制 spinner 停止时机

            # 启动 API 监听流
            async with self._anthropic_client.messages.stream(**create_params) as stream:
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
- **工具定义获取**：在实际 codebase 中，请使用 `get_active_tool_definitions(self.tools)` 动态获取当前激活的工具，以支持后续多 Agent 沙箱的限制工具列表。
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


# OpenAI 后端流式调用：处理增量分片并实时渲染文本
async def _call_openai_stream(self) -> dict:
    async def _do():
        # 启动 OpenAI 兼容端流式生成（include_usage 让最后一个 chunk 携带 token 统计）
        stream = await self._openai_client.chat.completions.create(
            model=self.config.model,
            messages=self.history.openai_messages,
            tools=_to_openai_tools(get_active_tool_definitions(self.tools)),
            stream=True,
            stream_options={"include_usage": True},  # 要求返回 token 用量统计
        )

        content = ""  # 累积完整的回复文本
        first_text = True  # 标记首个文本到达，用于控制 spinner 停止
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

            # 1. 处理正文输出文本，并进行流式刷新
            if delta and delta.content:
                if first_text:
                    stop_spinner()
                    self._emit_text("\n")  # 首字输出前先换行
                    first_text = False
                self._emit_text(delta.content)
                content += delta.content  # 累积完整文本用于历史记录

            # 2. 收集与累加工具调用参数分片
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

        # 返回统一的数据包供外层主循环更新历史
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": assembled,
                    },
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
from .ui import print_retry  # 导入重试渲染函数


# 判断错误是否可重试（瞬时网络故障 vs 永久性配置错误）
def _is_retryable(error: Exception) -> bool:
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


# 带指数退避和随机抖动的重试封装，防止重试风暴
async def _with_retry(fn, max_retries: int = 3):
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
   python -m mini_claude
   ```
2. 输入一个需要较长文本回复的复杂查询（例如让模型写一段 100 行 of 算法）。
3. **观察流式效果**：仔细核对字词是否是一个一个跳出来，在输出过程中，能否通过 `Ctrl+C` 中断输出流并成功返回 `> ` 提示符。
4. **模拟重试测试**：可以通过暂时掐断网线或提供一个极低重载限流的模拟接口 base_url，验证终端是否能正确捕获网络异常并成功打印出 `↻ Retry 1/3: ...` 的提示。

### 失败时如何排查

1. **终端加载动画（Spinner）无法停止**：检查在解析 `content_block_delta` 事件时，是否遗漏了在首个 `text` 或 `thinking` 块到达时调用 `stop_spinner()`。
2. **OpenAI 模式下提示 `400 Bad Request`**：检查在流式结束后向 `MessageHistory` 回填 assistant 消息时，是否错误地将已经拼接包装好的 `choice.message` 字典又做了一次冗余的 `role` 和 `content` 包装。
3. **未定义的函数报错**：确保 `tools.py` 导出的方法名称在 `agent.py` 顶部的 import 列表中拼写正确（使用 `get_active_tool_definitions` 而不是 `get_tool_definitions`）。

---

## 🧠 思考题

1. **为什么在 `_call_openai_stream` 中，我们需要使用 `sorted(tool_calls.items())` 对累加后的工具列表进行排序？**
   *(提示：大模型流式发回切片时，即使是多个工具的 delta 包，它们也有可能会由于网络传输因素发生乱序。在拼接时按照 index 序号对其重新排序，可以保证构建出的 arguments 结构严格对应。)*
2. **在 `_is_retryable` 判断中，我们为什么要主动忽略 HTTP 401（未授权）和 HTTP 400（请求不合法）错误而不进行重试？**
   *(提示：因为这些错误由于参数或配置写死，是无法通过“等待一段时间重新发送”来自动解决的。盲目重试只会徒增 API 等待时间和算力浪费。)*

---

## 📦 本节收获

1. **SSE 打字机交互**：掌握了利用 Server-Sent Events 事件机制渲染实时字符的终端交互技术。
2. **切片数据流累加**：掌握了还原多路并行乱序切片参数包（OpenAI 格式）的数据组装算法。
3. **退避防风暴设计**：理解了指数退避加随机抖动的算法在提高云端 API 交互可用性上的重大工程作用。

---

> **下一章**：现在 Agent 既能高速操作又能实时流式沟通。但一个能运行任意 Shell 命令的 Agent 是极其危险的，我们需要构筑防卫线——权限与安全系统。
