from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 会话文件存储目录，位于当前工作目录下
SESSION_DIR = Path.cwd() / ".mini-claude" / "sessions"

def _ensure_dir() -> None:
    """确保存储目录存在，不存在则递归创建"""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def save_session(session_id: str, data: dict[str, Any]) -> None:
    """将会话数据序列化为 JSON 并写入磁盘"""
    _ensure_dir()
    # 使用 indent=2 生成可读的 JSON，default=str 处理 datetime 等不可序列化类型
    (SESSION_DIR / f"{session_id}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

def load_session(session_id: str) -> dict[str, Any] | None:
    """从磁盘加载指定 ID 的会话数据，不存在或损坏则返回 None。"""
    path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # 文件存在但内容不是有效 JSON，可能是写入过程中被打断了
        return None
    except OSError:
        # 其他 I/O 错误，如权限问题等
        return None
    
def list_sessions() -> list[dict[str, Any]]:
    """列出所有已保存会话的元数据摘要。"""
    _ensure_dir()
    results = []
    for f in SESSION_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if "metadata" in data:
                results.append(data["metadata"])
        except json.JSONDecodeError:
            # 跳过损坏的会话文件
            continue
        except OSError:
            # 跳过无法读取的文件
            continue
    return results

def get_latest_session_id() -> str | None:
    """获取最近一次会话的 ID，用于 --resume 恢复。"""
    sessions = list_sessions()
    if not sessions:
        return None
    # 按 startTime 降序排序，取最新的一个
    sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    return sessions[0].get("id")
