import sys
import asyncio

try:
    from .agent import Agent, AgentConfig
except ImportError:
    from agent import Agent, AgentConfig


async def main():
    """程序入口：从命令行参数读取查询并启动 Agent"""
    # 读取命令行参数作为用户查询，默认为列出 .py 文件
    query = sys.argv[1] if len(sys.argv) > 1 else "列出当前目录下所有 .py 文件"
    agent = Agent(config=AgentConfig())
    await agent._chat(query)  # 暂用 _chat，后续章节会扩展为完整的 chat 方法


if __name__ == "__main__":
    asyncio.run(main())