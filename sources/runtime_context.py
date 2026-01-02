from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, Set

from sources.activity_bus import emit_activity
from datetime import datetime, timezone

def _ts_local() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")

class RunMode(str, Enum):
    STANDARD = "standard"
    TRACE = "trace"
    DEEP_RESEARCH = "deep_research"


@dataclass
class TraceConfig:
    """
    Controls what gets recorded in trace/raw mode.
    """
    enabled: bool = True
    # Event filtering (JSONL trace)
    enabled_events: Optional[Set[str]] = None  # None = allow all
    disabled_events: Set[str] = field(default_factory=set)

    # Artifact writing toggles
    save_query: bool = True
    save_plan: bool = True
    save_intermediate_outputs: bool = True
    save_final_answer: bool = True
    save_tool_blocks: bool = False
    save_web_snapshots: bool = True
    # Web tracing granularity
    save_web_navigation: bool = True  # web_search_query + web_navigate events
    save_web_page_text: bool = False  # include page_text in page_snapshot (jsonl_only)
    save_web_screenshots: bool = False  # save screenshots into the run folder (binary files)
    save_web_ocr: bool = False  # run OCR on screenshots and embed text into trace.jsonl
    save_web_history: bool = True  # emit a consolidated browser_history list for easy copy-out
    save_tool_outputs: bool = True
    save_chat_transcript: bool = True
    chat_transcript_file: str = "chat.txt"

    # Sources tab (NotebookLM-style): OFF by default; can be enabled per-run from UI.
    save_sources: bool = False
    # Whether to call the LLM for sources scoring/enrichment. Keeping this OFF avoids GPU stalls.
    # When False, sources will still capture URLs/screenshots/OCR context (raw-only).
    save_sources_llm: bool = False

    # Output formatting
    outputs_format: str = "jsonl_only"  # markdown | jsonl_only
    trace_max_chars_per_field: Optional[int] = None  # None = no truncation in trace.jsonl

    def allow_event(self, event: str) -> bool:
        if not self.enabled:
            return False
        if event in self.disabled_events:
            return False
        if self.enabled_events is None:
            return True
        return event in self.enabled_events


@dataclass
class ToolConfig:
    """
    Controls which tools are allowed to run (and a few output preferences).
    Names refer to the keys in each agent's `self.tools` dict (e.g. "web_search", "bash", "python").
    """
    enabled: bool = True
    enabled_tools: Optional[Set[str]] = None  # None = allow all (except disabled_tools)
    disabled_tools: Set[str] = field(default_factory=set)

    # Output preferences
    spreadsheet_format: str = "csv"  # csv | xlsx
    default_output_format: str = "none"  # none | md | csv - suggests output type to planner

    def allow_tool(self, tool_key: str, tool_tag: Optional[str] = None) -> bool:
        if not self.enabled:
            return True
        key = (tool_key or "").strip()
        tag = (tool_tag or "").strip()
        if key in self.disabled_tools or tag in self.disabled_tools:
            return False
        if self.enabled_tools is None:
            return True
        return (key in self.enabled_tools) or (tag in self.enabled_tools)


@dataclass
class AgentConfig:
    """
    Controls which agents are allowed to be selected/executed.
    Values refer to agent.type (e.g. 'code_agent') OR agent.role (e.g. 'code').
    """
    enabled: bool = True
    enabled_agents: Optional[Set[str]] = None  # None = allow all (except disabled_agents)
    disabled_agents: Set[str] = field(default_factory=set)

    def allow_agent(self, agent_type: Optional[str], agent_role: Optional[str]) -> bool:
        if not self.enabled:
            return True
        t = (agent_type or "").strip().lower()
        r = (agent_role or "").strip().lower()
        if t in self.disabled_agents or r in self.disabled_agents:
            return False
        if self.enabled_agents is None:
            return True
        return (t in self.enabled_agents) or (r in self.enabled_agents)


@dataclass
class RunContext:
    mode: RunMode = RunMode.STANDARD
    work_dir: str = "."
    run_id: Optional[str] = None
    output_dir: Optional[str] = None
    trace_file: Optional[str] = None
    findings_file: Optional[str] = None
    trace_config: TraceConfig = field(default_factory=TraceConfig)
    tool_config: ToolConfig = field(default_factory=ToolConfig)
    agent_config: AgentConfig = field(default_factory=AgentConfig)
    # objects
    trace_sink: Optional[object] = None  # TraceSink, but avoid import cycles

    def is_trace_enabled(self) -> bool:
        return self.mode in (RunMode.TRACE, RunMode.DEEP_RESEARCH) and self.trace_sink is not None and self.trace_config.enabled


_CTX: Optional[RunContext] = None


def set_run_context(ctx: Optional[RunContext]) -> None:
    global _CTX
    _CTX = ctx


def get_run_context() -> Optional[RunContext]:
    return _CTX


def trace_event(event: str, **fields: Dict[str, Any]) -> None:
    """
    Best-effort structured tracing. Never raises.
    """
    ctx = get_run_context()
    # Always emit to UI activity bus (best-effort), even in standard mode.
    try:
        if ctx is not None and ctx.trace_config.allow_event(event):
            emit_activity(event, run_id=ctx.run_id, **fields)
        elif ctx is None:
            emit_activity(event, run_id=None, **fields)
    except Exception:
        pass

    # File tracing only in trace/deep_research mode
    if ctx is None or not ctx.is_trace_enabled():
        return
    if not ctx.trace_config.allow_event(event):
        return

    # Append a human-readable transcript line (Raw mode).
    try:
        if ctx.trace_config.save_chat_transcript and ctx.trace_config.outputs_format != "jsonl_only":
            # Lazy import to avoid circular imports at startup (runtime_context <-> artifacts).
            from sources import artifacts
            role = "EVENT"
            if event == "user_query":
                role = "USER"
                msg = fields.get("query", "")
            elif event == "final_answer":
                role = "ASSISTANT"
                msg = fields.get("answer", "")
            elif event == "browser_notes":
                role = "NOTE"
                msg = fields.get("notes", "")
            elif event == "web_search_query":
                role = "SEARCH"
                msg = fields.get("query", "")
            elif event == "web_navigate":
                role = "NAVIGATE"
                msg = fields.get("url", "")
            elif event == "page_snapshot":
                role = "PAGE"
                msg = f"{fields.get('title','')}".strip()
                if fields.get("url"):
                    msg = (msg + " — " if msg else "") + str(fields.get("url"))
            elif event == "plan_step":
                role = "PLAN"
                st = fields.get("status", "")
                step = fields.get("step", {}) or {}
                msg = f"{st} {step.get('title') or step.get('task') or ''}".strip()
            elif event == "tool_executed":
                role = "TOOL"
                tool = fields.get("tool", "")
                ok = fields.get("success", True)
                msg = f"{tool} {'ok' if ok else 'failed'}"
                if ok is False:
                    fb = fields.get("feedback", "")
                    if fb:
                        msg += f"\n{fb}"
            else:
                # Keep other events short
                msg = ""
            if msg:
                artifacts.append_chat(f"[{_ts_local()}] {role}: {msg}")
    except Exception:
        pass

    try:
        ctx.trace_sink.write_event(event, **fields)
    except Exception:
        # Tracing should never break the agent.
        return


def trace_text(text: str, event: str = "text", **fields: Dict[str, Any]) -> None:
    """
    Convenience for freeform text lines.
    """
    trace_event(event, text=text, **fields)
