# 第 06 课：流式工具执行

## 🎯 本节目标

实现流式工具执行（Streaming Tool Execution）机制。当大模型流式输出工具调用块时，一旦某个只读的并发安全工具（如 `read_file`）的参数生成完毕，就立即通过异步后台任务“抢跑”执行，将工具的 I/O 延迟完全隐藏在模型的文本生成时间里。

---

## 🏆 最终效果

完成本节后，当 Agent 在处理复杂任务时：
- 大模型流式输出回复的过程中，无副作用的安全工具（如读取文件、列出文件等）会在后台秘密启动。
- 文本输出完全结束后，这些工具的结果已在后台加载完毕，Agent 可以**零延迟**地直接读取其结果进入下一轮思考，减少用户等待的迟滞感。

---

## 🛠️ 本节任务

1. **确定并发安全工具集**：明确哪些工具（如只读文件操作）可以安全地异步并行执行。
2. **实现流式 API 监听与回调**：实现 `_call_anthropic_stream`，在流式生成期间解析 `tool_use` 并在块结束时触发 `_on_tool_block_complete`。
3. **实现大结果持久化**：添加 `_persist_large_result` 方法，当工具返回超过 30KB 的结果时自动保存到磁盘并只保留预览摘要。
4. **构建后台抢跑任务注册表**：在 `_chat_anthropic` 核心循环中引入 `early_executions` 字典，启动后台异步任务。
5. **重构工具处理循环接收结果**：在获取回复后，对已抢跑的工具任务直接进行 `await`，未抢跑的工具则走常规同步调用。

---

## 📦 涉及文件

修改：
- `agent.py`
- `tools.py`

---

## 🚀 开始实现

### 步骤 1：定义并发安全工具集

#### 为什么做

并非所有工具都能在后台异步“抢跑”。写文件（`write_file`/`edit_file`）或运行命令（`run_shell`）会修改外部世界状态，存在读写冲突等并发风险。只有无副作用的“只读工具”（如 `read_file`、`list_files`）才能被安全地提前执行。

#### 做什么

修改 `tools.py`，在文件顶部定义并发安全工具集常量（后续由 `agent.py` 导入使用）：

```python
# tools.py 中的新增常量

# 并发安全工具白名单：只读、无副作用，允许在流式输出期间异步抢跑
# 使用 set 实现 O(1) 查找，避免在回调中频繁遍历列表
CONCURRENCY_SAFE_TOOLS = {"read_file", "list_files", "grep_search", "web_fetch"}
```

然后在 `agent.py` 中导入该常量：

```python
# agent.py 中的导入修改

from tools import (
    tool_definitions,
    execute_tool,
    CONCURRENCY_SAFE_TOOLS,  # 并发安全工具白名单，用于判断是否可以异步抢跑
    # ... 其他导入保持不变
)
```

---

### 步骤 2：实现流式 API 调用与 `_on_tool_block_complete` 触发

#### 为什么做

我们需要在 Anthropic 客户端流式获取响应时，实时监控内容块。
1. 当检测到 `tool_use` 类型的块开始（`content_block_start`）时，初始化一个字典记录其参数 JSON。
2. 随着流的输入，累加 `partial_json` 片段。
3. 一旦该块生成结束（`content_block_stop`），立即调用 `on_tool_block_complete` 回调通知 Agent，此时模型消息可能还在继续生成后续的文本。

#### 做什么

在 `agent.py` 中编写 `_call_anthropic_stream` 方法。注意 `max_tokens` 并非硬编码的 4096，而是通过 `_get_max_output_tokens()` 根据模型动态计算；工具列表使用 `get_active_tool_definitions(self.tools)` 而非无参的 `get_tool_definitions()`，后者会过滤掉尚未激活的延迟工具（deferred tools）：

```python
# agent.py（续）

    # 流式调用 Anthropic API，监听 tool_use 块并在完成时触发回调
    async def _call_anthropic_stream(self, on_tool_block_complete=None) -> Any:
        max_output = _get_max_output_tokens(self.config.model)
        create_params = {
            "model": self.config.model,
            # thinking 模式下使用动态上限，禁用时使用默认 16384
            "max_tokens": max_output if self.state.thinking_mode != "disabled" else 16384,
            "system": self._system_prompt,
            "tools": get_active_tool_definitions(self.tools),
            "messages": self.history.anthropic_messages,
        }

        # thinking 模式启用时，预留几乎全部 token 给思考过程
        if self.state.thinking_mode in ("adaptive", "enabled"):
            create_params["thinking"] = {
                "type": "enabled",
                "budget_tokens": max_output - 1,
            }

        # 按 content_block 的 index 追踪各工具块的累积参数
        tool_blocks_by_index = {}

        # 开启流式 API 监听，使用 async with 确保流正确关闭
        async with self._client.messages.stream(**create_params) as stream:
            async for event in stream:
                if not hasattr(event, 'type'):
                    continue
                # 1. 工具块开始：记录 id 和 name，初始化空的 input_json
                if event.type == "content_block_start":
                    cb = getattr(event, 'content_block', None)
                    if cb and getattr(cb, 'type', None) == "tool_use":
                        tool_blocks_by_index[event.index] = {
                            "id": cb.id,
                            "name": cb.name,
                            "input_json": "",
                        }
                # 2. JSON 增量片段：逐步拼接工具参数（流式传输时 JSON 是分片到达的）
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if hasattr(delta, 'partial_json'):
                        tb = tool_blocks_by_index.get(event.index)
                        if tb:
                            tb["input_json"] += delta.partial_json
                # 3. 工具块结束：解析完整 JSON 并触发抢跑回调
                elif event.type == "content_block_stop":
                    tb = tool_blocks_by_index.pop(event.index, None)
                    if tb and on_tool_block_complete:
                        try:
                            parsed = json.loads(tb["input_json"] or "{}")
                        except Exception:
                            parsed = {}

                        # 回调通知 Agent：此工具的参数已完整，可以开始执行
                        on_tool_block_complete({
                            "type": "tool_use",
                            "id": tb["id"],
                            "name": tb["name"],
                            "input": parsed,
                        })

            # 等待流完全结束，获取最终的完整消息对象
            final_message = await stream.get_final_message()
        return final_message
```

#### `_get_max_output_tokens` — 动态输出上限

此函数根据模型名称返回对应的最大输出 token 数，而非使用固定的 4096。不同模型的输出能力差异很大，opus-4-6 可达 64000，而较小的模型则为 16384：

```python
# agent.py 顶部的辅助函数

def _get_max_output_tokens(model: str) -> int:
    """根据模型名称动态返回最大输出 token 数，避免硬编码。"""
    m = model.lower()
    if "opus-4-6" in m:
        return 64000   # opus-4-6 拥有最大的输出能力
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    return 16384  # 未知模型使用保守默认值
```

---

### 步骤 3（插曲）：大结果持久化 `_persist_large_result`

#### 为什么做

当 Agent 执行工具（如 `run_shell` 运行了一条产生大量输出的命令，或 `read_file` 读取了一个巨大的文件），返回的结果可能高达几十甚至上百 KB。如果将这些原始文本直接塞入 API 请求的消息历史，会迅速撑爆上下文窗口，触发昂贵的压缩甚至导致请求失败。我们需要一个"溢出阀"：当结果超过阈值时，将完整内容保存到磁盘，只在消息历史中保留一个预览摘要。

#### 做什么

在 `agent.py` 的 `Agent` 类中添加 `_persist_large_result` 方法：

```python
# agent.py — 大结果持久化

LARGE_RESULT_THRESHOLD = 30 * 1024      # 30 KB 阈值，超过则持久化到磁盘
LARGE_RESULT_PREVIEW_LINES = 200        # 预览保留的行数


    # 当工具结果超过阈值时，将完整内容保存到磁盘并返回预览摘要
    def _persist_large_result(self, tool_name: str, result: str) -> str:
        # 小结果直接返回，避免不必要的磁盘 IO
        if len(result.encode()) <= LARGE_RESULT_THRESHOLD:
            return result
        # 创建工具结果存储目录
        d = Path.cwd() / ".mini-claude" / "tool-results"
        d.mkdir(parents=True, exist_ok=True)
        # 文件名包含毫秒时间戳和工具名，便于事后追溯
        filename = f"{int(time.time() * 1000)}-{tool_name}.txt"
        filepath = d / filename
        filepath.write_text(result, encoding="utf-8")

        lines = result.split("\n")
        preview = "\n".join(lines[:LARGE_RESULT_PREVIEW_LINES])
        # 使用字节数而非字符数衡量，确保中文等多字节字符被正确计算
        size_kb = len(result.encode()) / 1024

        return (
            f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
            f"Full output saved to {filepath}. "
            f"You can use read_file to see the full result.]\n\n"
            f"Preview (first {LARGE_RESULT_PREVIEW_LINES} lines):\n{preview}"
        )
```

#### 注意什么

- 阈值以**字节数**（`len(result.encode())`）而非字符数衡量，因为中文字符占 3 字节，确保对多语言内容一视同仁。
- 持久化目录 `./.mini-claude/tool-results` 会在首次调用时自动创建，文件名包含毫秒时间戳和工具名，便于事后追溯。
- 返回给 Agent 的预览摘要仍然包含足够信息让模型理解输出内容，同时大幅降低了 token 消耗。

---

### 步骤 4：构建后台抢跑任务注册表 `early_executions`

#### 为什么做

我们需要在主循环中接入回调。每当一个流式内容块解析完成，我们就判断该工具是否在 `CONCURRENCY_SAFE_TOOLS` 安全白名单中。若符合，即刻通过 `asyncio.create_task()` 创建后台异步任务启动执行，并将对应的 Promise（Future 任务）存入 `early_executions` 字典中，使用 `tool_use_id` 作为键进行标识。

#### 做什么

修改 `_chat_anthropic` 循环的开头，注册 `_on_tool_block_complete` 回调逻辑：

```python
# agent.py（续）

    async def _chat_anthropic(self, user_message: str) -> None:
        self.history.append_user_message(user_message)

        while True:
            current_system_prompt = build_system_prompt()

            # 抢跑任务注册表：{ tool_use_id -> asyncio.Task }
            # 用于在流式结束后直接 await 已启动的后台任务
            early_executions: dict[str, asyncio.Task] = {}

            # 回调函数：当安全工具参数生成完毕时被调用
            def _on_tool_block_complete(block: dict):
                # 只有白名单中的只读工具才允许抢跑
                if block["name"] in CONCURRENCY_SAFE_TOOLS:
                    # 权限检查：即使工具在白名单中，仍需验证用户是否授权
                    perm = check_permission(
                        block["name"], block["input"],
                        self.config.permission_mode, self.state.plan_file_path,
                    )
                    if perm["action"] == "allow":
                         # 创建后台异步任务立即开始执行，不阻塞流式接收
                         task = asyncio.create_task(self._execute_tool_call(block["name"], block["input"]))
                         early_executions[block["id"]] = task

            # 将回调传入流式 API 调用，每个工具块完成时都会触发
            response = await self._call_anthropic_stream(
                on_tool_block_complete=_on_tool_block_complete
            )
```

#### 注意什么

- **UI 渲染与抢跑任务解耦（关键交互细节）**：当安全工具在后台抢跑时，我们**绝不在此时打印工具调用日志**。因为大模型的文本流此时正在终端中实时显示，如果在此时突然输出类似 `🔧 read_file ...` 的日志，会与模型的输出字符混杂在一起，导致终端排版错乱。工具抢跑必须保持“静默”。

---

### 步骤 5：重构工具处理循环接收结果

#### 为什么做

流式响应全部输出完毕后，Agent 会进入常规的工具执行处理循环。对于此前已经触发了抢跑的安全任务，我们无需重复调用工具，只需直接通过 `await early_task` 拿回后台的运行结果。对于未抢跑的非安全工具，则沿用以往的正常流程进行处理。

#### 做什么

继续修改 `_chat_anthropic` 循环的工具处理段落，增加任务接管判定：

```python
# agent.py（续）

            # 将助手消息（含文本和工具调用）追加到历史记录
            self.history.append_assistant_message(
                [self._block_to_dict(b) for b in response.content]
            )

            # 提取所有工具调用块
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break  # 没有工具调用，对话循环结束

            tool_results = []
            for tu in tool_uses:
                inp = dict(tu.input) if hasattr(tu.input, "items") else tu.input

                # 1. 检查此工具是否已在后台抢跑执行
                early_task = early_executions.get(tu.id)
                if early_task:
                    # 抢跑任务静默运行，此时才渲染 UI 日志（避免与流式文本混杂）
                    print_tool_call(tu.name, inp)
                    # await 可能已完成的任务，几乎零等待
                    raw = await early_task
                    res = self._persist_large_result(tu.name, raw)
                    print_tool_result(tu.name, res)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": res,
                    })
                    continue

                # 2. 非安全工具（write_file/run_shell 等）走常规同步执行
                print_tool_call(tu.name, inp)
                raw = await self._execute_tool_call(tu.name, inp)
                res = self._persist_large_result(tu.name, raw)
                print_tool_result(tu.name, res)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": res,
                })

            # 将所有工具执行结果追加到历史，供下一轮对话使用
            self.history.append_tool_results(tool_results)

        # 输出最终回复（临时方案，第 7 课会替换为流式字符渲染）
        for block in response.content:
            if block.type == "text":
                print(block.text)
```

---

## 异步模式与并发设计

在构建高并发与快速响应的 Agent 系统时，我们需要在 Python 中使用不同的异步（`asyncio`）调用模式。不少开发者可能会认为这是一种“异步模式不一致”，但实际上它们各自有最适合的应用场景。

### 1. `asyncio.create_task` — 提前抢跑（并发）
- **场景**：用于流式输出期间安全工具的“提前抢跑（Early Execution）”。
- **原理**：`create_task` 会将协程包装为 `Task` 并提交给 `asyncio` 事件循环，使其在后台非阻塞地开始运行。大模型在继续生成字符的同时，磁盘/网络 I/O 已经在悄悄进行。
- **关键代码**：
  ```python
  task = asyncio.create_task(self._execute_tool_call(block["name"], block["input"]))
  early_executions[block["id"]] = task
  ```

### 2. `await` — 顺序执行（同步等待）
- **场景**：用于常规工具的顺次执行或等待抢跑任务的最终结果。
- **原理**：`await` 会暂停当前协程，直到目标协程或 Task 运行结束，这保证了执行的先后顺序与一致性。对于有副作用的工具（如 `edit_file`），必须使用 `await` 严格保证顺序，不能乱序或并行。
- **关键代码**：
  ```python
  # 等待后台早已抢跑的异步任务
  result = await early_task
  # 或直接顺序调用常规工具
  result = await self._execute_tool_call(tu.name, inp)
  ```

### 3. `asyncio.gather` — 并行工具执行（批量并发）
- **场景**：在 OpenAI 兼容后端中，如果模型一次性返回了多个独立的工具调用，且我们需要在流式之外对其进行并行加速。
- **原理**：`asyncio.gather` 会同时启动多个协程，并等待它们全部执行完毕。相较于 `create_task` 的“发后不管/稍后回收”，`gather` 用于在当前步骤中阻塞等待这组并行任务的全部结果。
- **关键代码**：
  ```python
  results = await asyncio.gather(*(self._execute_tool_call(tc.name, tc.args) for tc in tool_calls))
  ```

### 总结：
- **`create_task`** 是“非阻塞启动后台执行”。
- **`await`** 是“同步等待单任务完成”。
- **`gather`** 是“同步等待一组并行任务完成”。
理解这三种模式的职责，能帮助我们构建既安全又高效的异步 Agent 系统。

---

## ⚖️ 设计权衡

### 异步抢跑（Early Execution） vs 串行阻塞等待

- **方案 A**：**流式完成即抢跑**（我们所用）
  - 利用异步 `create_task`，在大模型还没生成完 assistant 消息的时候就提前调用 I/O 工具。
  - **优点**：隐藏网络与磁盘读写延迟，如果工具执行时间与模型生成剩余内容时间重合，用户的体感延迟接近于 0。
  - **缺点**：增加了并发调度的复杂性，调试异常时需要额外注意 Task 的状态。
- **方案 B**：**完全串行串联**
  - 等待 API 流通道完全关闭，解析最终的消息对象，再开启工具执行循环。
  - **优点**：逻辑极度简单，状态完全是线性的。
  - **缺点**：工具执行在模型生成之后额外累加时间，连续交互时显得不够流畅。

**结论**：流式抢跑是工业级 coding agent 提升极致响应速度的重要交互技术，方案 A 的少量异步代价可以换取极高的用户体验增益。

---

## ⚠️ 常见陷阱

### 1. 对非安全工具进行提前抢跑

```python
# ❌ 错误示例：对带副作用的工具进行抢跑
# run_shell 和 edit_file 会修改外部状态，不能在用户确认前静默执行
if block["name"] in ("run_shell", "edit_file"):
    task = asyncio.create_task(execute_tool(...))
```

**后果**：由于这些工具包含副作用，可能会导致在用户输入、安全确认机制生效前，危险命令已经在后台悄悄执行完毕（例如 `rm -rf`），直接击穿了安全防线。
**修正**：必须严格约束 `CONCURRENCY_SAFE_TOOLS` 白名单，只有无副作用的安全查询类工具才允许进入抢跑。

---

### 2. 忽略后台任务引发的异常

如果在后台运行 `execute_tool` 时发生了异常崩溃而没有被捕获，可能会在 `await early_task` 阶段直接把异常抛给主循环，导致程序崩溃，且没有进行会话自动保存。

**修正**：确保后台执行的 `execute_tool` 方法内具有健全的 `try...except` 容错机制，将异常包装为带 `Error:` 前缀的错误消息作为常规数据返回，让 Agent 在下一轮循环中自行处理该错误。

---

## ✅ 验收点

### 输入与验证

1. 启动 Agent，输入一个会触发只读工具链式调用的请求：
   ```bash
   python __main__.py "读取 prompt.py 的前 10 行"
   ```
2. **观察体感**：注意观察大模型输出“正在思考”或输出字符时，在它停止吐字的瞬间，终端是否**几乎没有任何等待**，就立刻展现了 `📖 read_file` 工具的输出结果。

### 预期结果

`read_file` 的结果能够正常返回，且整个交互由于提前抢跑，中间的卡顿时间明显缩短。

---

## 🧠 思考题

1. **既然我们在 `_on_tool_block_complete` 回调中已经将工具在后台跑起来了，为什么不当时就执行 `print_tool_call` 把它显示在终端屏幕上，而要等到最后的 `for tu in tool_uses:` 中才进行打印？**
   *(提示：大模型的文本输出和工具参数块生成通常是混合在一起的。如果抢跑时立即打印工具调用，它的控制台输出将会插在大模型正在流式输出的文本段落中间，导致控制台排版乱套。)*
2. **如果大模型在一个回复中同时调用了 3 个 `read_file`（读取 3 个不同的文件），我们目前的抢跑逻辑会如何执行它们？**
   *(提示：大模型是顺次流式输出这 3 个 block 的。当第 1 个 block 生成完毕触发 Stop 时，任务 1 在后台启动；随后模型继续输出，第 2 个 block 完毕触发 Stop，任务 2 启动；第 3 个同样如此。3 个读取任务实质上都在后台并行运行，极大缩短了总耗时。)*
3. **为什么抢跑机制只允许 `CONCURRENCY_SAFE_TOOLS`（只读工具）参与，而写入类工具（如 `write_file`、`run_shell`）不能抢跑？**
   *(提示：抢跑意味着工具可能在 LLM 还没输出完就开始执行了。如果 LLM 后续修改了意图或参数，已经执行的写入操作无法撤销——比如先写了一个文件，然后 LLM 决定改写另一个文件。只读操作没有副作用，即使抢跑了也不会造成不可逆的影响。)*

---

## 📦 本节收获

1. **异步并行调度**：掌握了利用 `asyncio.create_task` 实现多任务并发重叠执行的方法。
2. **流式增量解析**：理解了在 API 响应流中监听 `content_block_delta` 和 `content_block_stop` 获取实时局部数据的机制。
3. **UX 界面防混乱设计**：理解了将“后台提前执行”与“前台终端日志输出”在时序上进行分离解耦的优秀交互准则。

---

> **下一章**：我们隐藏了工具调用的后台等待时间。接下来我们将攻克前台界面的流式输出——让 Agent 的思考文本实时呈现在用户面前。
