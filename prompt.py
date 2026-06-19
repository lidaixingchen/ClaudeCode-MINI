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

def get_git_context() -> str:
    """获取当前 Git 仓库的上下文信息（分支、状态、未提交的更改）"""
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
            result += f"\nUncommitted changes:\n{status}"
        return result
    except Exception:
        return "Not a git repository"
    
# 匹配 @./path、@~/path、@/path 格式的 include 指令
_INCLUDE_RE = re.compile(r"^@(\./[^\s]+|~/[^\s]+|/[^\s]+)$", re.MULTILINE)
_MAX_INCLUDE_DEPTH = 5  # 防止恶性嵌套

def _resolve_includes(
    content: str,
    base_path: Path,
    visited: set[str] | None = None,
    depth: int = 0,
) -> str:
    """递归解析 @include 引用，将被引入文件的内容内联替换到当前文本中"""
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
        if key in visited:
            return f"<!-- Skipping already included file: {key} -->"
        if not resolved.is_file():
            return f"<!-- File not found: {key} -->"

        try:
            visited.add(key)
            included_content = resolved.read_text(encoding="utf-8")
            # 递归解析被引入文件的内容
            return _resolve_includes(included_content, resolved.parent, visited, depth + 1)
        except Exception as e:
            return f"<!-- Error reading file {key}: {e} -->"

    return _INCLUDE_RE.sub(_replace, content)   

def _load_rules_dir(directory: Path) -> str:
    """从 .claude/rules/ 目录加载所有 .md 规则文件，支持模块化规则管理"""
    # 定位 .claude/rules/ 目录
    rules_dir = directory / ".claude" / "rules"
    if not rules_dir.is_dir():
        return ""
    try:
        files = sorted(f for f in rules_dir.iterdir() if f.suffix == ".md" and f.is_file())
        if not files:
            return ""
        parts: list[str] = []
        for f in files:
            try:
                content = f.read_text(encoding="utf-8")
                # 规则文件内部也支持 @include 语法
                content = _resolve_includes(content, f.parent)
                # 用 HTML 注释标记来源文件名，便于调试
                content = f"<!-- Source: {f.name} -->\n{content}"
            except Exception as e:
                content = f"<!-- Error reading rules file {f.name}: {e} -->"
        # 所有规则合并为一个 section 返回
        return "\n\n## Rules\n" + "\n\n".join(parts) if parts else ""
    except Exception:
        return ""

def load_claude_md() -> str: 
    """从当前目录向上递归查找所有 CLAUDE.md 文件，合并为项目级指令集"""
    parts: list[str] = []
    d = Path.cwd().resolve()
    # 向上递归查找所有的 CLAUDE.md
    while True:
        candidate = d / "CLAUDE.md"
        if candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8")
                # 支持 CLAUDE.md 内部使用 @include 引入其他文件
                content = _resolve_includes(content, candidate.parent)
                parts.insert(0, content)  # 父级目录的规则插在前面，近因效应使其在后方覆盖
            except Exception as e:
                parts.append(f"<!-- Error reading CLAUDE.md at {candidate}: {e} -->")
        parent = d.parent
        if d.parent == d:
            break  # 已经到达文件系统根目录
        d = d.parent

    # 加载 .claude/rules/ 目录下的模块化规则文件
    rules = _load_rules_dir(Path.cwd())

    # 拼接 CLAUDE.md 部分和 rules 目录部分
    claude_md = ""
    if parts:
        claude_md += "\n\n# Project-specific Instructions\n" + "\n\n".join(parts)
    if rules:
        claude_md += rules

    return claude_md

from datetime import date

def build_system_prompt() -> str:
    """编译完整的 System Prompt：将动态上下文替换到静态模板中"""
    # 收集所有动态上下文，构建占位符替换映射
    replacements = {
        "{{cwd}}": str(Path.cwd()),                              # 当前工作目录
        "{{platform}}": f"{platform.system()} {platform.machine()}",  # 操作系统和架构
        "{{shell}}": os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown",  # Shell 类型
        "{{git_context}}": get_git_context(),                    # Git 状态信息
        "{{claude_md}}": load_claude_md(),                       # 项目级指令（CLAUDE.md + rules）
        "{{date}}": date.today().isoformat()                     # 今天的日期
    }
    # 逐个替换模板中的 {{placeholder}} 为实际值
    result = SYSTEM_PROMPT_TEMPLATE
    for key, value in replacements.items():
        result = result.replace(key, value)
    return result
