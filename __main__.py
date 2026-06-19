import os
import sys
import asyncio
from dotenv import load_dotenv

from agent import Agent, BackendConfig

# 加载环境变量
load_dotenv()

async def main():
    """程序入口：读取环境变量，自动选择后端并启动 Agent"""
    model = os.environ.get("MODEL_NAME") or "claude-sonnet-4-6"
    backend = BackendConfig.from_env(model=model)
    agent = Agent(backend=backend)

    print("Mini Claude Code 已启动！输入 'exit' 退出。\n")

    while True:
        try:
            user_input = input("> ")
            if user_input.strip().lower() in ("exit", "quit"):
                print("再见！")
                break
            if not user_input.strip():
                continue
            await agent._chat(user_input)
        except KeyboardInterrupt:
            print("\n再见！")
            break


if __name__ == "__main__":
    asyncio.run(main())