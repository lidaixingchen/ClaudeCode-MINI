# 第 11 课：技能系统与 AI 脚本

## 🎯 本节目标

为 Agent 构建可复用的 AI 脚本（技能系统）。实现技能的本地扫描与去重发现，开发模板变量（`$ARGUMENTS`）替换引擎，并支持双路调用机制：用户通过交互命令行手动调用斜杠命令（如 `/commit`），以及大模型需要时自主调用 `skill` 工具加载特定的工作流 Prompt。

---

## 🏆 最终效果

完成本节后，你可以在项目中建立统一的自动化 Prompt 模板。

1. **手动命令调用**：
   在终端中输入斜杠命令并传递参数：
   ```bash
   > /commit 修复了 memory 的空指针异常
   ```
   系统会自动定位到你定义的 `commit.md` 模板，将其中的 `$ARGUMENTS` 替换为 `"修复了 memory 的空指针异常"`，然后注入为当前会话的 `user` 消息，驱动 Agent 自动分析差异、写出标准的 Commit 日志并执行提交。

2. **模型自动触发**：
   当用户向 Agent 发送一句模糊的陈述（如：“帮我把当前改动提交了”）时，大模型会基于 System Prompt 里的描述，主动调用 `skill` 元工具，拉取对应的模板指令注入到上下文中自我执行。

---

## 🛠️ 本节任务

1. **实现技能扫描与去重加载**：在 `skills.py` 中编写 `discover_skills()`，扫描 `.claude/skills/{名称}/SKILL.md` 子目录结构，实现全局和项目本地技能的加载，同名技能项目级覆盖用户级。
2. **实现模板参数替换解析**：在 `skills.py` 中实现 `resolve_skill_prompt()`，支持对 `$ARGUMENTS` 及路径进行动态替换。
3. **在 REPL 中拦截手动指令**：在 `__main__.py` 中拦截以 `/` 开头的输入命令，由用户手动激活技能。
4. **注册 skill 元工具并分发**：在 `tools.py` 中注册 `skill` 工具，允许大模型在需要时内联调用。

---

## 📦 涉及文件

修改：
- `skills.py`
- `tools.py`
- `__main__.py`
- `prompt.py`

---

## 🚀 开始实现

### 步骤 1：技能自动扫描与优先级去重

#### 为什么做

技能是存放在目录 `.claude/skills/` 下的子目录中，每个技能是一个独立文件夹，包含一个 `SKILL.md` 主文件。我们应当支持两个加载源：
1. 全局配置源：`~/.claude/skills/{技能名}/SKILL.md`，用于存放用户通用的脚本（如通用的 commit 规范）。
2. 本地项目源：`./.claude/skills/{技能名}/SKILL.md`，用于存放当前项目特有的流程（如特定项目的部署指南）。
项目本地的技能具有更高优先级，同名时必须去重并覆盖全局的同名技能。

#### 做什么

创建（或覆写）`skills.py`，编写技能类的定义和目录递归扫描逻辑：

```python
# skills.py

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from frontmatter import parse_frontmatter


@dataclass
class SkillDefinition:
    """技能的元数据定义，包含名称、描述、模板等信息。"""
    name: str                                          # 技能唯一名称
    description: str                                   # 一句话描述
    when_to_use: str | None = None                     # 模型判断何时调用此技能的指引
    allowed_tools: list[str] | None = None             # 技能可使用的工具白名单
    user_invocable: bool = True                        # 是否允许用户通过 /name 手动调用
    context: str = "inline"                            # 执行模式："inline" 或 "fork"
    prompt_template: str = ""                          # Prompt 模板正文
    source: str = "project"                            # 来源："project"（项目级）或 "user"（用户级）
    skill_dir: str = ""                                # 技能目录的绝对路径


# 模块级缓存，避免重复扫描文件系统
_cached_skills: list[SkillDefinition] | None = None


def discover_skills() -> list[SkillDefinition]:
    """发现并返回所有已注册的技能，带缓存机制。

    优先级规则：项目本地技能 > 用户全局技能（同名时覆盖）。
    """
    global _cached_skills
    if _cached_skills is not None:
        return _cached_skills

    # 使用字典以技能名为 key，确保同名技能自动覆盖实现优先级
    skills: dict[str, SkillDefinition] = {}

    # 1. 扫描低优先级的用户全局技能
    _load_skills_from_dir(Path.home() / ".claude" / "skills", "user", skills)
    # 2. 扫描高优先级的项目本地技能（同名 key 覆盖，自动实现优先级）
    _load_skills_from_dir(Path.cwd() / ".claude" / "skills", "project", skills)

    _cached_skills = list(skills.values())
    return _cached_skills


def _load_skills_from_dir(directory: Path, source: str, skills: dict[str, SkillDefinition]) -> None:
    """从指定目录扫描并加载技能定义，填充到 skills 字典中。"""
    if not directory.is_dir():
        return
    # 遍历子目录，每个子目录是一个独立技能，内含 SKILL.md 主文件
    for entry in directory.iterdir():
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.exists():
            continue
        try:
            raw = skill_file.read_text(encoding="utf-8")
            result = parse_frontmatter(raw)
            meta = result.meta

            # 默认提取 YAML 头中的 name，缺省则以子目录名作为技能名
            name = meta.get("name") or entry.name
            user_invocable = meta.get("user-invocable", "true").lower() != "false"

            context = meta.get("context") or "inline"
            allowed_tools = None
            if "allowed-tools" in meta:
                try:
                    # 优先尝试 JSON 数组格式：["tool1", "tool2"]
                    import json
                    allowed_tools = json.loads(meta["allowed-tools"])
                except Exception:
                    # 降级为逗号分隔格式：tool1, tool2
                    allowed_tools = [t.strip() for t in meta["allowed-tools"].split(",")]

            # 使用字典赋值，同名技能会自动覆盖（实现优先级机制）
            skills[name] = SkillDefinition(
                name=name,
                description=meta.get("description", ""),
                when_to_use=meta.get("when_to_use") or meta.get("when-to-use", ""),
                prompt_template=result.body,
                user_invocable=user_invocable,
                skill_dir=str(entry),
                source=source,
                context=context,
                allowed_tools=allowed_tools,
            )
        except Exception:
            pass


#### 注意什么

- **数据字段的完整性**：在 `SkillDefinition` 中我们补充了 `source`, `context`, `allowed_tools` 属性，它们对于 REPL 渲染指令和后续的高级沙箱（Fork Mode）工具集必不可少。
- **配置文件的编码**：读取技能文件夹下的 `SKILL.md` 时，必须显式指定 `encoding="utf-8"`。
```

---

### 步骤 2：实现模板变量替换解析

#### 为什么做

技能文件本质上是一个 Prompt 模板。当用户在命令行发起 `/commit 修复拼写错误` 时，我们需要提取用户传入的参数（`"修复拼写错误"`），用其替换模板文件中的 `$ARGUMENTS` 占位符。同时需将 `${CLAUDE_SKILL_DIR}` 替换为技能的物理路径，使技能可以利用其他附属资源文件。

#### 做什么

在 `skills.py` 中实现模板替换方法：

```python
# skills.py（续）


def resolve_skill_prompt(skill: SkillDefinition, args: str) -> str:
    """将技能模板中的占位符替换为实际参数值。"""
    prompt = skill.prompt_template
    # 使用 re.sub 兼容 $ARGUMENTS 和 ${ARGUMENTS} 两种格式
    prompt = re.sub(r"\$ARGUMENTS|\$\{ARGUMENTS\}", args, prompt)
    # 替换路径占位符，使技能可以引用目录下的其他资源文件
    prompt = prompt.replace("${CLAUDE_SKILL_DIR}", skill.skill_dir)
    return prompt


def get_skill_by_name(name: str) -> SkillDefinition | None:
    """根据名称查找已注册的技能，未找到返回 None。"""
    for s in discover_skills():
        if s.name == name:
            return s
    return None


def execute_skill(
    skill_name: str, args: str
) -> dict | None:
    """执行技能的原子调用：查找、解析、替换三步封装。

    返回包含 prompt、allowed_tools、context 的字典，
    上层调用者无需关心底层细节，直接根据返回字典驱动执行。
    """
    skill = get_skill_by_name(skill_name)
    if not skill:
        return None
    return {
        "prompt": resolve_skill_prompt(skill, args),
        "allowed_tools": skill.allowed_tools,
        "context": skill.context,
    }


#### 注意什么

- **正则替换的兼容**：替换 `$ARGUMENTS` 时，使用 `re.sub` 兼容大括号形式的 `${ARGUMENTS}` 可以提升模板编写的容错度。
- **`execute_skill` 的封装意义**：`execute_skill` 将查找（`get_skill_by_name`）、解析、替换三个步骤封装为一个原子调用，返回的字典包含解析后的 Prompt、可选的工具限制（`allowed_tools`）以及执行模式（`context`）。这让上层调用者（如 `_execute_tool_call`）无需关心底层细节，直接根据返回的字典驱动执行。
```

---

### 步骤 3：在 REPL 终端中拦截斜杠指令（用户手动路径）

#### 为什么做

如果用户输入的字符串以斜杠 `/` 开头，这代表他需要主动触发某个技能或系统的内置命令（如 `/clear`）。我们需要解析这个格式：`/{name} {arguments}`，去已加载的技能集中匹配同名的 `user_invocable` 技能。匹配成功后解析模板并将结果提交给 Agent 对话。

#### 做什么

修改 `__main__.py` 的 REPL 主输入循环，增加对斜杠技能命令的分发解析：

```python
# __main__.py 中的修改

        # ... 在 run_repl 循环内捕获用户输入 inp 之后：
        if not inp:
            continue
            
        # 检测是否是斜杠技能命令
        if inp.startswith("/"):
            # 解析 /{name} {arguments} 格式
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""

            # 判断是否是已注册的技能
            from skills import discover_skills, resolve_skill_prompt
            skills = discover_skills()
            skill = next((s for s in skills if s.name == cmd_name), None)

            if skill and skill.user_invocable:
                resolved = resolve_skill_prompt(skill, cmd_args)
                print(f"  [cyan]ℹ Invoking skill: {skill.name}[/cyan]")
                # 将解析后的模板作为 user message 注入 Agent 对话
                await agent.chat(resolved)
                continue

            # 若非技能，判定是否是系统内置 CLI 指令
            if cmd_name not in ("clear", "plan", "cost", "compact"):
                print_error(f"Unknown skill command: /{cmd_name}")
                continue


#### 注意什么

- **技能拦截逻辑**：注意在 REPL 的命令拦截中，必须优先匹配已注册的技能，若没有命中，再检查系统内置的 REPL 命令。如果是用户禁用的技能（`user_invocable` 为 False），则不允许用户在终端手动通过 `/` 指令调起。
```

---

### 步骤 4：注册 `skill` 元工具（大模型调用路径）

#### 为什么做

大模型如何自动触发技能？
1. 我们必须向其暴露一个名为 `skill` 的工具，接收参数 `skill_name` 和 `args`。
2. 当用户说：“帮我把这次修改提交了”，大模型检测到该请求符合 `commit` 技能的 `when_to_use` 规范，会决定调用 `skill(skill_name="commit", args="...")`。
3. 工具执行被分发到 `execute_tool`，解析并渲染该技能的 Prompt，最终以 `[Skill "commit" activated...]` 的文本作为 `tool_result` 返回。
4. 模型在下一个思考回合读取到了这个 Prompt 指令，便会按照指令要求开始读取 Git status、撰写日志并调用 `run_shell` 执行提交，实现自动运行。

#### 做什么

修改 `tools.py`，注册 `skill` 工具并加入分发逻辑：

```python
# tools.py 中的修改

# 1. 注册工具声明
tool_definitions: list[dict] = [
    # ... read_file / write_file 等工具保持不变
    {
        "name": "skill",
        # 元指令工具：返回的不是数据，而是指导模型行为的 Prompt 指令
        "description": "Invoke a registered skill by name. Returns the skill's resolved prompt template to follow.",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "The name of the skill to invoke"},
                "args": {"type": "string", "description": "Optional arguments to pass to the skill"},
            },
            "required": ["skill_name"],
        },
    },
]

# ...

# 2. 在 agent.py 的 _execute_tool_call 中挂载 skill 工具的分发路由
# 必须在 agent.py 而非 tools.py 中，避免循环导入（Fork Mode 需要实例化 Agent）
async def _execute_tool_call(self, name: str, inp: dict) -> str:
    # ... 挂载对 skill 工具的调用分发
    if name == "skill":
        from skills import execute_skill
        skill_name = inp.get("skill_name", "")
        args = inp.get("args", "")

        result = execute_skill(skill_name, args)
        if not result:
            return f"Error: Unknown skill: {skill_name}"

        # 内联模式：将解析后的提示词作为 tool_result 返回给模型
        # 模型会在下一回合将其视作新的指导方针继续执行
        return f'[Skill "{skill_name}" activated. Follow these instructions:]\n\n{result["prompt"]}'


#### 注意什么

- **避免循环导入与路由正确性**：为什么必须在 `agent.py` 的 `_execute_tool_call()` 内部去拦截并路由 `skill` 工具，而不是在 `tools.py` 的全局 `execute_tool` 中执行？因为技能如果被配置为沙箱分支模式（Fork Mode），需要就地重新实例化一个新的 `Agent`（子会话代理）来执行。这需要访问高层的 `Agent` 类，如果将其写在底层 `tools.py` 模块中，会触发 `tools.py` 与 `agent.py` 的严重循环导入错误。
- **内联文本的作用机制**：Inline 模式下，工具返回解析后的提示词文本，大模型会在收到该 Tool Result 后，将其视作全新的指导方针在下一回合继续执行，从而优雅地改变了模型的指令上下文。
```

在 `prompt.py` 中引入 `build_skill_descriptions` 汇总并注入 `{{skills}}`：

```python
# prompt.py 中的修改

def build_skill_descriptions() -> str:
    """构建技能描述段落，注入到 System Prompt 中供模型识别可用技能。"""
    from skills import discover_skills
    skills = discover_skills()
    if not skills:
        return ""

    lines = ["# Available Skills", ""]
    # 将技能分为两组：用户可手动调用的 vs 仅模型自动调用的
    invocable = [s for s in skills if s.user_invocable]
    auto_only = [s for s in skills if not s.user_invocable]

    if invocable:
        lines.append("User-invocable skills (user types /<name> to invoke):")
        for s in invocable:
            lines.append(f"- **/{s.name}**: {s.description}")
            if s.when_to_use:
                lines.append(f"  When to use: {s.when_to_use}")
        lines.append("")

    if auto_only:
        lines.append("Auto-invocable skills (use the skill tool when appropriate):")
        for s in auto_only:
            lines.append(f"- **{s.name}**: {s.description}")
            if s.when_to_use:
                lines.append(f"  When to use: {s.when_to_use}")
        lines.append("")

    lines.append("To invoke a skill programmatically, use the `skill` tool with the skill name and optional arguments.")
    return "\n".join(lines)
```

并确保该函数返回值在 `build_system_prompt()` 中替换了 `{{skills}}` 占位符。

> ⚠️ **别忘了回到 `prompt.py` 接线！** 你需要做两件事：
>
> 1. 文件顶部添加 `from skills import build_skill_descriptions`
> 2. 在 `build_system_prompt()` 函数中调用它，将返回值替换 `{{skills}}` 占位符

#### 注意什么

- **用户可调用 vs 仅自动调用**：`build_skill_descriptions` 将技能分为两组呈现给大模型——`invocable`（用户可通过 `/name` 手动调用）和 `auto_only`（仅由模型在需要时通过 `skill` 工具自主调用）。这种区分让模型在 System Prompt 中就能清晰辨别哪些技能可以被用户直接触发、哪些只能由它自己主动发起。

---

## ⚖️ 设计权衡

### 内联模式（Inline） vs 分裂子会话模式（Fork）

- **方案 A**：**内联模式**（我们所用）
  - 工具直接将展开后的技能 Prompt 文本作为普通的 `tool_result` 喂回给当前的 Agent 对话。
  - **优点**：逻辑极度简单，完全共享当前的所有记忆和上下文历史，不需要额外的 Agent 状态复制。
  - **缺点**：如果技能中包含了大量的工具链调用（如 review 技能需要读取 10 个文件），这些临时读取的结果会在当前的消息历史中被越积越多，可能提前触发上下文压缩。
- **方案 B**：**分裂子会话模式（Fork）**
  - 触发技能时，在后台冷启动一个新的 `Agent`（子代理），传入该技能 Prompt 作为其专属 System Prompt，让其在隔离会话里跑完，只把最终的文字结论返回给主会话。
  - **优点**：主会话历史非常干净，不会被技能中途产生的繁杂文件读取碎片污染。
  - **缺点**：架构极度复杂，且要求依赖第 14 章的子代理技术，造成了严重的依赖超前。

**结论**：在基础教程前段，采用 Inline 模式能够最快闭环功能；Fork 模式应当作为后续高阶章节（多 Agent 系统）的重要扩展和对比进行学习。

---

## ⚠️ 常见陷阱

### 1. `ARGUMENTS` 占位符替换发生部分漏换

```python
# ❌ 错误：如果只粗暴进行了 string.replace("$ARGUMENTS") 替换
prompt = prompt.replace("$ARGUMENTS", args)
```

**后果**：由于用户可能会写 `${ARGUMENTS}` 以保证变量与周围字符隔离，或者在模板中多次书写该变量，只替换单种匹配或仅替换一次会导致模板渲染残缺。
**修正**：在 `skills.py` 中使用 `re.sub(r"\$ARGUMENTS|\$\{ARGUMENTS\}", args, prompt)` 进行全局匹配处理。

---

## ✅ 验收点

### 输入与验证

1. 在当前项目下建立 `.claude/skills/commit/` 子目录，并在里面创建一个 `SKILL.md` 文件：
   ```markdown
   ---
   name: commit
   description: Create a git commit with a descriptive message
   when_to_use: When the user asks to commit changes
   user-invocable: true
   ---
   Look at the current git status. Write a conventional commit message.
   User request: $ARGUMENTS
   ```
2. 启动 Agent，验证手动触发：
   ```bash
   python __main__.py
   ```
3. 在终端中键入：`/commit 修复拼写错误` 并按下回车。
4. **观察输出**：验证 Agent 是否输出了 `ℹ Invoking skill: commit` 并开始读取 git 状态。

*测试完成后，请记得删除 `.claude/skills/commit/` 目录。*

---

## 🧠 思考题

1. **为什么在 `skills.py` 的 `_load_skills_from_dir` 扫描逻辑中，我们是用 `skills[name] = ...` 的字典赋值，而不是直接 `list.append` 插入列表？**
   *(提示：使用字典赋值可以确保技能的 `name` 作为唯一的 key。当我们先读全局目录再读项目本地目录时，同名的项目级技能会直接覆盖字典中全局的旧技能，自然而然地实现了高优先级覆盖的特性。)*
2. **`skill` 工具的返回是一段 Prompt，这种将指令作为数据返回给模型的工具，我们称为什么？它和一般的“返回执行数据”（如 read_file）有什么本质区别？**
   *(提示：这被称为“元指令工具（Meta-Instruction Tool）”。普通的工具返回值是环境状态（告诉模型数据是啥），而元工具的返回值是行为准则（告诉模型应该怎么做）。它极大地扩展了模型的逻辑规划能力。)*

---

## 📦 本节收获

1. **元指令设计**：理解了如何通过工具返回 Prompt 指令引导模型自主执行复杂的多步逻辑。
2. **斜杠分流路由**：掌握了在 REPL 界面对用户命令进行拦截、解析及模板替换分发的 CLI 开发技巧。
3. **高优先级覆盖模式**：学会了利用 Key-Map 数据结构实现本地个性化规则自动覆盖全局规则的配置设计。

---

> **下一章**：现在 Agent 具备了脚本级别的 Prompt 模块扩展。下一步我们将教导 Agent 在开始大修大改代码前，如何“三思而后行”——构建 Plan Mode 只读规划系统。
