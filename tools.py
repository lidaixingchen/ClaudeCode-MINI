from __future__ import annotations

import os
from pathlib import Path

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
]


def get_tool_definitions() -> list[dict]:
    """返回所有工具的定义，供 LLM API 调用时传入"""
    return tool_definitions

# 单次列文件的最大条目数——防止超长输出撑爆上下文
MAX_LIST_FILES = 200


# 工具执行：根据名称分发到具体实现
async def execute_tool(name: str, inp: dict) -> str:
    """根据工具名称分发到具体实现"""
    if name == "list_files":
        return _list_files(inp)
    return f"Unknown tool: {name}"  # 返回字符串而非抛异常，让 LLM 自行修正

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
