# Mini Claude Code — Python 版

**需要 Python >= 3.11**。


## 快速开始

```bash
# 安装（需要 Python 3.11+）
cd python
pip install -e .

# 设置 API Key
export ANTHROPIC_API_KEY=sk-ant-...

# 运行
mini-claude-py "hello"               # 一次性模式
mini-claude-py                       # 交互式 REPL
mini-claude-py --yolo "list files"   # 跳过确认
mini-claude-py --plan "refactor this" # 计划模式
python -m mini_claude "hello"        # 也可以用 python -m 方式运行

# 使用 OpenAI 兼容后端
OPENAI_API_KEY=sk-xxx mini-claude-py --api-base https://api.openai.com/v1 --model gpt-4o "hello"
```

## 文件结构

| 文件 | 说明 |
|------|------|
| `agent.py` | Agent 核心循环、双后端、4 层压缩 |
| `tools.py` | 13 个工具 + 5 种权限模式 |
| `__main__.py` | CLI 入口与 REPL |
| `ui.py` | 终端 UI（rich） |
| `prompt.py` | 系统提示词构造 |
| `session.py` | 会话管理 |
| `memory.py` | 记忆系统 |
| `skills.py` | 技能系统 |
| `subagent.py` | 子 Agent |
| `frontmatter.py` | YAML frontmatter 解析 |

## 依赖

- `anthropic` — Anthropic SDK（流式）
- `openai` — OpenAI SDK（兼容后端）
- `rich` — 终端彩色输出
