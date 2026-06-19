from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, cast

from anthropic.types import MessageParam, ToolUseBlockParam, TextBlockParam, ToolResultBlockParam
from anthropic.types.tool_param import ToolParam
import openai
import anthropic

from tools import execute_tool, get_tool_definitions
from prompt import build_system_prompt

from dotenv import load_dotenv

#加载环境变量
load_dotenv()

@dataclass
class BackendConfig:
    """后端配置——封装后端选择逻辑，提供工厂方法"""
    provider: Literal["anthropic", "openai"]
    api_key: str
    base_url: str | None = None
    model: str = "claude-sonnet-4-6"

    @classmethod
    def from_env(cls, model: str | None = None, api_base_override: str | None = None) -> "BackendConfig":
        """从环境变量自动检测后端类型并返回配置实例"""
        if api_base_override:
            api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError("--api-base 需要设置 OPENAI_API_KEY 或 ANTHROPIC_API_KEY")
            return cls(provider="openai", api_key=api_key, base_url=api_base_override, model=model or "gpt-4o")
        if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL"):
            return cls(provider="openai", api_key=os.environ["OPENAI_API_KEY"],
                       base_url=os.environ["OPENAI_BASE_URL"], model=model or "gpt-4o")
        if os.environ.get("ANTHROPIC_API_KEY"):
            return cls(provider="anthropic", api_key=os.environ["ANTHROPIC_API_KEY"],
                       base_url=os.environ.get("ANTHROPIC_BASE_URL"), model=model or "claude-sonnet-4-6")
        if os.environ.get("OPENAI_API_KEY"):
            return cls(provider="openai", api_key=os.environ["OPENAI_API_KEY"],
                       base_url=os.environ.get("OPENAI_BASE_URL"), model=model or "gpt-4o")
        raise ValueError("未找到 API Key。请设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY + OPENAI_BASE_URL")

    def create_client(self) -> Any:
        """工厂方法：根据 provider 创建对应的异步 SDK 客户端"""
        if self.provider == "openai":
            return openai.AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return anthropic.AsyncAnthropic(**kwargs)

    @property
    def is_openai(self) -> bool:
        return self.provider == "openai"



@dataclass
class AgentConfig:
    """Agent 行为配置（不含后端信息）"""
    pass

@dataclass
class AgentState:
    """Agent 的运行时状态"""
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

    def update_system_prompt(self, system_prompt: str) -> None:
        """更新系统提示词"""
        self.system_prompt = system_prompt
        if self.use_openai and self._openai_messages:
            self._openai_messages[0]["content"] = system_prompt

    def append_tool_results(self, results: list[dict]) -> None:
        """添加工具执行结果。两种协议的消息格式不同"""
        if self.use_openai:
            # OpenAI：每个 tool 结果单独一条 role: "tool" 消息
            for r in results:
                self.messages.append(r)
        else:
            # Anthropic：所有 tool 结果合并为一条 role: "user" 消息，content 是 tool_result 块数组
            self.messages.append({"role": "user", "content": results})
            
def _to_openai_tools(tools: list[dict]) -> list[dict]:
    """将 Anthropic 格式的工具定义转换为 OpenAI 格式

    Anthropic: {"name": ..., "description": ..., "input_schema": ...}
    OpenAI:    {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],  # 底层 JSON Schema 一致，只是外层包装不同
            },
        }
        for t in tools
    ]


class Agent:
    def __init__(self, backend: BackendConfig):
        self.backend = backend
        self.config = AgentConfig()
        self.state = AgentState()
        self.use_openai = backend.is_openai

        system_prompt = "You are a helpful coding assistant with access to tools."
        self.history = MessageHistory(use_openai=self.use_openai, system_prompt=system_prompt)
       
        # 通过 BackendConfig 工厂方法创建客户端——一行搞定
        self._client = backend.create_client()

    async def _chat(self, user_message: str) -> None:
        """统一入口：根据后端配置自动分发到对应的聊天循环"""
        if self.use_openai:
            await self._chat_openai(user_message)
        else:
            await self._chat_anthropic(user_message)

    async def _chat_anthropic(self, user_message: str) -> None:
        """Anthropic 后端的 Agent Loop——与第 1 课逻辑一致，使用 history 抽象层"""

        # 1. 把用户消息推入历史
        self.history.append_user_message(user_message)

        while True:
            # 动态编译最新的系统提示词
            current_system_prompt = build_system_prompt()
            # 2. 调用 LLM 获取响应
            response = await self._client.messages.create(
                model=self.backend.model,
                max_tokens=4096,
                system=current_system_prompt,
                tools=get_tool_definitions(),
                messages=self.history.anthropic_messages,
            )

            # 3. 把 SDK 对象转为 dict 后存入历史
            self.history.append_assistant_message(
                [self._block_to_dict(b) for b in response.content]
            )
            
            # 4. 检查是否有 tool_use——这是循环终止的唯一判断条件
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break  # 没有工具调用 → 任务完成，退出循环

            # 5. 执行工具，把结果推入历史（Anthropic 协议：用 user 角色 + tool_result 块）
            tool_results = []
            for tu in tool_uses:
                result = await execute_tool(tu.name, dict(tu.input))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,  # 关联回对应的 tool_use 调用
                    "content": result,
                })
            # 工具结果用 role: "user" 推入——这是 Anthropic API 的协议要求
            self.history.append_tool_results(tool_results)

        # 输出最终回复
        for block in response.content:
            if block.type == "text":
                print(block.text)
                    
    @staticmethod
    def _block_to_dict(block) -> dict:
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
        return {"type": block.type}
    
    async def _chat_openai(self, user_message: str) -> None:
        """OpenAI 兼容后端的 Agent Loop"""
        # 1. 用户消息推入历史
        self.history.append_user_message(user_message)

        while True:
            # 动态编译最新的系统提示词
            current_system_prompt = build_system_prompt()
            self.history.update_system_prompt(current_system_prompt)
            # 2. 调用 OpenAI 兼容 API
            response = await self._client.chat.completions.create(
                model=self.backend.model,
                messages=self.history.openai_messages,
                tools=_to_openai_tools(get_tool_definitions()),
            )
            message = response.choices[0].message

            # 3. 构造 assistant 回复——必须保留 tool_calls 结构，否则后续 tool 消息会报协议错误
            msg_dict = {"role": "assistant", "content": message.content}
            if message.tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ]
            self.history.append_assistant_message(msg_dict)

            # 4. 检查是否有工具调用——循环终止条件与 Anthropic 后端一致
            if not message.tool_calls:
                break

            # 5. 执行工具并将结果（role: "tool"）推入历史
            import json
            tool_results = []
            for tc in message.tool_calls:
                # arguments 是 JSON 字符串，需要解析为 dict
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = {}  # 解析失败时用空 dict，让工具自行处理缺失参数
                
                result = await execute_tool(tc.function.name, args)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,  # 必须与 assistant 消息中的 tool_calls.id 对应
                    "name": tc.function.name,
                    "content": result,
                })
            self.history.messages.extend(tool_results)  # 直接 extend，因为 OpenAI 模式下工具结果也是 role: "tool" 消息

        # 输出最终回复
        if message.content:
            print(message.content)
        