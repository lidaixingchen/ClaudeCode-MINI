import os
import sys
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
    model = args.model or os.environ.get("MODEL_NAME") or "claude-opus-4-6"
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
            print_error(f"Error occurred: {e}")
    else:
        try:
            # 交互式 REPL 模式
            asyncio.run(run_repl(agent))
        except KeyboardInterrupt:
            pass


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
            print_error(f"Error occurred: {e}")

if __name__ == "__main__":
    main()