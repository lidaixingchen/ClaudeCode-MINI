"""Agent core loop — dual backend (Anthropic + OpenAI compatible), streaming,
4-layer compression, plan mode, sub-agents, budget control.
Mirrors Claude Code's agent architecture."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable, Literal

import anthropic
import openai

from .tools import (
    tool_definitions,
    execute_tool,
    check_permission,
    CONCURRENCY_SAFE_TOOLS,
    get_active_tool_definitions,
    ToolDef,
    PermissionMode,
)
from .memory import (
    start_memory_prefetch,
    format_memories_for_injection,
    MemoryPrefetch,
)
from .ui import (
    print_assistant_text,
    print_tool_call,
    print_tool_result,
    print_error,
    print_confirmation,
    print_divider,
    print_cost,
    print_retry,
    print_info,
    print_sub_agent_start,
    print_sub_agent_end,
    start_spinner,
    stop_spinner,
)
from .session import save_session
from .prompt import build_system_prompt
from .subagent import get_sub_agent_config
from .mcp_client import McpManager

# ─── Logger ─────────────────────────────────────────────────

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────

CONTEXT_WINDOW_SAFETY_MARGIN = 20000
AUTOCOMPACT_THRESHOLD = 0.85
COST_PER_INPUT_TOKEN = 3 / 1_000_000   # $3 per 1M tokens
COST_PER_OUTPUT_TOKEN = 15 / 1_000_000  # $15 per 1M tokens
LARGE_RESULT_THRESHOLD = 30 * 1024      # 30 KB
LARGE_RESULT_PREVIEW_LINES = 200
MAX_RETRIES = 3
MAX_RETRY_DELAY_MS = 30000

# ─── Model context windows ──────────────────────────────────

MODEL_CONTEXT = {
    "claude-opus-4-6": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-sonnet-4-20250514": 200000,
    "claude-haiku-4-5-20251001": 200000,
    "claude-opus-4-20250514": 200000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
}


def _get_context_window(model: str) -> int:
    return MODEL_CONTEXT.get(model, 200000)


# ─── Thinking support detection ─────────────────────────────


def _model_supports_thinking(model: str) -> bool:
    m = model.lower()
    if "claude-3-" in m or "3-5-" in m or "3-7-" in m:
        return False
    if "claude" in m and any(x in m for x in ("opus", "sonnet", "haiku")):
        return True
    return False


def _model_supports_adaptive_thinking(model: str) -> bool:
    m = model.lower()
    return "opus-4-6" in m or "sonnet-4-6" in m


def _get_max_output_tokens(model: str) -> int:
    m = model.lower()
    if "opus-4-6" in m:
        return 64000
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    return 16384


# ─── Convert tools to OpenAI format ─────────────────────────


def _to_openai_tools(tools: list[ToolDef]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


# ─── Multi-tier compression constants ────────────────────────

SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell"}
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIP_THRESHOLD = 0.60
MICROCOMPACT_IDLE_S = 5 * 60  # 5 minutes
KEEP_RECENT_RESULTS = 3

# ─── Retry with exponential backoff ──────────────────────────


def _is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in (429, 503, 529):
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    return False


async def _with_retry(fn, max_retries: int = MAX_RETRIES):
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as error:
            if attempt >= max_retries or not _is_retryable(error):
                raise
            # Exponential backoff with jitter to avoid thundering herd
            base_delay = min(1000 * (2 ** attempt), MAX_RETRY_DELAY_MS) / 1000
            jitter = (hash(str(time.time())) % 1000) / 1000  # 0-1s random jitter
            delay = base_delay + jitter
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else (getattr(error, "code", None) or "network error")
            print_retry(attempt + 1, max_retries, reason)
            await asyncio.sleep(delay)


# ─── Configuration and state dataclasses ────────────────────


@dataclass
class BackendConfig:
    """后端配置——封装后端选择逻辑，提供工厂方法"""
    provider: Literal["anthropic", "openai"]
    api_key: str
    base_url: str | None = None
    model: str = "claude-opus-4-6"

    @classmethod
    def from_env(
        cls,
        model: str | None = None,
        api_base_override: str | None = None,
    ) -> "BackendConfig":
        """从环境变量自动检测后端类型并返回配置实例

        优先级：
        0. api_base_override 存在 → 强制 OpenAI 模式（命令行 --api-base）
        1. OPENAI_API_KEY + OPENAI_BASE_URL → OpenAI 兼容
        2. ANTHROPIC_API_KEY → Anthropic（可选 ANTHROPIC_BASE_URL）
        3. 单独 OPENAI_API_KEY → OpenAI（兜底）
        4. 都没有 → 抛出 ValueError
        """
        if api_base_override:
            api_key = (
                os.environ.get("OPENAI_API_KEY")
                or os.environ.get("ANTHROPIC_API_KEY")
            )
            if not api_key:
                raise ValueError("--api-base 需要设置 OPENAI_API_KEY 或 ANTHROPIC_API_KEY")
            return cls(
                provider="openai",
                api_key=api_key,
                base_url=api_base_override,
                model=model or "gpt-4o",
            )
        if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL"):
            return cls(
                provider="openai",
                api_key=os.environ["OPENAI_API_KEY"],
                base_url=os.environ.get("OPENAI_BASE_URL"),
                model=model or "gpt-4o",
            )
        if os.environ.get("ANTHROPIC_API_KEY"):
            return cls(
                provider="anthropic",
                api_key=os.environ["ANTHROPIC_API_KEY"],
                base_url=os.environ.get("ANTHROPIC_BASE_URL"),
                model=model or "claude-opus-4-6",
            )
        if os.environ.get("OPENAI_API_KEY"):
            return cls(
                provider="openai",
                api_key=os.environ["OPENAI_API_KEY"],
                base_url=os.environ.get("OPENAI_BASE_URL"),
                model=model or "gpt-4o",
            )
        raise ValueError(
            "未找到 API Key。请设置以下任一环境变量：\n"
            "  ANTHROPIC_API_KEY（Anthropic 后端）\n"
            "  OPENAI_API_KEY + OPENAI_BASE_URL（OpenAI 兼容后端）"
        )

    def create_client(self) -> Any:
        """工厂方法：根据 provider 创建对应的异步 SDK 客户端"""
        if self.provider == "openai":
            return openai.AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
            )
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return anthropic.AsyncAnthropic(**kwargs)

    @property
    def is_openai(self) -> bool:
        """是否使用 OpenAI 兼容后端"""
        return self.provider == "openai"


@dataclass
class AgentConfig:
    """Agent configuration. permission_mode is mutable (toggle_plan_mode)."""
    permission_mode: str = "default"
    thinking: bool = False
    max_cost_usd: float | None = None
    max_turns: int | None = None
    custom_system_prompt: str | None = None
    custom_tools: list[ToolDef] | None = None
    is_sub_agent: bool = False


@dataclass
class AgentState:
    """Mutable runtime state for an Agent instance."""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    last_input_token_count: int = 0
    current_turns: int = 0
    last_api_call_time: float = 0.0
    aborted: bool = False
    current_task: asyncio.Task | None = None
    confirmed_paths: set[str] = field(default_factory=set)
    pre_plan_mode: str | None = None
    plan_file_path: str | None = None
    context_cleared: bool = False
    thinking_mode: str = "disabled"
    output_buffer: list[str] | None = None
    read_file_state: dict[str, float] = field(default_factory=dict)
    already_surfaced_memories: set[str] = field(default_factory=set)
    session_memory_bytes: int = 0


# ─── Message history abstraction ────────────────────────────


class MessageHistory:
    """Abstraction over Anthropic/OpenAI message formats.

    Provides a unified interface for managing conversation history,
    eliminating code duplication between the two backends.
    """

    def __init__(self, use_openai: bool, system_prompt: str):
        self.use_openai = use_openai
        self.system_prompt = system_prompt
        self._anthropic_messages: list[dict] = []
        self._openai_messages: list[dict] = []
        if use_openai:
            self._openai_messages.append({"role": "system", "content": system_prompt})

    @property
    def messages(self) -> list[dict]:
        """Return the active message list."""
        return self._openai_messages if self.use_openai else self._anthropic_messages

    @property
    def anthropic_messages(self) -> list[dict]:
        return self._anthropic_messages

    @property
    def openai_messages(self) -> list[dict]:
        return self._openai_messages

    def append_user_message(self, content: str | list) -> None:
        self.messages.append({"role": "user", "content": content})

    def append_assistant_message(self, content: Any) -> None:
        if self.use_openai and isinstance(content, dict) and "role" in content:
            self.messages.append(content)
        else:
            self.messages.append({"role": "assistant", "content": content})

    def append_tool_results(self, results: list[dict]) -> None:
        if self.use_openai:
            for r in results:
                self.messages.append(r)
        else:
            self.messages.append({"role": "user", "content": results})

    def append_openai_tool_message(self, tool_call_id: str, content: str) -> None:
        """Append an OpenAI-format tool result message."""
        self._openai_messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})

    def get_last_user_message(self) -> dict | None:
        for msg in reversed(self.messages):
            if msg.get("role") == "user":
                return msg
        return None

    def update_last_user_content(self, suffix: str) -> None:
        """Append text to the last user message (for memory injection)."""
        last = self.get_last_user_message()
        if last:
            content = last.get("content", "")
            if isinstance(content, str):
                last["content"] = content + "\n\n" + suffix
            elif isinstance(content, list):
                content.append({"type": "text", "text": suffix})

    def clear(self, keep_system: bool = True) -> None:
        """Clear history. If keep_system, preserve the system prompt for OpenAI."""
        self._anthropic_messages = []
        self._openai_messages = []
        if self.use_openai and keep_system:
            self._openai_messages.append({"role": "system", "content": self.system_prompt})

    def update_system_prompt(self, prompt: str) -> None:
        """Update the system prompt (for plan mode toggle)."""
        self.system_prompt = prompt
        if self.use_openai and self._openai_messages:
            self._openai_messages[0]["content"] = prompt

    def message_count(self) -> int:
        return len(self.messages)

    def to_dict(self) -> dict:
        """Serialize for session persistence."""
        return {
            "anthropicMessages": self._anthropic_messages if not self.use_openai else None,
            "openaiMessages": self._openai_messages if self.use_openai else None,
        }

    def restore(self, data: dict) -> None:
        """Restore from serialized session data."""
        if data.get("anthropicMessages"):
            self._anthropic_messages = data["anthropicMessages"]
        if data.get("openaiMessages"):
            self._openai_messages = data["openaiMessages"]

    def replace_anthropic_messages(self, messages: list[dict]) -> None:
        """Replace all Anthropic messages (used during compaction)."""
        self._anthropic_messages = messages

    def replace_openai_messages(self, messages: list[dict]) -> None:
        """Replace all OpenAI messages (used during compaction)."""
        self._openai_messages = messages


# ─── Agent ───────────────────────────────────────────────────


class Agent:
    def __init__(
        self,
        *,
        backend: BackendConfig,
        permission_mode: PermissionMode = "default",
        thinking: bool = False,
        max_cost_usd: float | None = None,
        max_turns: int | None = None,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
        custom_system_prompt: str | None = None,
        custom_tools: list[ToolDef] | None = None,
        is_sub_agent: bool = False,
    ):
        # Backend configuration
        self.backend = backend

        # Agent configuration
        self.config = AgentConfig(
            permission_mode=permission_mode,
            thinking=thinking,
            max_cost_usd=max_cost_usd,
            max_turns=max_turns,
            custom_system_prompt=custom_system_prompt,
            custom_tools=custom_tools,
            is_sub_agent=is_sub_agent,
        )

        # Mutable runtime state
        self.state = AgentState()

        # Callbacks
        self.confirm_fn = confirm_fn
        self._plan_approval_fn: Callable[[str], Awaitable[dict]] | None = None

        # Derived properties
        self.use_openai = backend.is_openai
        self.tools = custom_tools or tool_definitions
        self.effective_window = _get_context_window(backend.model) - CONTEXT_WINDOW_SAFETY_MARGIN
        self.session_id = uuid.uuid4().hex[:8]
        self.session_start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Resolve thinking mode
        self.state.thinking_mode = self._resolve_thinking_mode()

        # MCP integration
        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        # Build system prompt
        self._base_system_prompt = custom_system_prompt or build_system_prompt()
        if self.config.permission_mode == "plan":
            self.state.plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt

        # Unified message history
        self.history = MessageHistory(self.use_openai, self._system_prompt)

        # Initialize API client via BackendConfig factory
        self._client = backend.create_client()

    def _update_system_prompt(self) -> None:
        self._base_system_prompt = self.config.custom_system_prompt or build_system_prompt()
        if self.config.permission_mode == "plan":
            if not self.state.plan_file_path:
                self.state.plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt
        self.history.update_system_prompt(self._system_prompt)

    # ─── Properties for backward compatibility ────────────────

    @property
    def permission_mode(self) -> str:
        return self.config.permission_mode

    @property
    def model(self) -> str:
        return self.backend.model

    @property
    def is_sub_agent(self) -> bool:
        return self.config.is_sub_agent

    @property
    def _aborted(self) -> bool:
        return self.state.aborted

    @_aborted.setter
    def _aborted(self, value: bool) -> None:
        self.state.aborted = value

    @property
    def _output_buffer(self) -> list[str] | None:
        return self.state.output_buffer

    @_output_buffer.setter
    def _output_buffer(self, value: list[str] | None) -> None:
        self.state.output_buffer = value

    @property
    def _read_file_state(self) -> dict[str, float]:
        return self.state.read_file_state

    @property
    def _context_cleared(self) -> bool:
        return self.state.context_cleared

    @_context_cleared.setter
    def _context_cleared(self, value: bool) -> None:
        self.state.context_cleared = value

    @property
    def _thinking_mode(self) -> str:
        return self.state.thinking_mode

    @property
    def _plan_file_path(self) -> str | None:
        return self.state.plan_file_path

    @property
    def is_processing(self) -> bool:
        return self.state.current_task is not None and not self.state.current_task.done()

    # ─── Thinking mode resolution ────────────────────────────

    def _resolve_thinking_mode(self) -> str:
        if not self.config.thinking:
            return "disabled"
        if not _model_supports_thinking(self.backend.model):
            return "disabled"
        if _model_supports_adaptive_thinking(self.backend.model):
            return "adaptive"
        return "enabled"

    # ─── Side query for memory recall ─────────────────────────

    def _build_side_query(self) -> Callable[[str, str], Awaitable[str]] | None:
        """Build a sideQuery callable for memory recall, works with both backends."""
        client = self._client
        model = self.backend.model
        if not self.use_openai:

            async def _sq(system: str, user_message: str) -> str:
                resp = await client.messages.create(
                    model=model, max_tokens=256, system=system,
                    messages=[{"role": "user", "content": user_message}],
                )
                return "".join(b.text for b in resp.content if b.type == "text")
            return _sq

        async def _sq_oai(system: str, user_message: str) -> str:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
            )
            return resp.choices[0].message.content or "" if resp.choices else ""
        return _sq_oai

    # ─── Abort and callbacks ─────────────────────────────────

    def abort(self) -> None:
        self.state.aborted = True
        if self.state.current_task and not self.state.current_task.done():
            self.state.current_task.cancel()

    def set_confirm_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn

    def set_plan_approval_fn(self, fn: Callable[[str], Awaitable[dict]]) -> None:
        self._plan_approval_fn = fn

    # ─── Plan mode toggle ────────────────────────────────────

    def toggle_plan_mode(self) -> str:
        if self.config.permission_mode == "plan":
            self.config.permission_mode = self.state.pre_plan_mode or "default"
            self.state.pre_plan_mode = None
            self.state.plan_file_path = None
            self._system_prompt = self._base_system_prompt
            self.history.update_system_prompt(self._system_prompt)
            print_info(f"Exited plan mode → {self.config.permission_mode} mode")
            return self.config.permission_mode
        else:
            self.state.pre_plan_mode = self.config.permission_mode
            self.config.permission_mode = "plan"
            self.state.plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            self.history.update_system_prompt(self._system_prompt)
            print_info(f"Entered plan mode. Plan file: {self.state.plan_file_path}")
            return "plan"

    def get_token_usage(self) -> dict:
        return {"input": self.state.total_input_tokens, "output": self.state.total_output_tokens}

    # ─── Main entry point ────────────────────────────────────

    async def chat(self, user_message: str) -> None:
        # Lazily connect to MCP servers on first chat (main agent only)
        if not self._mcp_initialized and not self.config.is_sub_agent:
            self._mcp_initialized = True
            try:
                await self._mcp_manager.load_and_connect()
                mcp_defs = self._mcp_manager.get_tool_definitions()
                if mcp_defs:
                    self.tools = self.tools + mcp_defs
            except Exception as e:
                logger.warning(f"MCP init failed: {e}")

        self.state.aborted = False
        coro = self._chat_openai(user_message) if self.use_openai else self._chat_anthropic(user_message)
        self.state.current_task = asyncio.current_task()
        try:
            await coro
        except asyncio.CancelledError:
            self.state.aborted = True
        finally:
            self.state.current_task = None
        if not self.config.is_sub_agent:
            print_divider()
            self._auto_save()

    # ─── Sub-agent entry point ────────────────────────────────

    async def run_once(self, prompt: str) -> dict:
        self.state.output_buffer = []
        prev_in = self.state.total_input_tokens
        prev_out = self.state.total_output_tokens
        await self.chat(prompt)
        text = "".join(self.state.output_buffer)
        self.state.output_buffer = None
        return {
            "text": text,
            "tokens": {
                "input": self.state.total_input_tokens - prev_in,
                "output": self.state.total_output_tokens - prev_out,
            },
        }

    # ─── Output helper ────────────────────────────────────────

    def _emit_text(self, text: str) -> None:
        if self.state.output_buffer is not None:
            self.state.output_buffer.append(text)
        else:
            print_assistant_text(text)

    # ─── REPL commands ────────────────────────────────────────

    def clear_history(self) -> None:
        self.history.clear(keep_system=True)
        self.state.total_input_tokens = 0
        self.state.total_output_tokens = 0
        self.state.last_input_token_count = 0
        print_info("Conversation cleared.")

    def show_cost(self) -> None:
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.config.max_cost_usd} budget" if self.config.max_cost_usd else ""
        turn_info = f" | Turns: {self.state.current_turns}/{self.config.max_turns}" if self.config.max_turns else ""
        print_info(
            f"Tokens: {self.state.total_input_tokens} in / {self.state.total_output_tokens} out\n"
            f"  Estimated cost: ${total:.4f}{budget_info}{turn_info}"
        )

    def _get_current_cost_usd(self) -> float:
        return (
            self.state.total_input_tokens * COST_PER_INPUT_TOKEN
            + self.state.total_output_tokens * COST_PER_OUTPUT_TOKEN
        )

    def _check_budget(self) -> dict:
        if self.config.max_cost_usd is not None and self._get_current_cost_usd() >= self.config.max_cost_usd:
            return {"exceeded": True, "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.config.max_cost_usd})"}
        if self.config.max_turns is not None and self.state.current_turns >= self.config.max_turns:
            return {"exceeded": True, "reason": f"Turn limit reached ({self.state.current_turns} >= {self.config.max_turns})"}
        return {"exceeded": False}

    async def compact(self) -> None:
        await self._compact_conversation()

    # ─── Session ──────────────────────────────────────────────

    def restore_session(self, data: dict) -> None:
        self.history.restore(data)
        print_info(f"Session restored ({self.history.message_count()} messages).")

    def _auto_save(self) -> None:
        try:
            save_session(self.session_id, {
                "metadata": {
                    "id": self.session_id,
                    "model": self.backend.model,
                    "cwd": str(Path.cwd()),
                    "startTime": self.session_start_time,
                    "messageCount": self.history.message_count(),
                },
                **self.history.to_dict(),
            })
        except Exception as e:
            logger.debug(f"Session auto-save failed: {e}")

    # ─── Autocompact ──────────────────────────────────────────

    async def _check_and_compact(self) -> None:
        if self.state.last_input_token_count > self.effective_window * AUTOCOMPACT_THRESHOLD:
            print_info("Context window filling up, compacting conversation...")
            await self._compact_conversation()

    async def _compact_conversation(self) -> None:
        if self.use_openai:
            await self._compact_openai()
        else:
            await self._compact_anthropic()
        print_info("Conversation compacted.")

    async def _compact_anthropic(self) -> None:
        messages = self.history.anthropic_messages
        if len(messages) < 4:
            return
        last_user_msg = messages[-1]
        summary_resp = await self._client.messages.create(
            model=self.backend.model,
            max_tokens=2048,
            system="You are a conversation summarizer. Be concise but preserve important details.",
            messages=[
                *messages[:-1],
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary_text = summary_resp.content[0].text if summary_resp.content and summary_resp.content[0].type == "text" else "No summary available."
        new_messages = [
            {"role": "user", "content": f"[Previous conversation summary]\n{summary_text}"},
            {"role": "assistant", "content": "Understood. I have the context from our previous conversation. How can I continue helping?"},
        ]
        if last_user_msg.get("role") == "user":
            new_messages.append(last_user_msg)
        self.history.replace_anthropic_messages(new_messages)
        self.state.last_input_token_count = 0

    async def _compact_openai(self) -> None:
        messages = self.history.openai_messages
        if len(messages) < 5:
            return
        system_msg = messages[0]
        last_user_msg = messages[-1]
        summary_resp = await self._client.chat.completions.create(
            model=self.backend.model,
            messages=[  # type: ignore[arg-type]
                {"role": "system", "content": "You are a conversation summarizer. Be concise but preserve important details."},
                *messages[1:-1],
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary_text = summary_resp.choices[0].message.content or "No summary available."
        new_messages = [
            system_msg,
            {"role": "user", "content": f"[Previous conversation summary]\n{summary_text}"},
            {"role": "assistant", "content": "Understood. I have the context from our previous conversation. How can I continue helping?"},
        ]
        if last_user_msg.get("role") == "user":
            new_messages.append(last_user_msg)
        self.history.replace_openai_messages(new_messages)
        self.state.last_input_token_count = 0

    # ─── Multi-tier compression pipeline ──────────────────────

    def _run_compression_pipeline(self) -> None:
        if self.use_openai:
            self._budget_tool_results_openai()
            self._snip_stale_results_openai()
            self._microcompact_openai()
        else:
            self._budget_tool_results_anthropic()
            self._snip_stale_results_anthropic()
            self._microcompact_anthropic()

    # Tier 1: Budget tool results
    def _budget_tool_results_anthropic(self) -> None:
        utilization = self.state.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < 0.5:
            return
        budget = 15000 if utilization > 0.7 else 30000
        for msg in self.history.anthropic_messages:
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and len(block["content"]) > budget:
                    keep = (budget - 80) // 2
                    block["content"] = block["content"][:keep] + f"\n\n[... budgeted: {len(block['content']) - keep * 2} chars truncated ...]\n\n" + block["content"][-keep:]

    def _budget_tool_results_openai(self) -> None:
        utilization = self.state.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < 0.5:
            return
        budget = 15000 if utilization > 0.7 else 30000
        for msg in self.history.openai_messages:
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg["content"]) > budget:
                keep = (budget - 80) // 2
                msg["content"] = msg["content"][:keep] + f"\n\n[... budgeted: {len(msg['content']) - keep * 2} chars truncated ...]\n\n" + msg["content"][-keep:]

    # Tier 2: Snip stale results
    def _snip_stale_results_anthropic(self) -> None:
        utilization = self.state.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return

        results = []
        for mi, msg in enumerate(self.history.anthropic_messages):
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for bi, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] != SNIP_PLACEHOLDER:
                    tool_use_id = block.get("tool_use_id")
                    tool_info = self._find_tool_use_by_id(tool_use_id)
                    if tool_info and tool_info["name"] in SNIPPABLE_TOOLS:
                        results.append({"mi": mi, "bi": bi, "name": tool_info["name"], "file_path": tool_info.get("input", {}).get("file_path")})

        if len(results) <= KEEP_RECENT_RESULTS:
            return

        to_snip = set()
        seen_files: dict[str, list[int]] = {}
        for i, r in enumerate(results):
            if r["name"] == "read_file" and r.get("file_path"):
                seen_files.setdefault(r["file_path"], []).append(i)

        for indices in seen_files.values():
            if len(indices) > 1:
                for j in indices[:-1]:
                    to_snip.add(j)

        snip_before = len(results) - KEEP_RECENT_RESULTS
        for i in range(snip_before):
            to_snip.add(i)

        for idx in to_snip:
            r = results[idx]
            self.history.anthropic_messages[r["mi"]]["content"][r["bi"]]["content"] = SNIP_PLACEHOLDER

    def _snip_stale_results_openai(self) -> None:
        utilization = self.state.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return
        tool_msgs = []
        for i, msg in enumerate(self.history.openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] != SNIP_PLACEHOLDER:
                tool_msgs.append(i)
        if len(tool_msgs) <= KEEP_RECENT_RESULTS:
            return
        snip_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(snip_count):
            self.history.openai_messages[tool_msgs[i]]["content"] = SNIP_PLACEHOLDER

    # Tier 3: Microcompact
    def _microcompact_anthropic(self) -> None:
        if not self.state.last_api_call_time or (time.time() - self.state.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        all_results = []
        for mi, msg in enumerate(self.history.anthropic_messages):
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for bi, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                    all_results.append((mi, bi))
        clear_count = len(all_results) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            mi, bi = all_results[i]
            self.history.anthropic_messages[mi]["content"][bi]["content"] = "[Old result cleared]"

    def _microcompact_openai(self) -> None:
        if not self.state.last_api_call_time or (time.time() - self.state.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        tool_msgs = []
        for i, msg in enumerate(self.history.openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                tool_msgs.append(i)
        clear_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            self.history.openai_messages[tool_msgs[i]]["content"] = "[Old result cleared]"

    def _find_tool_use_by_id(self, tool_use_id: str) -> dict | None:
        for msg in self.history.anthropic_messages:
            if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == tool_use_id:
                    return {"name": block["name"], "input": block.get("input", {})}
        return None

    # ─── Large result persistence ─────────────────────────────────

    def _persist_large_result(self, tool_name: str, result: str) -> str:
        if len(result.encode()) <= LARGE_RESULT_THRESHOLD:
            return result
        d = Path.cwd() / ".mini-claude" / "tool-results"
        d.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time() * 1000)}-{tool_name}.txt"
        filepath = d / filename
        filepath.write_text(result, encoding="utf-8")

        lines = result.split("\n")
        preview = "\n".join(lines[:LARGE_RESULT_PREVIEW_LINES])
        size_kb = len(result.encode()) / 1024

        return (
            f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
            f"Full output saved to {filepath}. "
            f"You can use read_file to see the full result.]\n\n"
            f"Preview (first {LARGE_RESULT_PREVIEW_LINES} lines):\n{preview}"
        )

    # ─── Execute tool (handles agent/skill/plan mode internally) ─────

    async def _execute_tool_call(self, name: str, inp: dict) -> str:
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await self._execute_plan_mode_tool(name)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name == "skill":
            return await self._execute_skill_tool(inp)
        # Route MCP tool calls to the MCP manager
        if self._mcp_manager.is_mcp_tool(name):
            return await self._mcp_manager.call_tool(name, inp)
        return await execute_tool(name, inp, self.state.read_file_state)

    # ─── Skill fork mode ─────────────────────────────────────

    async def _execute_skill_tool(self, inp: dict) -> str:
        from .skills import execute_skill
        result = execute_skill(inp.get("skill_name", ""), inp.get("args", ""))
        if not result:
            return f"Unknown skill: {inp.get('skill_name', '')}"

        if result["context"] == "fork":
            tools = (
                [t for t in self.tools if t["name"] in result["allowed_tools"]]
                if result.get("allowed_tools")
                else [t for t in self.tools if t["name"] != "agent"]
            )
            print_sub_agent_start("skill-fork", inp.get("skill_name", ""))
            sub_agent = Agent(
                backend=self.backend,
                custom_system_prompt=result["prompt"],
                custom_tools=tools,
                is_sub_agent=True,
                permission_mode="plan" if self.config.permission_mode == "plan" else "bypassPermissions",
            )
            try:
                sub_result = await sub_agent.run_once(inp.get("args") or "Execute this skill task.")
                self.state.total_input_tokens += sub_result["tokens"]["input"]
                self.state.total_output_tokens += sub_result["tokens"]["output"]
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return sub_result["text"] or "(Skill produced no output)"
            except Exception as e:
                logger.error(f"Skill fork error: {e}")
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return f"Skill fork error: {e}"

        return f'[Skill "{inp.get("skill_name", "")}" activated]\n\n{result["prompt"]}'

    # ─── Plan mode helpers ──────────────────────────────────────

    def _generate_plan_file_path(self) -> str:
        d = Path.home() / ".claude" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"plan-{self.session_id}.md")

    def _build_plan_mode_prompt(self) -> str:
        return f"""

# Plan Mode Active

Plan mode is active. You MUST NOT make any edits (except the plan file below), run non-readonly tools, or make any changes to the system.

## Plan File: {self.state.plan_file_path}
Write your plan incrementally to this file using write_file or edit_file. This is the ONLY file you are allowed to edit.

## Workflow
1. **Explore**: Read code to understand the task. Use read_file, list_files, grep_search.
2. **Design**: Design your implementation approach. Use the agent tool with type="plan" if the task is complex.
3. **Write Plan**: Write a structured plan to the plan file including:
   - **Context**: Why this change is needed
   - **Steps**: Implementation steps with critical file paths
   - **Verification**: How to test the changes
4. **Exit**: Call exit_plan_mode when your plan is ready for user review.

IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""

    async def _execute_plan_mode_tool(self, name: str) -> str:
        if name == "enter_plan_mode":
            if self.config.permission_mode == "plan":
                return "Already in plan mode."
            self.state.pre_plan_mode = self.config.permission_mode
            self.config.permission_mode = "plan"
            self.state.plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            self.history.update_system_prompt(self._system_prompt)
            print_info("Entered plan mode (read-only). Plan file: " + self.state.plan_file_path)
            return (
                f"Entered plan mode. You are now in read-only mode.\n\n"
                f"Your plan file: {self.state.plan_file_path}\n"
                f"Write your plan to this file. This is the only file you can edit.\n\n"
                f"When your plan is complete, call exit_plan_mode."
            )

        if name == "exit_plan_mode":
            if self.config.permission_mode != "plan":
                return "Not in plan mode."
            plan_content = "(No plan file found)"
            if self.state.plan_file_path and Path(self.state.plan_file_path).exists():
                plan_content = Path(self.state.plan_file_path).read_text(encoding="utf-8")

            # Interactive approval flow
            if self._plan_approval_fn:
                result = await self._plan_approval_fn(plan_content)
                choice = result.get("choice", "manual-execute")

                if choice == "keep-planning":
                    feedback = result.get("feedback") or "Please revise the plan."
                    return (
                        f"User rejected the plan and wants to keep planning.\n\n"
                        f"User feedback: {feedback}\n\n"
                        f"Please revise your plan based on this feedback. When done, call exit_plan_mode again."
                    )

                # User approved — determine target mode
                if choice == "clear-and-execute":
                    target_mode = "acceptEdits"
                elif choice == "execute":
                    target_mode = "acceptEdits"
                else:  # manual-execute
                    target_mode = self.state.pre_plan_mode or "default"

                # Exit plan mode
                self.config.permission_mode = target_mode
                self.state.pre_plan_mode = None
                saved_plan_path = self.state.plan_file_path
                self.state.plan_file_path = None
                self._system_prompt = self._base_system_prompt
                self.history.update_system_prompt(self._system_prompt)

                if choice == "clear-and-execute":
                    self.history.clear(keep_system=True)
                    self.state.context_cleared = True
                    print_info(f"Plan approved. Context cleared, executing in {target_mode} mode.")
                    return (
                        f"User approved the plan. Context was cleared. Permission mode: {target_mode}\n\n"
                        f"Plan file: {saved_plan_path}\n\n"
                        f"## Approved Plan:\n{plan_content}\n\n"
                        f"Proceed with implementation."
                    )

                print_info(f"Plan approved. Executing in {target_mode} mode.")
                return (
                    f"User approved the plan. Permission mode: {target_mode}\n\n"
                    f"## Approved Plan:\n{plan_content}\n\n"
                    f"Proceed with implementation."
                )

            # Fallback: no approval function (e.g. sub-agents)
            self.config.permission_mode = self.state.pre_plan_mode or "default"
            self.state.pre_plan_mode = None
            self.state.plan_file_path = None
            self._system_prompt = self._base_system_prompt
            self.history.update_system_prompt(self._system_prompt)
            print_info("Exited plan mode. Restored to " + self.config.permission_mode + " mode.")
            return f"Exited plan mode. Permission mode restored to: {self.config.permission_mode}\n\n## Your Plan:\n{plan_content}"

        return f"Unknown plan mode tool: {name}"

    async def _execute_agent_tool(self, inp: dict) -> str:
        agent_type = inp.get("type", "general")
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")

        print_sub_agent_start(agent_type, description)

        config = get_sub_agent_config(agent_type)
        sub_agent = Agent(
            backend=self.backend,
            custom_system_prompt=config["system_prompt"],
            custom_tools=config["tools"],
            is_sub_agent=True,
            permission_mode="plan" if self.config.permission_mode == "plan" else "bypassPermissions",
        )

        try:
            result = await sub_agent.run_once(prompt)
            self.state.total_input_tokens += result["tokens"]["input"]
            self.state.total_output_tokens += result["tokens"]["output"]
            print_sub_agent_end(agent_type, description)
            return result["text"] or "(Sub-agent produced no output)"
        except Exception as e:
            logger.error(f"Sub-agent error: {e}")
            print_sub_agent_end(agent_type, description)
            return f"Sub-agent error: {e}"

    # ─── Anthropic backend ───────────────────────────────────────

    async def _chat_anthropic(self, user_message: str) -> None:
        self.history.append_user_message(user_message)
        await self._check_and_compact()

        # Start async memory prefetch
        memory_prefetch = self._start_memory_prefetch(user_message)

        while True:
            if self.state.aborted:
                break

            self._update_system_prompt()
            self._run_compression_pipeline()
            self._consume_memory_prefetch(memory_prefetch)

            if not self.config.is_sub_agent:
                start_spinner()

            # Streaming tool execution
            early_executions: dict[str, asyncio.Task] = {}

            def _on_tool_block(block: dict):
                if block["name"] in CONCURRENCY_SAFE_TOOLS:
                    perm = check_permission(block["name"], block["input"], self.config.permission_mode, self.state.plan_file_path)
                    if perm["action"] == "allow":
                        task = asyncio.create_task(self._execute_tool_call(block["name"], block["input"]))
                        early_executions[block["id"]] = task

            response = await self._call_anthropic_stream(on_tool_block_complete=_on_tool_block)

            if not self.config.is_sub_agent:
                stop_spinner()

            self.state.last_api_call_time = time.time()
            self.state.total_input_tokens += response.usage.input_tokens
            self.state.total_output_tokens += response.usage.output_tokens
            self.state.last_input_token_count = response.usage.input_tokens

            tool_uses = [b for b in response.content if b.type == "tool_use"]

            self.history.append_assistant_message(
                [self._block_to_dict(b) for b in response.content]
            )

            if not tool_uses:
                if not self.config.is_sub_agent:
                    print_cost(self.state.total_input_tokens, self.state.total_output_tokens)
                break

            self.state.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                break

            # Process tools
            tool_results, context_break = await self._process_anthropic_tools(
                tool_uses, early_executions
            )

            if not context_break and tool_results:
                self.history.append_tool_results(tool_results)
            self.state.context_cleared = False

    async def _process_anthropic_tools(
        self, tool_uses: list, early_executions: dict[str, asyncio.Task]
    ) -> tuple[list[dict], bool]:
        """Process Anthropic tool calls, returning (results, context_break)."""
        tool_results: list[dict] = []
        for tu in tool_uses:
            if self.state.context_cleared or self.state.aborted:
                break
            inp = dict(tu.input) if hasattr(tu.input, 'items') else tu.input
            print_tool_call(tu.name, inp)

            # Was this tool already started during streaming?
            early_task = early_executions.get(tu.id)
            if early_task:
                raw = await early_task
                res = self._persist_large_result(tu.name, raw)
                print_tool_result(tu.name, res)
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})
                continue

            # Permission check
            perm = check_permission(tu.name, inp, self.config.permission_mode, self.state.plan_file_path)
            if perm["action"] == "deny":
                print_info(f"Denied: {perm.get('message', '')}")
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": f"Action denied: {perm.get('message', '')}"})
                continue
            if perm["action"] == "confirm" and perm.get("message") and perm["message"] not in self.state.confirmed_paths:
                confirmed = await self._confirm_dangerous(perm["message"])
                if not confirmed:
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "User denied this action."})
                    continue
                self.state.confirmed_paths.add(perm["message"])

            raw = await self._execute_tool_call(tu.name, inp)
            res = self._persist_large_result(tu.name, raw)
            print_tool_result(tu.name, res)

            if self.state.context_cleared:
                self.state.context_cleared = False
                self.history.append_user_message(res)
                return tool_results, True
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})

        return tool_results, False

    @staticmethod
    def _block_to_dict(block) -> dict:
        """Convert an Anthropic content block to a plain dict for storage."""
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input) if hasattr(block.input, 'items') else block.input}
        return {"type": block.type}

    async def _call_anthropic_stream(self, on_tool_block_complete=None) -> Any:
        """Stream an Anthropic API call with streaming tool execution support."""
        async def _do():
            max_output = _get_max_output_tokens(self.backend.model)
            create_params: dict[str, Any] = {
                "model": self.backend.model,
                "max_tokens": max_output if self.state.thinking_mode != "disabled" else 16384,
                "system": self._system_prompt,
                "tools": get_active_tool_definitions(self.tools),
                "messages": self.history.anthropic_messages,
            }

            if self.state.thinking_mode in ("adaptive", "enabled"):
                create_params["thinking"] = {"type": "enabled", "budget_tokens": max_output - 1}

            first_text = True
            tool_blocks_by_index: dict[int, dict] = {}

            async with self._client.messages.stream(**create_params) as stream:
                async for event in stream:
                    if not hasattr(event, 'type'):
                        continue

                    if event.type == "content_block_start":
                        cb = getattr(event, 'content_block', None)
                        if cb and getattr(cb, 'type', None) == "tool_use":
                            tool_blocks_by_index[event.index] = {
                                "id": cb.id, "name": cb.name, "input_json": "",
                            }

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, 'text'):
                            if first_text:
                                stop_spinner()
                                self._emit_text("\n")
                                first_text = False
                            self._emit_text(delta.text)
                        elif hasattr(delta, 'thinking'):
                            if first_text:
                                stop_spinner()
                                self._emit_text("\n  [thinking] ")
                                first_text = False
                            self._emit_text(delta.thinking)
                        elif hasattr(delta, 'partial_json'):
                            tb = tool_blocks_by_index.get(event.index)
                            if tb:
                                tb["input_json"] += delta.partial_json

                    elif event.type == "content_block_stop":
                        tb = tool_blocks_by_index.pop(event.index, None)
                        if tb and on_tool_block_complete:
                            try:
                                parsed = json.loads(tb["input_json"] or "{}")
                            except Exception:
                                parsed = {}
                            on_tool_block_complete({
                                "type": "tool_use", "id": tb["id"],
                                "name": tb["name"], "input": parsed,
                            })

                final_message = await stream.get_final_message()

            # Filter out thinking blocks
            final_message.content = [b for b in final_message.content if b.type != "thinking"]
            return final_message

        return await _with_retry(_do)

    # ─── OpenAI-compatible backend ───────────────────────────────

    async def _chat_openai(self, user_message: str) -> None:
        self.history.append_user_message(user_message)
        await self._check_and_compact()

        # Start async memory prefetch
        memory_prefetch = self._start_memory_prefetch(user_message)

        while True:
            if self.state.aborted:
                break

            self._update_system_prompt()
            self._run_compression_pipeline()
            self._consume_memory_prefetch(memory_prefetch)

            if not self.config.is_sub_agent:
                start_spinner()

            response = await self._call_openai_stream()

            if not self.config.is_sub_agent:
                stop_spinner()

            self.state.last_api_call_time = time.time()

            if response.get("usage"):
                self.state.total_input_tokens += response["usage"]["prompt_tokens"]
                self.state.total_output_tokens += response["usage"]["completion_tokens"]
                self.state.last_input_token_count = response["usage"]["prompt_tokens"]

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            message = choice.get("message", {})

            self.history.append_assistant_message(message)

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                if not self.config.is_sub_agent:
                    print_cost(self.state.total_input_tokens, self.state.total_output_tokens)
                break

            self.state.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                break

            # Process tools
            context_break = await self._process_openai_tools(tool_calls)
            if context_break:
                break

    async def _process_openai_tools(self, tool_calls: list[dict]) -> bool:
        """Process OpenAI tool calls. Returns True if context was cleared."""
        # Phase 1: Parse & permission-check (serial)
        oai_checked: list[dict] = []
        for tc in tool_calls:
            if self.state.aborted:
                break
            if tc.get("type") != "function":
                continue
            fn_name = tc["function"]["name"]
            try:
                inp = json.loads(tc["function"]["arguments"])
            except Exception:
                inp = {}

            print_tool_call(fn_name, inp)

            perm = check_permission(fn_name, inp, self.config.permission_mode, self.state.plan_file_path)
            if perm["action"] == "deny":
                print_info(f"Denied: {perm.get('message', '')}")
                oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": f"Action denied: {perm.get('message', '')}"})
                continue
            if perm["action"] == "confirm" and perm.get("message") and perm["message"] not in self.state.confirmed_paths:
                confirmed = await self._confirm_dangerous(perm["message"])
                if not confirmed:
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": "User denied this action."})
                    continue
                self.state.confirmed_paths.add(perm["message"])
            oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})

        # Phase 2: Group & execute (parallel for consecutive safe tools)
        oai_batches: list[dict] = []
        for ct in oai_checked:
            safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
            if safe and oai_batches and oai_batches[-1]["concurrent"]:
                oai_batches[-1]["items"].append(ct)
            else:
                oai_batches.append({"concurrent": safe, "items": [ct]})

        for batch in oai_batches:
            if self.state.context_cleared or self.state.aborted:
                break

            if batch["concurrent"]:
                async def _run_oai_safe(ct_item: dict) -> tuple[dict, str]:
                    raw = await self._execute_tool_call(ct_item["fn"], ct_item["inp"])
                    res = self._persist_large_result(ct_item["fn"], raw)
                    print_tool_result(ct_item["fn"], res)
                    return ct_item, res

                results = await asyncio.gather(*[_run_oai_safe(ct) for ct in batch["items"]])
                for ct_item, res in results:
                    self.history.append_openai_tool_message(ct_item["tc"]["id"], res)
            else:
                for ct in batch["items"]:
                    if not ct["allowed"]:
                        self.history.append_openai_tool_message(ct["tc"]["id"], ct["result"])
                        continue
                    raw = await self._execute_tool_call(ct["fn"], ct["inp"])
                    res = self._persist_large_result(ct["fn"], raw)
                    print_tool_result(ct["fn"], res)

                    if self.state.context_cleared:
                        self.state.context_cleared = False
                        self.history.append_user_message(res)
                        return True
                    self.history.append_openai_tool_message(ct["tc"]["id"], res)

        self.state.context_cleared = False
        return False

    async def _call_openai_stream(self) -> Any:
        async def _do():
            stream = await self._client.chat.completions.create(
                model=self.backend.model,
                tools=_to_openai_tools(get_active_tool_definitions(self.tools)),  # type: ignore[arg-type]
                messages=self.history.openai_messages,  # type: ignore[arg-type]
                stream=True,
                stream_options={"include_usage": True},
            )

            content = ""
            first_text = True
            tool_calls: dict[int, dict] = {}
            finish_reason = ""
            usage = None

            async for chunk in stream:
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    }

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta and delta.content:
                    if first_text:
                        stop_spinner()
                        self._emit_text("\n")
                        first_text = False
                    self._emit_text(delta.content)
                    content += delta.content

                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        existing = tool_calls.get(tc.index)
                        if existing:
                            if tc.function and tc.function.arguments:
                                existing["arguments"] += tc.function.arguments
                        else:
                            tool_calls[tc.index] = {
                                "id": tc.id or "",
                                "name": (tc.function.name if tc.function else "") or "",
                                "arguments": (tc.function.arguments if tc.function else "") or "",
                            }

                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

            assembled = None
            if tool_calls:
                assembled = [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for _, tc in sorted(tool_calls.items())
                ]

            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": assembled,
                    },
                    "finish_reason": finish_reason or "stop",
                }],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
            }

        return await _with_retry(_do)

    # ─── Shared helpers ──────────────────────────────────────────

    def _start_memory_prefetch(self, user_message: str) -> MemoryPrefetch | None:
        """Start async memory prefetch if applicable."""
        if self.config.is_sub_agent:
            return None
        sq = self._build_side_query()
        if sq:
            return start_memory_prefetch(
                user_message, sq,
                self.state.already_surfaced_memories, self.state.session_memory_bytes,
            )
        return None

    def _consume_memory_prefetch(self, prefetch: MemoryPrefetch | None) -> None:
        """Consume memory prefetch results if settled."""
        if not prefetch or not prefetch.settled or prefetch.consumed:
            return
        prefetch.consumed = True
        try:
            memories = prefetch.task.result()
            if memories:
                injection_text = format_memories_for_injection(memories)
                self.history.update_last_user_content(injection_text)
                for m in memories:
                    self.state.already_surfaced_memories.add(m.path)
                    self.state.session_memory_bytes += len(m.content.encode())
        except Exception as e:
            logger.debug(f"Memory prefetch failed: {e}")

    async def _confirm_dangerous(self, command: str) -> bool:
        print_confirmation(command)
        if self.confirm_fn:
            return await self.confirm_fn(command)
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False
