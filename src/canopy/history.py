"""Query history — append, load, and clear the history JSONL file."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from canopy._json import Encoder

_log = logging.getLogger("canopy.history")
_lock = threading.Lock()

if TYPE_CHECKING:
    from canopy.query.loop import LoopResult


def _history_file() -> Path:
    from canopy.config import get_data_dir  # local import avoids circular at module level
    return get_data_dir() / "history.jsonl"


def append_history(result: LoopResult) -> None:
    """Append a completed query result to the history file.

    Creates the data directory if it does not exist. Each call appends one
    JSON line. Rows are serialised as lists (JSON has no tuple type).
    """
    path = _history_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "question": result.question,
        "sql": result.sql,
        "columns": result.columns,
        "rows": [list(row) for row in result.rows],
        "row_count": result.row_count,
        "model_text": result.model_text,
    }
    with _lock, path.open("a") as f:
        f.write(json.dumps(entry, cls=Encoder) + "\n")


def load_history(n: int = 20) -> list[dict]:
    """Return the last n history entries in chronological order.

    Returns an empty list if the history file does not exist or n <= 0.
    Corrupt lines are silently skipped.
    """
    path = _history_file()
    if n <= 0 or not path.exists():
        return []
    lines = path.read_text().splitlines()
    entries = []
    for line in lines[-n:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            _log.warning("skipping corrupt history line in %s", path)
    return entries


def clear_history() -> None:
    """Delete the history file. No-op if it does not exist."""
    path = _history_file()
    if path.exists():
        path.unlink()
