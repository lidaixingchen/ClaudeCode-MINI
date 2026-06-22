# 第 09 课：上下文分级压缩

## 🎯 本节目标

实现 Agent 消息历史自动摘要压缩机制（Auto-compaction）。当对话轮次过多、Token 消耗逼近大模型上下文窗口上限（85% 阈值）时，自动调用大模型将历史会话提炼为一段简明摘要，重构消息历史队列，在保留最新输入轮的同时瞬间释放高达 80% 的上下文空间，赋予 Agent 理论上无限长对话的能力。

---

## 🏆 最终效果

完成本节后，当 Agent 执行复杂多轮任务，或当用户在 REPL 交互终端中手动键入 `/compact` 指令时：
1. Agent 将历史对话提取并发起一次摘要生成请求。
2. 历史中的数十条消息、冗余工具结果，会被替换为一条简洁的用户摘要和模型确认：
   ```
   [Previous conversation summary]
   用户要求修改 main.py 的 bug。我通过 read_file 读取了内容，发现第 12 行有拼写错误，已通过 edit_file 完成了拼写订正，目前正准备运行 pytest 验证。
   ```
3. 腾出大量窗口空间，避免发生上下文爆表（Context Window Exceeded）的灾难性错误。

---

## 🛠️ 本节任务

1. **引入 Token 统计与阈值检测**：在 `Agent` 中加入 Token 跟踪变量，实现 `_check_and_compact()` 触发检查函数。
2. **编写双后端自动摘要器**：实现 `_compact_conversation()` 以及对应的 `_compact_anthropic()` 与 `_compact_openai()` 消息历史重构逻辑（Tier 0: 摘要压缩）。
3. **实现三级压缩流水线**：实现 `_run_compression_pipeline()`，包含 Tier 1（预算裁剪）、Tier 2（陈旧结果替换）、Tier 3（空闲清理）三级压缩机制。
4. **在主循环中接入自动压缩**：将自动检测压缩和三级流水线织入 Agent 的轮次边界（Turn Boundary）上。
5. **连通手动压缩命令行指令**：在 `__main__.py` 的 REPL 循环中绑定 `/compact`，支持用户强制压缩。

---

## 📦 涉及文件

修改：
- `agent.py`
- `__main__.py`

---

## 🚀 开始实现

### 步骤 1：引入 Token 统计与阈值检测

#### 为什么做

我们需要在每次大模型 API 响应返回时，记录大模型返回的 `input_tokens`。
1. 以该统计值判定当前会话对窗口的占用比例。
2. 设定 `effective_window`（有效安全边界，如 `200000 - 20000 = 180000` tokens，留出 20k tokens 给当前输入与输出）。
3. 当占用率超过有效边界的 **85%** 时，发出提示并自动开启压缩流程。

#### 做什么

修改 `agent.py`，定义相关常量与 Token 跟踪，并编写 `_check_and_compact` 方法：

```python
# agent.py 中的修改

# ─── Constants ──────────────────────────────────────────────
CONTEXT_WINDOW_SAFETY_MARGIN = 20000  # 窗口安全边界 (Tokens)，为系统提示词和回复预留空间
AUTOCOMPACT_THRESHOLD = 0.85          # 触发自动压缩的阈值 (85%)，防止窗口爆满
LARGE_RESULT_THRESHOLD = 30 * 1024     # 大结果文件持久化阈值 (30 KB)，超过则保存到本地文件
LARGE_RESULT_PREVIEW_LINES = 200       # 大结果文件预览行数，只保留前 200 行在历史中

# ─── Tier 2 compression constants ───────────────────────────
SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell"}  # 可被裁剪的工具（输出通常冗长）
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"  # 裁剪后的占位符文本
SNIP_THRESHOLD = 0.60  # 利用率超过 60% 时触发 Tier 2 压缩
MICROCOMPACT_IDLE_S = 5 * 60  # 空闲 5 分钟后触发 Tier 3 清理（300 秒）
KEEP_RECENT_RESULTS = 3  # 保留最近 3 个工具结果不被裁剪（防止删除有用上下文）

@dataclass
class AgentState:
    """Agent 的运行时状态"""
    # 保留第 6 课的字段
    thinking_mode: Literal["disabled", "adaptive", "enabled"] = "disabled"
    # 保留第 8 课的字段
    confirmed_paths: set[str] = field(default_factory=set)
    current_task: asyncio.Task | None = None
    # 第 9 课新增的字段
    last_input_token_count: int = 0  # 最近一次 API 返回的输入 Token 数
    last_api_call_time: float | None = None  # 最近一次 API 调用的时间戳


class Agent:
    # ... 在 __init__ 中计算并保存有效窗口边界：
    # self.effective_window = _get_context_window(model) - CONTEXT_WINDOW_SAFETY_MARGIN
    # self.state = AgentState()
    # ... 其余初始化代码保持不变

    async def _check_and_compact(self) -> None:
        """检查是否需要自动压缩：当 Token 占用率超过 85% 时触发。"""
        # 当最近一次模型返回的输入 Token 超过有效窗口的 85% 时，触发压缩
        if self.state.last_input_token_count > self.effective_window * AUTOCOMPACT_THRESHOLD:
            print_info("Context window filling up, compacting conversation...")
            await self._compact_conversation()
```

#### 注意什么

- **缓冲水位线设计**：
  1. **`CONTEXT_WINDOW_SAFETY_MARGIN` (20000)**: 保留 20,000 tokens 作为安全缓冲区。大模型的系统提示词、用户最新提问以及回复都需要占用空间，不能等窗口 100% 满时才处理。
  2. **`AUTOCOMPACT_THRESHOLD` (0.85)**: 触发自动压缩的水位线阈值。如果已用空间达到有效窗口（总容量减去安全边界）的 85%，即刻开始压缩。
  3. **`LARGE_RESULT_THRESHOLD` (30 KB)**: 用以检查单次工具结果（如极长的 shell 输出）是否过长，若过长则通过持久化文件保存并自动截断，避免单个巨大结果瞬间爆表。

---

### 步骤 2：实现双后端自动摘要器

#### 为什么做

这是本节最核心的技术实现。我们将利用大模型自身的理解和归纳能力来缩减对话。
1. **备份最新消息**：必须先备份当前等待回复 the 最后一条 `user` 消息。
2. **请求摘要**：将除最后一条外的所有历史消息发给模型，要求模型提炼成一段摘要（保留核心决策、修改过的文件、目前进展）。
3. **消息历史重构**：将历史清空，用 `[Previous conversation summary] \n 摘要` 和 `Understood...` 这一对问答覆盖历史，随后在队列最末尾追加回此前备份的最新 `user` 消息，实现无缝平滑替换。
4. **封装性保证**：不要直接修改 `MessageHistory` 的底层私有列表（如 `_anthropic_messages`），而是调用其暴露的公有方法 `replace_anthropic_messages(new_messages)` 和 `replace_openai_messages(new_messages)`。

#### 做什么

在 `agent.py` 中编写 `_compact_conversation` 路由分发器及具体的后端压缩实现：

```python
# agent.py（续）

    async def compact(self) -> None:
        """手动触发压缩的公共接口（供 /compact 命令调用）。"""
        await self._compact_conversation()

    async def _compact_conversation(self) -> None:
        """根据后端类型分发到对应的压缩实现。"""
        if self.use_openai:
            await self._compact_openai()
        else:
            await self._compact_anthropic()
        print_info("Conversation compacted.")

    async def _compact_anthropic(self) -> None:
        """Anthropic 后端压缩：利用大模型自身能力总结历史对话。"""
        messages = self.history.anthropic_messages
        # 消息数过少（不足以进行有意义的总结）则直接返回
        if len(messages) < 4:
            return

        # 1. 备份最后一条用户消息（当前正在处理的请求）
        last_user_msg = messages[-1]

        # 2. 向上游 API 请求总结（排除最后一条用户消息，避免重复）
        response = await self._client.messages.create(
            model=self.backend.model,
            max_tokens=2048,  # 摘要输出通常不需要太多 token
            system="You are a conversation summarizer. Be concise but preserve important details.",
            messages=[
                *messages[:-1],  # 排除最后一条用户消息
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary = (
            response.content[0].text
            if response.content and response.content[0].type == "text"
            else "No summary available."  # 响应异常时的回退值
        )

        # 3. 重塑消息历史队列：用摘要对替换原始历史
        new_messages = [
            {"role": "user", "content": f"[Previous conversation summary]\n{summary}"},
            {
                "role": "assistant",
                "content": "Understood. I have the context from our previous conversation. How can I continue helping?",
            },
        ]

        # 4. 将最新用户请求重新接驳到历史队列末端，确保当前轮继续正常处理
        if last_user_msg.get("role") == "user":
            new_messages.append(last_user_msg)
        
        # ✅ 使用 MessageHistory 公共方法进行安全替换，不直接修改私有列表
        self.history.replace_anthropic_messages(new_messages)
        
        # 重置 Token 计数器，让下次检查重新开始计数
        self.state.last_input_token_count = 0

    async def _compact_openai(self) -> None:
        """OpenAI 后端压缩：保持 system 消息在队列首位。"""
        messages = self.history.openai_messages
        # OpenAI 最少包含 system + 2 轮对话 + 最新用户消息 = 5 条消息
        if len(messages) < 5:
            return

        # 1. 备份首位 system 消息（必须保持在队列首位）和末位 user 消息
        system_msg = messages[0]
        last_user_msg = messages[-1]

        # 2. 向上游 OpenAI API 发送总结请求（排除 system 和最后一条 user）
        response = await self._client.chat.completions.create(
            model=self.backend.model,
            messages=[
                {"role": "system", "content": "You are a conversation summarizer. Be concise but preserve important details."},
                *messages[1:-1],  # 排除首位的 system 和末位的最新 user 消息
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary = response.choices[0].message.content or "No summary available."

        # 3. 重新构造 OpenAI 历史列表（必须以 system 消息开头）
        new_messages = [
            system_msg,  # OpenAI 协议要求 system 消息必须在队列首位
            {"role": "user", "content": f"[Previous conversation summary]\n{summary}"},
            {
                "role": "assistant",
                "content": "Understood. I have the context from our previous conversation. How can I continue helping?",
            },
        ]

        # 4. 追加最新用户消息到队列末尾
        if last_user_msg.get("role") == "user":
            new_messages.append(last_user_msg)
            
        # ✅ 使用 MessageHistory 公共方法进行安全替换
        self.history.replace_openai_messages(new_messages)
        self.state.last_input_token_count = 0


#### 注意什么

- **摘要指令生成**：摘要生成请求属于高爆炸半径操作。如果我们在请求中允许大模型随意发散，可能会遗漏关键的文件编辑或运行结果。因此，摘要提示词应极力强调整理”发生了什么”、”编辑了哪些文件”以及”命令运行的最终产出”，保证压缩后的极简上下文没有信息丢失。
```

---

### 步骤 2.5：实现三级压缩流水线（Multi-tier Compression Pipeline）

#### 为什么做

单靠摘要压缩（Tier 0）不足以应对复杂会话场景。我们需要一个渐进式、低开销的多级压缩流水线，在对话过程中持续清理冗余数据，而非等到窗口爆满才紧急压缩：

1. **Tier 1（预算裁剪）**：当利用率超过 50% 时，裁剪过长的工具结果（如 `read_file` 返回的 5000 行文件），将其压缩为前后各保留一部分的摘要格式。
2. **Tier 2（陈旧结果替换）**：当利用率超过 60% 时，将旧的工具结果替换为占位符，只保留最近 3 个结果。
3. **Tier 3（空闲清理）**：当空闲 5 分钟以上时，清除所有旧结果为 `[Old result cleared]`。

这三级压缩在每次 API 调用前执行，开销极低（纯本地字符串操作），但能有效控制消息历史膨胀。

#### 做什么

在 `agent.py` 中实现三级压缩流水线及辅助方法：

```python
# agent.py（续）

    # ─── Multi-tier compression pipeline ──────────────────────

    def _run_compression_pipeline(self) -> None:
        """执行三级压缩流水线：Tier 1（预算裁剪）-> Tier 2（陈旧替换）-> Tier 3（空闲清理）。"""
        if self.use_openai:
            self._budget_tool_results_openai()
            self._snip_stale_results_openai()
            self._microcompact_openai()
        else:
            self._budget_tool_results_anthropic()
            self._snip_stale_results_anthropic()
            self._microcompact_anthropic()

    # ─── Tier 1: Budget tool results（预算裁剪）────────────────
    def _budget_tool_results_anthropic(self) -> None:
        """利用率 50% 时裁剪过长的单个工具结果，保留首尾部分。"""
        utilization = self.state.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < 0.5:
            return  # 利用率未达阈值，跳过
        # 利用率越高，预算越紧：50%-70% 用 30k，超过 70% 收紧到 15k
        budget = 15000 if utilization > 0.7 else 30000
        for msg in self.history.anthropic_messages:
            if msg.get(“role”) != “user” or not isinstance(msg.get(“content”), list):
                continue
            for block in msg[“content”]:
                if isinstance(block, dict) and block.get(“type”) == “tool_result” and isinstance(block.get(“content”), str) and len(block[“content”]) > budget:
                    # 保留首尾各一半空间，中间部分用截断标记替换
                    keep = (budget - 80) // 2
                    block[“content”] = block[“content”][:keep] + f”\n\n[... budgeted: {len(block['content']) - keep * 2} chars truncated ...]\n\n” + block[“content”][-keep:]

    def _budget_tool_results_openai(self) -> None:
        """OpenAI 后端的 Tier 1 裁剪逻辑。"""
        utilization = self.state.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < 0.5:
            return
        budget = 15000 if utilization > 0.7 else 30000
        for msg in self.history.openai_messages:
            if msg.get(“role”) == “tool” and isinstance(msg.get(“content”), str) and len(msg[“content”]) > budget:
                keep = (budget - 80) // 2
                msg[“content”] = msg[“content”][:keep] + f”\n\n[... budgeted: {len(msg['content']) - keep * 2} chars truncated ...]\n\n” + msg[“content”][-keep:]

    # ─── Tier 2: Snip stale results（陈旧结果替换）─────────────
    def _snip_stale_results_anthropic(self) -> None:
        """利用率 60% 时将旧的工具结果替换为占位符，保留最近 3 个。"""
        utilization = self.state.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return

        # 收集所有可裁剪的工具结果（排除已裁剪的和非可裁剪工具）
        results = []
        for mi, msg in enumerate(self.history.anthropic_messages):
            if msg.get(“role”) != “user” or not isinstance(msg.get(“content”), list):
                continue
            for bi, block in enumerate(msg[“content”]):
                if isinstance(block, dict) and block.get(“type”) == “tool_result” and isinstance(block.get(“content”), str) and block[“content”] != SNIP_PLACEHOLDER:
                    tool_use_id = block.get(“tool_use_id”)
                    tool_info = self._find_tool_use_by_id(tool_use_id)
                    # 只有 SNIPPABLE_TOOLS 中的工具结果才可裁剪
                    if tool_info and tool_info[“name”] in SNIPPABLE_TOOLS:
                        results.append({“mi”: mi, “bi”: bi, “name”: tool_info[“name”], “file_path”: tool_info.get(“input”, {}).get(“path”)})

        if len(results) <= KEEP_RECENT_RESULTS:
            return  # 结果数量未超阈值，无需裁剪

        # 标记需要裁剪的索引
        to_snip = set()
        seen_files: dict[str, list[int]] = {}
        for i, r in enumerate(results):
            if r[“name”] == “read_file” and r.get(“path”):
                seen_files.setdefault(r[“path”], []).append(i)

        # 同一文件的多次读取，只保留最后一次（前面的已过时）
        for indices in seen_files.values():
            if len(indices) > 1:
                for j in indices[:-1]:
                    to_snip.add(j)

        # 裁剪超出保留数量的旧结果
        snip_before = len(results) - KEEP_RECENT_RESULTS
        for i in range(snip_before):
            to_snip.add(i)

        # 执行裁剪：将选中的结果内容替换为占位符
        for idx in to_snip:
            r = results[idx]
            self.history.anthropic_messages[r[“mi”]][“content”][r[“bi”]][“content”] = SNIP_PLACEHOLDER

    def _snip_stale_results_openai(self) -> None:
        """OpenAI 后端的 Tier 2 裁剪逻辑。"""
        utilization = self.state.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return
        # 收集所有未被裁剪的 tool 消息索引
        tool_msgs = []
        for i, msg in enumerate(self.history.openai_messages):
            if msg.get(“role”) == “tool” and isinstance(msg.get(“content”), str) and msg[“content”] != SNIP_PLACEHOLDER:
                tool_msgs.append(i)
        if len(tool_msgs) <= KEEP_RECENT_RESULTS:
            return
        # 裁剪最旧的结果，保留最近 3 个
        snip_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(snip_count):
            self.history.openai_messages[tool_msgs[i]][“content”] = SNIP_PLACEHOLDER

    # ─── Tier 3: Microcompact（空闲清理）───────────────────────
    def _microcompact_anthropic(self) -> None:
        """空闲 5 分钟后清除所有旧结果为占位符。"""
        # 检查是否已空闲足够长时间
        if not self.state.last_api_call_time or (time.time() - self.state.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        # 收集所有未被清理的 tool_result 位置
        all_results = []
        for mi, msg in enumerate(self.history.anthropic_messages):
            if msg.get(“role”) != “user” or not isinstance(msg.get(“content”), list):
                continue
            for bi, block in enumerate(msg[“content”]):
                if isinstance(block, dict) and block.get(“type”) == “tool_result” and isinstance(block.get(“content”), str) and block[“content”] not in (SNIP_PLACEHOLDER, “[Old result cleared]”):
                    all_results.append((mi, bi))
        # 清理旧结果，保留最近 3 个
        clear_count = len(all_results) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            mi, bi = all_results[i]
            self.history.anthropic_messages[mi][“content”][bi][“content”] = “[Old result cleared]”

    def _microcompact_openai(self) -> None:
        """OpenAI 后端的 Tier 3 清理逻辑。"""
        if not self.state.last_api_call_time or (time.time() - self.state.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        tool_msgs = []
        for i, msg in enumerate(self.history.openai_messages):
            if msg.get(“role”) == “tool” and isinstance(msg.get(“content”), str) and msg[“content”] not in (SNIP_PLACEHOLDER, “[Old result cleared]”):
                tool_msgs.append(i)
        clear_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            self.history.openai_messages[tool_msgs[i]][“content”] = “[Old result cleared]”

    # ─── Helper: 查找工具调用信息 ──────────────────────────────
    def _find_tool_use_by_id(self, tool_use_id: str) -> dict | None:
        """根据 tool_use_id 在历史中反向查找对应的工具调用（用于 Tier 2 判断工具类型）。"""
        for msg in self.history.anthropic_messages:
            if msg.get(“role”) != “assistant” or not isinstance(msg.get(“content”), list):
                continue
            for block in msg[“content”]:
                if isinstance(block, dict) and block.get(“type”) == “tool_use” and block.get(“id”) == tool_use_id:
                    return {“name”: block[“name”], “input”: block.get(“input”, {})}
        return None  # 未找到匹配的工具调用

    # ─── Large result persistence（大结果持久化）────────────────
    def _persist_large_result(self, tool_name: str, result: str) -> str:
        """将超大工具结果（>30KB）保存到本地文件，只在历史中保留预览。"""
        if len(result.encode()) <= LARGE_RESULT_THRESHOLD:
            return result  # 未超阈值，直接返回原文
        # 确保存储目录存在
        d = Path.cwd() / “.mini-claude” / “tool-results”
        d.mkdir(parents=True, exist_ok=True)
        # 用时间戳和工具名生成唯一文件名
        filename = f”{int(time.time() * 1000)}-{tool_name}.txt”
        filepath = d / filename
        filepath.write_text(result, encoding=”utf-8”)

        # 生成预览：只保留前 200 行
        lines = result.split(“\n”)
        preview = “\n”.join(lines[:LARGE_RESULT_PREVIEW_LINES])
        size_kb = len(result.encode()) / 1024

        # 返回摘要信息 + 预览，引导用户用 read_file 查看完整内容
        return (
            f”[Result too large ({size_kb:.1f} KB, {len(lines)} lines). “
            f”Full output saved to {filepath}. “
            f”You can use read_file to see the full result.]\n\n”
            f”Preview (first {LARGE_RESULT_PREVIEW_LINES} lines):\n{preview}”
        )
```

#### 注意什么

- **渐进式降级策略**：
  1. **Tier 1（50% 触发）**：只裁剪过长的单个结果，保留所有结果的存在。budget 随利用率动态调整：50%-70% 时预算 30000 字符，超过 70% 时收紧到 15000 字符。
  2. **Tier 2（60% 触发）**：替换旧结果为占位符，但保留最近 3 个结果。对同一文件的多次读取，只保留最后一次（因为前面的读取结果已过时）。
  3. **Tier 3（空闲 5 分钟触发）**：利用用户离开的时间窗口，清理所有旧结果为 `[Old result cleared]`。

- **`SNIPPABLE_TOOLS` 的选择**：只有 `read_file`、`grep_search`、`list_files`、`run_shell` 这四类工具的结果可以被裁剪，因为它们的输出通常冗长但后续引用概率低。而 `edit_file`、`write_file` 等写入类工具的结果通常很短，且记录了关键操作，不应裁剪。

- **`_find_tool_use_by_id` 的作用**：在 Anthropic 协议中，`tool_result` 通过 `tool_use_id` 与 `tool_use` 配对。Tier 2 需要知道结果对应的工具名称才能判断是否可裁剪，因此需要遍历历史查找。

- **`_persist_large_result` 的设计**：当单个工具结果超过 30KB 时，将其完整内容保存到 `./.mini-claude/tool-results/` 目录，只在消息历史中保留前 200 行预览。这避免了单个巨大结果（如完整的构建日志）瞬间撑爆上下文窗口。

---

### 步骤 3：在轮次边界（Turn Boundary）上接入自动检测

#### 为什么做

这是本节**最关键的避坑要点**。
- 为什么必须在 **Turn Boundary（轮次边界，即每次用户新输入被 push 进队列之后、while 主循环启动并发送新请求之前）** 触发压缩检测？
- 因为在此处，队列的最末端永远是一条干净的纯文本 `user` 消息（例如 `{"role": "user", "content": "新提问"}`）。
- 如果你在工具循环执行的过程中（`while True` 中段）触发压缩，此时队列末端可能是未配对的 `tool_result`，执行切片（`[:-1]`）会直接截断 `tool_use` 与 `tool_result` 的物理配对关系，直接导致 LLM 报错拒绝服务。

#### 做什么

修改 `agent.py` 的 `_chat_anthropic` 和 `_chat_openai` 核心入口，在将用户消息推入历史后，率先调用压缩审查：

```python
# agent.py 中的修改

    async def _chat_anthropic(self, user_message: str) -> None:
        """Anthropic 后端的主对话循环：处理用户消息并管理工具调用。"""
        # 1. 用户消息推入历史
        self.history.append_user_message(user_message)

        # 2. 在轮次边界（Turn Boundary）检查是否需要自动压缩
        # 必须在此处检查，因为队列末尾是干净的 user 消息，不会破坏 tool_use/tool_result 配对
        await self._check_and_compact()

        # 3. 启动异步记忆预取（第 10 课实现）
        # 注：第 10 课会实现 _start_memory_prefetch 方法
        # memory_prefetch = self._start_memory_prefetch(user_message)

        # 4. 进入工具循环
        while True:
            if self._aborted:
                break

            # 每次迭代前更新系统提示词并执行三级压缩流水线
            self._update_system_prompt()
            self._run_compression_pipeline()  # Tier 1/2/3 本地压缩，开销极低
            # 注：第 10 课会实现 _consume_memory_prefetch 方法
            # self._consume_memory_prefetch(memory_prefetch)

            # ... 调用 API 并处理响应 ...

            # 请求成功后更新 Token 计数（流式结束后一次性更新，避免冗余赋值）
            self.state.last_input_token_count = response.usage.input_tokens

            # ... 处理工具调用 ...

            # 如果没有工具调用，退出循环
            if not tool_uses:
                break
```

同样地，在 `_chat_openai` 内部也做相同修改：

```python
    async def _chat_openai(self, user_message: str) -> None:
        """OpenAI 后端的主对话循环：与 Anthropic 版本结构相同。"""
        # 1. 用户消息推入历史
        self.history.append_user_message(user_message)

        # 2. 在轮次边界（Turn Boundary）检查是否需要自动压缩
        await self._check_and_compact()

        # 3. 启动异步记忆预取（第 10 课实现）
        # 注：第 10 课会实现 _start_memory_prefetch 方法
        # memory_prefetch = self._start_memory_prefetch(user_message)

        # 4. 进入工具循环
        while True:
            if self._aborted:
                break

            # 每次迭代前更新系统提示词并执行三级压缩流水线
            self._update_system_prompt()
            self._run_compression_pipeline()  # Tier 1/2/3 本地压缩
            # 注：第 10 课会实现 _consume_memory_prefetch 方法
            # self._consume_memory_prefetch(memory_prefetch)

            # ... 调用 API 并处理响应 ...

            # 请求成功后更新 Token 计数
            self.state.last_input_token_count = response.usage.input_tokens

            # ... 处理工具调用 ...

            # 如果没有工具调用，退出循环
            if not tool_uses:
                break
```

注意：在 `while True` 循环的每次迭代中，必须依次调用：

1. `_update_system_prompt()`：确保系统提示词包含最新的记忆注入和工具列表。
2. `_run_compression_pipeline()`：执行三级压缩流水线（Tier 1/2/3），在每次 API 调用前清理冗余数据。
3. `_consume_memory_prefetch(memory_prefetch)`：消费异步预取的记忆结果，避免阻塞主循环。


#### 注意什么

- **Token 更新的生命周期**：为什么必须在 `_chat_anthropic`/`_chat_openai` 外层循环中更新 `last_input_token_count`？因为在 streaming 流式通信中，大模型的 chunk 包被多路并发异步接收，而在流结束时进行一次性更新，可以保证计算的水位线永远最新，避免频繁在流的每一个 chunk 里去做冗余的重复赋值动作。

---

### 步骤 4：连通 REPL 终端中的手动压缩指令 `/compact`

#### 为什么做

除了自动阈值检测外，我们需要向终端暴露一个直观的手动调试入口 `/compact`。当用户认为对话太长，或者发现系统有些变慢时，可以强制触发会话压缩，立刻清理历史。

#### 做什么

修改 `__main__.py` 中的 REPL 指令解析段，连通 Agent 的手动压缩接口：

```python
# __main__.py 中的修改

        # ... 在 run_repl 函数的命令分发处修改：
        if inp == "/clear":
            agent.clear_history()
            continue
            
        # 连通手动压缩命令：用户可强制触发历史压缩
        if inp == "/compact":
            try:
                await agent.compact()
            except Exception as e:
                # 捕获异常避免网络故障导致 REPL 闪退
                print(f"  [red]Error: {e}[/red]")
            continue


#### 注意什么

- **异常拦截**：手动压缩指令 `/compact` 在 REPL 中以独立控制台命令执行。必须加 `try...except` 拦截异常，避免因为云端限流等网络偶发失败导致交互终端闪退。
```

---

## ⚖️ 设计权衡

### 本地编译 Token 估算（Tokenizer） vs 依赖 API 响应元数据

- **方案 A**：**基于 API 响应元数据（Usage）记录**（我们所用）
  - 每次请求成功后，读取大模型官方响应中携带的 `prompt_tokens` 或 `input_tokens` 精确数值存入 `self.last_input_token_count`。
  - **优点**：数据 100% 物理精准，没有任何估算误差，代码实现极其轻量。
  - **缺点**：只有在一次完整对话请求成功后，才能拿到最新数据，无法在发送前对超长的单次输入（如输入一个 10MB 的文本）进行事前阻断。
- **方案 B**：**本地引入 Tiktoken / Anthropic Tokenizer**
  - 在本地加载大模型词表分词器，对消息队列进行实时离线 Token 估算。
  - **优点**：可以在请求发出之前阻断和压缩。
  - **缺点**：需要引入大型外部依赖库，且不同后端（Anthropic vs OpenAI vs DeepSeek）的词表不同，本地估算存在 5%-10% 左右的系统误差，违背了 mini-claude 的“零第三方依赖”极简原则。

**结论**：使用 API 响应提供的真实元数据，是教学版本和简化设计下保持高精准度、零外部依赖的最佳实践。

---

## ⚠️ 常见陷阱

### 1. 错误的压缩时机切断了工具匹配对

```python
# ❌ 错误：在工具执行的 while 循环中途执行了 compact
# 此时 history.messages = [..., tool_use, tool_result]
# 切片 messages[:-1] 丢弃了最后一条 tool_result，但保留了前一条 assistant 的 tool_use 块
response = await client.messages.create(...)
```

**后果**：API 服务端会直接返回 `400 BadRequest` 报错，提示 `assistant` 的工具调用缺失对应的结果回执。
**修正**：自动压缩只能位于 **Turn Boundary（轮次边界）**。如果需要在中途强制触发，必须保证当前队列的最末尾是常规文本消息。

---

### 2. 摘要中遗漏了关键的文件修改历史

在总结时，模型可能会为了简短而忽略之前修改过哪些文件，导致压缩后 Agent 忘记之前的进度而造成重复改写。

**修正**：在总结的 `system` 提示词中，要给模型施加严格的提示规则：`system="You are a conversation summarizer. Be concise but preserve important details."`。

---

## ✅ 验收点

### 输入与执行

1. 启动 Agent REPL：
   ```bash
   python __main__.py
   ```
2. 进行 2-3 轮常规问答。
3. 输入手动压缩命令 `/compact`。
4. **验证压缩历史**：
   - 终端应该打印 `ℹ Conversation compacted.`。
   - 接着向 Agent 提问：`“我们刚才聊了什么？”`，确认大模型是否能准确复述出压缩前的前情概要。

---

## 🧠 思考题

1. **在 `_compact_openai` 中，我们读取首位 system 消息并最终把它重新塞回到新历史 `self.history.openai_messages[0]` 的首位，为什么？**
   *(提示：OpenAI 协议中，系统提示词必须一直处于整个消息历史队列的最开头。如果压缩时把 `system` 消息丢弃了，模型会失去对自身身份和工具规范的认知，直接退化为普通的无工具聊天助手。)*
2. **自动摘要压缩虽然释放了 Token 空间，但它需要我们专门调用一次大模型 API。频繁自动压缩会产生什么副作用？**
   *(提示：频繁压缩会增加额外的 API 费用和响应等待时间。因此，我们设定了高达 85% 的”高水位线”触发阈值，尽量减少总结 API 的调用频次。)*
3. **三级压缩流水线（Tier 1/2/3）为什么要在每次 API 调用前执行，而不是只在触发摘要压缩（Tier 0）时执行？**
   *(提示：Tier 1/2/3 是纯本地字符串操作，开销极低，能在对话过程中持续清理冗余数据，避免窗口突然爆满。而 Tier 0（摘要压缩）需要调用大模型 API，开销较高。三级流水线的设计理念是”勤清理、少急救”。)*

---

## 📦 本节收获

1. **四级压缩架构**：掌握了从 Tier 1（预算裁剪）-> Tier 2（陈旧结果替换）-> Tier 3（空闲清理）-> Tier 0（摘要压缩）的渐进式上下文容错架构。
2. **轮次边界控制（Turn Boundary）**：理解了在 Agent 状态机中保护 `tool_use`/`tool_result` 协议配对完整性的重要设计原理。
3. **大结果持久化**：掌握了将超大工具结果（>30KB）自动保存到本地文件、只在历史中保留预览的工程手段。
4. **元数据校准**：掌握了利用大模型 API 元数据反馈校正 Token 计算偏离的工程实现手段。

---

> **下一章**：现在 Agent 拥有了长期对话的无限记忆。接下来我们将为其实现长效记忆机制——通过本地持久化实现跨会话的语义召回记忆系统。
