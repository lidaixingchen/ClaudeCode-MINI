import sys
import threading
import time

from rich.console import Console

console = Console(highlight=False)

def print_welcome() -> None:
    """打印欢迎横幅和使用提示。"""
    console.print("\n  [bold cyan]Mini Claude Code[/bold cyan][dim] — A minimal coding agent[/dim]\n")
    console.print("[dim]  Type your request, or 'exit' to quit.[/dim]")
    console.print("[dim]  Commands: /clear /plan /cost[/dim]\n")

def print_user_prompt() -> None:
    """打印用户输入提示符（不换行，等待用户输入）。"""
    console.print("\n[bold green]> [/bold green]", end="")

def print_error(msg: str) -> None:
    """打印错误信息（红色）。"""
    console.print(f"\n  [red]Error: {msg}[/red]")

def print_info(msg: str) -> None:
    """打印提示信息（青色，用于状态通知）。"""
    console.print(f"\n  [cyan]ℹ {msg}[/cyan]")

def print_tool_call(name: str, args: dict) -> None:
    """打印工具调用信息（黄色，显示工具名和参数摘要）。"""
    # 只显示前 3 个参数，避免输出过长
    args_preview = {k: v for i, (k, v) in enumerate(args.items()) if i < 3}
    args_str = ", ".join(f"{k}={repr(v)[:50]}" for k, v in args_preview.items())
    if len(args) > 3:
        args_str += ", ..."
    console.print(f"\n  [yellow]🔧 {name}[/yellow] [dim]({args_str})[/dim]")


def print_tool_result(name: str, result: str) -> None:
    """打印工具执行结果（灰色，截断过长内容）。"""
    # 截断显示，避免刷屏
    preview = result[:200] + "..." if len(result) > 200 else result
    console.print(f"  [dim]{preview}[/dim]")


def print_assistant_text(text: str) -> None:
    """打印助手回复文本（普通颜色，支持流式追加）。"""
    console.print(text, end="", markup=False)


def print_retry(attempt: int, max_retries: int, reason: str) -> None:
    """打印重试信息（黄色，显示重试次数和原因）。"""
    console.print(f"\n  [yellow]↻ Retry {attempt}/{max_retries}: {reason}[/yellow]")


def print_sub_agent_start(agent_type: str, description: str) -> None:
    """打印子代理启动信息（紫色边框）。"""
    console.print(f"\n  [magenta]┌─ Sub-agent [{agent_type}]: {description}[/magenta]")


def print_sub_agent_end(agent_type: str, description: str) -> None:
    """打印子代理结束信息（紫色边框）。"""
    console.print(f"  [magenta]└─ Sub-agent [{agent_type}] completed[/magenta]")

# Spinner 动画帧序列（Braille 字符）
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_spinner_thread: threading.Thread | None = None
_spinner_stop = threading.Event()


def start_spinner(label: str = "Thinking") -> None:
    """启动 Spinner 动画，在后台线程中运行。"""
    global _spinner_thread, _spinner_stop
    if _spinner_thread and _spinner_thread.is_alive():
        return  # Spinner 已在运行

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