from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Literal, cast

from anthropic.types import MessageParam, ToolUseBlockParam, TextBlockParam, ToolResultBlockParam
from anthropic.types.tool_param import ToolParam
import openai
import anthropic

from tools import get_tool_definitions, execute_tool, CONCURRENCY_SAFE_TOOLS
from prompt import build_system_prompt

import uuid
import time
from pathlib import Path
from session import save_session  # 导入会话保存函数

from dotenv import load_dotenv

from ui import print_tool_call, print_tool_result

#加载环境变量
load_dotenv()

LARGE_RESULT_THRESHOLD = 30 * 1024      # 30 KB 阈值，超过则持久化到磁盘
LARGE_RESULT_PREVIEW_LINES = 200        # 预览保留的行数

# 已知支持推理/思考的第三方模型关键词（小写）
_THINKING_MODEL_KEYWORDS = ("deepseek", "qwq", "grok-3", "reasoning", "think")

def _model_supports_thinking(model: str) -> bool:
    """判断模型是否支持 Extended Thinking（思考链）功能。

    检测优先级：
    1. Claude 模型硬编码（已知型号自动识别）
    2. THINKING_MODE 环境变量显式覆盖（第三方模型推荐方式）
    3. 关键词启发式匹配（deepseek-r1、qwq 等）
    4. 默认返回 False（安全回退）
    """
    m = model.lower()
    if "claude" in m:
        # Claude 模型根据版本自动识别，4-6 版本及以上支持思考链
        return any(x in m for x in ("4-6", "4.6", "5", "5.0", "sonnet-4-6", "opus-4-6"))
    # 2. 环境变量覆盖：用户可以通过设置 THINKING_MODE=enabled 来启用第三方模型的思考链功能
    thinking_mode_env = os.environ.get("THINKING_MODE", "").lower()
    if thinking_mode_env == "enabled":
        return True
    if thinking_mode_env == "disabled":
        return False
    # 3. 关键词启发式匹配：如果模型名称包含已知支持思考的关键词，则启用思考链
    return any(keyword in m for keyword in _THINKING_MODEL_KEYWORDS)

def _model_supports_adaptive_thinking(model: str) -> bool:
    """判断模型是否支持自适应思考模式（可动态调整思考深度）。

    仅 Claude opus-4-6 / sonnet-4-6 原生支持。
    第三方模型需显式设置 THINKING_MODE=adaptive 才会启用。
    """
    m = model.lower()
    if "claude" in m:
        return any(x in m for x in ("opus-4-6", "sonnet-4-6"))
    thinking_mode_env = os.environ.get("THINKING_MODE", "").lower()
    return thinking_mode_env == "adaptive"


def _get_max_output_tokens(model: str) -> int:
    """根据模型版本返回最大输出 Token 数。

    Claude 模型使用硬编码值；第三方模型通过 MAX_OUTPUT_TOKENS 环境变量覆盖。
    """
    env_override = os.environ.get("MAX_OUTPUT_TOKENS")
    if env_override and env_override.isdigit():
        return int(env_override)

    m = model.lower()
    if "claude" in m:
        if any(x in m for x in ("4-6", "4.6", "sonnet-4-6", "opus-4-6")):
            return 16384
        if "5" in m or "5.0" in m:
            return 32768
    # 默认值，适用于未知模型或第三方模型
    return 8192

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
    thinking_mode: Literal["disabled", "adaptive", "enabled"] = "disabled"

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
        # 系统提示词的快捷引用
        self._system_prompt = self.history.system_prompt

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
        """自动保存当前会话到磁盘，供 --resume 恢复使用"""
        try:
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
        self.history.restore(data)
        print(f"  [cyan]ℹ Session restored ({self.history.message_count()} messages).[/cyan]")

    def clear_history(self) -> None:
        """清空对话历史，保留系统提示词。"""
        self.history.clear(keep_system=True)
        print("  [cyan]ℹ Conversation history cleared.[/cyan]")

    async def _chat(self, user_message: str) -> None:
        """统一入口：根据后端配置自动分发到对应的聊天循环"""
        if self.use_openai:
            await self._chat_openai(user_message)
        else:
            await self._chat_anthropic(user_message)

    async def _chat_anthropic(self, user_message: str) -> None:
        """Anthropic 后端的 Agent Loop——与第 1 课逻辑一致，使用 history 抽象层"""

        #  把用户消息推入历史
        self.history.append_user_message(user_message)

        while True:
            # 动态编译最新的系统提示词
            current_system_prompt = build_system_prompt()
            
            # 抢跑任务注册表：{ tool_use_id -> asyncio.Task }
            # 用于在流式结束后直接 await 已启动的后台任务
            early_executions: dict[str, asyncio.Task] = {}
            
            # 回调函数：当安全工具参数生成完毕时被调用
            def _on_tool_block_complete(block: dict):
                # 只有白名单中的只读工具才允许抢跑
                if block["name"] in CONCURRENCY_SAFE_TOOLS:
                    # TODO: 第 8 课会添加权限检查逻辑（check_permission）
                    # 目前只读工具直接允许执行
                    task = asyncio.create_task(self._execute_tool_call(block["name"], block["input"]))
                    early_executions[block["id"]] = task

            # 将回调传入流式 API 调用，每个工具块完成时都会触发
            response = await self._call_anthropic_stream(on_tool_block_complete=_on_tool_block_complete)

            # 将助手消息（含文本和工具调用）追加到历史记录
            self.history.append_assistant_message(
                [self._block_to_dict(b) for b in response.content]
            )

            # 提取所有工具调用块
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break  # 没有工具调用，循环终止

            tool_results: list[dict] = []
            for tu in tool_uses:
                inp = dict(tu.input) if hasattr(tu.input, "items") else tu.input

                # 1. 检查此工具是否已在后台抢跑执行
                early_task = early_executions.get(tu.id)
                if early_task:
                    # 抢跑任务静默运行，此时才渲染 UI 日志（避免与流式文本混杂）
                    print_tool_call(tu.name, inp)
                    # await 可能已完成的任务，几乎零等待
                    raw = await early_task
                    res = self._persist_large_result(tu.name, raw)
                    print_tool_result(tu.name, res)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": res,
                    })
                    continue

                # 2. 非安全工具（write_file/run_shell 等）走常规同步执行
                print_tool_call(tu.name, inp)
                raw = await self._execute_tool_call(tu.name, inp)
                res = self._persist_large_result(tu.name, raw)
                print_tool_result(tu.name, res)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": res,
                })

            # 将所有工具执行结果追加到历史，供下一轮对话使用 
            self.history.append_tool_results(tool_results)   

                    
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

    async def _call_anthropic_stream(self, on_tool_block_complete=None) -> Any:
        """流式调用 Anthropic API，监听 tool_use 块并在完成时触发回调。"""
        max_output = _get_max_output_tokens(self.backend.model)
        create_params = {
            "model": self.backend.model,
            # thinking 模式下使用动态上限，禁用时使用默认 16384
            "max_tokens": max_output if self.state.thinking_mode != "disabled" else 16384,
            "system": self._system_prompt,
            "tools": get_tool_definitions(),
            "messages": self.history.anthropic_messages,
        }

        # thinking 模式启用时，预留几乎全部 token 给思考过程
        if self.state.thinking_mode in ("adaptive", "enabled"):
            create_params["thinking"] = {
                "type": "enabled",
                "budget_tokens": max_output - 1,
            }
        
        # 按 content_block 的 index 追踪各工具块的累积参数
        tool_blocks_by_index = {}

        # 开启流式 API 监听，使用 async with 确保流正确关闭
        async with self._client.messages.stream(**create_params) as stream:
            async for event in stream:
                if not hasattr(event, 'type'):
                    continue
                # 1. 工具块开始：记录 id 和 name，初始化空的 input_json
                if event.type == "content_block_start":
                    cb = getattr(event, 'content_block', None)
                    if cb and getattr(cb, 'type', None) == "tool_use":
                        tool_blocks_by_index[event.index] = {
                            "id": cb.id,
                            "name": cb.name,
                            "input_json": "",
                        }
                # 2. JSON 增量片段：逐步拼接工具参数（流式传输时 JSON 是分片到达的）
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if hasattr(delta, 'partial_json'):
                        tb = tool_blocks_by_index.get(event.index)
                        if tb:
                            tb["input_json"] += delta.partial_json
                # 3. 工具块结束：解析 JSON 并触发回调
                elif event.type == "content_block_stop":
                    tb = tool_blocks_by_index.pop(event.index, None)
                    if tb and on_tool_block_complete:
                        try:
                            parsed = json.loads(tb["input_json"] or "{}")
                        except Exception:
                            parsed = {}
                        
                        # 回调通知 Agent：此工具的参数已完整，可以开始执行
                        on_tool_block_complete({
                            "type": "tool_use",
                            "id": tb["id"],
                            "name": tb["name"],
                            "input": parsed,
                        })
            # 等待流完全结束，获取最终的完整消息对象
            final_message = await stream.get_final_message()
        return final_message
    
    def _persist_large_result(self, tool_name: str, result: str) -> str:
        """当工具结果超过阈值时，将完整内容保存到磁盘并返回预览摘要。"""
        # 小结果直接返回，避免不必要的磁盘 IO
        if len(result) <= LARGE_RESULT_THRESHOLD:
            return result
        # 创建工具结果存储目录
        d = Path.cwd() / ".mini-claude" / "tool-results"
        d.mkdir(parents=True, exist_ok=True)
        # 文件名包含毫秒时间戳和工具名，便于事后追溯
        filename = f"{int(time.time() * 1000)}-{tool_name}.txt"
        filepath = d / filename
        filepath.write_text(result, encoding="utf-8")

        lines = result.split("\n")
        preview = "\n".join(lines[:LARGE_RESULT_PREVIEW_LINES])
        # 使用字节数而非字符数衡量，确保中文等多字节字符被正确计算
        size_kb = len(result.encode()) / 1024

        return (
            f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
            f"Full output saved to {filepath}. "
            f"You can use read_file to see the full result.]\n\n"
            f"Preview (first {LARGE_RESULT_PREVIEW_LINES} lines):\n{preview}"
        )
    
    async def _execute_tool_call(self, name: str, args: dict) -> str:
        """执行工具调用的封装方法，便于后续添加日志、权限检查等逻辑。"""
        return await execute_tool(name, args)
    
    
    

    

