from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast
import anthropic
from anthropic.types import MessageParam, ToolUseBlockParam, TextBlockParam, ToolResultBlockParam
from anthropic.types.tool_param import ToolParam
from openai import OpenAI
try:
    from .tools import execute_tool, get_tool_definitions
except ImportError:
    from tools import execute_tool, get_tool_definitions


from dotenv import load_dotenv

#加载环境变量
load_dotenv()

@dataclass
class AgentConfig:
    model: str = os.getenv("OPENAI_MODEL", "deepseek-v4-flash")
    api_base: str | None = None  # 自定义 API 地址，非空时启用 OpenAI 兼容模式
    api_key: str | None = None  # API 密钥，可从环境变量或显式传入
    temperature: float = 0.7
    max_tokens: int = 1000

@dataclass
class AgentState:
    pass

class MessageHistory:
    """统一 Anthropic/OpenAI 消息格式的抽象层"""
    def __init__(self, use_openai: bool, system_prompt: str):
        self.use_openai = use_openai
        self.system_prompt = system_prompt
        self._anthropic_messages: list[dict] = []
        self._openai_messages: list[dict] = []
        # OpenAI 协议要求 system prompt 作为消息列表的首条
        if use_openai:
            self._openai_messages.append({"role": "system", "content": system_prompt})

    @property
    def messages(self) -> list[dict]:
        """根据当前后端返回对应的消息列表"""
        return self._openai_messages if self.use_openai else self._anthropic_messages
    
    @property
    def anthropic_messages(self) -> list[dict]:
        return self._anthropic_messages

    @property
    def openai_messages(self) -> list[dict]:
        return self._openai_messages
    
    def append_user_message(self, content: str | list) -> None:
        """添加用户消息"""
        self.messages.append({"role": "user", "content": content})

    def append_assistant_message(self, content: Any) -> None:
        """添加助手回复。OpenAI 模式下保留 tool_calls 结构"""
        if self.use_openai and isinstance(content, dict) and "role" in content:
            self.messages.append(content)
        else:
            self.messages.append({"role": "assistant", "content": content})

    def append_tool_results(self, results: list[dict]) -> None:
        """添加工具执行结果。两种协议的消息格式不同"""
        if self.use_openai:
            # OpenAI：每个 tool 结果单独一条 role: "tool" 消息
            for r in results:
                self.messages.append(r)
        else:
            # Anthropic：所有 tool 结果合并为一条 role: "user" 消息，content 是 tool_result 块数组
            self.messages.append({"role": "user", "content": results})
            



class Agent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.state = AgentState()
        self._client = anthropic.AsyncAnthropic()
        self._messages: list[MessageParam] = []

    async def _chat(self, user_message: str) -> None:
        """Agent Loop 核心：循环调用 LLM 直到任务完成"""

        # 1. 把用户消息推入历史
        self._messages.append({"role": "user", "content": user_message})

        while True:
            # 2. 调用 LLM 获取响应
            response = await self._client.messages.create(
                model=self.config.model,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                system="You are a helpful coding assistant with access to tools.",
                tools=cast(list[ToolParam], get_tool_definitions()),  # 从 tools.py 导入
                messages=self._messages,
            )

            # 3. 把 LLM 的回复推入历史——必须在检查 tool_use 之前，否则上下文断裂
            self._messages.append({"role": "assistant", "content": [self._block_to_dict(b) for b in response.content],})

            # 4. 检查是否有 tool_use——这是循环终止的唯一判断条件
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break  # 没有工具调用 → 任务完成，退出循环

            # 5. 执行工具，把结果推入历史（Anthropic 协议：用 user 角色 + tool_result 块）
            tool_results: list[ToolResultBlockParam] = []
            for tu in tool_uses:
                result = await execute_tool(tu.name, dict(tu.input))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result,
                })
            # 工具结果用 role: "user" 推入——这是 Anthropic API 的协议要求
            self._messages.append({"role": "user", "content": tool_results})

        # 输出最终回复
        for block in response.content:
            if block.type == "text":
                print(block.text)
                    
    @staticmethod
    def _block_to_dict(block) -> TextBlockParam | ToolUseBlockParam:
        """将 Anthropic SDK 对象转为普通 dict，因为消息数组只能存 dict"""
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                # block.input 可能是 dict 或自定义对象，需要统一处理
                "input": dict(block.input) if hasattr(block.input, "items") else block.input,
            }
        # 不应到达此处，但安全兜底
        return {"type": "text", "text": str(block)}