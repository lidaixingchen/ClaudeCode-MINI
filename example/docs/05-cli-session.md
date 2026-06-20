# 第 05 课：CLI 与会话持久化

## 🎯 本节目标

为 Agent 构建用户交互层与状态持久化机制。实现交互式命令行终端（REPL）、Ctrl+C 优雅中断机制，并支持将会话历史以 JSON 格式持久化到本地磁盘，允许用户通过 `--resume` 参数恢复上次对话。

---

## 🏆 最终效果

完成本节后，用户可以直接启动 Agent 进入交互式终端（REPL）：

```bash
python __main__.py
```

你将看到精美的欢迎信息与命令行提示符：
```
  Mini Claude Code — A minimal coding agent

  Type your request, or 'exit' to quit.
  Commands: /clear /cost

> hello
```

**功能测试**：
1. **指令测试**：输入 `/clear` 可以清空对话历史。
2. **中断测试**：当 Agent 正在思考或执行工具时，按下 `Ctrl+C` 将会中断本次执行并返回 `> ` 提示符，而不会导致整个程序退出。在空闲状态下，双击 `Ctrl+C` 会退出程序。
3. **会话恢复测试**：输入几句对话后输入 `exit` 退出，再次运行 `python __main__.py --resume`，Agent 将完美读取历史会话。

---

## 🛠️ 本节任务

1. **实现会话存储读写库**：在 `session.py` 中编写会话保存、读取与检索逻辑。
2. **封装 Agent 公共接口与自动保存**：在 `agent.py` 中实现 `chat()` 封装，并在对话结束时自动持久化。
3. **创建最简终端 UI 辅助库**：在 `ui.py` 中定义欢迎横幅、提示符、错误输出等基础函数。
4. **编写命令行参数解析**：在 `__main__.py` 中解析运行参数，支持命令行 prompt 直发与 `--resume` 恢复。
5. **构建交互式 REPL 循环与信号拦截**：在 `__main__.py` 中实现 REPL 循环，注入 `Ctrl+C` 信号处理器。

---

## 📦 涉及文件

修改：
- `agent.py`
- `__main__.py`

创建：
- `session.py`
- `ui.py`

---

## 🚀 开始实现

### 步骤 1：实现会话存储读写库 `session.py`

#### 为什么做

Agent 在运行过程中，我们需要随时将其当前的对话历史持久化到本地。这样即便程序崩溃或用户主动退出，对话记录也不会丢失。我们会将会话以 JSON 格式保存在当前目录下的 `.mini-claude/sessions` 目录中。

#### 做什么

创建（或覆写）`session.py`，实现会话的目录创建、保存、读取与查找最新会话 ID 的功能：

```python
# session.py — 会话持久化存储模块

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 会话文件存储目录，位于当前工作目录下
SESSION_DIR = Path.cwd() / ".mini-claude" / "sessions"


def _ensure_dir() -> None:
    """确保存储目录存在，不存在则递归创建。"""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def save_session(session_id: str, data: dict[str, Any]) -> None:
    """将会话数据序列化为 JSON 并写入磁盘。"""
    _ensure_dir()
    # 使用 indent=2 生成可读的 JSON，default=str 处理 datetime 等不可序列化类型
    (SESSION_DIR / f"{session_id}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def load_session(session_id: str) -> dict[str, Any] | None:
    """从磁盘加载指定 ID 的会话数据，不存在或损坏则返回 None。"""
    path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_sessions() -> list[dict[str, Any]]:
    """列出所有已保存会话的元数据摘要。"""
    _ensure_dir()
    results = []
    for f in SESSION_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            # 只提取元数据部分，避免加载完整消息历史
            if "metadata" in data:
                results.append(data["metadata"])
        except Exception:
            pass
    return results


def get_latest_session_id() -> str | None:
    """获取最近一次会话的 ID，用于 --resume 恢复。"""
    sessions = list_sessions()
    if not sessions:
        return None
    # 按启动时间降序排序，取最新的会话
    sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    return sessions[0].get("id")
```

---

### 步骤 2：封装 Agent 公共接口与自动保存

#### 为什么做

我们在第 1、2 课中实现的 `_chat` 是包含核心 `while` 循环的内部逻辑。我们需要向外部调用者提供一个更加健壮的公共接口 `chat()`，在开始前重置状态，并在每轮对话顺利结束时自动触发会话存储。

#### 做什么

修改 `agent.py`，需要完成三件事：

1. 为 `MessageHistory` 类补充会话持久化相关的方法。
2. 在 `Agent` 初始化中定义唯一的 `session_id` 和 `session_start_time`。
3. 封装公共 `chat` 方法，并在其中添加 `_auto_save()` 和 `restore_session()` 逻辑。

首先，在 `MessageHistory` 类中补充以下方法（会话持久化需要用到）：

```python
# MessageHistory 类中新增的方法

def message_count(self) -> int:
    """返回消息总数（不含系统提示词）"""
    if self.use_openai:
        # OpenAI 模式下减去首条系统消息
        return len(self._openai_messages) - 1
    return len(self._anthropic_messages)

def to_dict(self) -> dict[str, list[dict]]:
    """将消息历史序列化为字典，用于会话持久化"""
    return {
        "anthropicMessages": self._anthropic_messages,
        "openaiMessages": self._openai_messages,
    }

def restore(self, data: dict[str, list[dict]]) -> None:
    """从持久化数据恢复消息历史"""
    if "anthropicMessages" in data and data["anthropicMessages"]:
        self._anthropic_messages = data["anthropicMessages"]
    if "openaiMessages" in data and data["openaiMessages"]:
        self._openai_messages = data["openaiMessages"]

def clear(self, keep_system: bool = True) -> None:
    """清空消息历史"""
    self._anthropic_messages.clear()
    if keep_system and self.use_openai:
        # 保留系统提示词
        self._openai_messages.clear()
        self._openai_messages.append({"role": "system", "content": self.system_prompt})
    else:
        self._openai_messages.clear()
```

然后，修改 `Agent` 类：

```python
# agent.py 中的修改

import uuid
import time
from pathlib import Path
from session import save_session  # 导入会话保存函数


class Agent:
    def __init__(self, backend: BackendConfig):
        """初始化 Agent 实例。"""
        self.backend = backend
        self.config = AgentConfig()
        self.state = AgentState()
        self.use_openai = backend.is_openai

        # 初始化消息历史管理器，负责统一格式化两种协议的消息结构
        system_prompt = "You are a helpful coding assistant with access to tools."
        self.history = MessageHistory(use_openai=self.use_openai, system_prompt=system_prompt)
        # 生成 8 位十六进制会话 ID，用于磁盘文件名和会话恢复
        self.session_id = uuid.uuid4().hex[:8]
        # 记录会话启动时间（UTC），用于 --resume 时按时间排序
        self.session_start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._aborted = False  # Ctrl+C 中断标志位

        # 通过 BackendConfig 工厂方法创建客户端——一行搞定
        self._client = backend.create_client()

    def abort(self) -> None:
        """设置中断标志，供信号处理器调用以终止当前任务。"""
        self._aborted = True

    async def chat(self, user_message: str) -> None:
        """封装公共的对外对话方法，提供自动保存和异常隔离。"""
        self._aborted = False  # 每轮对话开始前重置中断标志
        try:
            await self._chat(user_message)
        finally:
            # finally 确保即使被 Ctrl+C 中断也能保存已生成的对话历史
            self._auto_save()

    def _auto_save(self) -> None:
        """自动将会话状态持久化到磁盘。"""
        try:
            # 组装会话文件内容：顶层包含 metadata 和消息历史
            save_session(self.session_id, {
                "metadata": {
                    "id": self.session_id,
                    "model": self.backend.model,
                    "cwd": str(Path.cwd()),
                    "startTime": self.session_start_time,
                    "messageCount": self.history.message_count(),
                },
                **self.history.to_dict(),  # 展开 anthropicMessages/openaiMessages
            })
        except Exception:
            # 保存失败不应影响用户体验，静默忽略
            pass

    def restore_session(self, data: dict) -> None:
        """从持久化的会话数据中恢复消息历史。"""
        # data 包含 anthropicMessages 或 openaiMessages，由 history.restore 统一处理
        self.history.restore(data)
        print(f"  [cyan]ℹ Session restored ({self.history.message_count()} messages).[/cyan]")

    def clear_history(self) -> None:
        """清空对话历史，保留系统提示词。"""
        self.history.clear(keep_system=True)
        print("  [cyan]ℹ Conversation history cleared.[/cyan]")
```

#### 注意什么

- `_auto_save` 必须包裹在 `try...except` 块中。会话保存属于非核心功能，绝不能因为磁盘写保护、空间不足等外部 IO 问题导致整个 Agent 执行崩溃。

---

### 步骤 3：创建终端 UI 辅助库 `ui.py`

#### 为什么做

接下来的 `__main__.py` 需要打印欢迎横幅、输入提示符、错误信息等格式化输出。我们将这些重复的终端打印逻辑抽取到独立的 `ui.py` 中，保持 `__main__.py` 的整洁。我们使用 `rich` 库来实现彩色终端输出，它提供了比原生 `print()` 更强大的格式化能力。

#### 做什么

首先安装依赖：

```bash
pip install rich
```

然后创建 `ui.py`，使用 `rich` 库定义基础输出函数和 Spinner 动画：

```python
# ui.py — 终端 UI 辅助函数

import sys
import threading
import time

from rich.console import Console

console = Console(highlight=False)


# ─── Basic output ──────────────────────────────────────────


def print_welcome() -> None:
    """打印欢迎横幅和使用提示。"""
    console.print("\n  [bold cyan]Mini Claude Code[/bold cyan][dim] — A minimal coding agent[/dim]\n")
    console.print("[dim]  Type your request, or 'exit' to quit.[/dim]")
    console.print("[dim]  Commands: /clear /cost[/dim]\n")


def print_user_prompt() -> None:
    """打印用户输入提示符（不换行，等待用户输入）。"""
    console.print("\n[bold green]> [/bold green]", end="")


def print_error(msg: str) -> None:
    """打印错误信息（红色）。"""
    console.print(f"\n  [red]Error: {msg}[/red]")


def print_info(msg: str) -> None:
    """打印提示信息（青色，用于状态通知）。"""
    console.print(f"\n  [cyan]ℹ {msg}[/cyan]")


# ─── Spinner ──────────────────────────────────────────────

# Spinner 动画帧序列（Braille 字符）
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_spinner_thread: threading.Thread | None = None
_spinner_stop = threading.Event()


def start_spinner(label: str = "Thinking") -> None:
    """启动 Spinner 动画，在后台线程中运行。"""
    global _spinner_thread
    if _spinner_thread is not None:
        return
    _spinner_stop.clear()

    def _run() -> None:
        frame = 0
        sys.stdout.write(f"\n  {SPINNER_FRAMES[0]} {label}...")
        sys.stdout.flush()
        while not _spinner_stop.is_set():
            time.sleep(0.08)
            frame = (frame + 1) % len(SPINNER_FRAMES)
            sys.stdout.write(f"\r  {SPINNER_FRAMES[frame]} {label}...")
            sys.stdout.flush()

    _spinner_thread = threading.Thread(target=_run, daemon=True)
    _spinner_thread.start()


def stop_spinner() -> None:
    """停止 Spinner 动画并清除该行。"""
    global _spinner_thread
    if _spinner_thread is None:
        return
    _spinner_stop.set()
    _spinner_thread.join(timeout=1)
    _spinner_thread = None
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()
```

#### 注意什么

- `Console(highlight=False)` 禁用自动高亮，避免将数字、URL 等自动渲染为特殊样式。
- `rich` 的标记语法 `[red]`、`[cyan]`、`[bold]`、`[dim]` 等会真正渲染颜色，比原生 `print()` 体验更好。
- Spinner 使用 Braille 字符（⠋⠙⠹...）实现动画效果，在后台线程中运行，不会阻塞主循环。
- `stop_spinner()` 使用 `\r\033[K` 清除当前行，确保 Spinner 消失后终端干净。
- 第 7 课实现流式输出时，会在首个文本到达时调用 `stop_spinner()` 停止动画。

---

### 步骤 4：编写参数解析与会话恢复逻辑

#### 为什么做

我们需要在命令行接收各种执行选项（例如 `--resume`、`--model`），并获取环境变量中的 API Key。同时要实现：若传入了具体的提示词，则直接运行单次任务并退出；若无参数，则启动 REPL 交互环境。

#### 做什么

重写 `__main__.py` 中的 `parse_args()`、`main()` 等函数，接入参数解析与会话恢复流程：

```python
# __main__.py — 命令行入口与 REPL 循环

import os
import sys
import signal
import asyncio
import argparse
from agent import Agent, BackendConfig
from session import load_session, get_latest_session_id
from ui import print_welcome, print_user_prompt, print_error, print_info


def parse_args() -> argparse.Namespace:
    """解析命令行参数，返回命名空间对象。"""
    parser = argparse.ArgumentParser(
        prog="mini-claude",
        description="Mini Claude Code — a minimal coding agent",
        add_help=False,  # 手动处理 --help 以自定义格式
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--api-base", default=None,
                        help="OpenAI-compatible API base URL")
    parser.add_argument("--resume", action="store_true",
                        help="Resume last session")
    parser.add_argument("--help", "-h", action="store_true",
                        help="Show help")
    return parser.parse_args()


def main() -> None:
    """程序主入口：解析参数、初始化 Agent、决定单次/交互模式。"""
    args = parse_args()

    if args.help:
        print("""
Usage: mini-claude [options] [prompt]

Options:
  --model, -m         Model to use (default: claude-opus-4-6, or MODEL_NAME env)
  --api-base URL      Use OpenAI-compatible API endpoint (key via env var)
  --resume            Resume the last session
  --help, -h          Show this help

REPL commands:
  /clear              Clear conversation history
  /cost               Show token usage and cost
  /compact            Manually compact conversation

Examples:
  mini-claude "fix the bug in src/app.ts"
  mini-claude --model gpt-4o "hello"
  OPENAI_API_KEY=sk-xxx mini-claude --api-base https://aihubmix.com/v1 --model gpt-4o "hello"
  mini-claude --resume
  mini-claude  # starts interactive REPL
""")
        sys.exit(0)

    # 模型选择优先级：命令行参数 > 环境变量 > 默认值
    model = args.model or os.environ.get("MODEL_NAME", "claude-opus-4-6")
    api_base = args.api_base

    # ── API 密钥与端点解析 ────────────────────────────────────
    try:
        backend = BackendConfig.from_env(model=model, api_base_override=api_base)
    except ValueError as e:
        print_error(str(e))
        sys.exit(1)

    agent = Agent(backend=backend)

    # --resume 模式：恢复最近一次会话的历史消息
    if args.resume:
        session_id = get_latest_session_id()
        if session_id:
            session = load_session(session_id)
            if session:
                # 只恢复消息历史部分，元数据由当前 Agent 重新生成
                agent.restore_session({
                    "anthropicMessages": session.get("anthropicMessages"),
                    "openaiMessages": session.get("openaiMessages"),
                })
            else:
                print_info("No session found to resume.")
        else:
            print_info("No previous sessions found.")

    prompt = " ".join(args.prompt) if args.prompt else None

    if prompt:
        # 单次执行模式：传入 prompt 后执行一轮对话即退出
        try:
            asyncio.run(agent.chat(prompt))
        except Exception as e:
            print_error(str(e))
            sys.exit(1)
    else:
        # 交互式 REPL 模式
        try:
            asyncio.run(run_repl(agent))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
```

---

### 步骤 5：构建交互式 REPL 循环

#### 为什么做

交互模式是 Agent 最常见的使用方式。用户输入一条消息，Agent 回复，循环往复，直到用户输入 `exit` 或 `quit` 退出。REPL（Read-Eval-Print Loop）就是这个"读取-执行-打印-循环"的过程。

#### 做什么

在 `__main__.py` 中实现 REPL 循环函数 `run_repl`：

```python
# __main__.py（续）


async def run_repl(agent: Agent) -> None:
    """交互式 REPL 循环：读取用户输入、分发命令。"""
    print_welcome()

    while True:
        print_user_prompt()
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!\n")
            break

        inp = line.strip()

        if not inp:
            continue
        if inp in ("exit", "quit"):
            print("\nBye!\n")
            break

        # ── REPL 内置命令分发 ──
        if inp == "/clear":
            agent.clear_history()
            continue

        # ── 普通对话：将用户输入发送给 Agent ──
        try:
            await agent.chat(inp)
        except Exception as e:
            print_error(str(e))
```

#### 注意什么

- `print_welcome()` 在 REPL 启动时打印欢迎横幅。
- `EOFError` 和 `KeyboardInterrupt` 的双重捕获保证了在任意系统下都能优雅退出。
- 更多 REPL 命令（`/cost`、`/compact`、`/plan` 等）将在后续课程中逐步添加。

---

## ⚖️ 设计权衡

### 覆盖式写入 JSON vs 追加式写入 JSONL

- **方案 A**：**覆盖式写入 JSON**（我们所用）
  - 每次调用后将完整的会话消息结构序列化为单个 JSON 文件。
  - **优点**：结构简单，读取解析极度方便，原生支持复杂的元数据嵌套。
  - **缺点**：如果对话很长，每次写入的文件较大。
- **方案 B**：**追加式写入 JSONL**
  - 每进行一轮对话，直接在历史文件末尾 append 写入一行新的 JSON。
  - **优点**：写入性能始终为 O(1)，防中途崩溃损坏性极佳。
  - **缺点**：元数据结构维护复杂，需要额外的解析处理才能恢复消息上下文。

**结论**：由于 Mini Agent 会在后续课时中实现上下文压缩，历史消息总数会受到合理限制，JSON 单文件写入的开销微乎其微，因此方案 A 具有更高的代码可维护性。

---

## ⚠️ 常见陷阱

### 1. `input()` 阻塞导致信号无法被即时处理

在执行 `line = input()` 期间，操作系统处理 `Ctrl+C` 会直接在主线程抛出 `KeyboardInterrupt` 异常，而不会顺利进入 `handle_sigint` 回调。

**后果**：用户在空闲输入时按一下 `Ctrl+C` 程序便会直接闪退，体验很差。
**修正**：必须在外层使用 `try...except (EOFError, KeyboardInterrupt):` 捕获此异常，使其安全输出 `Bye!` 并退出，而非让程序抛出冗长的报错堆栈。

---

### 2. 重置 `_aborted` 状态时机不对

如果 `_aborted` 标记在 `chat()` 执行后没有被重置，一旦用户中断了一次任务，随后的所有任务都将因为 `_aborted = True` 而被自动跳过。

**修正**：在 `chat()` 入口函数的第一行，必须强制重置：`self._aborted = False`。

---

## ✅ 验收点

### 输入与执行

1. 直接运行启动 REPL：

   ```bash
   python __main__.py
   ```

2. 输入 `hello` 并按下回车，等待 Agent 回复。
3. 输入命令 `/clear`，验证历史是否清空。
4. 输入 `exit` 退出。
5. 运行恢复会话命令：

   ```bash
   python __main__.py --resume
   ```

### 预期结果

- 运行 `exit` 退出时显示 `Bye!`。
- 使用 `--resume` 恢复后，终端会打印出类似 `Session restored (X messages)` 的提示词。

---

## 🧠 思考题

1. **为什么在 `chat` 接口的 `finally` 块中调用 `_auto_save`，而不是在 `try` 块的最后一行调用？**
   *(提示：如果用户使用 `Ctrl+C` 强行终止了正在运行的 Agent，代码会抛出异常中断执行，如果放在 try 块末尾，中断前的对话历史就无法被保存。放进 finally 块可确保即便被中断也能保存已生成的内容。)*

2. **为什么 `run_repl` 中要同时捕获 `EOFError` 和 `KeyboardInterrupt` 两种异常？只捕获其中一个会有什么问题？**
   *(提示：`EOFError` 在用户按下 `Ctrl+D`（Unix/macOS）或管道输入结束时抛出；`KeyboardInterrupt` 在用户按下 `Ctrl+C` 时抛出。不同操作系统和终端环境下，用户退出 REPL 的习惯不同，双重捕获确保跨平台兼容性。)*

3. **为什么 `_auto_save` 必须用 `try...except` 包裹并静默忽略异常？如果保存失败直接抛出异常会怎样？**
   *(提示：会话保存属于"锦上添花"的非核心功能，而磁盘写入可能因权限不足、空间满等外部原因失败。如果让异常冒泡，用户的一次正常对话会因为保存失败而报错退出，体验极差。这种"核心功能优先，辅助功能降级"是防御性编程的典型设计。)*

---

## 📦 本节收获

1. **会话持久化**：掌握了利用序列化文件恢复 Agent 工作上下文的设计方法。
2. **信号流拦截**：掌握了利用系统 SIGINT 信号分流“任务中止”与“程序退出”的交互技巧。
3. **REPL 健壮性**：体验了防御性异常拦截（IO 失败降级、阻塞异常捕获），使 CLI 工具具备了生产级的高可用交互体验。

---

> **下一章**：现在我们有了一个易用的终端交互界面。下一章我们将攻克流式输出——让 Agent 的思考过程与工具调用过程流式呈现在终端上。
