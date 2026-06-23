# 第 08 课：权限与安全限制

## 🎯 本节目标

为 Agent 构筑一道坚实的安全防线。实现纵深权限拦截引擎：检测危险 Shell 命令，解析项目级与全局的 `settings.json` 规则，支持 5 种不同的权限安全模式，并提供带有会话级白名单保护的 `y/n` 交互式确认弹窗。

---

## 🏆 最终效果

完成本节后，当 Agent 尝试运行具有破坏性的 Shell 命令（如 `git push --force` 或 `rm -rf`）或在未授权模式下修改系统敏感文件时：
- 终端会自动暂停，弹出醒目的黄字警告并询问用户 `Allow? (y/n): `。
- 用户输入 `y` 同意后，命令继续执行，且相同的命令在该会话中会被加入白名单，不会重复弹窗打扰用户。
- 用户若输入 `n` 拒绝，Agent 将立刻获得一个表示“被拒绝”的工具结果回复，并被逼退去思考更加安全的备选方案，而绝不会让程序发生崩溃。

---

## 🛠️ 本节任务

1. **实现危险命令匹配器**：在 `tools.py` 中编写 `is_dangerous` 函数，用正则匹配具有破坏性的 Shell 命令。
2. **实现配置文件权限加载**：实现 `_load_settings` 和规则匹配，能够读取全局与项目级的安全配置。
3. **编写统一权限评判引擎**：在 `tools.py` 中实现 `check_permission()`，结合安全模式、配置文件、只读工具等做出 allow/deny/confirm 决策。
4. **与 Agent 循环对接并实现确认弹窗**：在 `agent.py` 中接入权限限制，管理已确认过的命令白名单，并支持 REPL 下的 `y/n` 实时确认。

---

## 📦 涉及文件

修改：
- `tools.py`
- `agent.py`

---

## 🚀 开始实现

### 步骤 1：危险命令静态扫描器

#### 为什么做

最直接的安全漏洞来自 `run_shell` 命令的滥用。如果模型被引导运行恶意指令或产生了破坏性幻觉（如 `git checkout .` 意外抹去你本地没保存的代码），我们必须能在执行前将其扫描拦截。我们将定义 16 个典型危险命令正则模式（兼容 Unix/Windows）作为内置检测的第二防线。

#### 做什么

修改 `tools.py`，导入必要包，定义安全白名单和危险正则规则：

```python
# tools.py 中的修改

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
    return any(p.search(command) for p in DANGEROUS_PATTERNS)
```

#### 注意什么

- **正则局限性**：静态扫描危险命令仅是辅助第一层防护，大模型或用户可能有各种方式绕过正则，真正的防线在后续的白名单规则匹配和交互确认。

---

### 步骤 2：读取配置文件并匹配 allow/deny 规则

#### 为什么做

静态的检测无法应付各种定制需求。我们需要支持类似于 Claude Code 的 `settings.json` 权限配置，允许用户在全局（`~/.claude/settings.json`）和项目级（`./.claude/settings.json`）配置自定义规则。
- 规则格式如 `run_shell(npm test*)` 表示允许以 `npm test` 开头的命令。
- 我们需要支持精确匹配和带 `*` 的前缀通配符匹配。

#### 做什么

在 `tools.py` 中编写配置读取、规则解析与判定方法：

```python
# tools.py（续）


def _load_settings(file_path: Path) -> dict | None:
    """加载 JSON 配置文件，文件不存在或解析失败时返回 None。"""
    if not file_path.exists():
        return None
    try:
        # 指定 UTF-8 编码避免非 ASCII 路径/内容的乱码问题
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_rule(rule: str) -> dict:
    """解析权限规则字符串，格式如 "run_shell(git status)" 或 "read_file"。"""
    # 匹配类似于 run_shell(git status) 的格式
    m = re.match(r"^([a-z_]+)\((.+)\)$", rule)
    if m:
        return {"tool": m.group(1), "pattern": m.group(2)}
    # 没有括号的规则只匹配工具名，不限制参数
    return {"tool": rule, "pattern": None}


_cached_rules: dict | None = None  # 缓存已加载的规则，避免重复读取文件


def load_permission_rules() -> dict:
    """加载并合并全局与项目级权限规则。"""
    global _cached_rules
    if _cached_rules is not None:
        return _cached_rules

    allow = []
    deny = []
    
    # 按优先级顺序：全局配置先读，项目级配置后读（项目级覆盖全局）
    paths = [
        Path.home() / ".claude" / "settings.json",  # 全局配置
        Path.cwd() / ".claude" / "settings.json",   # 项目级配置
    ]
    for p in paths:
        settings = _load_settings(p)
        if settings is None:
            continue
        perms = settings.get("permissions", {})
        for r in perms.get("allow", []):
            allow.append(_parse_rule(r))
        for r in perms.get("deny", []):
            deny.append(_parse_rule(r))
            
    _cached_rules = {"allow": allow, "deny": deny}
    return _cached_rules


def _matches_rule(rule: dict, tool_name: str, inp: dict) -> bool:
    """检查单条规则是否匹配当前工具调用（支持通配符和精确匹配）。"""
    if rule["tool"] != tool_name:
        return False
    if rule["pattern"] is None:
        return True  # 无参数限制，工具名匹配即生效

    # 统一提取用于匹配的值：run_shell 用 command，其余工具用 file_path 或 path
    # 覆盖所有工具参数命名：read_file/write_file 用 file_path，edit_file/list_files/grep_search 用 path
    value = inp.get("command") or inp.get("file_path") or inp.get("path", "")
    if not value:
        return True  # 没有可匹配的值时，视为匹配所有规则

    pattern = rule["pattern"]
    # 支持 * 通配符前缀匹配（如 "npm test*" 匹配 "npm test --coverage"）
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    # 精确匹配
    return value == pattern
```

#### 注意什么

- **配置文件的编码**：读取 JSON 配置文件时，必须指定 `encoding="utf-8"`，这能保证在非 ASCII 文件路径和非英语环境下规则文件正常解析而不产生 Unicode 乱码崩溃。

---

### 步骤 3：编写统一权限评判引擎 `check_permission`

#### 为什么做

现在我们将前面的积木拼装起来。`check_permission` 是整个安全防火墙的唯一评估总线。它接受当前的工具调用和系统当前设定的“权限安全模式”，按照以下**漏洞防火墙的评估漏斗**输出三态动作之一：
1. **`allow`**（安全，直接放行）
2. **`deny`**（危险，拒绝并返回报错）
3. **`confirm`**（可疑，挂起并弹窗询问用户）

**评估漏斗规则优先级**：`deny 规则` > `allow 规则` > `只读工具放行` > `安全模式限制` > `危险正则检测` > `默认放行`。

#### 做什么

在 `tools.py` 中实现 `check_permission` 核心总线：

```python
# tools.py（续）


def _check_permission_rules(tool_name: str, inp: dict) -> str | None:
    """评估配置文件中的 allow/deny 规则，返回 "deny"/"allow"/None。"""
    rules = load_permission_rules()
    # deny 规则优先级更高，先遍历
    for rule in rules["deny"]:
        if _matches_rule(rule, tool_name, inp):
            return "deny"
    # 再检查 allow 规则
    for rule in rules["allow"]:
        if _matches_rule(rule, tool_name, inp):
            return "allow"
    return None  # 无匹配规则，交给后续逻辑判断


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
        # 新建文件需要确认（可能是误操作）
        needs_confirm = True
        confirm_message = f"write new file: {inp.get('path', '')}"
    elif tool_name == "edit_file" and not Path(inp.get("path", "")).exists():
        # 编辑不存在的文件需要确认
        needs_confirm = True
        confirm_message = f"edit non-existent file: {inp.get('path', '')}"

    if needs_confirm:
        # dontAsk (CI 环境) 模式下，需要确认的操作直接拒绝（无法交互）
        if mode == "dontAsk":
            return {"action": "deny", "message": f"Auto-denied (dontAsk mode): {confirm_message}"}
        # 默认行为：挂起并弹窗询问用户
        return {"action": "confirm", "message": confirm_message}

    # 7. 未命中任何规则，默认放行
    return {"action": "allow"}
```

#### 注意什么

- **只读模式豁免**：在规划模式（Plan Mode）下，应特别豁免状态转换命令（如 `enter_plan_mode` 和 `exit_plan_mode`），否则用户将无法启动或退出该模式。

---

### 步骤 4：与 Agent 循环对接并实现确认弹窗

#### 为什么做

我们在步骤 3 输出的 `confirm` 和 `deny` 动作必须被 Agent 严格贯彻。
1. 若为 `deny`，Agent 绝对不能调起工具，要立即回填一个“操作被拒绝”的 Tool Result 喂回给大模型。
2. 若为 `confirm`，Agent 在弹窗前必须先核对**会话级已确认白名单**。若白名单已记录此消息，直接运行；若无记录，暂停并阻塞等待用户命令行键入 `y/n` 回车确认。确认后将其存入白名单，保障后续交互体验。

#### 做什么

首先，在 `tools.py` 中添加权限模式的类型定义：

```python
# tools.py 顶部添加

from typing import Literal

# 权限安全模式：5 种预设选项
PermissionMode = Literal["default", "plan", "acceptEdits", "bypassPermissions", "dontAsk"]
```

然后，在 `__main__.py` 中添加权限相关的命令行参数：

```python
# __main__.py 中的修改

from tools import PermissionMode


def parse_args() -> argparse.Namespace:
    """解析命令行参数，返回命名空间对象。"""
    parser = argparse.ArgumentParser(
        prog="mini-claude",
        description="Mini Claude Code — a minimal coding agent",
        add_help=False,
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    # 权限控制参数（第 08 课新增）
    parser.add_argument("--yolo", "-y", action="store_true",
                        help="Skip all confirmation prompts")
    parser.add_argument("--plan", action="store_true",
                        help="Plan mode: read-only")
    parser.add_argument("--accept-edits", action="store_true",
                        help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true",
                        help="Auto-deny confirmations (for CI)")
    # 原有参数保持不变
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--api-base", default=None,
                        help="OpenAI-compatible API base URL")
    parser.add_argument("--resume", action="store_true",
                        help="Resume last session")
    parser.add_argument("--max-cost", type=float, default=None,
                        help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None,
                        help="Max agentic turns")
    parser.add_argument("--help", "-h", action="store_true",
                        help="Show help")
    return parser.parse_args()


def _resolve_permission_mode(args: argparse.Namespace) -> PermissionMode:
    """将命令行权限参数映射为 Agent 内部权限模式字符串。"""
    if args.yolo:
        return "bypassPermissions"
    if args.plan:
        return "plan"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    return "default"


def main() -> None:
    args = parse_args()
    # ... 其他代码 ...
    permission_mode = _resolve_permission_mode(args)
    agent = Agent(backend=backend, permission_mode=permission_mode)
    # ...
```

最后，修改 `agent.py`，添加权限相关的方法并重构工具执行处理循环：

```python
# agent.py 中的修改

import asyncio
from dataclasses import dataclass, field
from tools import check_permission  # 导入权限检查引擎


@dataclass
class AgentState:
    """Agent 的运行时状态"""
    # 保留第 6 课的字段
    thinking_mode: Literal["disabled", "adaptive", "enabled"] = "disabled"
    # 第 8 课新增的字段
    confirmed_paths: set[str] = field(default_factory=set)  # 已确认的路径白名单
    current_task: asyncio.Task | None = None  # 当前正在执行的任务
    plan_file_path: str | None = None  # 磁盘上 Plan Markdown 文件的绝对路径（第 12 课详述）


@dataclass
class AgentConfig:
    """Agent 行为配置（不含后端信息）"""
    permission_mode: str = "default"  # 权限模式：default, plan, acceptEdits, bypassPermissions, dontAsk


class Agent:
    def __init__(self, backend: BackendConfig, permission_mode: str = "default"):
        self.backend = backend
        self.config = AgentConfig(permission_mode=permission_mode)
        self.state = AgentState()
        # ... 其他初始化代码 ...
        self.confirm_fn = None  # 确认回调函数，由 REPL 注入

    @property
    def is_processing(self) -> bool:
        """判断 Agent 是否正在执行任务（供 SIGINT 信号处理器使用）"""
        return self.state.current_task is not None and not self.state.current_task.done()

    def set_confirm_fn(self, fn) -> None:
        """设置确认回调函数（供 REPL 注入 y/n 确认逻辑）"""
        self.confirm_fn = fn

    async def _confirm_dangerous(self, command: str) -> bool:
        """弹窗询问用户是否允许危险操作，返回 True/False。"""
        print_confirmation(command)
        if self.confirm_fn:
            return await self.confirm_fn(command)
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    # ... 在 _chat_anthropic 的工具执行循环中修改为：
            tool_results = []
            for tu in tool_uses:
                inp = dict(tu.input) if hasattr(tu.input, "items") else tu.input

                # 【1. 执行统一权限评估】
                perm = check_permission(
                    tu.name, inp, self.config.permission_mode, self.state.plan_file_path
                )

                # 命中 Deny 拦截：返回错误消息给模型，让其自适应调整策略
                if perm["action"] == "deny":
                    print(f"  [red]✗ Action Denied: {perm.get('message')}[/red]")
                    # 关键设计：将拒绝消息作为 tool_result 返回，而非抛异常终止会话
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": f"Action denied: {perm.get('message')}",
                    })
                    continue

                # 命中 Confirm：需弹窗二次确认
                if perm["action"] == "confirm":
                    confirm_key = perm["message"]
                    # 检查会话级白名单，避免重复弹窗打扰用户
                    if confirm_key not in self.state.confirmed_paths:
                        confirmed = await self._confirm_dangerous(confirm_key)
                        if not confirmed:
                            # 用户拒绝：返回拒绝消息，让模型尝试其他方案
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": "User denied this action.",
                            })
                            continue
                        # 用户授权成功，加入会话白名单避免后续重复确认
                        self.state.confirmed_paths.add(confirm_key)

                # 【2. 常规工具执行】
                # 此处省略原有执行工具与 early_task 判断，直接调用 execute_tool ...
```

同时，需要更新第 6 课中 `_on_tool_block` 回调，添加权限检查：

```python
# agent.py -> _chat_anthropic 中的 _on_tool_block 回调

            def _on_tool_block(block: dict):
                # 只有白名单中的只读工具才允许抢跑
                if block["name"] in CONCURRENCY_SAFE_TOOLS:
                    # 权限检查：即使工具在白名单中，仍需验证用户是否授权
                    perm = check_permission(
                        block["name"], block["input"],
                        self.config.permission_mode, self.state.plan_file_path,
                    )
                    if perm["action"] == "allow":
                        # 创建后台异步任务立即开始执行，不阻塞流式接收
                        task = asyncio.create_task(self._execute_tool_call(block["name"], block["input"]))
                        early_executions[block["id"]] = task
```

对于 OpenAI 后端，需要先添加两个前置依赖，然后将工具处理逻辑重构为独立的 `_process_openai_tools` 方法：

**前置依赖 1：在 `AgentState` 中添加缺失的属性**

```python
# agent.py -> AgentState 类

@dataclass
class AgentState:
    """Agent 的运行时状态"""
    thinking_mode: Literal["disabled", "adaptive", "enabled"] = "disabled"
    confirmed_paths: set[str] = field(default_factory=set)
    current_task: asyncio.Task | None = None
    plan_file_path: str | None = None
    aborted: bool = False  # 新增：中断标志位
    context_cleared: bool = False  # 新增：上下文是否被清除的标志
```

**前置依赖 2：在 `MessageHistory` 中添加 `append_openai_tool_message` 方法**

```python
# agent.py -> MessageHistory 类

    def append_openai_tool_message(self, tool_call_id: str, content: str) -> None:
        """添加 OpenAI 格式的工具结果消息。"""
        self._openai_messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })
```

**然后，修改 `_chat_openai` 方法并新增 `_process_openai_tools` 方法：**

```python
# agent.py -> _chat_openai 方法中修改工具调用部分

            # 5. 处理工具调用（重构为独立方法）
            if message.get("tool_calls"):
                context_break = await self._process_openai_tools(message["tool_calls"])
                if context_break:
                    break


# agent.py -> 新增 _process_openai_tools 方法

    async def _process_openai_tools(self, tool_calls: list[dict]) -> bool:
        """处理 OpenAI 工具调用，返回 True 表示上下文已被清除。"""
        # 阶段 1：解析并检查权限（串行）
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

            # 执行权限评估
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

        # 阶段 2：分组执行（连续安全工具可并行）
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
                # 并发执行安全工具
                async def _run_oai_safe(ct_item: dict) -> tuple[dict, str]:
                    raw = await self._execute_tool_call(ct_item["fn"], ct_item["inp"])
                    res = self._persist_large_result(ct_item["fn"], raw)
                    print_tool_result(ct_item["fn"], res)
                    return ct_item, res

                results = await asyncio.gather(*[_run_oai_safe(ct) for ct in batch["items"]])
                for ct_item, res in results:
                    self.history.append_openai_tool_message(ct_item["tc"]["id"], res)
            else:
                # 串行执行：处理被拒绝的和普通工具
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
```

最后，在 `__main__.py` 的 REPL 中注入确认回调。`confirm_fn` 定义在 `run_repl` 函数内、`while True` 循环外：

```python
# __main__.py 中的修改

async def run_repl(agent: Agent) -> None:
    """交互式 REPL 循环：读取用户输入、分发命令。"""

    # 【新增】定义确认回调函数（在循环外，只定义一次）
    async def confirm_fn(message: str) -> bool:
        """确认回调：Agent 在需要用户授权时暂停执行并等待终端输入 y/n。"""
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    agent.set_confirm_fn(confirm_fn)  # 注入确认回调

    # 【原有】REPL 主循环
    while True:
        print_user_prompt()
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!\n")
            break
        # ... 其余 REPL 逻辑不变
```

#### 注意什么

- **PermissionMode 类型**：`PermissionMode` 在本课的 `tools.py` 中定义为 `Literal` 类型，包含 5 种预设选项：`default`（默认模式）、`plan`（规划模式）、`acceptEdits`（自动接受修改）、`bypassPermissions`（YOLO 模式）、`dontAsk`（CI 模式）。使用 `Literal` 而非普通 `str` 可以在静态分析时捕获拼写错误。
- **避免高频频繁询问**：通过会话级白名单 `confirmed_paths` 缓存已授权的路径，能够保证在同一个会话中 Agent 修改同一个文件时，用户只需确认一次，有效减少了弹窗对开发流程的心智干扰。

---

## ⚖️ 设计权衡

### 拦截报错作为异常终止 vs 拦截报错作为 Tool Result 返回给模型

- **方案 A**：**作为 Tool Result 返回给模型**（我们所用）
  - 拦截后，不掐断会话。构造一个假的工具运行结果，告诉大模型该项动作已被系统拦截（例如返回 `Action denied: ...`）。
  - **优点**：Agent 仍然能够正常运转，并且可以通过大模型极强的语义理解力”自我纠正”——例如被拦截了危险的 `rm -rf`，大模型会道歉并改用安全的 `run_shell(“git clean”)`，不需要人类二次干预。
  - **缺点**：如果大模型很倔强，可能会反复尝试被拒绝的操作（后续可以通过引入总步数限制来防爆）。
- **方案 B**：**直接抛出 Exception 终止会话**
  - 一旦触发 deny 或用户输入 `n`，抛出运行时异常直接令程序退出。
  - **优点**：绝对安全。
  - **缺点**：交互极其生硬，模型没有自我调整策略的任何机会，体验极差。

**结论**：方案 A 完美地践行了“错误是数据而不是程序崩溃”的 Agent 设计信条，能极大增强 Agent 的自适应纠错能力。

---

## ⚠️ 常见陷阱

### 1. `deny` 拦截时未能加入 `tool_use_id` 拼装

```python
# ❌ 错误：在拦截 deny 时，直接使用 continue 跳出了循环而未返回 tool_result
if perm["action"] == "deny":
    continue
```

**后果**：由于大模型提出了工具调用申请，但响应中没有给出对应的 `tool_result` 对齐，会导致 API 在下一轮报错崩溃，直接破坏了对话历史一致性。
**修正**：必须将拦截消息伪装成 `tool_result` 结果，带上 `tool_use_id` 推回历史中。

---

### 2. `--yolo` 模式下依然误弹拦截警告

如果权限评估总线在 `bypassPermissions`（即 `--yolo`）模式下没有写在最顶层放行，就会导致用户明明开启了免确认，系统还是会频繁弹出危险命令确认窗口。

**修正**：在 `check_permission` 评估函数最开头的第 0 行，必须立刻对 `bypassPermissions` 模式进行直接 `allow` 放行。

---

## ✅ 验收点

### 输入与验证

1. 在当前目录下建立一个 `.claude` 目录并在里面创建 `settings.json`：

   ```json
   {
     "permissions": {
       "deny": [
         "run_shell(git push*)"
       ]
     }
   }
   ```

2. 启动 Agent，要求其测试 push：

   ```bash
   python __main__.py "将代码强推上库"
   ```
3. 验证安全模式拦截：
   - 观察终端是否出现 `✗ Action Denied: Denied by permission rule for run_shell` 的拦截字样，且没有发生程序报错退出。

*测试完成后，请记得删除 `.claude/settings.json` 以免干扰后边开发。*

---

## 🧠 思考题

1. **为什么在 `load_permission_rules` 中，我们将“全局设置”和“项目本地设置”读取出的规则，是用全局先追加、项目后追加的形式加入同一个数组，并且匹配时 deny 规则的匹配先于 allow 规则遍历？**
    *(提示：这代表项目本地的配置优先级更高，且由于 `deny 优先判定`，用户可以使用 allow 规则放开大部分命令，再专门编写 deny 规则进行细节上的高危排除，是符合优先级覆盖与最小特权原则的安全设计思想。)*
2. **在 `check_permission` 中，为什么 `read_file` 等工具被判定为“永远放行”？读取文件难道不会泄露隐私或密钥吗？**
   *(提示：只读工具不具备写盘或破坏外部世界物理状态的能力，属于“低爆炸半径”操作。如果读取了密钥，在本地沙箱内仍然是安全的；对它的防护通常应放在外层的环境隔离和网络防泄露上，而不是在工具执行阶段频繁弹框干扰开发心智。)*
3. **为什么 `confirmed_paths`（已确认路径白名单）要在会话级别缓存，而不是每次工具调用都重新确认？**
   *(提示：如果 Agent 要对同一个文件执行多次编辑（比如分步重构），每次都弹窗确认会严重打断用户心流。会话级缓存意味着用户只需确认一次“我允许编辑这个文件”，后续操作自动放行。这是“安全性”与“可用性”之间的工程权衡——会话结束后缓存清空，下次启动仍需重新确认。)*

---

## 📦 本节收获

1. **漏斗防御模型**：学会了利用多层过滤门槛（全局模式、规则库、动态正则扫描）构筑防卫体系。
2. **三态判定法**：掌握了利用 `allow`/`deny`/`confirm` 优雅判定复杂环境操作的架构技巧。
3. **已确认白名单机制**：理解了如何通过会话级白名单在用户安全性与交互丝滑感之间进行完美权衡。

---

> **下一章**：现在 Agent 既快速、交互好又具备安全性。但频繁对话很快就会撑爆大模型的上下文窗口，下一章我们将来构建上下文管理的灵魂——4 层分级压缩流水线。
