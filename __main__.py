import os
import sys
import asyncio
from dotenv import load_dotenv

try:
    from .agent import Agent, BackendConfig
except ImportError:
    from agent import Agent, BackendConfig

# 加载环境变量
load_dotenv()

async def main():
    """程序入口：读取环境变量，自动选择后端并启动 Agent"""
    query = sys.argv[1] if len(sys.argv) > 1 else "列出当前目录下所有 .py 文件"

    model = os.environ.get("MODEL_NAME") or "claude-sonnet-4-6"
    backend = BackendConfig.from_env(model=model)
    agent = Agent(backend=backend)
    await agent._chat(query)


if __name__ == "__main__":
    asyncio.run(main())