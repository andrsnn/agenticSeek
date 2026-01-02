from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_mkdirs(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def _truncate(text: Any, limit: int = 4000) -> Any:
    try:
        s = str(text)
    except Exception:
        return text
    if len(s) <= limit:
        return s
    return s[:limit] + f"... [truncated {len(s) - limit} chars]"


class TraceSink:
    """
    Append-only trace sink. Writes JSONL events for easy parsing and a lightweight 'tee' experience.
    """

    def __init__(self, path: str, truncate_limit: Optional[int] = 4000):
        self.path = path
        _safe_mkdirs(self.path)
        self._lock = threading.Lock()
        self.truncate_limit = truncate_limit

    def write_event(self, event: str, **fields: Dict[str, Any]) -> None:
        payload: Dict[str, Any] = {"ts": _utc_iso(), "event": event}
        for k, v in fields.items():
            if self.truncate_limit is None:
                payload[k] = v
            else:
                payload[k] = _truncate(v, limit=self.truncate_limit)
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def write_text(self, text: str, event: str = "text", color: Optional[str] = None) -> None:
        self.write_event(event, text=text, color=color)
