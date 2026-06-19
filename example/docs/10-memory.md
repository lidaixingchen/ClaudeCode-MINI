# 第 10 课：项目级记忆系统

## 🎯 本节目标

为 Agent 构建跨会话的记忆机制（Persistence Memory）。使用散列哈希对项目目录进行空间隔离，实现轻量级 Frontmatter 结构解析和 `MEMORY.md` 索引文件的自动重建。通过 Memory Header 扫描与语义召回（Semantic Recall）实现基于 sideQuery 的智能记忆筛选，利用异步预取（Prefetching）在用户输入时提前拉取相关记忆，并通过 System Prompt 注入指引，使 Agent 能够自主读写记忆目录实现记忆的自我进化。

---

## 🏆 最终效果

完成本节后，Agent 会”认得”你是谁，并且能够跨会话记住项目的一些核心信息：
1. **记忆自我沉淀**：当你向 Agent 提到你的偏好（如：”不要在回复末尾做多余的总结”）时，Agent 会自主决定调用 `write_file`，在后台创建一个名为 `feedback_xxx.md` 的记忆文件。
2. **跨会话感知**：你关闭 Agent 后再次启动，向其发起另一个新任务，Agent 的系统提示词中会自动带入更新后的 `MEMORY.md` 索引。模型读取到先前的记忆，便会在新的回复中自动遵守”不要总结”的习惯。
3. **语义记忆召回**：当用户输入多词查询时，系统会异步调用模型进行语义匹配，自动筛选最相关的记忆文件并注入上下文，无需用户手动请求回忆。

---

## 🛠️ 本节任务

1. **实现项目哈希物理隔离**：在 `memory.py` 中编写 `_project_hash()` 和 `get_memory_dir()`，使每个项目目录都映射到独立的存储路径。
2. **编写 YAML Frontmatter 解析器**：在 `frontmatter.py` 中实现 `FrontmatterResult` 数据类和 `parse_frontmatter`/`format_frontmatter`，供 `memory.py` 和第 11 课的 `skills.py` 共享复用。
3. **实现索引文件自动重建**：实现 `_update_memory_index` 逻辑，在新增记忆时重新生成 `MEMORY.md` 索引。
4. **实现记忆 CRUD 辅助函数**：编写 `save_memory`、`delete_memory`、`_slugify` 等辅助函数，统一记忆文件的增删操作。
5. **实现 Memory Header 扫描**：编写 `MemoryHeader` 类和 `scan_memory_headers`，实现轻量级的元数据快速扫描，为语义召回提供候选列表。
6. **实现语义记忆召回系统**：通过 `select_relevant_memories` 调用 sideQuery 进行智能记忆筛选，实现基于模型的语义匹配。
7. **实现 Memory Prefetch 系统**：编写 `MemoryPrefetch` 类和 `start_memory_prefetch`，在用户输入时异步预取相关记忆。
8. **编译记忆提示词段并织入 Prompt**：实现 `build_memory_prompt_section()`，并在 `prompt.py` 中连通替换 `{{memory}}` 占位符。
9. **实现 sideQuery 构建器**：在 `agent.py` 中编写 `_build_side_query` 方法，为语义召回提供模型调用接口。

---

## 📦 涉及文件

修改：
- `memory.py`
- `prompt.py`
- `agent.py`

创建：
- `frontmatter.py`

---

## 🚀 开始实现

### 步骤 1：基于 CWD 路径进行记忆空间哈希隔离

#### 为什么做

如果用户在本地开发多个不同的项目，我们决不能让 A 项目的上下文记忆混入 B 项目中。我们需要通过计算当前工作目录（CWD）的 SHA-256 散列值，将记忆文件夹物理隔离存放在全局主目录下的 `.mini-claude/projects/{hash}/memory` 中。

#### 做什么

创建（或覆写）`memory.py`，编写路径生成算法：

```python
# memory.py

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from dataclasses import dataclass

# ─── Types ──────────────────────────────────────────────────

VALID_TYPES = {"user", "feedback", "project", "reference"}
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25000


@dataclass
class MemoryEntry:
    """单条记忆的数据模型，用于在内存中表示一条记忆记录。"""
    name: str                          # 记忆的显示名称
    description: str                   # 一句话描述
    type: str                          # 记忆类型（user/feedback/project/reference）
    filename: str                      # 对应的 .md 文件名
    content: str = ""                  # 文件正文内容


# ─── Paths ──────────────────────────────────────────────────


def _project_hash() -> str:
    """基于当前工作目录生成项目哈希值，用于记忆目录的物理隔离。"""
    # 取前 16 位十六进制字符足够唯一，同时避免路径过长
    return hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:16]


def get_memory_dir() -> Path:
    """获取当前项目的记忆存储目录，不存在时自动创建。"""
    d = Path.home() / ".mini-claude" / "projects" / _project_hash() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d
```

#### 注意什么

- **物理隔离的重要性**：利用哈希值做项目目录隔离，可以避免 A 项目的开发上下文或规则泄露到 B 项目中，保护了数据安全，也让 Agent 的记忆具有了精准的范围上下文。

---

### 步骤 2：编写 YAML Frontmatter 轻量解析器

#### 为什么做

为了教大模型以结构化的形式保存记忆（如记忆名字、描述及类型），记忆文件会采用类似 Markdown 博客的 Frontmatter 格式头（夹在 `---` 之间的元数据）。
我们需要手写一个极其紧凑的解析器解析这些元数据，而不需要引入像 `PyYAML` 这样臃肿的第三方依赖。同时，我们需要一个对称的格式化函数，用于将元数据和正文重新组装成标准的 Frontmatter 格式文件。

#### 做什么

首先，创建 `frontmatter.py` 作为独立的共享解析模块（后续第 11 课的技能系统也将复用此模块）：

```python
# frontmatter.py

from dataclasses import dataclass, field


@dataclass
class FrontmatterResult:
    """Frontmatter 解析结果，包含元数据字典和正文内容。"""
    meta: dict[str, str] = field(default_factory=dict)  # YAML 风格的键值对元数据
    body: str = ""                                        # 去除 Frontmatter 后的正文


def parse_frontmatter(content: str) -> FrontmatterResult:
    """从 Markdown 内容中解析 YAML Frontmatter 元数据块。

    格式约定：文件以 --- 开头和结尾包裹元数据，中间为 key: value 键值对。
    """
    lines = content.split("\n")
    # 第一行必须是 ---，否则认为没有 Frontmatter
    if not lines or lines[0].strip() != "---":
        return FrontmatterResult(body=content)

    # 查找结束标记 ---
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    # 未找到闭合标记，返回原始内容作为正文
    if end_idx == -1:
        return FrontmatterResult(body=content)

    # 逐行解析 key: value 格式的元数据
    meta: dict[str, str] = {}
    for i in range(1, end_idx):
        colon = lines[i].find(":")
        if colon == -1:
            continue
        key = lines[i][:colon].strip()
        value = lines[i][colon + 1:].strip()
        if key:
            meta[key] = value

    # 跳过结束标记，重组正文部分
    body = "\n".join(lines[end_idx + 1:]).strip()
    return FrontmatterResult(meta=meta, body=body)


def format_frontmatter(meta: dict[str, str], body: str) -> str:
    """将元数据字典和正文内容格式化为标准的 Frontmatter 文件格式。"""
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)
```

然后，在 `memory.py` 中引入解析器：

```python
# memory.py（续）

from frontmatter import parse_frontmatter, format_frontmatter
```

#### 注意什么

- **无依赖解析器**：通过手写 YAML 键值对行切割与正则，可以极大节省包体积，避免了初学者拉取项目时必须安装 PyYAML 第三方包的步骤，降低了启动成本。
- **对称的读写设计**：`parse_frontmatter` 负责"读"（从文本提取元数据），`format_frontmatter` 负责"写"（从元数据生成文本）。这种对称设计使得记忆文件的创建和解析使用完全一致的格式约定，避免了格式漂移。

注意：`parse_frontmatter` 返回 `FrontmatterResult` 数据类，通过 `.meta` 和 `.body` 属性访问元数据与正文。后续所有调用方（包括第 11 课的技能系统）都统一使用点操作符访问。

---

### 步骤 3：实现索引文件 `MEMORY.md` 自动重建

#### 为什么做

Agent 在提问时，如果一次性将所有的记忆文件全部发给 API 会极大耗费 Token。
- 我们的设计策略是：在记忆目录下维护一个唯一的索引文件 `MEMORY.md`。
- 每次写盘新记忆后，系统会自动扫描目录下所有的 `.md` 文件，读取元数据，并在 `MEMORY.md` 写入结构化目录。
- 这样，系统启动时只需将 `MEMORY.md` 索引注入 System Prompt。Agent 可以通过索引知晓自己存了哪些记忆，当需要具体细节时，通过 `read_file` 主动读取对应文件。

#### 做什么

在 `memory.py` 中编写 `MEMORY.md` 自动重建与检索函数，以及记忆的增删辅助函数：

```python
# memory.py（续）

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Any

logger = logging.getLogger(__name__)

# sideQuery 的类型签名：异步函数，接收 system prompt 和 user message，返回模型生成的文本
# 实际类型为 async (system: str, user_message: str) -> str
SideQueryFn = Callable[[str, str], Any]  # 实际返回 Awaitable[str]


# ─── Slugify ────────────────────────────────────────────────


def _slugify(text: str) -> str:
    """将文本转换为 URL 友好的 slug 格式，用于生成记忆文件名。"""
    # 替换所有非字母数字字符为下划线，截断到 40 字符避免文件名过长
    s = re.sub(r"[^a-z0-9]+", "_", text.lower())
    s = s.strip("_")
    return s[:40]


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
        except (OSError, ValueError) as e:
            logger.debug(f"Skipping memory file {f}: {e}")
    # 按文件修改时间降序排列，最新的记忆排在前面
    entries.sort(key=lambda e: (d / e.filename).stat().st_mtime, reverse=True)
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
    filepath = get_memory_dir() / filename
    if not filepath.exists():
        return False
    filepath.unlink()
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
        return ""
    content = index_path.read_text(encoding="utf-8")
    lines = content.split("\n")
    # 行数截断：最多保留 200 行
    if len(lines) > MAX_INDEX_LINES:
        content = "\n".join(lines[:MAX_INDEX_LINES]) + "\n\n[... truncated, too many memory entries ...]"
    # 字节数截断：最多保留 25000 字节
    if len(content.encode()) > MAX_INDEX_BYTES:
        content = content[:MAX_INDEX_BYTES] + "\n\n[... truncated, index too large ...]"
    return content
```

#### 注意什么

- **类型 Fallback 机制**：在 `list_memories()` 中，当记忆文件的 `type` 字段不在 `VALID_TYPES` 集合中时，我们不会直接跳过该文件，而是将其 fallback 为 `"project"` 类型。这保证了用户手写的记忆文件（即使类型拼写不标准）仍能被索引收录，而非被静默丢弃。
- **数据类型一致性**：在 `list_memories()` 中，我们必须返回定义好的 `MemoryEntry` 对象列表，并在 `_update_memory_index` 中以点号属性访问字段（如 `m.name`），这能保证与第 5 课所实现的 REPL 快捷查看命令 `/memory` （以对象属性方式遍历打印记忆）在数据结构类型上完全一致，避免了在 REPL 下调用命令抛出 `AttributeError` 崩溃。
- **健全的错误处理与日志记录**：在扫描项目目录、读取和解析 YAML Frontmatter 记忆文件时，由于文件可能被外部程序占用、甚至被用户修改损坏，我们需要精准捕获 `OSError` 和 `ValueError`，并通过 `logger.debug` 记录具体的跳过原因，避免异常被无声隐藏，方便后期维护。
- **索引截断保护**：`load_memory_index` 会对索引进行行数（`MAX_INDEX_LINES = 200`）和字节数（`MAX_INDEX_BYTES = 25000`）双重截断，防止极端情况下索引膨胀导致 System Prompt 爆炸。

---

### 步骤 4：实现 Memory Header 轻量扫描与新鲜度追踪

#### 为什么做

语义召回系统需要快速获取所有记忆文件的元数据（类型、描述、修改时间），但不需要读取完整内容。如果每次都读取全部文件内容，当记忆文件较多时会造成严重的性能问题。我们需要一个轻量级的扫描机制，只读取每个文件的前 30 行（Frontmatter 区域），快速构建候选列表。

#### 做什么

在 `memory.py` 中编写 `MemoryHeader` 类和相关扫描/格式化函数：

```python
# memory.py（续）

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
    for f in d.glob("*.md"):
        # 跳过索引文件自身
        if f.name == "MEMORY.md":
            continue
        try:
            stat = f.stat()
            raw = f.read_text(encoding="utf-8")
            # 只读前 30 行，足以覆盖 Frontmatter 区域（通常在 10 行以内）
            first30 = "\n".join(raw.split("\n")[:30])
            result = parse_frontmatter(first30)
            meta = result.meta
            t = meta.get("type")
            headers.append(MemoryHeader(
                filename=f.name,
                file_path=str(f),
                # 转换为毫秒级时间戳，便于精确排序和比较
                mtime_ms=stat.st_mtime * 1000,
                description=meta.get("description"),
                type=t if t in VALID_TYPES else None,
            ))
        except (OSError, ValueError) as e:
            logger.debug(f"Skipping memory file {f}: {e}")
    # 按修改时间降序排列，最新的记忆优先展示
    headers.sort(key=lambda h: h.mtime_ms, reverse=True)
    # 截断到最大文件数限制
    return headers[:MAX_MEMORY_FILES]


def format_memory_manifest(headers: list[MemoryHeader]) -> str:
    """将记忆头信息格式化为语义选择器可读的清单，每条记忆一行。"""
    lines = []
    for h in headers:
        tag = f"[{h.type}] " if h.type else ""
        ts = datetime.fromtimestamp(h.mtime_ms / 1000, tz=timezone.utc).isoformat()
        if h.description:
            lines.append(f"- {tag}{h.filename} ({ts}): {h.description}")
        else:
            lines.append(f"- {tag}{h.filename} ({ts})")
    return "\n".join(lines)


# ─── Memory Age / Freshness ────────────────────────────────


def memory_age(mtime_ms: float) -> str:
    """将毫秒级时间戳转换为人类可读的相对时间描述。"""
    # 86_400_000 = 24 * 60 * 60 * 1000（一天的毫秒数）
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def memory_freshness_warning(mtime_ms: float) -> str:
    """如果记忆文件较旧（超过 1 天），返回新鲜度警告文本；近期记忆返回空字符串。

    提醒 Agent 记忆是时间点快照而非实时状态，避免基于过时信息做出错误断言。
    """
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days <= 1:
        return ""
    return (f"This memory is {days} days old. Memories are point-in-time observations, "
            "not live state — claims about code behavior may be outdated. "
            "Verify against current code before asserting as fact.")
```

#### 注意什么

- **只读前 30 行**：`scan_memory_headers` 通过 `"\n".join(raw.split("\n")[:30])` 只读取文件前 30 行，这足以覆盖 Frontmatter 区域（通常在 10 行以内），大幅减少 I/O 开销。
- **`__slots__` 优化**：`MemoryHeader` 使用 `__slots__` 声明固定属性，相比普通 `dataclass` 可节省约 40% 的内存占用，当记忆文件多达 200 个时效果显著。
- **新鲜度警告机制**：`memory_freshness_warning` 会对超过 1 天的记忆发出警告，提醒 Agent 该记忆是"时间点快照"而非"实时状态"，避免 Agent 基于过时信息做出错误断言。

---

### 步骤 5：实现语义记忆召回系统（Semantic Recall）

#### 为什么做

仅仅把 `MEMORY.md` 索引注入 System Prompt 是不够的——当记忆文件很多时，Agent 可能无法从索引中准确判断哪些记忆与当前查询相关。我们需要一个"语义召回"机制：通过调用模型进行一次轻量级 sideQuery，让模型从候选记忆列表中筛选出最相关的文件，然后只将这些相关记忆注入上下文。

这是 Claude Code 记忆系统的核心能力：**不是暴力加载所有记忆，而是智能筛选相关记忆**。

#### 做什么

在 `memory.py` 中编写语义召回系统：

```python
# memory.py（续）

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
        selected = [h for h in candidates if h.file_path in selected_filenames][:5]

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
```

#### 注意什么

- **sideQuery 的设计**：`select_relevant_memories` 接收一个 `SideQueryFn` 参数，这是一个 `async (system, user_message) -> str` 签名的可调用对象。这种设计使得语义召回可以与任何后端（Anthropic/OpenAI）解耦，只需在 `agent.py` 中构建对应的 sideQuery 函数即可。
- **去重机制**：`already_surfaced` 参数记录了已经在当前会话中展示过的记忆文件路径，避免同一记忆被重复注入上下文，造成 Token 浪费。
- **渐进式加载**：最多只召回 5 条记忆（`[:5]` 截断），且单个文件最大 4096 字节，双重限制确保不会因记忆召回导致上下文爆炸。
- **优雅降级**：当 sideQuery 失败（JSON 解析错误、网络超时、文件读取失败）时，系统返回空列表而非抛出异常，确保语义召回的失败不会阻塞正常的对话流程。
- **新鲜度标注**：每条被召回的记忆都会附带时间信息（"saved today" 或 "This memory is N days old..."），帮助 Agent 判断记忆的时效性。

---

### 步骤 6：实现 Memory Prefetch 异步预取系统

#### 为什么做

语义召回需要调用模型进行 sideQuery，这会产生一次额外的 API 请求延迟。如果等到用户消息处理时才开始召回，会显著增加响应时间。我们需要一个"预取"机制：在用户开始输入时就异步启动记忆召回，当模型真正需要使用记忆时，结果已经准备好了。

#### 做什么

在 `memory.py` 中编写 `MemoryPrefetch` 类和预取函数：

```python
# memory.py（续）

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
```

#### 注意什么

- **三重门控机制**：`start_memory_prefetch` 在启动预取前会检查三个条件——查询是否包含多词（单词太模糊）、会话记忆预算是否已满、目录下是否有记忆文件。这避免了无意义的 API 调用开销。
- **`<system-reminder>` 包裹**：`format_memories_for_injection` 使用 `<system-reminder>` 标签包裹每条记忆，这告诉模型这些内容是系统级的上下文信息而非用户消息，有助于模型正确理解记忆的语义角色。
- **`consumed` 标记**：`MemoryPrefetch` 的 `consumed` 字段用于追踪预取结果是否已被消费，防止同一预取结果被多次使用。
- **预算控制**：`MAX_SESSION_MEMORY_BYTES = 60 * 1024`（60KB）限制了单个会话中注入的记忆总量，超过预算后不再进行新的预取，防止上下文窗口被记忆内容占满。

---

### 步骤 7：编译记忆提示词段并织入 System Prompt

#### 为什么做

这是促成”记忆自我进化”的魔法：**我们不需要为记忆设计任何特有工具**。
我们只需在 System Prompt 中告诉大模型：”你拥有一个位于 `{dir}` 的记忆系统，可以使用已有的 `write_file`/`edit_file` 工具往里面写文件来记住偏好；并向其展示当前的 `MEMORY.md` 索引”。模型就会自主决定什么时候需要写文件来”记住”某些规则！

#### 做什么

1. 在 `memory.py` 末尾编写 `build_memory_prompt_section`。
2. 修改 `prompt.py`，引入该方法并在 `build_system_prompt` 中替换 `{{memory}}` 占位符。

```python
# memory.py（续）


# ─── System prompt section ──────────────────────────────────


def build_memory_prompt_section() -> str:
    “””编译记忆系统的 System Prompt 段落，注入到主提示词中。

    包含记忆类型说明、写入格式、当前索引等内容，
    引导大模型自主决定何时写文件来”记住”用户偏好。
    “””
    index = load_memory_index()
    memory_dir = str(get_memory_dir())

    return f”””# Memory System

You have a persistent, file-based memory system at `{memory_dir}`.

## Memory Types
- **user**: User's role, preferences, knowledge level
- **feedback**: Corrections and guidance from the user (include Why + How to apply)
- **project**: Ongoing work, goals, deadlines, decisions
- **reference**: Pointers to external resources (URLs, tools, dashboards)

## How to Save Memories
Use the write_file tool to create a memory file with YAML frontmatter:

\\`\\`\\`markdown
---
name: memory name
description: one-line description
type: user|feedback|project|reference
---
Memory content here.
\\`\\`\\`

Save to: `{memory_dir}/`
Filename format: `{{type}}_{{slugified_name}}.md`

The MEMORY.md index is auto-updated when you write to the memory directory — do NOT update it manually.

## What NOT to Save
- Code patterns or architecture (read the code instead)
- Git history (use git log)
- Anything already in CLAUDE.md
- Ephemeral task details

## When to Recall
When the user asks you to remember or recall, or when prior context seems relevant.
{chr(10) + “## Current Memory Index” + chr(10) + index if index else chr(10) + “(No memories saved yet.)”}”””
```

在 `prompt.py` 中更新引入和替换：

> ⚠️ **别忘了回到 `prompt.py` 接线！** 你需要做两件事：
>
> 1. 文件顶部添加 `from memory import build_memory_prompt_section`
> 2. 在 `build_system_prompt()` 函数中调用它，将返回值替换 `{{memory}}` 占位符

```python
# prompt.py 中的修改

# 1. 引入记忆编译函数
from memory import build_memory_prompt_section

# ...

def build_system_prompt() -> str:
    “””构建完整的 System Prompt，替换所有模板占位符。”””
    replacements = {
        “{{cwd}}”: str(Path.cwd()),
        “{{date}}”: date.today().isoformat(),
        “{{platform}}”: f”{platform.system()} {platform.machine()}”,
        “{{shell}}”: os.environ.get(“SHELL”) or os.environ.get(“COMSPEC”) or “unknown”,
        “{{git_context}}”: get_git_context(),
        “{{claude_md}}”: load_claude_md(),
        # 2. 替换记忆占位符为编译后的记忆提示词段
        “{{memory}}”: build_memory_prompt_section(),
    }
    # ... 后续替换逻辑保持不变
```

#### 注意什么

- **大模型自主记忆驱动**：在编译记忆提示词时，我们告诉大模型记忆格式和路径，由大模型在主循环决策中”自主”写文件来记住用户偏好。
- **反馈记忆的双要素**：`feedback` 类型记忆特别要求包含 “Why + How to apply”，这确保 Agent 不仅记住”用户说了什么”，还理解”为什么这么说”以及”如何在未来应用这条规则”，避免机械式记忆导致的规则误用。

---

### 步骤 8：实现 sideQuery 构建器（agent.py）

#### 为什么做

语义召回系统需要一个 `SideQueryFn` 可调用对象来执行轻量级模型调用。这个函数需要支持 Anthropic 和 OpenAI 两种后端，并且使用较小的 `max_tokens`（256）以减少延迟和成本。我们在 `agent.py` 中构建这个函数，使语义召回可以与主对话循环解耦。

#### 做什么

在 `agent.py` 中编写 `_build_side_query` 方法：

```python
# agent.py（续）

from typing import Callable, Awaitable


class Agent:
    # ... 前面的代码保持不变 ...

    # ─── Side query for memory recall ─────────────────────────

    def _build_side_query(self) -> Callable[[str, str], Awaitable[str]] | None:
        “””构建 sideQuery 可调用对象，用于语义记忆召回。

        通过闭包捕获对应的客户端实例，返回签名统一的异步函数，
        调用方无需关心底层使用的是 Anthropic 还是 OpenAI API。
        “””
        if self._anthropic_client:
            client = self._anthropic_client
            model = self.config.model

            async def _sq(system: str, user_message: str) -> str:
                # max_tokens=256 限制输出长度，语义召回只需返回简短的 JSON 对象
                resp = await client.messages.create(
                    model=model, max_tokens=256, system=system,
                    messages=[{“role”: “user”, “content”: user_message}],
                )
                # Anthropic 后端需要从 content 中筛选 type == “text” 的块并拼接
                return “”.join(b.text for b in resp.content if b.type == “text”)
            return _sq
        if self._openai_client:
            client = self._openai_client
            model = self.config.model

            async def _sq_oai(system: str, user_message: str) -> str:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {“role”: “system”, “content”: system},
                        {“role”: “user”, “content”: user_message},
                    ],
                )
                # OpenAI 后端直接从 choices[0].message.content 获取
                return resp.choices[0].message.content or “” if resp.choices else “”
            return _sq_oai
        # 未配置任何 API 客户端时返回 None，调用方需检查后跳过语义召回
        return None
```

#### 注意什么

- **双后端适配**：`_build_side_query` 通过闭包捕获对应的客户端实例（`_anthropic_client` 或 `_openai_client`），返回一个签名统一的异步函数。调用方无需关心底层使用的是哪个 API。
- **轻量级调用**：`max_tokens=256` 限制了 sideQuery 的输出长度，因为语义召回只需返回一个简短的 JSON 对象，不需要长篇大论。这既降低了延迟，也节省了 API 成本。
- **文本提取差异**：Anthropic 后端需要从 `resp.content` 中筛选 `type == “text”` 的块并拼接；OpenAI 后端直接从 `resp.choices[0].message.content` 获取。两种实现都做了防御性处理（空值检查）。
- **返回 None 的情况**：当没有配置任何 API 客户端时，方法返回 `None`。调用方需要检查返回值，如果为 `None` 则跳过语义召回。

---

## ⚖️ 设计权衡

### 零工具的“寄生”文件读写 vs 引入专门的 MemoryTool

- **方案 A**：**“寄生”文件读写**（我们所用）
  - 记忆就是普通的文件。大模型使用已有的 `write_file`/`edit_file` 和 `read_file` 读写记忆。
  - **优点**：不需要编写任何新工具，降低了工具池的 Token 开销；且用户可以直接进入 `.mini-claude/projects/` 目录下用本地编辑器（VS Code/Vim）增删改记忆。
  - **缺点**：大模型需要先理解文件写入格式，写入的规范性依赖 System Prompt 的指引强度。
- **方案 B**：**引入定制的 MemoryTool 工具**
  - 在 API 中暴露类似 `save_memory(key, val)` 的强约束工具。
  - **优点**：格式强制统一。
  - **缺点**：用户无法轻易查看或离线修改；增加了额外的工具参数开销。

**结论**：方案 A 将记忆巧妙地“归一化”为文件读写，最完美地践行了“Unix 哲学：一切皆文件”的设计思想。

---

## ⚠️ 常见陷阱

### 1. `MEMORY.md` 索引自身没有被屏蔽

```python
# ❌ 错误：在 list_memories 中没有跳过 MEMORY.md 自身
for f in d.glob("*.md"):
    # 会把 MEMORY.md 的数据也读出来加入它自己，造成无限套娃
```

**修正**：在 glob 遍历时，必须写明 `if f.name == "MEMORY.md": continue` 予以过滤。

---

### 2. 记忆没有按照项目做沙箱隔离

如果简单把所有项目的记忆混着存在全局 `~/.mini-claude/memory/` 目录下，一旦大模型在前端项目 A 下检索到后端项目 B 的命名规范或文件规则，就会产生严重的牛头不对马嘴的逻辑干扰。

**修正**：必须在路径中引入当前工作目录的 hash 值进行强物理隔离。

---

## ✅ 验收点

### 输入与验证

1. 启动 Agent，向其告知一条你的角色和习惯偏好：
   ```bash
   python -m mini_claude "记住：我叫小明，负责前端 React 重构工作"
   ```
2. **观察工具调用**：大模型在返回时，应当自主产生了一次 `write_file` 的调用，在你的后台写入了一个以 `user_` 或 `project_` 开头的记忆 md 文件。
3. **验证持久化**：退出程序。
4. 在另一目录或者直接在当前目录下重新启动 Agent：
   ```bash
   python -m mini_claude "你是谁？当前项目负责人是谁？"
   ```

### 预期结果

大模型能够准确复述出：“我是 Mini Claude Code。当前项目的负责人是小明，他负责前端 React 重构。”

---

## 🧠 思考题

1. **既然我们在 `build_memory_prompt_section` 中只把索引文件 `MEMORY.md` 编译进了 System Prompt，大模型怎么知道每条记忆的具体详细内容呢？**
   *(提示：索引中有类似 `-[不总结](feedback_no_summary.md)` 的格式。模型看到文件名后，当需要查阅“如何不总结”的详情时，会自主发起一次 `read_file` 读取该文件的命令获取内容，实现了“按需拉取”的懒加载模式。)*
2. **为什么我们把 `What NOT to Save`（不要保存什么）加入到了记忆系统的 System Prompt 中？大模型会胡乱保存什么？**
   *(提示：如果不加以限制，大模型倾向于将之前的整个代码结构、Git 记录、历史代码片全部当成记忆存盘，这会导致记忆目录短时间内发生文件大爆炸。)*

---

## 📦 本节收获

1. **一切皆文件的记忆哲学**：理解了在 Agent 系统中通过 Prompt 引导，将记忆维护降维到已有文件工具上的巧妙设计。
2. **多租户空间隔离**：掌握了利用 CWD 哈希实现本地多项目数据沙箱物理隔离的开发模式。
3. **按需加载（Lazy Loading）索引**：掌握了”只曝露索引，按需加载内容”的 Token 减负与缓存友好型架构方案。
4. **语义记忆召回（Semantic Recall）**：掌握了通过 sideQuery 调用模型进行智能记忆筛选的核心机制，理解了”不是暴力加载所有记忆，而是智能筛选相关记忆”的设计思想。
5. **异步预取（Prefetching）**：理解了通过 `asyncio.create_task` 在用户输入时提前异步启动记忆召回，以及三重门控机制（多词检查、预算控制、文件存在性）如何避免无意义的 API 调用开销。
6. **Memory Header 轻量扫描**：掌握了只读取文件前 30 行 Frontmatter 的快速扫描策略，以及 `__slots__` 内存优化技巧。

---

> **下一章**：现在 Agent 具备了跨会话记忆。下一步我们将实现技能系统——允许 Agent 发现并运行用户或团队定制的 Prompt 工作流。
