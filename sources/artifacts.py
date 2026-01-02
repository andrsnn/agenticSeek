from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from sources.runtime_context import get_run_context, trace_event


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_join(base: str, rel: str) -> Optional[str]:
    if not base:
        return None
    base_abs = os.path.abspath(base)
    r = str(rel).replace("\\", os.sep).replace("/", os.sep).lstrip(os.sep)
    target = os.path.abspath(os.path.join(base_abs, r))
    if os.path.commonpath([base_abs, target]) != base_abs:
        return None
    return target


def ensure_run_dir() -> Optional[str]:
    ctx = get_run_context()
    if ctx is None:
        return None
    if not ctx.output_dir:
        return None
    try:
        os.makedirs(ctx.output_dir, exist_ok=True)
        return ctx.output_dir
    except Exception:
        return None


def write_text(rel_path: str, content: str, append: bool = False) -> Optional[str]:
    ctx = get_run_context()
    if ctx is None or not ctx.is_trace_enabled():
        return None
    # Single-file trace mode: avoid creating additional artifacts.
    try:
        if getattr(ctx.trace_config, "outputs_format", "jsonl_only") == "jsonl_only":
            return None
    except Exception:
        return None
    base = ensure_run_dir()
    if not base:
        return None
    target = _safe_join(base, rel_path)
    if not target:
        return None
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(content or "")
            if append and content and not content.endswith("\n"):
                f.write("\n")
        trace_event("artifact_written", path=target, rel_path=rel_path, append=append)
        return target
    except Exception as e:
        trace_event("artifact_write_failed", rel_path=rel_path, error=str(e))
        return None


def write_json(rel_path: str, obj: Any) -> Optional[str]:
    try:
        content = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        content = json.dumps({"error": "failed to serialize"}, ensure_ascii=False, indent=2)
    return write_text(rel_path, content + "\n", append=False)


def write_markdown_snapshot(kind: str, title: str, body: str) -> Optional[str]:
    """
    Write a timestamped markdown snapshot, e.g. intermediate report.
    """
    stamp = _utc_stamp()
    safe_kind = "".join([c for c in kind if c.isalnum() or c in ("_", "-")]) or "snapshot"
    rel = os.path.join("snapshots", safe_kind, f"{stamp}.md")
    md = f"# {title}\n\n{body}\n"
    return write_text(rel, md, append=False)


def write_tool_output(tool_name: str, content: str) -> Optional[str]:
    """
    Persist raw tool output to a file under the run folder.
    """
    stamp = _utc_stamp()
    safe_tool = "".join([c for c in str(tool_name) if c.isalnum() or c in ("_", "-")]) or "tool"
    rel = os.path.join("tool_outputs", safe_tool, f"{stamp}.txt")
    return write_text(rel, str(content or "") + "\n", append=False)


def append_chat(line: str) -> Optional[str]:
    """
    Append a line to the per-run chat transcript file, if enabled.
    """
    ctx = get_run_context()
    if ctx is None or not ctx.is_trace_enabled():
        return None
    tc = getattr(ctx, "trace_config", None)
    if tc is None or not getattr(tc, "save_chat_transcript", False):
        return None
    if getattr(tc, "outputs_format", "jsonl_only") == "jsonl_only":
        return None
    rel = getattr(tc, "chat_transcript_file", "chat.txt") or "chat.txt"
    return write_text(rel, line.rstrip("\n") + "\n", append=True)
