from __future__ import annotations

import re
from pathlib import Path
from frontmatter import parse_frontmatter, format_frontmatter
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Any

# ─── Types ──────────────────────────────────────────────────

VALID_TYPES = {"user", "feedback", "project", "reference"}
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25000

class MemoryEntry:
    """单条记忆的数据模型，用于在内存中表示一条记忆记录。"""
    __slots__ = ("name", "description", "type", "filename", "content")

    def __init__(self, name: str, description: str, type: str, filename: str, content: str = ""):
        self.name = name                          # 记忆的显示名称
        self.description = description             # 一句话描述
        self.type = type                           # 记忆类型（user/feedback/project/reference）
        self.filename = filename                   # 对应的 .md 文件名
        self.content = content                     # 文件正文内容


# ─── Paths ──────────────────────────────────────────────────
def get_memory_dir() -> Path:
    """获取当前项目的记忆存储目录，不存在时自动创建。"""
    memory_dir = Path.cwd() / ".mini-claude" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    return memory_dir

def _get_index_path() -> Path:
    return get_memory_dir() / "MEMORY.md"

logger = logging.getLogger(__name__)

# sideQuery 的类型签名：异步函数，接收 system prompt 和 user message，返回模型生成的文本
# 实际类型为 async (system: str, user_message: str) -> str
SideQueryFn = Callable[[str, str], Any]  # 实际返回 Awaitable[str]


# ─── Slugify ────────────────────────────────────────────────


def _slugify(text: str) -> str:
    """将文本转换为 URL 友好的 slug 格式，用于生成记忆文件名。"""
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)  # 移除非字母数字、空格和连字符的字符
    text = re.sub(r"[\s_-]+", "-", text)  # 将空格和下划线替换为单个连字符
    text = re.sub(r"^-+|-+$", "", text)    # 移除开头和结尾的连字符
    return text[:40]  # 限制长度为 40 个字符

# ─── CRUD ───────────────────────────────────────────────────


def list_memories() -> list[MemoryEntry]:
    """扫描记忆目录并返回所有记忆条目列表，按修改时间降序排列。"""
    d = get_memory_dir()
    entries: list[MemoryEntry] = []
    # 遍历所有 .md 文件并解析元数据
    for f in sorted(d.glob("*.md")):
        # 跳过索引文件自身，避免无限循环
        if f.name == "MEMORY.md":
            continue
        try:
            result = parse_frontmatter(f.read_text(encoding="utf-8"))
            meta = result.meta
            # 必须包含 name 和 type 字段才视为有效记忆
            if not meta.get("name") or not meta.get("type"):
                logger.warning(f"Memory file {f} is missing required metadata fields.")
                continue
            # 非标准类型 fallback 为 "project"，保证用户手写的记忆不会被丢弃
            t = meta["type"] if meta["type"] in VALID_TYPES else "project"
            entries.append(MemoryEntry(
                name=meta["name"],
                description=meta.get("description", ""),
                type=t,
                filename=f.name,
                content=result.body,
            ))
        except Exception as e:
            logger.error(f"Failed to parse memory file {f}: {e}")
    # 按文件修改时间降序排列，最新的记忆排在前面
    entries.sort(key=lambda e: (get_memory_dir() / e.filename).stat().st_mtime, reverse=True)
    return entries

def save_memory(name: str, description: str, type: str, content: str) -> str:
    """创建或更新一条记忆文件，返回生成的文件名。"""
    d = get_memory_dir()
    # 文件名格式：类型_slug化的名称.md
    filename = f"{type}_{_slugify(name)}.md"
    text = format_frontmatter({"name": name, "description": description, "type": type}, content)
    (d / filename).write_text(text, encoding="utf-8")
    # 写入后自动更新索引文件
    _update_memory_index()
    return filename

def delete_memory(filename: str) -> bool:
    """删除指定的记忆文件，返回是否成功。"""
    file_path = get_memory_dir() / filename
    if not file_path.exists():
        logger.warning(f"Memory file {filename} does not exist.")
        return False
    file_path.unlink()
    # 删除后自动更新索引文件
    _update_memory_index()
    return True

# ─── Index ──────────────────────────────────────────────────


def _update_memory_index() -> None:
    """重新生成 MEMORY.md 索引文件，包含所有记忆的摘要信息。"""
    memories = list_memories()
    lines = ["# Memory Index", ""]
    for m in memories:
        lines.append(f"- **[{m.name}]({m.filename})** ({m.type}) — {m.description}")

    # 覆盖重写唯一的索引文件
    _get_index_path().write_text("\n".join(lines), encoding="utf-8")

def load_memory_index() -> str:
    """加载 MEMORY.md 索引内容，带截断保护防止 System Prompt 爆炸。"""
    index_path = _get_index_path()
    if not index_path.exists():
        return "# Memory Index\n\nNo memories yet."
    content = index_path.read_text(encoding="utf-8")
    # 截断行数和字节数，避免过长
    lines = content.splitlines()
    if len(lines) > MAX_INDEX_LINES:
        lines = lines[:MAX_INDEX_LINES] + ["..."]
    truncated_content = "\n".join(lines)
    if len(truncated_content.encode("utf-8")) > MAX_INDEX_BYTES:
        truncated_content = truncated_content.encode("utf-8")[:MAX_INDEX_BYTES].decode("utf-8", errors="ignore") + "\n..."
    return truncated_content

# ─── Memory Header (lightweight scan) ──────────────────────

MAX_MEMORY_FILES = 200
MAX_MEMORY_BYTES_PER_FILE = 4096
MAX_SESSION_MEMORY_BYTES = 60 * 1024  # 单会话累计注入的记忆总量上限


class MemoryHeader:
    """轻量级记忆文件元数据，只存储扫描结果而非完整内容。

    使用 __slots__ 相比普通 dataclass 可节省约 40% 内存占用，
    当记忆文件多达 200 个时效果显著。
    """
    __slots__ = ("filename", "file_path", "mtime_ms", "description", "type")

    def __init__(self, filename: str, file_path: str, mtime_ms: float,
                 description: str | None, type: str | None):
        self.filename = filename            # 文件名，如 user_xiaoming.md
        self.file_path = file_path          # 绝对路径，用于后续按需读取
        self.mtime_ms = mtime_ms            # 毫秒级修改时间戳，用于新鲜度判断
        self.description = description      # 一句话描述，用于语义匹配
        self.type = type                    # 记忆类型，用于分类筛选

def scan_memory_headers() -> list[MemoryHeader]:
    """扫描记忆目录，只读取 Frontmatter 区域（前 30 行）以提高性能。"""
    d = get_memory_dir()
    headers: list[MemoryHeader] = []
    for f in sorted(d.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            # 只读取前 30 行，避免加载整个文件
            with f.open(encoding="utf-8") as fp:
                frontmatter_lines = []
                for _ in range(30):
                    line = fp.readline()
                    if not line:
                        break
                    frontmatter_lines.append(line)
            frontmatter_content = "".join(frontmatter_lines)
            result = parse_frontmatter(frontmatter_content)
            meta = result.meta
            headers.append(MemoryHeader(
                filename=f.name,
                file_path=str(f.resolve()),
                mtime_ms=f.stat().st_mtime * 1000,
                description=meta.get("description"),
                type=meta.get("type"),
            ))
        except Exception as e:
            logger.error(f"Failed to scan memory file {f}: {e}")
    # 按修改时间降序排列，最新的记忆优先展示
    headers.sort(key=lambda h: h.mtime_ms, reverse=True)
    # 截断到最大文件数限制
    return headers[:MAX_MEMORY_FILES]

def format_memory_manifest(headers: list[MemoryHeader]) -> str:
    """将记忆头信息格式化为语义选择器可读的清单，每条记忆一行。"""
    lines = []
    for h in headers:
        tag = f"[{h.type}] " if h.type else ""
        ts = datetime.fromtimestamp(h.mtime_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if h.description:
            lines.append(f"- {tag}{h.filename} ({ts}): {h.description}")
        else:
            lines.append(f"- {tag}{h.filename} ({ts})")
    return "\n".join(lines)

def memory_age(mtime_ms: float) -> str:
    """将毫秒级时间戳转换为人类可读的相对时间描述。"""
    # 86_400_000 = 24 * 60 * 60 * 1000（一天的毫秒数）
    days = int((time.time() * 1000 - mtime_ms) / 86_400_000)
    if days < 1:
        return "today"
    elif days == 1:
        return "yesterday"
    return f"{days} days ago"

def memory_freshness_warning(mtime_ms: float) -> str:
    """如果记忆文件较旧（超过 1 天），返回新鲜度警告文本；近期记忆返回空字符串。

    提醒 Agent 记忆是时间点快照而非实时状态，避免基于过时信息做出错误断言。
    """
    days = int((time.time() * 1000 - mtime_ms) / 86_400_000)
    if days < 1:
        return ""
    elif days == 1:
        return "⚠️ This memory is from yesterday and may be outdated."
    return f"⚠️ This memory is {days} days old and may be outdated."
    
# ─── Semantic Recall (sideQuery) ────────────────────────────

# 语义召回的 System Prompt：指导模型从候选记忆中筛选最相关的文件
SELECT_MEMORIES_PROMPT = """You are selecting memories that will be useful to an AI coding assistant as it processes a user's query. You will be given the user's query and a list of available memory files with their filenames and descriptions.

Return a JSON object with a "selected_memories" array of filenames for the memories that will clearly be useful (up to 5). Only include memories that you are certain will be helpful based on their name and description.
- If you are unsure if a memory will be useful, do not include it.
- If no memories would clearly be useful, return an empty array."""


class RelevantMemory:
    """语义召回选中的记忆文件，包含完整内容和元数据。"""
    __slots__ = ("path", "content", "mtime_ms", "header")

    def __init__(self, path: str, content: str, mtime_ms: float, header: str):
        self.path = path                    # 文件绝对路径
        self.content = content              # 记忆正文内容
        self.mtime_ms = mtime_ms            # 修改时间戳，用于新鲜度标注
        self.header = header                # 格式化的头部文本（包含新鲜度警告）

async def select_relevant_memories(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
) -> list[RelevantMemory]:
    """通过 sideQuery 调用模型进行语义记忆筛选，返回最相关的记忆列表。"""
    headers = scan_memory_headers()
    if not headers:
        return []

    # 去重：过滤已在当前会话中展示过的记忆，避免重复注入
    candidates = [h for h in headers if h.file_path not in already_surfaced]
    if not candidates:
        return []

    manifest = format_memory_manifest(candidates)

    try:
        # 调用 sideQuery 进行语义匹配，由模型判断哪些记忆与查询相关
        text = await side_query(
            SELECT_MEMORIES_PROMPT,
            f"Query: {query}\n\nAvailable memories:\n{manifest}",
        )

        # 从模型响应中提取 JSON 对象（模型可能在 JSON 前后添加解释文本）
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return []

        parsed = json.loads(match.group(0))
        selected_filenames = set(parsed.get("selected_memories", []))

        # 最多只召回 5 条记忆，防止上下文爆炸
        selected = [h for h in candidates if h.filename in selected_filenames][:5]

        result: list[RelevantMemory] = []
        for h in selected:
            try:
                content = Path(h.file_path).read_text(encoding="utf-8")
            except OSError as e:
                logger.debug(f"Failed to read memory file {h.file_path}: {e}")
                continue
            # 单个记忆文件最大 4096 字节，超出部分截断
            if len(content.encode()) > MAX_MEMORY_BYTES_PER_FILE:
                content = content[:MAX_MEMORY_BYTES_PER_FILE] + "\n\n[... truncated, memory file too large ...]"
            # 根据记忆的新鲜度生成头部文本
            freshness = memory_freshness_warning(h.mtime_ms)
            header_text = (
                f"{freshness}\n\nMemory: {h.file_path}:" if freshness
                else f"Memory (saved {memory_age(h.mtime_ms)}): {h.file_path}:"
            )
            result.append(RelevantMemory(
                path=h.file_path, content=content,
                mtime_ms=h.mtime_ms, header=header_text,
            ))
        return result
    except asyncio.CancelledError:
        # 用户取消或超时，静默返回空列表，不阻塞对话流程
        return []
    except (json.JSONDecodeError, OSError) as e:
        # 语义召回失败时优雅降级，返回空列表而非抛出异常
        logger.warning(f"Semantic recall failed: {e}")
        return []
    
# ─── Prefetch Handle ────────────────────────────────────────


class MemoryPrefetch:
    """异步预取句柄，包装 asyncio.Task 以提供轮询接口。"""
    def __init__(self, task: asyncio.Task):
        self.task = task                    # 底层的异步任务
        self.consumed = False               # 标记是否已被消费，防止重复使用

    @property
    def settled(self) -> bool:
        """检查预取任务是否已完成。"""
        return self.task.done()


def start_memory_prefetch(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
    session_memory_bytes: int,
) -> MemoryPrefetch | None:
    """启动异步记忆预取，返回可轮询结果的句柄。

    在用户开始输入时提前异步启动记忆召回，当模型真正需要使用记忆时，结果已准备好。
    """
    # 门控 1：只对多词输入进行预取（单词太模糊，语义匹配效果差）
    if not re.search(r"\s", query.strip()):
        return None

    # 门控 2：会话记忆预算已满则跳过（避免上下文窗口被记忆占满）
    if session_memory_bytes >= MAX_SESSION_MEMORY_BYTES:
        return None

    # 门控 3：记忆目录下没有任何 .md 文件则跳过（避免无意义的 API 调用）
    d = get_memory_dir()
    has_memories = any(f.suffix == ".md" and f.name != "MEMORY.md" for f in d.iterdir())
    if not has_memories:
        return None

    # 创建异步任务，在后台执行语义召回
    task = asyncio.create_task(
        select_relevant_memories(query, side_query, already_surfaced)
    )
    return MemoryPrefetch(task)


def format_memories_for_injection(memories: list[RelevantMemory]) -> str:
    """将召回的记忆格式化为可注入的 user message 内容。"""
    parts = []
    for m in memories:
        # 使用 <system-reminder> 标签告知模型这是系统级上下文而非用户消息
        parts.append(f"<system-reminder>\n{m.header}\n\n{m.content}\n</system-reminder>")
    return "\n\n".join(parts)

