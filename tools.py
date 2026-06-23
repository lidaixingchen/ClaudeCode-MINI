from __future__ import annotations

import re
import subprocess
import os
from pathlib import Path
from typing import Literal

MAX_RESULT_CHARS = 50000  # 限制单次工具返回最大字符数——防止撑爆上下文窗口


# 权限安全模式：5 种预设选项
PermissionMode = Literal["default", "plan", "acceptEdits", "bypassPermissions", "dontAsk"]


# 并发安全工具白名单：只读、无副作用，允许在流式输出期间异步抢跑
# 使用 set 实现 O(1) 查找，避免在回调中频繁遍历列表
CONCURRENCY_SAFE_TOOLS = {"read_file", "list_files", "grep_search", "web_fetch"}

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
                "path": {
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
            'required': ['path', 'old_string', 'new_string'],
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
        return f"Successfully wrote to {inp['path']} ({len(lines)} lines):\n\n{preview}{trunc}"
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
        path = Path(inp["path"])
        content = path.read_text(encoding="utf-8")

         # 带引号容错的查找
        actual = _find_actual_string(content, inp["old_string"])
        if not actual:
            return f"Error: Could not find the specified string in {inp['path']}."

        # 唯一性校验防止误替换——匹配多次时拒绝执行，由 LLM 调整 old_string 重试
        count = content.count(actual)
        if count > 1:
            return f"Error: The specified string occurs {count} times in {inp['path']}. Please provide a more specific string to replace."

        # replace 第三个参数 1 表示只替换首个匹配——即使校验通过也做防御
        new_content = content.replace(actual, inp["new_string"], 1)
        path.write_text(new_content, encoding="utf-8")

        diff = _generate_diff(content, actual, inp["new_string"])
        # 若实际匹配的字符串与请求不同，说明经历了引号标准化
        note = " (matched via quote normalization)" if actual != inp["old_string"] else ""
        return f"Successfully edited {inp['path']}{note}. Diff:\n\n{diff}"
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

import json
from pathlib import Path

# 读工具永远不需要确认权限（低爆炸半径操作）
READ_TOOLS = {"read_file", "list_files", "grep_search", "web_fetch"}
# 写/编辑工具属于敏感操作（可能修改外部状态）
EDIT_TOOLS = {"write_file", "edit_file"}

# 16 个内置的危险 Shell 命令正则扫描模式（兼容 Unix & Windows）
# 覆盖文件删除、系统操作、进程管理等高危操作
DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s"),  # Unix 删除文件
    re.compile(r"\bgit\s+(push|reset|clean|checkout\s+\.)"),  # git 危险操作
    re.compile(r"\bsudo\b"),  # 提权操作
    re.compile(r"\bmkfs\b"),  # 格式化文件系统
    re.compile(r"\bdd\s"),  # 磁盘复制（可覆写整个磁盘）
    re.compile(r">\s*/dev/"),  # 向设备文件写入
    re.compile(r"\bkill\b"),  # 终止进程
    re.compile(r"\bpkill\b"),  # 按名称终止进程
    re.compile(r"\breboot\b"),  # 重启系统
    re.compile(r"\bshutdown\b"),  # 关机
    re.compile(r"\bdel\s", re.IGNORECASE),  # Windows 删除文件
    re.compile(r"\brmdir\s", re.IGNORECASE),  # Windows 删除目录
    re.compile(r"\bformat\s", re.IGNORECASE),  # Windows 格式化磁盘
    re.compile(r"\btaskkill\s", re.IGNORECASE),  # Windows 终止进程
    re.compile(r"\bRemove-Item\s", re.IGNORECASE),  # PowerShell 删除
    re.compile(r"\bStop-Process\s", re.IGNORECASE),  # PowerShell 终止进程
]


def is_dangerous(command: str) -> bool:
    """静态扫描命令是否匹配危险模式（作为第一层防护）。"""
    # 只要命中任意正则，即标记为危险
    return any(pattern.search(command) for pattern in DANGEROUS_PATTERNS)    

def _load_settings(file_path: Path) -> dict | None:
    """加载 JSON 配置文件，文件不存在或解析失败时返回 None。"""
    if not file_path.is_file():
        return None
    try:
        with file_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
    
def _parse_rule(rule: str) -> dict:
    """解析权限规则字符串，格式如 "run_shell(git status)" 或 "read_file"。"""
    # 匹配类似于 run_shell(git status) 的格式
    m = re.match(r"^([a-z_]+)\((.+)\)$", rule)
    if m:
        return {"tool": m.group(1), "args": m.group(2)}
    return {"tool": rule, "pattern": None}

_cached_rules: dict | None = None  # 缓存已加载的规则，避免重复读取文件

def load_permission_rules() -> dict:
    """加载并合并全局与项目级权限规则。"""
    global _cached_rules
    if _cached_rules is not None:
        return _cached_rules  # 返回缓存的规则

    allow = []
    deny = []
    
    # 按优先级顺序：全局配置先读，项目级配置后读（项目级覆盖全局）
    paths = [
        Path.home() / ".claude" / "settings.json",  # 全局配置
        Path.cwd() / ".claude" / "settings.json",   # 项目级配置
    ]
    for path in paths:
        settings = _load_settings(path)
        if settings is None:
            continue
        perm = settings.get("permissions", {})
        for rule in perm.get("allow", []):
            allow.append(_parse_rule(rule))
        for rule in perm.get("deny", []):
            deny.append(_parse_rule(rule))

    _cached_rules = {"allow": allow, "deny": deny}
    return _cached_rules

def _matches_rule(rule: dict, tool_name: str, inp: dict) -> bool:
    """检查单条规则是否匹配当前工具调用（支持通配符和精确匹配）。"""
    if rule["tool"] != tool_name:
        return False
    if rule["pattern"] is None:
        return True  # 无参数限制，工具名匹配即生效
    
    # 根据工具类型提取用于匹配的值
    value = ""
    if tool_name == "run_shell":
        value = inp.get("command", "")
    elif "path" in inp:
        value = inp["path"]
    else:
        return True  # 对于其他工具，若规则指定了参数但工具没有对应参数，则视为匹配

    pattern = rule["pattern"]
    # 支持 * 通配符前缀匹配（如 "npm test*" 匹配 "npm test --coverage"）
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return value == pattern

def _check_permission_rules(tool_name: str, inp: dict) -> str | None:
    """评估配置文件中的 allow/deny 规则，返回 "deny"/"allow"/None。"""
    rules = load_permission_rules()
    for rule in rules["deny"]:
        if _matches_rule(rule, tool_name, inp):
            return "deny"
    for rule in rules["allow"]:
        if _matches_rule(rule, tool_name, inp):
            return "allow"
    return None  # 无匹配规则

def check_permission(
    tool_name: str,
    inp: dict,
    mode: str = "default",
    plan_file_path: str | None = None,
) -> dict:
    """统一权限评判引擎：根据安全模式和规则库返回 allow/deny/confirm 三态决策。"""
    # 0. bypassPermissions (--yolo) 模式：无条件直接放行，必须在最顶层
    if mode == "bypassPermissions":
        return {"action": "allow"}
    
    # 1. 检查配置文件中的 allow/deny 规则（deny 优先级更高）
    rule_result = _check_permission_rules(tool_name, inp)
    if rule_result == "deny":
        return {"action": "deny", "message": f"Denied by permission rule for {tool_name}"}
    if rule_result == "allow":
        return {"action": "allow"}
    
    # 2. 只读工具永远安全（低爆炸半径操作）
    if tool_name in READ_TOOLS:
        return {"action": "allow"}
    
    # 3. plan 模式：禁止除写入 plan 文件外的任何编辑/执行操作
    if mode == "plan":
        if tool_name in EDIT_TOOLS:
            file_path = inp.get("path")
            # 仅允许写入计划文件本身
            if plan_file_path and file_path == plan_file_path:
                return {"action": "allow"}
            return {"action": "deny", "message": f"Blocked in plan mode: {tool_name}"}
        if tool_name == "run_shell":
            return {"action": "deny", "message": "Shell commands blocked in plan mode"}

    # 4. 豁免模式切换工具——否则用户将无法进入或退出规划模式
    if tool_name in ("enter_plan_mode", "exit_plan_mode"):
        return {"action": "allow"}
    
    # 5. acceptEdits 模式下，编辑文件直接放行
    if mode == "acceptEdits" and tool_name in EDIT_TOOLS:
        return {"action": "allow"}
    
    # 6. 内置危险检测（针对危险 shell 命令或写入/编辑不存在的文件）
    needs_confirm = False
    confirm_message = ""

    if tool_name == "run_shell" and is_dangerous(inp.get("command", "")):
        needs_confirm = True
        confirm_message = inp.get("command", "")
    elif tool_name == "write_file" and not Path(inp.get("path", "")).exists():
        needs_confirm = True
        confirm_message = f"Write to new file: {inp.get('path', '')}"
    elif tool_name == "edit_file" and not Path(inp.get("path", "")).exists():
        needs_confirm = True
        confirm_message = f"Edit non-existent file: {inp.get('path', '')}"

    if needs_confirm:
        # dontAsk (CI 环境) 模式下，需要确认的操作直接拒绝（无法交互）
        if mode == "dontAsk":
            return {"action": "deny", "message": f"Blocked dangerous operation in dontAsk mode: {confirm_message}"}
        return {"action": "confirm", "message": confirm_message}
    
    # 7. 未命中任何规则，默认放行
    return {"action": "allow"}