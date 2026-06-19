from __future__ import annotations

import re
import subprocess
import os
from pathlib import Path

MAX_RESULT_CHARS = 50000  # 限制单次工具返回最大字符数——防止撑爆上下文窗口

# 工具定义：告诉 LLM 有哪些工具可用
tool_definitions: list[dict] = [
    {
        "name": "list_files",
        "description": "List files matching a glob pattern. Returns matching file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": 'Glob pattern to match files (e.g., "**/*.py", "src/**/*")',
                },
                "path": {
                    "type": "string",
                    "description": "Base directory to search from. Defaults to current directory.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the content of a file. Returns the file content with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates parent directories if they do not exist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            'Edit a file by replacing a specific string with a new string. '
            'The old_string must match exactly one occurrence in the file. '
            'If it matches multiple times, the tool will return an error.'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": 'Path to the file to edit.',
                },
                "old_string": {
                    "type": 'string',
                    'description': 'The exact string in the file to be replaced. Must match exactly one occurrence.',
                },
                'new_string': {
                    'type': 'string',
                    'description': 'The new string that will replace the old string.',
                },
            },
            'required': ['file_path', 'old_string', 'new_string'],
        },
    },
    {
        "name": "run_shell",
        "description": "Execute a shell command. Returns the command output or error.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Optional timeout in seconds for the command execution.",
                },
            },
            "required": ["command"],
        },
    },
]


def get_tool_definitions() -> list[dict]:
    """返回所有工具的定义，供 LLM API 调用时传入"""
    return tool_definitions

# 单次列文件的最大条目数——防止超长输出撑爆上下文
MAX_LIST_FILES = 200


# 工具执行：根据名称分发到具体实现
async def execute_tool(name: str, inp: dict) -> str:
    """工具分发器：根据名称路由到具体实现函数，并自动截断大结果"""
    handlers = {
        "list_files": _list_files,  
        "read_file": _read_file,
        "write_file": _write_file,
        "edit_file": _edit_file,
        "run_shell": _run_shell,
    }
    handler = handlers.get(name)
    if not handler:
        return f"Unknown tool: {name}"  # 返回字符串而非抛异常，让 LLM 自行修正工具名

    # 执行对应函数并自动拦截大结果输出
    result = handler(inp)
    return _truncate_result(result)

def _list_files(inp: dict) -> str:
    """列出目录下匹配 glob 模式的文件"""
    base = Path(inp.get("path", "."))  # 默认当前目录
    pattern = inp["pattern"]
    files = []

    for p in base.glob(pattern):
        if p.is_file():
            rel = str(p.relative_to(base) if base != Path(".") else p)
            # 跳过 node_modules 和 .git，避免返回垃圾结果
            if "node_modules" in rel or ".git" in rel.split(os.sep):
                continue
            files.append(rel)
        if len(files) >= MAX_LIST_FILES:
            break
    
    if not files:
        return "No files found."
    return "\n".join(files[:MAX_LIST_FILES])

def _read_file(inp: dict) -> str:
    """读取文件内容并附加行号，方便 LLM 精准定位"""
    try:
        content = Path(inp["path"]).read_text(encoding="utf-8", errors="ignore")
        lines = content.splitlines()
        # 附加行号
        numbered_lines = [f"{i+1:04d}: {line}" for i, line in enumerate(lines)]
        return "\n".join(numbered_lines)
    except Exception as e:
        return f"Error reading file: {e}"
    
def _truncate_result(result: str) -> str:
    """头尾截断：保留文件开头和末尾（报错通常在尾部），裁剪中间部分"""
    if len(result) <= MAX_RESULT_CHARS:
        return result
    # 预留 60 字符给截断提示信息，其余平均分配给头尾
    keep = (MAX_RESULT_CHARS - 60) // 2
    return result[:keep] + f"\n\n[... truncated {len(result) - keep * 2} chars ...]\n\n" + result[-keep:]

def _write_file(inp: dict) -> str:
    """写入文件，自动创建不存在的父目录"""
    try:
        path = Path(inp["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        content = inp["content"]
        path.write_text(content, encoding="utf-8")
        # 返回前 30 行预览，让 Agent 确认写入格式正确，无需再调 read_file
        lines = content.splitlines()
        preview = "\n".join(f"{i+1:4d} | {l}" for i, l in enumerate(lines[:30]))
        trunc = f"\n  ... ({len(lines)} lines total)" if len(lines) > 30 else ""
        return f"Successfully wrote to {inp['file_path']} ({len(lines)} lines):\n\n{preview}{trunc}"
    except Exception as e:
        return f"Error writing file: {e}"
    
def _normalize_quotes(s: str) -> str:
    """将弯引号和特殊引号统一为直引号——LLM 常混淆这些字符"""
    s = re.sub("[‘’′]", "'", s)
    s = re.sub("[“”″]", '"', s)
    return s

def _find_actual_string(file_content: str, search_string: str) -> str | None:
    """在文件中查找目标字符串，支持引号容错"""
    # 先尝试精确匹配——优先使用原始字符串
    if search_string in file_content:
        return search_string
    # 尝试将引号统一为直引号后再匹配
    norm_search = _normalize_quotes(search_string)
    norm_file = _normalize_quotes(file_content)
    idx = norm_file.find(norm_search)
    if idx != -1:
        return file_content[idx:idx + len(norm_search)]
    return None

def _generate_diff(old_content: str, old_string: str, new_string: str) -> str:
    """生成轻量级 diff 输出，方便 Agent 确认修改内容"""
    # 根据 old_string 在前文中出现的 \n 计算起始行号
    line_num = old_content.split(old_string)[0].count("\n") + 1
    old_lines = old_string.split("\n")
    new_lines = new_string.split("\n")
    parts = [f"@@ -{line_num},{len(old_lines)} +{line_num},{len(new_lines)} @@"]
    parts.extend(f"- {l}" for l in old_lines)
    parts.extend(f"+ {l}" for l in new_lines)
    return "\n".join(parts)

def _edit_file(inp: dict) -> str:
    """精确编辑：通过唯一匹配的 old_string 替换为 new_string"""
    try:
        path = Path(inp["file_path"])
        content = path.read_text(encoding="utf-8")

         # 带引号容错的查找
        actual = _find_actual_string(content, inp["old_string"])
        if not actual:
            return f"Error: Could not find the specified string in {inp['file_path']}."
        
        # 唯一性校验防止误替换——匹配多次时拒绝执行，由 LLM 调整 old_string 重试
        count = content.count(actual)
        if count > 1:
            return f"Error: The specified string occurs {count} times in {inp['file_path']}. Please provide a more specific string to replace."
        
        # replace 第三个参数 1 表示只替换首个匹配——即使校验通过也做防御
        new_content = content.replace(actual, inp["new_string"], 1)
        path.write_text(new_content, encoding="utf-8")

        diff = _generate_diff(content, actual, inp["new_string"])
        # 若实际匹配的字符串与请求不同，说明经历了引号标准化
        note = " (matched via quote normalization)" if actual != inp["old_string"] else ""
        return f"Successfully edited {inp['file_path']}{note}. Diff:\n\n{diff}"
    except Exception as e:
        return f"Error editing file: {e}"
    
def _run_shell(inp: dict) -> str:
    """执行 Shell 命令，合并 stdout 和 stderr，支持超时保护"""
    try:
        cmd = inp["command"]
        timeout = inp.get("timeout", 30)  # 默认 30 秒超时
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        stdout = f"\nStdout:\n{result.stdout}" if result.stdout else ""
        stderr = f"\nStderr:\n{result.stderr}" if result.stderr else ""
        if result.returncode != 0:
            # 非零退出码——将 stderr 一起返回，错误信息通常在 stderr 中
            return f"Command failed with return code {result.returncode}.{stdout}{stderr}"
        return result.stdout if result.stdout else "Command executed successfully with no output."
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout} seconds."
    except Exception as e:
        return f"Error executing command: {e}"
    