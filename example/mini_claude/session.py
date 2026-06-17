"""Session management — JSON file persistence for conversation history."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SESSION_DIR = Path.home() / ".mini-claude" / "sessions"


def _ensure_dir() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def save_session(session_id: str, data: dict[str, Any]) -> None:
    _ensure_dir()
    (SESSION_DIR / f"{session_id}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def load_session(session_id: str) -> dict[str, Any] | None:
    path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning(f"Corrupted session file {path}: {e}")
        return None
    except OSError as e:
        logger.warning(f"Failed to read session file {path}: {e}")
        return None


def list_sessions() -> list[dict[str, Any]]:
    _ensure_dir()
    results = []
    for f in SESSION_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if "metadata" in data:
                results.append(data["metadata"])
        except json.JSONDecodeError as e:
            logger.debug(f"Skipping corrupted session file {f}: {e}")
        except OSError as e:
            logger.debug(f"Skipping unreadable session file {f}: {e}")
    return results


def get_latest_session_id() -> str | None:
    sessions = list_sessions()
    if not sessions:
        return None
    sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    return sessions[0].get("id")
