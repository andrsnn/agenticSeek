from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ActivityBus:
    """
    In-memory activity/event buffer for UI. This is NOT persistent.

    Events are stored with a monotonically increasing integer id, and optionally a run_id.
    """

    def __init__(self, max_events: int = 2000):
        self._max = max_events
        self._lock = threading.Lock()
        self._events: Deque[Dict[str, Any]] = deque()
        self._next_id = 1

    def emit(self, event: str, run_id: Optional[str] = None, **fields: Any) -> int:
        with self._lock:
            eid = self._next_id
            self._next_id += 1
            payload: Dict[str, Any] = {
                "id": eid,
                "ts": _utc_iso(),
                "event": event,
                "run_id": run_id,
                "fields": fields or {},
            }
            self._events.append(payload)
            while len(self._events) > self._max:
                self._events.popleft()
            return eid

    def get(self, run_id: Optional[str] = None, since_id: int = 0, limit: int = 200) -> Dict[str, Any]:
        with self._lock:
            latest_id = self._next_id - 1
            items: List[Dict[str, Any]] = []
            for ev in self._events:
                if ev["id"] <= since_id:
                    continue
                if run_id is not None and ev.get("run_id") != run_id:
                    continue
                items.append(ev)
                if len(items) >= limit:
                    break
            last_id = items[-1]["id"] if items else since_id
            return {"events": items, "next_since_id": last_id, "latest_id": latest_id}

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._next_id = 1


_BUS = ActivityBus()


def emit_activity(event: str, run_id: Optional[str] = None, **fields: Any) -> None:
    try:
        _BUS.emit(event, run_id=run_id, **fields)
    except Exception:
        return


def get_activity(run_id: Optional[str] = None, since_id: int = 0, limit: int = 200) -> Dict[str, Any]:
    return _BUS.get(run_id=run_id, since_id=since_id, limit=limit)


def reset_activity() -> None:
    try:
        _BUS.reset()
    except Exception:
        return
