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
  Commands: /clear /plan /cost

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

修改 `agent.py`。
1. 在初始化中定义唯一的 `session_id` 和 `session_start_time`。
2. 封装公共 `chat` 方法，并在其中添加 `_auto_save()` 和 `restore_session()` 逻辑。

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
                    "model": self.config.model,
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

### 步骤 3：创建最简终端 UI 辅助库 `ui.py`

#### 为什么做

接下来的 `__main__.py` 需要打印欢迎横幅、输入提示符、错误信息等格式化输出。我们将这些重复的终端打印逻辑抽取到独立的 `ui.py` 中，保持 `__main__.py` 的整洁。后续章节会逐步丰富这个模块的渲染能力（如颜色、Spinner 动画），但本课只需要最基础的 `print` 封装。

#### 做什么

创建 `ui.py`，定义四个基础输出函数：

```python
# ui.py — 终端 UI 辅助函数

def print_welcome() -> None:
    """打印欢迎横幅和使用提示。"""
    print("  Mini Claude Code — A minimal coding agent\n")
    print("  Type your request, or 'exit' to quit.")
    print("  Commands: /clear /plan /cost\n")


def print_user_prompt() -> None:
    """打印用户输入提示符（不换行，等待用户输入）。"""
    print("\n> ", end="")


def print_error(msg: str) -> None:
    """打印错误信息（红色标签，后续引入 rich 库后会渲染颜色）。"""
    print(f"  [red]Error: {msg}[/red]")


def print_info(msg: str) -> None:
    """打印提示信息（青色标签，用于状态通知）。"""
    print(f"  [cyan]ℹ {msg}[/cyan]")
```

#### 注意什么

- 此刻的 `print_info` 和 `print_error` 只是简单的字符串打印，`[red]`/`[cyan]` 标签不会渲染颜色（需要后续引入 `rich` 库）。这不影响功能正确性，先让代码跑起来。

---

### 步骤 4：编写参数解析与会话恢复逻辑

#### 为什么做

我们需要在命令行接收各种执行选项（例如 `--resume`、`--model`），并获取环境变量中的 API Key。同时要实现：若传入了具体的提示词，则直接运行单次任务并退出；若无参数，则启动 REPL 交互环境。

#### 做什么

重写 `__main__.py` 中的 `parse_args()`、`main()` 等函数，接入参数解析与会话恢复流程。注意此处的参数列表比初始版本更完整——新增了 `--yolo`、`--plan`、`--accept-edits`、`--dont-ask`、`--thinking`、`--max-cost`、`--max-turns` 等权限和预算控制参数，并通过 `_resolve_permission_mode()` 将它们映射为 Agent 内部的权限模式字符串：

```python
# __main__.py — 命令行入口与 REPL 循环

import os
import sys
import signal
import asyncio
import argparse
from agent import Agent, BackendConfig
from tools import PermissionMode
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
    parser.add_argument("--yolo", "-y", action="store_true",
                        help="Skip all confirmation prompts")
    parser.add_argument("--plan", action="store_true",
                        help="Plan mode: read-only")
    parser.add_argument("--accept-edits", action="store_true",
                        help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true",
                        help="Auto-deny confirmations (for CI)")
    parser.add_argument("--thinking", action="store_true",
                        help="Enable extended thinking")
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--api-base", default=None,
                        help="OpenAI-compatible API base URL")
    parser.add_argument("--resume", action="store_true",
                        help="Resume last session")
    parser.add_argument("--max-cost", type=float, default=None,
                        help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None,
                        help="Max agentic turns")
    parser.add_argument("--help", "-h", action="store_true",
                        help="Show help")
    return parser.parse_args()


def _resolve_permission_mode(args: argparse.Namespace) -> PermissionMode:
    """将命令行权限参数映射为 Agent 内部权限模式字符串。"""
    if args.yolo:
        return "bypassPermissions"
    if args.plan:
        return "plan"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    return "default"


def main() -> None:
    """程序主入口：解析参数、初始化 Agent、决定单次/交互模式。"""
    args = parse_args()

    if args.help:
        print("""
Usage: mini-claude [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts (bypassPermissions mode)
  --plan              Plan mode: read-only, describe changes without executing
  --accept-edits      Auto-approve file edits, still confirm dangerous shell
  --dont-ask          Auto-deny anything needing confirmation (for CI)
  --thinking          Enable extended thinking (Anthropic only)
  --model, -m         Model to use (default: claude-opus-4-6, or MODEL_NAME env)
  --api-base URL      Use OpenAI-compatible API endpoint (key via env var)
  --resume            Resume the last session
  --max-cost USD      Stop when estimated cost exceeds this amount
  --max-turns N       Stop after N agentic turns
  --help, -h          Show this help

REPL commands:
  /clear              Clear conversation history
  /plan               Toggle plan mode (read-only <-> normal)
  /cost               Show token usage and cost
  /compact            Manually compact conversation
  /memory             List saved memories
  /skills             List available skills
  /<skill-name>       Invoke a skill (e.g. /commit "fix types")

Examples:
  mini-claude "fix the bug in src/app.ts"
  mini-claude --yolo "run all tests and fix failures"
  mini-claude --plan "how would you refactor this?"
  mini-claude --max-cost 0.50 --max-turns 20 "implement feature X"
  OPENAI_API_KEY=sk-xxx mini-claude --api-base https://aihubmix.com/v1 --model gpt-4o "hello"
  mini-claude --resume
  mini-claude  # starts interactive REPL
""")
        sys.exit(0)

    permission_mode = _resolve_permission_mode(args)
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
        asyncio.run(run_repl(agent))


if __name__ == "__main__":
    main()
```

---

### 步骤 5：构建交互式 REPL 循环与信号拦截

#### 为什么做

在交互模式下，如果大模型输出失控或执行了错误的工具，用户按下 `Ctrl+C` 的首要期望是**终止本次 Agent 执行并退回输入提示符**，而不是直接杀掉整个进程导致历史记录全部丢失。只有在没有任务执行且处于输入状态下连续按下 `Ctrl+C` 时，才应该退出程序。

#### 做什么

在 `__main__.py` 中实现 REPL 循环函数 `run_repl`。注意：`/plan` 和 `/cost` 命令直接调用 Agent 的真实方法（`toggle_plan_mode()`、`show_cost()`），而非打印占位符信息：

```python
# __main__.py（续）


async def run_repl(agent: Agent) -> None:
    """交互式 REPL 循环：读取用户输入、分发命令、处理信号。"""

    async def confirm_fn(message: str) -> bool:
        """确认回调：Agent 在需要用户授权时暂停执行并等待终端输入 y/n。"""
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    agent.set_confirm_fn(confirm_fn)

    # ── SIGINT 信号处理器：区分"中断任务"与"退出程序" ──
    sigint_count = 0  # 用于检测连续两次 Ctrl+C

    def handle_sigint(sig, frame):
        """SIGINT 信号处理器：区分"中断任务"与"退出程序"。"""
        nonlocal sigint_count
        if agent._aborted is False and agent.is_processing:
            # Agent 正在执行任务，中断当前任务但不退出程序
            agent.abort()
            print("\n  (interrupted)")
            sigint_count = 0
            print_user_prompt()
        else:
            # Agent 空闲，连续两次 Ctrl+C 才退出程序（防止误触）
            sigint_count += 1
            if sigint_count >= 2:
                print("\nBye!\n")
                sys.exit(0)
            print("\n  Press Ctrl+C again to exit.")
            print_user_prompt()

    # 注册 SIGINT 信号处理器（仅主线程有效）
    signal.signal(signal.SIGINT, handle_sigint)
    print_welcome()

    while True:
        print_user_prompt()
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            # input() 阻塞期间的 Ctrl+C 会直接抛出 KeyboardInterrupt
            print("\nBye!\n")
            break

        inp = line.strip()
        sigint_count = 0  # 有新输入时重置连续中断计数

        if not inp:
            continue
        if inp in ("exit", "quit"):
            print("\nBye!\n")
            break

        # ── REPL 内置命令分发 ──
        if inp == "/clear":
            agent.clear_history()
            continue
        if inp == "/plan":
            agent.toggle_plan_mode()  # 在 plan mode 和 normal mode 间切换
            continue
        if inp == "/cost":
            agent.show_cost()  # 打印当前会话的 token 用量和预估费用
            continue
        if inp == "/compact":
            try:
                await agent.compact()  # 手动触发上下文压缩
            except Exception as e:
                print_error(str(e))
            continue
        if inp == "/memory":
            from memory import list_memories
            memories = list_memories()
            if not memories:
                print_info("No memories saved yet.")
            else:
                print_info(f"{len(memories)} memories:")
                for m in memories:
                    print(f"    [{m.type}] {m.name} — {m.description}")
            continue
        if inp == "/skills":
            from skills import discover_skills
            skills = discover_skills()
            if not skills:
                print_info("No skills found. Add skills to .claude/skills/<name>/SKILL.md")
            else:
                print_info(f"{len(skills)} skills:")
                for s in skills:
                    tag = f"/{s.name}" if s.user_invocable else s.name
                    print(f"    {tag} ({s.source}) — {s.description}")
            continue

        # ── Skill 调用：/<skill-name> [args] ──
        if inp.startswith("/"):
            from skills import get_skill_by_name, resolve_skill_prompt, execute_skill
            # 解析 skill 名称和参数（以第一个空格分隔）
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
            skill = get_skill_by_name(cmd_name)
            if skill and skill.user_invocable:
                print_info(f"Invoking skill: {skill.name}")
                try:
                    if skill.context == "fork":
                        # fork 类型的 skill：先执行 skill 脚本，再将结果注入对话
                        result = execute_skill(skill.name, cmd_args)
                        if result:
                            await agent.chat(
                                f'Use the skill tool to invoke "{skill.name}" '
                                f'with args: {cmd_args or "(none)"}'
                            )
                    else:
                        # 普通 skill：解析 prompt 模板后直接作为用户消息发送
                        resolved = resolve_skill_prompt(skill, cmd_args)
                        await agent.chat(resolved)
                except Exception as e:
                    if "abort" not in str(e).lower():
                        print_error(str(e))
                continue

        # ── 普通对话：将用户输入发送给 Agent ──
        try:
            await agent.chat(inp)
        except Exception as e:
            # 过滤掉 abort 异常（用户主动中断时不显示错误）
            if "abort" not in str(e).lower():
                print_error(str(e))
```

#### 注意什么

- `/plan` 命令直接调用 `agent.toggle_plan_mode()`，这会在 Agent 内部切换权限模式并更新系统提示词。第一次调用进入 plan mode，第二次调用恢复正常模式。`/cost` 命令调用 `agent.show_cost()`，它会计算并打印当前会话的 token 用量和预估费用。
- `confirm_fn` 回调在 REPL 启动时注入给 Agent，使得 Agent 在需要用户确认（如执行危险 shell 命令）时，能暂停执行并等待用户在终端中输入 `y/n`。
- `signal.signal` 只在主线程有效。因为 `input()` 是阻塞性 IO，而在 Windows 和 Unix 下 Python 对 `input()` 中断的处理稍有不同，通过捕获 `KeyboardInterrupt` 异常和信号拦截双重机制可以保证在任意系统下都能优雅退回命令行提示符。

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
2. **在 `run_repl` 中，如果大模型返回了大量的工具调用，我们正在并发执行，此时按 `Ctrl+C` 会如何影响这些正在执行的子进程？**
   *(提示：我们设置了 `agent.abort()` 会将中断标志置为 True。工具分发器在每次执行前或执行中可以检测该标志位，主动中止后续工具的调用。)*

---

## 📦 本节收获

1. **会话持久化**：掌握了利用序列化文件恢复 Agent 工作上下文的设计方法。
2. **信号流拦截**：掌握了利用系统 SIGINT 信号分流“任务中止”与“程序退出”的交互技巧。
3. **REPL 健壮性**：体验了防御性异常拦截（IO 失败降级、阻塞异常捕获），使 CLI 工具具备了生产级的高可用交互体验。

---

> **下一章**：现在我们有了一个易用的终端交互界面。下一章我们将攻克流式输出——让 Agent 的思考过程与工具调用过程流式呈现在终端上。
