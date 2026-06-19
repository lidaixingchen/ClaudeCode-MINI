# 第 04 课：System Prompt 动态编译

## 🎯 本节目标

为 Agent 动态编译一个结构严密的 System Prompt。使其在运行时能够根据当前环境（包括工作目录、操作系统、Git 状态以及项目指令文件 `CLAUDE.md`）自动调整提示词，并为大模型接种“反模式疫苗”（防止模型过度工程和盲目修改代码）。

---

## 🏆 最终效果

完成本节后，当你在不同目录下、或在不同的 Git 分支中运行 Agent 时，Agent 发送给大模型的 System Prompt 将会动态更新。

你可以通过添加测试代码打印编译后的 Prompt，或者直接观察 Agent 的行为：
- 当项目根目录下存在 `CLAUDE.md` 时，大模型能自动读取其中的项目规范并严格遵守。
- 当有 Git 未提交的文件时，大模型能清晰知道哪些代码正处于修改中，避免盲目重置你的本地工作区。

---

## 🛠️ 本节任务

1. **定义基础 System Prompt 模板**：编写 `SYSTEM_PROMPT_TEMPLATE`，融入反模式疫苗和工具偏好表。
2. **收集环境与 Git 上下文**：实现 `get_git_context()`，通过子进程获取 Git 分支、最近提交以及状态。
3. **实现项目规则加载与 `@include` 解析**：实现 `load_claude_md()`、`_resolve_includes()` 与 `_load_rules_dir()`，支持向上递归查找项目规范并从 `.claude/rules/` 加载模块化规则文件。
4. **编译与路由替换**：实现 `build_system_prompt()` 替换占位符。
5. **在 Agent 中应用 Prompt**：修改 `agent.py`，将编译后的 System Prompt 注入 API 请求。

---

## 📦 涉及文件

修改：
- `prompt.py`
- `agent.py`

---

## 🚀 开始实现

### 步骤 1：定义嵌入式 System Prompt 模板

#### 为什么做

静态提示词是 Agent 行为的基石。我们需要在此模板中定义 Agent 的身份，接种“反模式疫苗”（如“不要过早抽象”、“不要在未修改的代码中加注释”、“不要做未要求的重构”），并确立工具偏好表（强制大模型优先使用专用的 `read_file`/`edit_file` 而非通过 `run_shell` 调用命令行命令）。

#### 做什么

创建（或覆盖）`prompt.py`，嵌入基础模板及占位符：

```python
# prompt.py

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from pathlib import Path

# ─── 嵌入的 System Prompt 模板 ──────────────────────
# 包含核心行为限制（反模式疫苗）、工具偏好表和环境占位符
SYSTEM_PROMPT_TEMPLATE = """\
You are Mini Claude Code, a lightweight coding assistant CLI.
You are an interactive agent that helps users with software engineering tasks.

# Doing tasks
 - Avoid over-engineering. Only make changes that are directly requested or clearly necessary. Keep solutions simple and focused.
   - Don't add features, refactor code, or make "improvements" beyond what was asked. A bug fix doesn't need surrounding code cleaned up.
   - Don't add docstrings, comments, or type annotations to code you didn't change.
   - Don't create helpers, utilities, or abstractions for one-time operations. Three similar lines of code is better than a premature abstraction.
 - In general, do not propose changes to code you haven't read. If a user asks about or wants you to modify a file, read it first.

# Using your tools
 - Do NOT use run_shell to run commands when a relevant dedicated tool is provided. Using dedicated tools allows the user to better review your work:
   - To read files use read_file instead of cat, head, or tail
   - To edit files use edit_file instead of sed or awk
   - To create files use write_file instead of echo redirection
   - To search for files use list_files instead of find or ls

# Environment
Working directory: {{cwd}}
Date: {{date}}
Platform: {{platform}}
Shell: {{shell}}
{{git_context}}
{{claude_md}}"""
```

#### 注意什么

- 工具偏好表非常关键。若无显式要求，大模型会默认调用它最熟悉的命令行命令（如 `cat`、`sed`），这将导致我们在第 3 课实现的精确编辑工具失去用武之地。

---

### 步骤 2：收集 Git 上下文信息

#### 为什么做

让 Agent 了解当前的 Git 状态（所处分支、最近提交记录、未暂存的文件）。这些信息能让大模型了解项目的最新开发进展，防止在写代码时与已有的未提交修改发生冲突。

#### 做什么

在 `prompt.py` 中编写 `get_git_context` 函数，利用 `subprocess` 执行 git 命令并捕获其输出：

```python
# prompt.py（续）


# 获取当前 Git 仓库的分支、最近提交和文件状态信息
def get_git_context() -> str:
    try:
        # 通用子进程参数：UTF-8 编码、3 秒超时、捕获标准输出
        opts = {"encoding": "utf-8", "timeout": 3, "capture_output": True}
        # 获取当前分支名
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], **opts).stdout.strip()
        # 获取最近 5 次提交的单行日志
        log = subprocess.run(["git", "log", "--oneline", "-5"], **opts).stdout.strip()
        # 获取文件变更的简短状态
        status = subprocess.run(["git", "status", "--short"], **opts).stdout.strip()

        # 拼接所有 Git 信息为一个文本块
        result = f"\nGit branch: {branch}"
        if log:
            result += f"\nRecent commits:\n{log}"
        if status:
            result += f"\nGit status:\n{status}"
        return result
    except Exception:
        # 非 Git 仓库或未安装 Git 时返回空，保证鲁棒性
        return ""
```

#### 注意什么

- 调用外部命令必须添加超时机制（`timeout=3`），防止由于 Git 挂起或命令卡死导致编译 System Prompt 过程永久阻塞。

---

### 步骤 3：项目级规则加载与 `@include` 引用解析

#### 为什么做

Claude Code 项目的一个核心特色是 `CLAUDE.md`（项目级指令集，用于写明项目的构建、测试、代码规范命令）。
为了让规则生效，我们需要：
1. 从当前工作目录（CWD）开始，不断向上级目录递归检索 `CLAUDE.md` 文件并加载，离当前目录最近的规则优先级最高。
2. 支持 `@` 语法（例如 `@./style.md`）导入子文件，从而实现配置文件的模块化管理。

#### 做什么

在 `prompt.py` 中实现三个关键函数：`@include` 语法递归解析 `_resolve_includes`、从 `.claude/rules/` 目录加载模块化规则的 `_load_rules_dir`、以及向上递归查找 `CLAUDE.md` 的 `load_claude_md`：

```python
# prompt.py（续）

# 匹配 @./path、@~/path、@/path 格式的 include 指令
_INCLUDE_RE = re.compile(r"^@(\./[^\s]+|~/[^\s]+|/[^\s]+)$", re.MULTILINE)
_MAX_INCLUDE_DEPTH = 5  # 防止恶性嵌套


# 递归解析 @include 引用，将被引入文件的内容内联替换到当前文本中
def _resolve_includes(
    content: str,
    base_path: Path,
    visited: set[str] | None = None,
    depth: int = 0,
) -> str:
    if depth >= _MAX_INCLUDE_DEPTH:
        return content
    if visited is None:
        visited = set()

    def _replace(m: re.Match) -> str:
        raw = m.group(1)
        # 兼容相对路径、用户主目录和绝对路径
        if raw.startswith("~/"):
            resolved = Path.home() / raw[2:]
        elif raw.startswith("/"):
            resolved = Path(raw)
        else:
            resolved = base_path / raw

        resolved = resolved.resolve()
        key = str(resolved)
        # 检测循环引用，防止递归死循环
        if key in visited:
            return f"<!-- circular include: {raw} -->"
        if not resolved.is_file():
            return f"<!-- file not found: {raw} -->"

        try:
            visited.add(key)
            included = resolved.read_text(encoding="utf-8")
            # 递归处理被引入文件的内部引用
            return _resolve_includes(included, resolved.parent, visited, depth + 1)
        except Exception:
            return f"<!-- error reading: {raw} -->"

    return _INCLUDE_RE.sub(_replace, content)


# 从 .claude/rules/ 目录加载所有 .md 规则文件，支持模块化规则管理
def _load_rules_dir(directory: Path) -> str:
    # 定位 .claude/rules/ 目录
    rules_dir = directory / ".claude" / "rules"
    if not rules_dir.is_dir():
        return ""
    try:
        # 只加载 .md 文件并按文件名排序，保证加载顺序一致
        files = sorted(f for f in rules_dir.iterdir() if f.suffix == ".md" and f.is_file())
        if not files:
            return ""
        parts: list[str] = []
        for f in files:
            try:
                content = f.read_text(encoding="utf-8")
                # 规则文件内部也支持 @include 语法
                content = _resolve_includes(content, rules_dir)
                # 用 HTML 注释标记来源文件名，便于调试
                parts.append(f"<!-- rule: {f.name} -->\n{content}")
            except Exception:
                pass
        # 所有规则合并为一个 section 返回
        return "\n\n## Rules\n" + "\n\n".join(parts) if parts else ""
    except Exception:
        return ""


# 从当前目录向上递归查找所有 CLAUDE.md 文件，合并为项目级指令集
def load_claude_md() -> str:
    parts: list[str] = []
    d = Path.cwd().resolve()
    # 向上递归查找所有的 CLAUDE.md
    while True:
        f = d / "CLAUDE.md"
        if f.is_file():
            try:
                content = f.read_text(encoding="utf-8")
                # 解析其中的 @include 内容
                content = _resolve_includes(content, d)
                parts.insert(0, content)  # 父级目录的规则插在前面，近因效应使其在后方覆盖
            except Exception:
                pass
        parent = d.parent
        if parent == d:
            break
        d = parent

    # 加载 .claude/rules/ 目录下的模块化规则文件
    rules = _load_rules_dir(Path.cwd())

    # 拼接 CLAUDE.md 部分和 rules 目录部分
    claude_md = ""
    if parts:
        claude_md = "\n\n# Project Instructions (CLAUDE.md)\n" + "\n\n---\n\n".join(parts)
    return claude_md + rules
```

#### 注意什么

- **死循环防护**：引入 `visited` 集合记录已被读取的绝对路径。如果 A 引入 B，B 又引入 A，能防止递归调用栈爆掉。

- **模块化规则目录**：`_load_rules_dir` 从 `.claude/rules/` 目录加载所有 `.md` 文件，实现了比单个 `CLAUDE.md` 更灵活的模块化规则管理。规则文件按文件名排序加载，支持在 `CLAUDE.md` 中通过 `@./style.md` 引入规则文件，也支持规则文件之间相互引用。

- **`load_claude_md` 的双重加载**：该函数同时收集 `CLAUDE.md` 向上递归的内容和 `.claude/rules/` 目录的内容，最终合并返回。`CLAUDE.md` 部分以 `# Project Instructions (CLAUDE.md)` 为标题，`rules` 部分以 `## Rules` 为标题，两者拼接在一起。

---

### 步骤 4：编译提示词主函数 `build_system_prompt`

#### 为什么做

最后，我们将所有静态和动态信息结合起来，用收集到的数据替换模板中的 `{{placeholder}}` 占位符，生成最终发送给 API 的完整提示词。

#### 做什么

在 `prompt.py` 末尾编写 `build_system_prompt`：

```python
# prompt.py（续）

from datetime import date


# 编译完整的 System Prompt：将动态上下文替换到静态模板中
def build_system_prompt() -> str:
    # 收集所有动态上下文，构建占位符替换映射
    replacements = {
        "{{cwd}}": str(Path.cwd()),                              # 当前工作目录
        "{{date}}": date.today().isoformat(),                    # 今天的日期
        "{{platform}}": f"{platform.system()} {platform.machine()}",  # 操作系统和架构
        "{{shell}}": os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown",  # Shell 类型
        "{{git_context}}": get_git_context(),                    # Git 状态信息
        "{{claude_md}}": load_claude_md(),                       # 项目级指令（CLAUDE.md + rules）
    }
    # 逐个替换模板中的 {{placeholder}} 为实际值
    result = SYSTEM_PROMPT_TEMPLATE
    for key, value in replacements.items():
        result = result.replace(key, value)
    return result
```

#### 注意什么

- **教学简化与源码差异**：本教程使用 6 个占位符的精简模板（`cwd`/`date`/`platform`/`shell`/`git_context`/`claude_md`），不需要任何跨模块导入。但源码 `prompt.py` 的 `build_system_prompt()` 实际使用 **10 个占位符**，额外包含 `{{memory}}`、`{{skills}}`、`{{agents}}`、`{{deferred_tools}}`，因此源码文件顶部有 4 条额外导入：

  ```python
  # 源码 prompt.py 顶部的完整导入（本教程暂不需要）
  from .memory import build_memory_prompt_section   # 第 10 课实现
  from .skills import build_skill_descriptions       # 第 11 课实现
  from .subagent import build_agent_descriptions     # 第 14 课实现
  from .tools import get_deferred_tool_names         # 延迟加载工具名
  ```

  这些模块分别在第 10、11、14 课中逐步创建。在学习到对应章节时，再回来补齐 `prompt.py` 中的相应导入、占位符和调用即可。现阶段只需保证 6 个占位符能正确替换、`build_system_prompt()` 可被 `agent.py` 正常调用。

- **`from datetime import date` 放在文件中间而非顶部**：源码将 `from datetime import date` 写在 `build_system_prompt()` 函数体内部（延迟导入），这里为了简洁直接放在函数前。两种写法均可运行，不影响功能。

---

### 步骤 5：在 Agent 请求中注入编译后的 Prompt

#### 为什么做

虽然编写了 `build_system_prompt`，但我们还需要在 `agent.py` 的 API 调用中实际使用它。

#### 做什么

修改 `agent.py`。
1. 在文件头部导入 `build_system_prompt`。
2. 在调用大模型时，动态生成系统提示词，并分别传入 Anthropic（通过 `system` 参数）和 OpenAI 后端（通过插入到 messages 首位的 `system` 角色消息）。

```python
# agent.py 中的修改

# 1. 导入编译函数
from .prompt import build_system_prompt

# ... 


# 2. 修改 _chat_anthropic
async def _chat_anthropic(self, user_message: str) -> None:
    self.history.append_user_message(user_message)

    while True:
        # 动态编译最新的系统提示词
        current_system_prompt = build_system_prompt()

        response = await self._anthropic_client.messages.create(
            model=self.config.model,
            max_tokens=4096,
            system=current_system_prompt,  # 传入编译后的提示词
            tools=get_tool_definitions(),
            messages=self.history.anthropic_messages,
        )
        # ... 后续逻辑保持不变


# 3. 修改 _chat_openai
async def _chat_openai(self, user_message: str) -> None:
    self.history.append_user_message(user_message)

    while True:
        # 动态编译最新的系统提示词
        current_system_prompt = build_system_prompt()
        self.history.update_system_prompt(current_system_prompt)

        response = await self._openai_client.chat.completions.create(
            model=self.config.model,
            messages=self.history.openai_messages,  # 统一由 history 管理
            tools=_to_openai_tools(get_tool_definitions()),
        )
        # ... 后续逻辑保持不变
```

---

## ⚖️ 设计权衡

### 占位符渐进增强策略

本课使用 6 个占位符的精简模板是为了聚焦核心机制（模板定义 + 动态上下文收集 + `@include` 解析）。随着后续课程推进，`prompt.py` 的模板和 `build_system_prompt()` 函数会逐步扩展：第 10 课添加 `{{memory}}`，第 11 课添加 `{{skills}}`，第 14 课添加 `{{agents}}`，延迟加载机制引入后添加 `{{deferred_tools}}`。每次扩展只需三步：在模板末尾追加占位符、在文件顶部添加对应 `import`、在 `replacements` 字典中增加一行键值对。

### 本地编译提示词 vs 模型调用自动召回

- **方案 A**：**本地编译注入**（我们所用）
  - 每次请求前，在本地用 Python 执行轻量脚本，把环境、CWD、Git 信息编译成文本发给模型。
  - **优点**：数据 100% 准确，无延迟，不需要额外的 LLM 调用。
  - **缺点**：如果信息非常多（如超长的 Git status），会导致 Prompt 的基础 Token 占用增加。
- **方案 B**：**Agent 自主使用工具查询**
  - Prompt 里不包含环境信息，仅提供 `run_shell("git status")` 等工具，由大模型需要时自行调用。
  - **优点**：初始 Prompt 干净。
  - **缺点**：模型可能由于“不知道自己不知道”而漏掉调用，且多出一轮 API 往返，极大地降低了冷启动速度。

**结论**：将关键的“只读性”环境要素直接本地编译进 System Prompt，是保障 Agent 高效做对事情的 industry standard。

---

## ⚠️ 常见陷阱

### 1. 未处理 `@include` 自引用导致递归栈溢出

```python
# ❌ 错误：缺少已读和深度控制，若 CLAUDE.md 中包含 @./CLAUDE.md 则会直接陷入死循环导致内存耗尽
def _resolve_includes(content, base_path):
    # 直接正则替换，未检测 visited
    return _INCLUDE_RE.sub(lambda m: Path(m.group(1)).read_text(), content)
```

**修正**：必须引入 `visited = set()` 记录文件绝对路径，并在深度 `depth >= _MAX_INCLUDE_DEPTH` 时进行截断保护。

---

### 2. Windows 下 Shell 占位符报错或为空

在 Windows 环境下，`os.environ.get("SHELL")` 通常不存在，会导致在编译阶段该位置为 None 造成字符串拼接类型报错。

**修正**：使用 `os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown"`，兼容 Windows 下的 `cmd.exe`/`powershell.exe`。

---

## ✅ 验收点

### 输入

1. 在项目根目录下临时创建一个 `CLAUDE.md` 文件，写入以下特殊测试规则：
   ```markdown
   # Project Rules
   Always greet the user with "Hello from CLAUDE.md!" before answering.
   ```
2. 运行 Agent 并发送任意消息：
   ```bash
   python -m mini_claude "你好"
   ```

### 预期结果

大模型在回复中必须包含 `"Hello from CLAUDE.md!"` 的问候语。这证明 Agent 在调用 API 时成功加载了递归规则，并将其编译到了最终发送给大模型的 System Prompt 中。

*测试完成后，请记得删除测试用的 `CLAUDE.md`，以免干扰后续学习。*

---

## 🧠 思考题

1. **为什么在 `load_claude_md` 中，我们向上遍历目录找到的所有 `CLAUDE.md` 规则，是用 `parts.insert(0, content)` 将父级目录内容插在前面，而不是 `append` 插在后面？**
   *(提示：大语言模型具有近因效应（Recency Effect），越靠后的内容模型越容易记住。因此我们把工作目录（CWD）最近的、最具体的规则插在最后，从而覆盖父级目录的通用规则。)*
2. **在 `get_git_context` 中我们使用子进程运行 Git，如果用户把 Agent 拷贝到一个没有初始化 Git 仓库的普通文件夹下运行，代码会报错崩溃吗？**
   *(提示：不会，因为我们用 `try...except` 块包裹了执行，返回了空字符串，这实现了优雅的“降级保障”。)*

---

## 📦 本节收获

1. **动态编译设计**：理解了静态 Prompt 模板与动态运行时上下文的拼接融合技术。
2. **近因效应应用**：掌握了规则分层覆盖（CWD 优先级高于 Parent Dir）的设计技巧。
3. **模块化规则管理**：通过 `_load_rules_dir` 实现了从 `.claude/rules/` 目录加载规则文件，使项目规范可拆分为多个独立模块。
4. **健壮性防卫**：实现了死循环 include 防护和外部命令超时机制，保证提示词引擎 100% 稳定。

---

> **下一章**：有了工具和提示词，我们的 Agent 已经成型。下一步是让它变得更易用——打造交互式 CLI 终端、命令系统和会话恢复。
