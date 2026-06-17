# 第 13 课：预算控制与成本管理

## 🎯 本节目标

为 Agent 构建 **双层安全预算与成本管理机制**：在软件工程自主执行场景中，为了防止 Agent 因死循环、指令冲突或大面积 API 调用而导致资金超出预期或上下文爆栈，我们需要在底层设计两重防线：
1. **硬性消费/生命周期预算（USD / 回合限制）**：限制单次会话的美元总开销及交互轮数。
2. **上下文负载字符预算（Tier 1 级工具结果压缩）**：在上下文负载较高时，自动对超长工具执行结果实施截断，以此控制单次 API 请求的 Token 大小并节省消费预算。

---

## 🏆 最终效果

完成本节课程的开发后，学员可以测试如下功能：

### 1. 美元预算超限拦截
运行以下命令，设置极低的消费限额（0.0001 美元）：
```bash
mini-claude-py --max-cost 0.0001 "查找当前项目所有的 .py 文件并做分析"
```
系统在第一轮迭代结束、更新 Token 账单后，会触发拦截器，终止 Agent Loop 并提示：
```
[INFO] Budget exceeded: Cost limit reached ($0.0006 >= $0.0001)
```

### 2. 回合数超限拦截
限制 Agent 最多只能执行 2 个回合：
```bash
mini-claude-py --max-turns 2 "搜索项目目录，修改 tools.py 并运行测试"
```
Agent 执行完两轮工具调用与思考后，自动被系统阻断并退出，不再发起下一次 API 请求：
```
[INFO] Budget exceeded: Turn limit reached (2 >= 2)
```

### 3. REPL 实时账单查询
在 REPL 交互界面中，随时可以输入 `/cost` 查看当前会话的总 Token 吞吐以及估算的美元花费：
```
> /cost
[INFO] Tokens: 12040 in / 1024 out
  Estimated cost: $0.0515 / $5.0 budget | Turns: 3/50
```

---

## 🛠️ 本节任务

- **任务 1**：在 `__main__.py` 中添加 `--max-cost` 与 `--max-turns` 命令行参数解析，并在实例化 `Agent` 时透传该限制。
- **任务 2**：在 `agent.py` 的常量定义区硬编码 Token 计费单价，并在 `AgentState` 中追踪总输入/输出 Token 数。
- **任务 3**：编写 `_get_current_cost_usd` 计费公式与 `_check_budget` 拦截器。
- **任务 4**：在 Agent 主循环中（工具调用结果处理后）插入预算超额检查，并在 REPL 侧实现 `/cost` 指令和 `show_cost` 展示。
- **任务 5**：实现 Tier 1 级别的上下文压缩机制，并通过 `_run_compression_pipeline` 串联 Tier 2/3（详见第 09 课）形成完整压缩流水线。

---

## 📦 涉及文件

修改：
- [agent.py](file:///e:/project/claude-code-from-scratch/agent.py)
- [__main__.py](file:///e:/project/claude-code-from-scratch/__main__.py)

---

## 🚀 开始实现

### 步骤 1：在 `__main__.py` 中集成控制台参数与透传

#### 为什么做
预算控制需要允许用户在启动时根据任务复杂度进行配置。我们需要让 CLI 入口接收最大美元预算与最大执行回合数的控制参数，并传入 Agent 的配置中。

#### 做什么
打开 `__main__.py`，修改 `_parse_args` 函数添加命令行参数；同时在 `main` 函数实例化 `Agent` 时将参数传入：

```python
# __main__.py -> _parse_args()

    # ... 其他参数 ...
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")       # 美元预算上限，超过即熔断
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")    # 最大执行轮次，防止死循环
```

随后，在 `main()` 函数中实例化 `Agent` 的位置，将这两个参数传递给 `Agent` 的构造器：

```python
# __main__.py -> main()

    # 将 CLI 传入的预算限制透传至 Agent 配置
    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        max_cost_usd=args.max_cost,          # 美元预算上限，None 表示不限制
        max_turns=args.max_turns,            # 轮次上限，None 表示不限制
        api_base=resolved_api_base if resolved_use_openai else None,
        anthropic_base_url=resolved_api_base if not resolved_use_openai else None,
        api_key=resolved_api_key,
    )
```

#### 注意什么
确保参数类型正确。`--max-cost` 为 `float`，而 `--max-turns` 为 `int`。

---

### 步骤 2：在 `agent.py` 中定义计费常量并记录状态

#### 为什么做
实时估算消费金额需要有底层的计费标准。我们在本地硬编码模型单价，并在 `AgentState` 运行时状态中累加每轮 API 请求所消耗的 Token 数量。

#### 做什么
打开 `agent.py`。在常量区添加计费常量（对应 Claude 3.5 Sonnet 的标准计费）：

```python
# agent.py -> Constants

# Claude 3.5 Sonnet 标准计费单价（每百万 Token）
COST_PER_INPUT_TOKEN = 3 / 1_000_000   # 输入: $3.00/1M tokens
COST_PER_OUTPUT_TOKEN = 15 / 1_000_000  # 输出: $15.00/1M tokens（输出单价更高，因此控制输出长度对成本影响更大）
```

在 `AgentState` 类中，确保追踪如下 Token 和轮数变量（这些字段应在构造时或每轮 API 交互后累加更新）：

```python
# agent.py -> AgentState

@dataclass
class AgentState:
    """Mutable runtime state for an Agent instance."""
    total_input_tokens: int = 0          # 累计输入 Token（跨会话持续累加，用于计费）
    total_output_tokens: int = 0         # 累计输出 Token
    last_input_token_count: int = 0      # 最近一次 API 请求的输入 Token 数（用于上下文负载评估）
    current_turns: int = 0               # 当前会话已执行的轮次（每次工具调用+思考为一轮）
    # ... 其他状态 ...
```

#### 注意什么
计费单价是以单条 Token 为单位计算的，因此在定义常量时需要除以 `1,000,000`。

---

### 步骤 3：编写计费与超限拦截函数

#### 为什么做
我们需要封装底层的计费公式以及限额比对判定逻辑，当任何一项指标（美元花费或轮数）超过设定值时，返回明确的超额信号和拦截原因。

#### 做什么
在 `agent.py` 的 `Agent` 类中，编写 `_get_current_cost_usd` 和 `_check_budget` 两个核心助手方法：

```python
# agent.py -> Agent 类内部

    # 根据累计 Token 数与本地单价估算当前总花费
    def _get_current_cost_usd(self) -> float:
        """根据当前已消耗的输入与输出 Token 估算总 USD 花费。"""
        return (
            self.state.total_input_tokens * COST_PER_INPUT_TOKEN
            + self.state.total_output_tokens * COST_PER_OUTPUT_TOKEN
        )

    # 双重熔断：美元超限 + 轮次超限，任一触发即终止
    def _check_budget(self) -> dict:
        """检查当前的美元消费及执行回合数是否已经超出预算限制。"""
        # 先检查美元预算（Cost-bound），后检查轮次预算（Turn-bound）
        if self.config.max_cost_usd is not None and self._get_current_cost_usd() >= self.config.max_cost_usd:
            return {
                "exceeded": True, 
                "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.config.max_cost_usd})"
            }
        if self.config.max_turns is not None and self.state.current_turns >= self.config.max_turns:
            return {
                "exceeded": True, 
                "reason": f"Turn limit reached ({self.state.current_turns} >= {self.config.max_turns})"
            }
        return {"exceeded": False}
```

#### 注意什么
计费统计在 `clear-history` 后总 Token 消费是持续累加的，这符合计费的全局逻辑。

---

### 步骤 4：主循环集成拦截与 REPL `/cost` 命令

#### 为什么做
有拦截函数还不够，我们必须将其织入 Agent Loop 的每个决策节点。同时，为交互端提供可视化的账单命令 `/cost` 以提升用户体验。

#### 做什么

1. **主循环拦截**：在 `agent.py` 的 `chat` / `run_once` 的主 `while` 循环体中，工具调用结果处理完成后（Token 更新与轮次递增后），进行超限拦截判定：

```python
# agent.py -> _chat_anthropic() / _chat_openai()

        while True:
            # ... 压缩流水线 ...
            # ... API 调用与流式输出 ...
            # ... 更新 Token 计数与轮次 ...

            self.state.current_turns += 1  # 每轮工具调用后递增轮次计数
            # 在轮次递增后、下一轮 API 请求前拦截，避免浪费一轮调用
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                break  # 超限则跳出循环，终止迭代
```

2. **REPL 互动展现**：在 `Agent` 类中实现 `show_cost` 方法：

```python
# agent.py

    # REPL /cost 命令的展示入口，格式化输出 Token 用量与估算花费
    def show_cost(self) -> None:
        total = self._get_current_cost_usd()
        # 只有设置了预算上限时才显示预算信息，避免误导用户
        budget_info = f" / ${self.config.max_cost_usd} budget" if self.config.max_cost_usd else ""
        turn_info = f" | Turns: {self.state.current_turns}/{self.config.max_turns}" if self.config.max_turns else ""
        print_info(
            f"Tokens: {self.state.total_input_tokens} in / {self.state.total_output_tokens} out\n"
            f"  Estimated cost: ${total:.4f}{budget_info}{turn_info}"
        )
```

并在 `__main__.py` 的 REPL 命令处理分支中拦截 `/cost` 输入：

```python
# __main__.py -> run_repl()

        # REPL 斜杠命令拦截：/cost 展示当前会话的实时账单
        if inp == "/cost":
            agent.show_cost()
            continue
```

#### 注意什么

拦截检查必须同时覆盖 `run_once`（单次命令模式）和 `chat`（交互式 REPL 模式）两条入口主循环路径。注意 `_check_budget()` 的调用位置是在 `current_turns` 递增之后、工具结果处理之前——这确保了模型在发起下一轮 API 请求前就能被拦截，避免浪费一轮 API 调用。

---

### 步骤 5：实现 Tier 1 字符级工具结果压缩预算

#### 为什么做
API 费用支出大多由上下文输入（历史记录）贡献。当大模型在一个会话中调用了如 `read_file`、`grep_search` 返回了巨大的字符串，而对话轮数又很多时，会导致之后每次请求的 Input Tokens 指数级暴涨。
我们需要在输入积压时，主动截断超大工具结果的中间部分，把输入限制在“字符级预算”内。

#### 做什么
在 `agent.py` 中，根据当前上下文负载百分比（`utilization`）对大文本结果进行剪切。我们分别为 Anthropic 与 OpenAI 格式编写截断处理方法：

```python
# agent.py

    # Tier 1 压缩：按上下文负载率对超长工具结果进行首尾保留截断
    def _budget_tool_results_anthropic(self) -> None:
        # 计算当前上下文占窗口的百分比（utilization），低于 50% 不干预以避免过度截断
        utilization = self.state.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < 0.5:
            return  # 负载低于 50% 不做干预

        # 超过 70% 负载时实行 15KB（15000字）严限，否则为 30KB 限额
        budget = 15000 if utilization > 0.7 else 30000
        for msg in self.history.anthropic_messages:
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue  # 只处理 user 角色的复合消息（工具结果嵌在 user 消息中）
            for block in msg["content"]:
                # 如果是工具执行结果块，且文本长度超预算
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and len(block["content"]) > budget:
                    keep = (budget - 80) // 2  # 预留 80 字符给截断提示信息
                    # 保留首尾各 keep 字符，中间用 budgeted 截断信息替换
                    block["content"] = block["content"][:keep] + f"\n\n[... budgeted: {len(block['content']) - keep * 2} chars truncated ...]\n\n" + block["content"][-keep:]

    # OpenAI 格式的 Tier 1 压缩：逻辑与 Anthropic 版相同，仅消息结构不同
    def _budget_tool_results_openai(self) -> None:
        utilization = self.state.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < 0.5:
            return
        budget = 15000 if utilization > 0.7 else 30000
        for msg in self.history.openai_messages:
            # OpenAI 格式中工具结果是独立的 tool 角色消息
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg["content"]) > budget:
                keep = (budget - 80) // 2
                msg["content"] = msg["content"][:keep] + f"\n\n[... budgeted: {len(msg['content']) - keep * 2} chars truncated ...]\n\n" + msg["content"][-keep:]
```

Tier 1 压缩是三级压缩流水线的第一级。完整的 `_run_compression_pipeline` 方法将 Tier 1、Tier 2（陈旧结果替换）、Tier 3（空闲清理）串联执行，在每次 API 调用前运行：

```python
# agent.py -> _run_compression_pipeline()

    # 三级压缩流水线：按需截断 -> 陈旧替换 -> 空闲清理，每轮 API 调用前执行
    def _run_compression_pipeline(self) -> None:
        """执行三级压缩流水线：Tier 1 -> Tier 2 -> Tier 3"""
        if self.use_openai:
            self._budget_tool_results_openai()      # Tier 1: 按字符预算截断超大工具结果
            self._snip_stale_results_openai()        # Tier 2: 替换陈旧的文件读取结果
            self._microcompact_openai()              # Tier 3: 长时间空闲后清理中间历史
        else:
            self._budget_tool_results_anthropic()    # Tier 1: 按字符预算截断超大工具结果
            self._snip_stale_results_anthropic()     # Tier 2: 替换陈旧的文件读取结果
            self._microcompact_anthropic()           # Tier 3: 长时间空闲后清理中间历史
```

Tier 2（`_snip_stale_results`）和 Tier 3（`_microcompact`）的详细实现已在 **第 09 课：上下文管理** 中完整覆盖，此处不再重复。本节聚焦 Tier 1 的预算裁剪逻辑。

该流水线在 `_chat_anthropic` 和 `_chat_openai` 的主循环中，每次 API 调用前被调用：

```python
# agent.py -> _chat_anthropic() / _chat_openai() 主循环

        while True:
            # ...
            self._update_system_prompt()
            self._run_compression_pipeline()  # 每轮 API 调用前执行三级压缩，防止上下文爆炸
            # ... API 调用 ...
```

#### 注意什么
截断只针对 `tool_result` / `tool` 类型的消息。千万不能误伤用户输入（`user` 消息中的文本）或模型的推理回复（`assistant` 消息），否则会导致大模型无法理解上下文。

---

## ⚖️ 设计权衡

### 方案 A：本地硬编码 Token 计费估算
* **优点**：
  1. 计算速度极快：无需等待 API 响应，可本地直接对每一轮流式输出产生的 Token 进行加权计算。
  2. 实现简单：只需在配置文件中静态绑定几组模型单价常量。
* **缺点**：
  * 模型价格调整或使用非标准 API 服务（如自定义中转平台、混合调用）时，本地算出的美元账单可能不够精准。

### 方案 B：通过 API 返回的 usage 信息动态解析计费
* **优点**：
  * 账单 100% 精准，完全以 API Provider 最终结算为准，自动支持 API 动态调价和混合模型计费。
* **缺点**：
  * 在流式输出（Streaming）过程中，多数 API 服务商在流片段中不会返回 usage 信息，只有在最终流结束的尾部 block 才会汇总，导致在流输出中途无法实时计算消费预算。

### 结论
在 `mini-claude` 中，我们采用 **本地单价硬编码估算（方案 A）**。这主要是为了在每一轮迭代、甚至是流式输出过程中能够以极低的延迟向用户展示成本，保障预算超限拦截的及时性。

---

## ⚠️ 常见陷阱

### 1. thinking (深度思考) 模式下的额外计费遗漏
* **陷阱**：在启用 Extended Thinking（深度思考）功能时，输出的推理 Token（Thinking Tokens）在某些 API 服务商处的费率与普通 Output Token 不同。若粗暴地将其与普通 Output Token 混算，可能导致成本统计偏低。
* **解决方案**：若模型配置启用了 thinking 选项，建议在计算单价时将这部分思考输出单独分类，或在提示词费率计算中预留一定的安全缓冲区。

### 2. 流式响应未归档导致的计数延迟
* **陷阱**：如果只在接收到完整模型回复后才结算 Token 计数，那么在一轮极其冗长的大文本流式输出中，如果单轮的开销就已经超过了总额，系统将无法在中途终止它，直到整个巨额流接收完毕，这会导致预算防线失效。
* **解决方案**：在流读取过程中，累加已接收字符，并根据比例在循环中估算当前输出 Token 并不断比对。

---

## ✅ 验收点

### 1. 验证单轮低美元预算拦截
* **输入命令**：
  ```bash
  mini-claude-py --max-cost 0.0001 "Hello, who are you?"
  ```
* **预期效果**：
  第一轮对话接收完模型回复后，终端显示预算超限，程序退出。

### 2. 验证低轮数拦截
* **输入命令**：
  ```bash
  mini-claude-py --max-turns 1 "在当前目录下找一个 .md 文件，读取它，然后分析它的结构"
  ```
* **预期效果**：
  Agent 在调用第一个工具（如 `list_files` 或 `read_file`）返回结果并被模型思考后，不再发起下一次决策请求，系统直接抛出超限并干净地退出。

### 3. 验证 `/cost` 展现
* **操作流程**：
  1. 启动 REPL：`mini-claude-py`
  2. 输入交互指令：`Hello`
  3. 输入 `/cost` 命令。
  4. 验证打印的 `Estimated cost` 小数点后保留 4 位，且能够正确显示类似 `/ $5.0 budget`（若指定了 --max-cost）或回合数 `Turns: 1/50`。

---

## 🧠 思考题

1. **若在大模型与工具交互的中途，我们对其进行了 Tier 1 工具结果截断，被截断的内容包含代码修改报错信息，大模型收到被截断的结果后会有什么表现？如何引导它妥善处理？**

2. **除了在 API 层面拦截，对于 run_shell 命令工具，我们是否也需要对其运行时间或占用的系统资源设定“工具执行预算”？为什么？**

---

## 📦 本节收获

* **多维度熔断拦截**：学会了如何在 Agent 核心回路中织入美元成本（Cost-bound）与运行周期（Turn-bound）的双重硬性拦截防线。
* **按负载动态截断**：掌握了根据上下文积压率动态计算”字符级预算”的 Tier 1 算法，并通过 `_run_compression_pipeline` 将其与 Tier 2（陈旧结果替换）和 Tier 3（空闲清理）串联为完整的三级压缩流水线。
* **流式成本估算**：理解了在无法中途取得真实 Usage 数据时，如何使用本地计费模型进行近实时估算的架构思路。

---

> **下一章**：现在 Agent 具备了完善的安全防线与计费机制。下一步我们将教导 Agent 在面对复杂的巨型工程任务时，如何运用“分而治之”的设计艺术——派生并管理独立的 Sub-Agent 子代理系统。
