#!/usr/bin/env python3

import os, sys
import re
import uvicorn
import aiofiles
import configparser
import asyncio
import time
from typing import List
import json
from fastapi import FastAPI
from fastapi import Body
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uuid
import threading
from collections import deque

from sources.llm_provider import Provider
from sources.interaction import Interaction
from sources.agents import CasualAgent, CoderAgent, FileAgent, PlannerAgent, BrowserAgent
from sources.browser import Browser, create_driver
from sources.utility import pretty_print
from sources.logger import Logger
from sources.schemas import QueryRequest, QueryResponse
from sources.runtime_context import RunContext, RunMode, set_run_context
from sources.runtime_context import TraceConfig, ToolConfig, AgentConfig
from sources.trace_sink import TraceSink
from sources.workdir import resolve_work_dir
from sources.activity_bus import get_activity, reset_activity
from sources.sources_store import (
    get_sources as _get_sources,
    reset_sources as _reset_sources,
    render_sources_markdown as _render_sources_markdown,
    import_sources as _import_sources,
)

from dotenv import load_dotenv

load_dotenv()

def _safe_run_prefix(project_name: str | None, max_len: int = 40) -> str | None:
    """
    Return a filesystem-safe prefix for run_id/output_dir.
    """
    if not project_name:
        return None
    s = str(project_name).strip().lower()
    if not s:
        return None
    out = []
    last_dash = False
    for ch in s:
        if ch.isalnum() or ch in ("_", ".", "-"):
            out.append(ch)
            last_dash = False
        elif ch.isspace():
            if not last_dash:
                out.append("-")
                last_dash = True
        else:
            if not last_dash:
                out.append("-")
                last_dash = True
    slug = "".join(out).strip("-. _")
    slug = slug.replace("/", "-").replace("\\", "-").replace(os.sep, "-")
    slug = slug[:max_len].strip("-. _")
    return slug or None


def is_running_in_docker():
    """Detect if code is running inside a Docker container."""
    # Method 1: Check for .dockerenv file
    if os.path.exists('/.dockerenv'):
        return True
    
    # Method 2: Check cgroup
    try:
        with open('/proc/1/cgroup', 'r') as f:
            return 'docker' in f.read()
    except:
        pass
    
    return False


from celery import Celery

api = FastAPI(title="AgenticSeek API", version="0.1.0")
celery_app = Celery("tasks", broker="redis://localhost:6379/0", backend="redis://localhost:6379/0")
celery_app.conf.update(task_track_started=True)
logger = Logger("backend.log")
config = configparser.ConfigParser()
config.read('config.ini')

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve screenshots from the same folder the Browser writes to.
# Prefer WORK_DIR (mounted workspace in Docker) so the host can see updates.
_WORK_DIR = os.getenv("WORK_DIR")
_SCREENSHOT_DIR = os.path.join(_WORK_DIR, ".screenshots") if _WORK_DIR else os.path.join(os.getcwd(), ".screenshots")
os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
api.mount("/screenshots", StaticFiles(directory=_SCREENSHOT_DIR), name="screenshots")

def initialize_system():
    stealth_mode = config.getboolean('BROWSER', 'stealth_mode')
    personality_folder = "jarvis" if config.getboolean('MAIN', 'jarvis_personality') else "base"
    languages = config["MAIN"]["languages"].split(' ')
    
    # Force headless mode in Docker containers
    headless = config.getboolean('BROWSER', 'headless_browser')
    if is_running_in_docker() and not headless:
        # Print prominent warning to console (visible in docker-compose output)
        print("\n" + "*" * 70)
        print("*** WARNING: Detected Docker environment - forcing headless_browser=True ***")
        print("*** INFO: To see the browser, run 'python cli.py' on your host machine ***")
        print("*" * 70 + "\n")
        
        # Flush to ensure it's displayed immediately
        sys.stdout.flush()
        
        # Also log to file
        logger.warning("Detected Docker environment - forcing headless_browser=True")
        logger.info("To see the browser, run 'python cli.py' on your host machine instead")
        
        headless = True
    
    provider = Provider(
        provider_name=config["MAIN"]["provider_name"],
        model=config["MAIN"]["provider_model"],
        server_address=config["MAIN"]["provider_server_address"],
        is_local=config.getboolean('MAIN', 'is_local')
    )
    logger.info(f"Provider initialized: {provider.provider_name} ({provider.model})")

    def _driver_factory():
        return create_driver(headless=headless, stealth_mode=stealth_mode, lang=languages[0])

    browser = Browser(
        _driver_factory(),
        anticaptcha_manual_install=stealth_mode,
        driver_factory=_driver_factory,
    )
    logger.info("Browser initialized")

    agents = [
        CasualAgent(
            name=config["MAIN"]["agent_name"],
            prompt_path=f"prompts/{personality_folder}/casual_agent.txt",
            provider=provider, verbose=False
        ),
        CoderAgent(
            name="coder",
            prompt_path=f"prompts/{personality_folder}/coder_agent.txt",
            provider=provider, verbose=False
        ),
        FileAgent(
            name="File Agent",
            prompt_path=f"prompts/{personality_folder}/file_agent.txt",
            provider=provider, verbose=False
        ),
        BrowserAgent(
            name="Browser",
            prompt_path=f"prompts/{personality_folder}/browser_agent.txt",
            provider=provider, verbose=False, browser=browser
        ),
        PlannerAgent(
            name="Planner",
            prompt_path=f"prompts/{personality_folder}/planner_agent.txt",
            provider=provider, verbose=False, browser=browser
        )
    ]
    logger.info("Agents initialized")

    interaction = Interaction(
        agents,
        tts_enabled=config.getboolean('MAIN', 'speak'),
        stt_enabled=config.getboolean('MAIN', 'listen'),
        recover_last_session=config.getboolean('MAIN', 'recover_last_session'),
        langs=languages
    )
    logger.info("Interaction initialized")
    return interaction

_SKIP_INIT = os.getenv("AGENTICSEEK_SKIP_INIT", "0") == "1"
interaction = None if _SKIP_INIT else initialize_system()
is_generating = False
query_resp_history = []
_paused = False

# Power management settings (UI-configurable; default OFF).
_power_lock = threading.Lock()
_power_settings = {
    "sleep_when_queue_done": False,
    "sleep_after_idle_seconds": 3 * 60 * 60,  # 3 hours by default
    "sleep_after_idle_enabled": False,        # OFF by default
    "sleep_grace_seconds": 30,
}
_last_nonidle_ts = time.time()
_sleep_issued = False

def _host_sleep_allowed() -> bool:
    # Safety gate: must be explicitly enabled and not running inside Docker.
    return (os.getenv("AGENTICSEEK_ALLOW_HOST_SLEEP", "0") == "1") and (not is_running_in_docker())

def _issue_host_sleep(reason: str = "") -> bool:
    """
    Best-effort attempt to put the host to sleep (ONLY when running on host, not in Docker).
    Returns True if a sleep command was issued.
    """
    global _sleep_issued
    if not _host_sleep_allowed():
        return False
    if _sleep_issued:
        return False
    _sleep_issued = True
    try:
        trace_event("power_sleep_requested", reason=str(reason or ""))
    except Exception:
        pass
    try:
        if sys.platform.startswith("win"):
            # Windows sleep (may require admin/policy). This is best-effort.
            os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
            return True
        # Linux / macOS best-effort
        os.system("systemctl suspend || pmset sleepnow || true")
        return True
    except Exception:
        return False

# Post-run trace summary settings (UI-configurable; default ON to preserve prior behavior).
_post_run_summary_lock = threading.Lock()
_post_run_summary_settings = {
    "enabled": True,
    "max_events": 6000,
    "max_lines": 260,
    # If true, use the same LLM provider/model as the run. If false, use the configured summary provider override.
    "use_run_llm": False,
    # Optional provider override for summaries (when use_run_llm is False).
    "provider_name": None,
    "provider_model": None,
    "provider_server_address": None,
    "provider_is_local": None,
}

def _get_trace_provider(settings: dict | None = None) -> Provider:
    """
    Provider used for post-run summaries when not using the run LLM.
    Preference order:
      1) explicit settings override (from UI)
      2) config.ini [POST_RUN_SUMMARY] overrides (if present)
      3) config.ini [MAIN] defaults
    """
    s = settings or {}
    # Settings override
    pn = s.get("provider_name")
    pm = s.get("provider_model")
    psa = s.get("provider_server_address")
    pil = s.get("provider_is_local")

    # Config override (optional section)
    if not pn:
        pn = config.get("POST_RUN_SUMMARY", "provider_name", fallback=config.get("MAIN", "provider_name", fallback="ollama"))
    if not pm:
        pm = config.get("POST_RUN_SUMMARY", "provider_model", fallback=config.get("MAIN", "provider_model", fallback=""))
    if not psa:
        psa = config.get(
            "POST_RUN_SUMMARY",
            "provider_server_address",
            fallback=config.get("MAIN", "provider_server_address", fallback="http://host.docker.internal:11434"),
        )
    if pil is None:
        try:
            pil = config.getboolean("POST_RUN_SUMMARY", "is_local", fallback=config.getboolean("MAIN", "is_local", fallback=True))
        except Exception:
            pil = True
    return Provider(provider_name=str(pn), model=str(pm), server_address=str(psa), is_local=bool(pil))

# Simple FIFO queue for API queries (single Interaction, sequential execution).
_queue_lock = threading.Lock()
_queue = deque()  # items: dict(uid, query, mode, trace_file, findings_file)
_results = {}     # uid -> result dict (QueryResponse.jsonify()-shape)
_status = {}      # uid -> status string
_uid_to_run_id = {}       # uid -> run_id (assigned when job starts)
_uid_to_output_dir = {}   # uid -> output_dir
_uid_to_trace_file = {}   # uid -> trace_file (if enabled)

# Amendments: notes injected into a running task (keyed by run_id)
_amendments_lock = threading.Lock()
_amendments = {}  # run_id -> list of {"text": str, "timestamp": float}
_active_run_id = None  # The currently executing run_id (set by worker thread)
_active_run_uid = None  # The currently executing uid

def get_active_run_id() -> str | None:
    """Get the run_id of the currently executing task."""
    return _active_run_id

def set_active_run(uid: str, run_id: str) -> None:
    """Set the active run (called when a task starts processing)."""
    global _active_run_id, _active_run_uid
    _active_run_id = run_id
    _active_run_uid = uid

def clear_active_run() -> None:
    """Clear the active run (called when a task finishes)."""
    global _active_run_id, _active_run_uid
    _active_run_id = None
    _active_run_uid = None

def get_amendments(run_id: str) -> list:
    """Get all amendments for a run."""
    with _amendments_lock:
        result = list(_amendments.get(run_id, []))
        # Always log to debug run_id mismatch issues
        stored_keys = list(_amendments.keys())
        print(f"[API] get_amendments('{run_id}') -> {len(result)} amendments. Stored run_ids: {stored_keys}", flush=True)
        return result

def add_amendment(run_id: str, text: str) -> dict:
    """Add an amendment to a run. Returns the amendment dict."""
    import time
    amendment = {"text": text.strip(), "timestamp": time.time()}
    with _amendments_lock:
        if run_id not in _amendments:
            _amendments[run_id] = []
        _amendments[run_id].append(amendment)
        print(f"[API] add_amendment: stored amendment for run_id={run_id}, total={len(_amendments[run_id])}")
        print(f"[API] add_amendment: all run_ids with amendments: {list(_amendments.keys())}")
    return amendment

def clear_amendments(run_id: str) -> None:
    """Clear amendments for a run."""
    with _amendments_lock:
        _amendments.pop(run_id, None)

# UI/session reset id (for "New Run" button)
_ui_session_id = str(uuid.uuid4())

def _provider_from_item(item: dict) -> Provider:
    """
    Build a Provider for this run, using request overrides if provided, otherwise config.ini defaults.
    """
    pn = (item.get("provider_name") or config.get("MAIN", "provider_name", fallback="ollama"))
    pm = (item.get("provider_model") or config.get("MAIN", "provider_model", fallback=""))
    psa = (item.get("provider_server_address") or config.get("MAIN", "provider_server_address", fallback="http://host.docker.internal:11434"))
    pil = item.get("provider_is_local")
    if pil is None:
        pil = config.getboolean("MAIN", "is_local", fallback=True)
    return Provider(provider_name=pn, model=pm, server_address=psa, is_local=bool(pil))

def _safe_join_under(base_dir: str, rel: str) -> str | None:
    if not base_dir:
        return None
    base_abs = os.path.abspath(base_dir)
    r = str(rel).replace("\\", os.sep).replace("/", os.sep).lstrip(os.sep)
    target = os.path.abspath(os.path.join(base_abs, r))
    try:
        if os.path.commonpath([base_abs, target]) != base_abs:
            return None
    except Exception:
        return None
    return target

def _list_run_dirs(work_dir: str, run_parent: str) -> list[dict]:
    base = _safe_join_under(work_dir, run_parent or "runs")
    if not base or not os.path.isdir(base):
        return []
    out = []
    try:
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if os.path.isdir(p):
                out.append({"run_id": name, "output_dir": p})
    except Exception:
        return []
    return sorted(out, key=lambda x: x["run_id"])

def _read_trace_events(trace_path: str, max_events: int = 2500) -> list[dict]:
    events = []
    try:
        with open(trace_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
                if len(events) >= max_events:
                    break
    except Exception:
        return []
    return events

def _compact_event(ev: dict, max_chars: int = 900) -> str:
    try:
        s = json.dumps(ev, ensure_ascii=False)
    except Exception:
        s = str(ev)
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "…"


async def _post_run_trace_summary(run_id: str, prompt: str, trace_file: str, item: dict | None = None, settings: dict | None = None) -> None:
    """
    After a run completes, read its trace and append a bullet summary event back into trace.jsonl.
    Must NOT block the main agent queue.
    """
    if not trace_file or not os.path.exists(trace_file):
        return
    try:
        cfg = settings or {}
        try:
            max_events = int(cfg.get("max_events") or 6000)
        except Exception:
            max_events = 6000
        max_events = max(200, min(50000, max_events))
        try:
            max_lines = int(cfg.get("max_lines") or 260)
        except Exception:
            max_lines = 260
        max_lines = max(30, min(2500, max_lines))

        events = _read_trace_events(trace_file, max_events=max_events)
        if not events:
            return
        digest_lines = _build_digest(events, question=str(prompt or ""), max_lines=max_lines)
        if not digest_lines:
            return
        system = (
            "You are an expert analyst of agent execution traces.\n"
            "You will be given trace excerpts from a single run (JSONL events).\n"
            "Given the user's ORIGINAL PROMPT, summarize what the agent found and did.\n"
            "Return a concise BULLETED LIST only.\n"
            "Do NOT invent sources or links. Prefer URLs from navigation/history/search events.\n"
            "When citing evidence, include the bracketed event index like [123].\n"
            "If the trace excerpts do not contain enough evidence, say so explicitly (e.g., 'insufficient evidence in trace').\n"
            "Do NOT claim you saw pages, quotes, timestamps, or URLs unless they appear in the provided trace.\n"
        )
        user = (
            f"ORIGINAL PROMPT:\n{prompt}\n\n"
            f"TRACE EXCERPTS (JSONL snippets):\n" + "\n".join(digest_lines)
        )
        # Choose provider for the post-run summary.
        use_run_llm = bool(cfg.get("use_run_llm"))
        if use_run_llm and item:
            provider = _provider_from_item(item)
        else:
            provider = _get_trace_provider(cfg)
        summary = await asyncio.to_thread(
            provider.respond,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            False,
        )
        summary_text = str(summary or "").strip()
        if not summary_text:
            return

        # Append summary into same trace file (no truncation).
        try:
            ts = TraceSink(trace_file, truncate_limit=None)
            ts.write_event("run_summary", run_id=run_id, prompt=prompt, summary=summary_text)
        except Exception:
            pass

        # Also emit into in-memory activity for convenience (non-persistent).
        try:
            from sources.activity_bus import emit_activity
            emit_activity("print", run_id=run_id, text=f"Run summary (auto):\n{summary_text}", color="output")
        except Exception:
            pass
    except Exception:
        return

def _build_digest(events: list[dict], question: str, max_lines: int = 220) -> list[str]:
    """
    Build a compact, relevance-biased digest of events for LLM consumption.
    """
    q = (question or "").lower()
    terms = [t for t in re.findall(r"[a-z0-9_\\-]{3,}", q) if t]

    def score(ev: dict) -> int:
        try:
            s = json.dumps(ev, ensure_ascii=False).lower()
        except Exception:
            s = str(ev).lower()
        sc = 0
        for t in terms[:25]:
            if t in s:
                sc += 1
        return sc

    always = {"user_query", "final_answer", "plan_step", "web_search_query", "web_navigate", "browser_notes", "tool_executed", "page_snapshot"}
    picked = []
    for idx, ev in enumerate(events):
        e = ev.get("event")
        if e in always:
            picked.append((idx, ev, 5 + score(ev)))
        else:
            s = score(ev)
            if s > 0:
                picked.append((idx, ev, s))
    picked.sort(key=lambda x: (-x[2], x[0]))
    top = picked[:max_lines]
    top.sort(key=lambda x: x[0])
    lines = []
    for idx, ev, _ in top:
        ts = ev.get("ts") or ""
        e = ev.get("event") or ""
        lines.append(f"[{idx}] {ts} {e} { _compact_event(ev)}")
    return lines

def _enqueue(item: dict) -> None:
    with _queue_lock:
        _queue.append(item)
        _status[item["uid"]] = "queued"


def _dequeue() -> dict | None:
    with _queue_lock:
        if not _queue:
            return None
        return _queue.popleft()


def _queue_len() -> int:
    with _queue_lock:
        return len(_queue)

@api.get("/screenshot")
async def get_screenshot():
    logger.info("Screenshot endpoint called")
    screenshot_path = os.path.join(_SCREENSHOT_DIR, "updated_screen.png")
    if os.path.exists(screenshot_path):
        return FileResponse(
            screenshot_path,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    logger.error("No screenshot available")
    return JSONResponse(
        status_code=404,
        content={"error": "No screenshot available"}
    )

@api.get("/health")
async def health_check():
    logger.info("Health check endpoint called")
    return {"status": "healthy", "version": "0.1.0"}

@api.get("/is_active")
async def is_active():
    logger.info("Is active endpoint called")
    return {"is_active": interaction.is_active}

@api.get("/stop")
async def stop():
    global _paused
    logger.info("Stop endpoint called")
    _paused = True
    if interaction and interaction.current_agent:
        interaction.current_agent.request_stop()
    return JSONResponse(status_code=200, content={"status": "paused"})


@api.get("/resume")
async def resume():
    global _paused
    logger.info("Resume endpoint called")
    _paused = False
    return JSONResponse(status_code=200, content={"status": "running"})

@api.get("/latest_answer")
async def get_latest_answer():
    global query_resp_history
    if interaction is None or interaction.current_agent is None:
        return JSONResponse(status_code=404, content={"error": "No agent available"})
    uid = str(uuid.uuid4())
    if not any(q["answer"] == interaction.current_agent.last_answer for q in query_resp_history):
        query_resp = {
            "done": "false",
            "answer": interaction.current_agent.last_answer,
            "reasoning": interaction.current_agent.last_reasoning,
            "agent_name": interaction.current_agent.agent_name if interaction.current_agent else "None",
            "success": interaction.current_agent.success,
            "blocks": {f'{i}': block.jsonify() for i, block in enumerate(interaction.get_last_blocks_result())} if interaction.current_agent else {},
            "status": interaction.current_agent.get_status_message if interaction.current_agent else "No status available",
            "uid": uid
        }
        interaction.current_agent.last_answer = ""
        interaction.current_agent.last_reasoning = ""
        query_resp_history.append(query_resp)
        return JSONResponse(status_code=200, content=query_resp)
    if query_resp_history:
        return JSONResponse(status_code=200, content=query_resp_history[-1])
    return JSONResponse(status_code=404, content={"error": "No answer available"})

async def think_wrapper(interaction, query):
    try:
        interaction.last_query = query
        logger.info("Agents request is being processed")
        success = await interaction.think()
        if not success:
            interaction.last_answer = "Error: No answer from agent"
            interaction.last_reasoning = "Error: No reasoning from agent"
            interaction.last_success = False
        else:
            interaction.last_success = True
        pretty_print(interaction.last_answer)
        interaction.speak_answer()
        return success
    except Exception as e:
        logger.error(f"Error in think_wrapper: {str(e)}")
        interaction.last_answer = "Error: agent run failed. Please retry."
        interaction.last_reasoning = f"Error: {str(e)}"
        interaction.last_success = False
        return False


async def _process_one(item: dict) -> dict:
    """
    Process exactly one item, returning a QueryResponse-like dict.
    """
    global is_generating
    uid = item["uid"]
    try:
        _status[uid] = "running"
        is_generating = True

        # Configure run context per-item.
        work_dir = resolve_work_dir()
        prefix = _safe_run_prefix(item.get("project_name") or config.get("MAIN", "project_name", fallback=""))
        # IMPORTANT: run_id is assigned when the job STARTS (not when queued),
        # so every queued job gets its own fresh run folder/trace.
        run_uuid = str(uuid.uuid4())
        uid_run = f"{prefix}_{run_uuid}" if prefix else run_uuid
        run_parent = item.get("run_parent_dir") or "runs"
        # Ensure run_parent stays under work_dir
        run_parent = str(run_parent).replace("\\", os.sep).replace("/", os.sep).lstrip(os.sep)
        output_dir = os.path.abspath(os.path.join(work_dir, run_parent, uid_run))
        if os.path.commonpath([os.path.abspath(work_dir), output_dir]) != os.path.abspath(work_dir):
            output_dir = os.path.abspath(os.path.join(work_dir, "runs", uid_run))
        os.makedirs(output_dir, exist_ok=True)
        _uid_to_run_id[uid] = uid_run
        _uid_to_output_dir[uid] = output_dir

        # Set active run for amendments
        set_active_run(uid, uid_run)

        mode_str = (item.get("mode") or os.getenv("AGENTICSEEK_MODE") or config.get("MAIN", "run_mode", fallback=RunMode.STANDARD.value))
        # Deep research mode is deprecated in the UI; treat it as normal trace mode for compatibility.
        if str(mode_str) == RunMode.DEEP_RESEARCH.value:
            mode_str = RunMode.TRACE.value
        mode = RunMode(mode_str) if mode_str in [m.value for m in RunMode] else RunMode.STANDARD

        trace_file = item.get("trace_file")
        findings_file = item.get("findings_file")
        trace_cfg_raw = item.get("trace_config") or {}
        tc = TraceConfig()
        # Apply user overrides (best-effort)
        try:
            if isinstance(trace_cfg_raw, dict):
                for k, v in trace_cfg_raw.items():
                    if hasattr(tc, k):
                        setattr(tc, k, v)
        except Exception:
            pass
        print(f"[API] trace_config: save_sources={tc.save_sources}, save_sources_llm={tc.save_sources_llm}")

        tool_cfg_raw = item.get("tool_config") or {}
        toolc = ToolConfig()
        try:
            if isinstance(tool_cfg_raw, dict):
                # allow spreadsheet_format + enabled_tools/disabled_tools
                if "spreadsheet_format" in tool_cfg_raw:
                    toolc.spreadsheet_format = str(tool_cfg_raw.get("spreadsheet_format") or toolc.spreadsheet_format)
                if "default_output_format" in tool_cfg_raw:
                    toolc.default_output_format = str(tool_cfg_raw.get("default_output_format") or toolc.default_output_format)
                if "enabled_tools" in tool_cfg_raw and tool_cfg_raw["enabled_tools"] is not None:
                    toolc.enabled_tools = set([str(x) for x in tool_cfg_raw["enabled_tools"]])
                if "disabled_tools" in tool_cfg_raw and tool_cfg_raw["disabled_tools"] is not None:
                    toolc.disabled_tools = set([str(x) for x in tool_cfg_raw["disabled_tools"]])
        except Exception:
            pass

        agent_cfg_raw = item.get("agent_config") or {}
        agentc = AgentConfig()
        try:
            if isinstance(agent_cfg_raw, dict):
                if "enabled_agents" in agent_cfg_raw and agent_cfg_raw["enabled_agents"] is not None:
                    agentc.enabled_agents = set([str(x).lower() for x in agent_cfg_raw["enabled_agents"]])
                if "disabled_agents" in agent_cfg_raw and agent_cfg_raw["disabled_agents"] is not None:
                    agentc.disabled_agents = set([str(x).lower() for x in agent_cfg_raw["disabled_agents"]])
        except Exception:
            pass

        if mode in (RunMode.TRACE, RunMode.DEEP_RESEARCH):
            if not trace_file:
                trace_file = os.path.join(output_dir, "trace.jsonl")
            elif not os.path.isabs(trace_file):
                trace_file = os.path.join(output_dir, trace_file)
        if mode == RunMode.DEEP_RESEARCH:
            if not findings_file:
                findings_file = os.path.join(output_dir, "findings.md")
            elif not os.path.isabs(findings_file):
                findings_file = os.path.join(output_dir, findings_file)

        truncate_limit = getattr(tc, "trace_max_chars_per_field", None)
        # If user wants markdown artifacts, keep jsonl reasonably small by default
        if truncate_limit is None and getattr(tc, "outputs_format", "jsonl_only") != "jsonl_only":
            truncate_limit = 4000
        trace_sink = TraceSink(trace_file, truncate_limit=truncate_limit) if trace_file else None
        set_run_context(RunContext(mode=mode, work_dir=work_dir, run_id=uid_run, output_dir=output_dir, trace_file=trace_file, findings_file=findings_file, trace_config=tc, tool_config=toolc, agent_config=agentc, trace_sink=trace_sink))
        if trace_file:
            _uid_to_trace_file[uid] = trace_file

        # Apply per-run provider selection (updates agents' providers; queue is sequential so safe).
        try:
            provider_for_run = _provider_from_item(item)
            if interaction is not None:
                for a in getattr(interaction, "agents", []) or []:
                    try:
                        a.provider = provider_for_run
                    except Exception:
                        pass
        except Exception as e:
            # Provider errors should not crash the worker; they will appear in response.
            raise

        # Clean state per dequeued job: reset volatile flags, clear per-run memory (keep system prompt),
        # and clear agent-specific ephemeral navigation/plan state. Does NOT delete prompt/system messages.
        try:
            if interaction is not None:
                interaction.last_answer = None
                interaction.last_reasoning = None
                # Reset all agents (best-effort)
                for a in getattr(interaction, "agents", []) or []:
                    try:
                        if hasattr(a, "reset_run_state"):
                            a.reset_run_state()
                    except Exception:
                        pass
                    try:
                        # Clear tool execution results
                        if hasattr(a, "blocks_result"):
                            a.blocks_result = []
                    except Exception:
                        pass
                    try:
                        # Clear conversational memory but keep system prompt
                        if getattr(a, "memory", None) is not None and hasattr(a.memory, "clear"):
                            a.memory.clear()
                    except Exception:
                        pass
                    # Planner sub-agents persist across runs inside PlannerAgent; clear them too to avoid mission drift.
                    try:
                        if getattr(a, "type", None) == "planner_agent" and hasattr(a, "agents"):
                            for sub in (getattr(a, "agents", {}) or {}).values():
                                try:
                                    if hasattr(sub, "reset_run_state"):
                                        sub.reset_run_state()
                                except Exception:
                                    pass
                                try:
                                    if getattr(sub, "memory", None) is not None and hasattr(sub.memory, "clear"):
                                        sub.memory.clear()
                                except Exception:
                                    pass
                                try:
                                    if hasattr(sub, "blocks_result"):
                                        sub.blocks_result = []
                                except Exception:
                                    pass
                                # Browser sub-agent state (inside planner) can also persist
                                try:
                                    if getattr(sub, "type", None) == "browser_agent":
                                        sub.current_page = ""
                                        sub.search_history = []
                                        sub.navigable_links = []
                                        sub.notes = []
                                        sub.last_action = "NAVIGATE"
                                        sub._step = 0
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    # Agent-specific ephemeral resets
                    try:
                        if getattr(a, "type", None) == "browser_agent":
                            a.current_page = ""
                            a.search_history = []
                            a.navigable_links = []
                            a.notes = []
                            a.url_screenshots = {}
                            a.url_contexts = {}
                            a._run_goal = ""
                            a._sources_enrich_lock = None
                            a._url_last_scored_at = {}
                            a.last_action = "NAVIGATE"
                            a._step = 0
                    except Exception:
                        pass
                    try:
                        if getattr(a, "type", None) == "planner_agent":
                            a.plan_steps = []
                            a.plan_goal = None
                            a.plan_current_step = None
                    except Exception:
                        pass
                    try:
                        if hasattr(a, "apply_run_context"):
                            a.apply_run_context()
                    except Exception:
                        pass
        except Exception:
            pass

        success = await think_wrapper(interaction, item["query"])
        resp = {
            "done": "true",
            "answer": interaction.last_answer or "",
            "reasoning": interaction.last_reasoning or "",
            "agent_name": interaction.current_agent.agent_name if interaction.current_agent else "deep_research",
            "success": str(bool(success)),
            "blocks": {f'{i}': block.jsonify() for i, block in enumerate(interaction.get_last_blocks_result())} if interaction else {},
            "status": "done" if success else "failed",
            "uid": uid,
            "run_id": uid_run,
            "output_dir": output_dir,
            "trace_file": trace_file,
        }
        _results[uid] = resp
        _status[uid] = "done" if success else "failed"

        # If queue is now empty, optionally sleep after grace period.
        try:
            with _power_lock:
                do_sleep = bool(_power_settings.get("sleep_when_queue_done"))
                grace = int(_power_settings.get("sleep_grace_seconds") or 0)
            if do_sleep and _queue_len() == 0 and not _paused:
                await asyncio.sleep(max(0, min(600, grace)))
                # Re-check to avoid sleeping if new items were queued.
                if _queue_len() == 0 and not _paused and not bool(is_generating):
                    _issue_host_sleep(reason="queue_done")
        except Exception:
            pass

        # Fire-and-forget: summarize trace at end of run (does not block queue). UI can toggle/configure this.
        try:
            do_summary = False
            with _post_run_summary_lock:
                do_summary = bool(_post_run_summary_settings.get("enabled", True))
                cfg = dict(_post_run_summary_settings)
            if do_summary and trace_file and uid_run:
                asyncio.create_task(_post_run_trace_summary(uid_run, item.get("query") or "", trace_file, item=item, settings=cfg))
        except Exception:
            pass
        return resp
    except Exception as e:
        resp = {
            "done": "true",
            "answer": "Error: agent run failed. Please retry.",
            "reasoning": str(e),
            "agent_name": "Unknown",
            "success": "false",
            "blocks": {},
            "status": "failed",
            "uid": uid,
            "run_id": _uid_to_run_id.get(uid),
            "output_dir": _uid_to_output_dir.get(uid),
            "trace_file": _uid_to_trace_file.get(uid),
        }
        _results[uid] = resp
        _status[uid] = "failed"
        return resp
    finally:
        set_run_context(None)
        clear_active_run()
        is_generating = False


async def _queue_worker_loop():
    """
    Background loop that processes queued requests sequentially.
    """
    while True:
        await asyncio.sleep(0.05)
        if interaction is None:
            continue
        if _paused:
            continue
        if is_generating:
            continue
        # Track last non-idle activity for idle sleep timer.
        try:
            global _last_nonidle_ts, _sleep_issued
            if _queue_len() > 0:
                _last_nonidle_ts = time.time()
                _sleep_issued = False
        except Exception:
            pass
        item = _dequeue()
        if item is None:
            continue
        # Never allow the worker loop to die; if a single job crashes unexpectedly,
        # mark it failed and continue processing the queue.
        try:
            await _process_one(item)
        except Exception as e:
            try:
                uid = item.get("uid", "unknown")
                _status[uid] = "failed"
                _results[uid] = {
                    "done": "true",
                    "answer": "Error: agent run failed (internal worker crash). Please retry.",
                    "reasoning": str(e),
                    "agent_name": "System",
                    "success": "false",
                    "blocks": {},
                    "status": "failed",
                    "uid": uid,
                }
                logger.error(f"Queue worker crashed while processing {uid}: {e}")
            except Exception:
                # Last resort: swallow to keep loop alive.
                pass


async def _power_idle_monitor_loop():
    """
    Background loop: if enabled, put host to sleep after being idle for N seconds.
    Idle means: not generating AND queue empty AND not paused.
    """
    global _last_nonidle_ts
    while True:
        await asyncio.sleep(5.0)
        try:
            if _paused:
                continue
            if bool(is_generating) or _queue_len() > 0:
                _last_nonidle_ts = time.time()
                continue
            with _power_lock:
                enabled = bool(_power_settings.get("sleep_after_idle_enabled"))
                idle_s = int(_power_settings.get("sleep_after_idle_seconds") or 0)
            if not enabled or idle_s <= 0:
                continue
            if (time.time() - _last_nonidle_ts) >= idle_s:
                _issue_host_sleep(reason=f"idle_timer:{idle_s}s")
        except Exception:
            continue


@api.on_event("startup")
async def _startup_queue_worker():
    if interaction is None:
        return
    asyncio.create_task(_queue_worker_loop())
    asyncio.create_task(_power_idle_monitor_loop())


@api.get("/runs")
async def list_runs(project_name: str | None = None, run_parent_dir: str | None = None, limit: int = 200):
    """
    List available run folders under WORK_DIR/<run_parent_dir>.
    Optionally filter by project_name prefix.
    """
    try:
        limit_i = int(limit)
    except Exception:
        limit_i = 200
    limit_i = max(1, min(2000, limit_i))
    work_dir = resolve_work_dir()
    run_parent = run_parent_dir or "runs"
    runs = _list_run_dirs(work_dir, run_parent)
    if project_name:
        pref = _safe_run_prefix(project_name)
        if pref:
            runs = [r for r in runs if r["run_id"].startswith(pref + "_")]
    return {"runs": runs[-limit_i:]}


@api.get("/queue_status/{uid}")
async def queue_status(uid: str):
    return {"uid": uid, "status": _status.get(uid, "unknown"), "run_id": _uid_to_run_id.get(uid)}


@api.get("/queue_items")
async def queue_items():
    """
    List all queued/running items, newest-last. Allows UI to edit/remove queued items.
    """
    items = []
    try:
        with _queue_lock:
            q = list(_queue)
        for it in q:
            uid = it.get("uid")
            items.append(
                {
                    "uid": uid,
                    "status": _status.get(uid, "queued"),
                    "query": it.get("query") or "",
                    "project_name": it.get("project_name"),
                    "mode": it.get("mode"),
                    "provider_name": it.get("provider_name"),
                    "provider_model": it.get("provider_model"),
                    "provider_server_address": it.get("provider_server_address"),
                    "provider_is_local": it.get("provider_is_local"),
                }
            )
    except Exception:
        pass
    running = []
    try:
        # expose current running item if any
        if is_generating and interaction is not None and interaction.last_query:
            running.append({"uid": None, "status": "running", "query": str(interaction.last_query)})
    except Exception:
        pass
    return {"items": items, "running": running, "queue_length": _queue_len()}


@api.put("/queue_item/{uid}")
async def update_queue_item(uid: str, payload: dict = Body(default={})):
    """
    Update a queued item (only allowed if it has not started).
    """
    p = payload or {}
    has_query = "query" in p
    has_project = "project_name" in p
    has_provider = any(
        k in p
        for k in (
            "provider_name",
            "provider_model",
            "provider_server_address",
            "provider_is_local",
        )
    )
    if not has_query and not has_project and not has_provider:
        return JSONResponse(
            status_code=400,
            content={"error": "At least one of query, project_name, or provider_* must be provided"},
        )

    new_query = None
    if has_query:
        q = str(p.get("query") or "").strip()
        if not q:
            return JSONResponse(status_code=400, content={"error": "query cannot be empty"})
        new_query = q

    new_project_name = None
    if has_project:
        pn = p.get("project_name")
        if pn is None:
            new_project_name = None
        else:
            s = str(pn).strip()
            new_project_name = s if s else None

    # Provider overrides (all optional, allow clearing back to defaults by sending nulls/empties)
    new_provider_name = None
    new_provider_model = None
    new_provider_server_address = None
    new_provider_is_local = None
    if has_provider:
        if "provider_name" in p:
            v = p.get("provider_name")
            new_provider_name = (None if v is None else (str(v).strip() or None))
        if "provider_model" in p:
            v = p.get("provider_model")
            new_provider_model = (None if v is None else (str(v).strip() or None))
        if "provider_server_address" in p:
            v = p.get("provider_server_address")
            new_provider_server_address = (None if v is None else (str(v).strip() or None))
        if "provider_is_local" in p:
            v = p.get("provider_is_local")
            new_provider_is_local = (None if v is None else bool(v))
    with _queue_lock:
        # only allow editing if status is still queued and item exists in _queue
        if _status.get(uid) != "queued":
            return JSONResponse(status_code=409, content={"error": "Cannot edit item once started", "uid": uid, "status": _status.get(uid)})
        found = False
        newq = deque()
        for it in _queue:
            if it.get("uid") == uid:
                it = dict(it)
                if new_query is not None:
                    it["query"] = new_query
                if has_project:
                    it["project_name"] = new_project_name
                if has_provider:
                    if "provider_name" in p:
                        it["provider_name"] = new_provider_name
                    if "provider_model" in p:
                        it["provider_model"] = new_provider_model
                    if "provider_server_address" in p:
                        it["provider_server_address"] = new_provider_server_address
                    if "provider_is_local" in p:
                        it["provider_is_local"] = new_provider_is_local
                found = True
            newq.append(it)
        if not found:
            return JSONResponse(status_code=404, content={"error": "Queued item not found", "uid": uid})
        _queue.clear()
        _queue.extend(newq)
    return {"ok": True, "uid": uid}


@api.delete("/queue_item/{uid}")
async def delete_queue_item(uid: str):
    """
    Delete a queued item (only allowed if it has not started).
    """
    with _queue_lock:
        if _status.get(uid) != "queued":
            return JSONResponse(status_code=409, content={"error": "Cannot delete item once started", "uid": uid, "status": _status.get(uid)})
        found = False
        newq = deque()
        for it in _queue:
            if it.get("uid") == uid:
                found = True
                continue
            newq.append(it)
        if not found:
            return JSONResponse(status_code=404, content={"error": "Queued item not found", "uid": uid})
        _queue.clear()
        _queue.extend(newq)
        try:
            _status.pop(uid, None)
            _results.pop(uid, None)
        except Exception:
            pass
    return {"ok": True, "uid": uid}


@api.get("/power_settings")
async def get_power_settings():
    with _power_lock:
        cfg = dict(_power_settings)
    cfg["host_sleep_allowed"] = _host_sleep_allowed()
    return cfg


@api.post("/power_settings")
async def set_power_settings(payload: dict = Body(default={})):
    """
    Update power settings (sleep policy). Safety: host sleep requires AGENTICSEEK_ALLOW_HOST_SLEEP=1 and not running in Docker.
    """
    global _sleep_issued, _last_nonidle_ts
    p = payload or {}
    with _power_lock:
        if "sleep_when_queue_done" in p:
            _power_settings["sleep_when_queue_done"] = bool(p.get("sleep_when_queue_done"))
        if "sleep_after_idle_enabled" in p:
            _power_settings["sleep_after_idle_enabled"] = bool(p.get("sleep_after_idle_enabled"))
        if "sleep_after_idle_seconds" in p:
            try:
                v = int(p.get("sleep_after_idle_seconds") or 0)
            except Exception:
                v = 0
            _power_settings["sleep_after_idle_seconds"] = max(0, min(365 * 24 * 60 * 60, v))
        if "sleep_grace_seconds" in p:
            try:
                v = int(p.get("sleep_grace_seconds") or 0)
            except Exception:
                v = 0
            _power_settings["sleep_grace_seconds"] = max(0, min(600, v))
    _sleep_issued = False
    _last_nonidle_ts = time.time()
    return await get_power_settings()

@api.get("/post_run_summary_settings")
async def get_post_run_summary_settings():
    with _post_run_summary_lock:
        return dict(_post_run_summary_settings)


@api.post("/post_run_summary_settings")
async def set_post_run_summary_settings(payload: dict = Body(default={})):
    """
    Update post-run trace summary settings. This controls whether the backend appends a `run_summary`
    event into the trace file after each run completes.
    """
    p = payload or {}
    with _post_run_summary_lock:
        if "enabled" in p:
            _post_run_summary_settings["enabled"] = bool(p.get("enabled"))
        if "use_run_llm" in p:
            _post_run_summary_settings["use_run_llm"] = bool(p.get("use_run_llm"))
        if "max_events" in p:
            try:
                v = int(p.get("max_events") or 0)
            except Exception:
                v = _post_run_summary_settings.get("max_events", 6000)
            _post_run_summary_settings["max_events"] = max(200, min(50000, v))
        if "max_lines" in p:
            try:
                v = int(p.get("max_lines") or 0)
            except Exception:
                v = _post_run_summary_settings.get("max_lines", 260)
            _post_run_summary_settings["max_lines"] = max(30, min(2500, v))

        # Optional provider override (used when use_run_llm is False).
        if "provider_name" in p:
            _post_run_summary_settings["provider_name"] = p.get("provider_name") or None
        if "provider_model" in p:
            _post_run_summary_settings["provider_model"] = p.get("provider_model") or None
        if "provider_server_address" in p:
            _post_run_summary_settings["provider_server_address"] = p.get("provider_server_address") or None
        if "provider_is_local" in p:
            v = p.get("provider_is_local")
            _post_run_summary_settings["provider_is_local"] = (None if v is None else bool(v))

    return await get_post_run_summary_settings()


@api.get("/result/{uid}")
async def get_result(uid: str):
    if uid in _results:
        return JSONResponse(status_code=200, content=_results[uid])
    return JSONResponse(status_code=404, content={"error": "No result available", "uid": uid, "status": _status.get(uid, "unknown")})


@api.get("/llm_options")
async def llm_options():
    """
    Options for the Settings dropdown (frontend convenience).
    """
    # Per user UX request: only expose two LOCAL Ollama models.
    # Default should be deepseek-r1:32b.
    base = {
        "provider_name": "ollama",
        "provider_server_address": config.get("MAIN", "provider_server_address", fallback="http://host.docker.internal:11434"),
        "provider_is_local": True,
    }
    opts = [
        {
            "id": "deepseek_r1_32b",
            "label": "deepseek-r1:32b",
            **base,
            "provider_model": "deepseek-r1:32b",
        },
        {
            "id": "gpt_oss_20b",
            "label": "gpt-oss:20b",
            **base,
            "provider_model": "gpt-oss:20b",
        },
    ]
    return {"default_id": "deepseek_r1_32b", "options": opts}


@api.post("/restart")
async def restart_server():
    """
    Restart the backend container by exiting the process.
    Requires Docker Compose service restart policy (recommended: unless-stopped).
    """
    def _exit_soon():
        try:
            time.sleep(0.5)
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=_exit_soon, daemon=True).start()
    return {"ok": True}


@api.get("/run_files")
async def run_files(run_id: str, run_parent_dir: str | None = None, limit: int = 200):
    """
    List readable files in a run directory (for UI viewing/downloading).
    """
    work_dir = resolve_work_dir()
    run_parent = (run_parent_dir or "runs")
    base = _safe_join_under(work_dir, os.path.join(run_parent, str(run_id)))
    if not base or not os.path.isdir(base):
        return JSONResponse(status_code=404, content={"error": "Run not found", "run_id": run_id})
    try:
        lim = max(1, min(500, int(limit)))
    except Exception:
        lim = 200
    out = []
    try:
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in (".jsonl", ".txt", ".md", ".log"):
                continue
            try:
                st = os.stat(p)
                out.append({"name": name, "size_bytes": int(st.st_size), "mtime": float(st.st_mtime)})
            except Exception:
                out.append({"name": name})
            if len(out) >= lim:
                break
    except Exception:
        pass
    out.sort(key=lambda x: x.get("name", ""))
    return {"run_id": run_id, "files": out}


def _safe_run_file_path(work_dir: str, run_parent: str, run_id: str, filename: str) -> str | None:
    # filename must be a simple basename (no path traversal)
    fn = str(filename or "").strip()
    if not fn or any(sep in fn for sep in ("/", "\\", os.sep)) or ".." in fn:
        return None
    base = _safe_join_under(work_dir, os.path.join(run_parent or "runs", str(run_id)))
    if not base:
        return None
    p = os.path.join(base, fn)
    try:
        if os.path.commonpath([os.path.abspath(base), os.path.abspath(p)]) != os.path.abspath(base):
            return None
    except Exception:
        return None
    if not os.path.isfile(p):
        return None
    return p


def _safe_run_asset_path(work_dir: str, run_parent: str, run_id: str, rel_path: str) -> str | None:
    """
    Safe join for run assets that may live in subfolders (e.g. screenshots/foo.png).
    Disallows absolute paths and path traversal.
    """
    rp = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
    if not rp:
        return None
    parts = [p for p in rp.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    base = _safe_join_under(work_dir, os.path.join(run_parent or "runs", str(run_id)))
    if not base:
        return None
    p = os.path.abspath(os.path.join(base, *parts))
    try:
        if os.path.commonpath([os.path.abspath(base), os.path.abspath(p)]) != os.path.abspath(base):
            return None
    except Exception:
        return None
    if not os.path.isfile(p):
        return None
    return p


@api.get("/run_file_text")
async def run_file_text(run_id: str, file: str, run_parent_dir: str | None = None, max_bytes: int = 200_000):
    """
    Read a run file as text for in-app viewing (truncated).
    """
    work_dir = resolve_work_dir()
    run_parent = (run_parent_dir or "runs")
    try:
        mb = max(1_000, min(5_000_000, int(max_bytes)))
    except Exception:
        mb = 200_000
    p = _safe_run_file_path(work_dir, run_parent, str(run_id), str(file))
    if not p:
        return JSONResponse(status_code=404, content={"error": "File not found", "run_id": run_id, "file": file})
    try:
        with open(p, "rb") as f:
            raw = f.read(mb + 1)
        truncated = len(raw) > mb
        txt = raw[:mb].decode("utf-8", errors="replace")
        return {"run_id": run_id, "file": os.path.basename(p), "truncated": bool(truncated), "content": txt}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e), "run_id": run_id, "file": file})


@api.get("/run_file_download")
async def run_file_download(run_id: str, file: str, run_parent_dir: str | None = None):
    """
    Download a run file.
    """
    work_dir = resolve_work_dir()
    run_parent = (run_parent_dir or "runs")
    p = _safe_run_file_path(work_dir, run_parent, str(run_id), str(file))
    if not p:
        return JSONResponse(status_code=404, content={"error": "File not found", "run_id": run_id, "file": file})
    return FileResponse(p, filename=os.path.basename(p))


@api.get("/run_asset_download")
async def run_asset_download(run_id: str, path: str, run_parent_dir: str | None = None):
    """
    Download a run asset by relative path (supports subfolders like screenshots/*.png).
    """
    work_dir = resolve_work_dir()
    run_parent = (run_parent_dir or "runs")
    p = _safe_run_asset_path(work_dir, run_parent, str(run_id), str(path))
    if not p:
        return JSONResponse(status_code=404, content={"error": "File not found", "run_id": run_id, "path": path})
    return FileResponse(p, filename=os.path.basename(p))


@api.get("/status")
async def status():
    current_status = interaction.current_agent.get_status_message if (interaction and interaction.current_agent) else "idle"
    agent_type = interaction.current_agent.type if (interaction and interaction.current_agent) else None
    agent_name = interaction.current_agent.agent_name if (interaction and interaction.current_agent) else None
    # Run context info (best-effort)
    try:
        from sources.runtime_context import get_run_context
        ctx = get_run_context()
        run_id = ctx.run_id if ctx else None
        output_dir = ctx.output_dir if ctx else None
    except Exception:
        run_id, output_dir = None, None
    plan = None
    plan_goal = None
    plan_current_step = None
    # Always surface planner state (even while a sub-agent is executing), so the checklist keeps updating.
    try:
        planner = None
        if interaction is not None:
            # Prefer current_agent if it's the planner; otherwise find the planner in the agent list.
            if getattr(interaction.current_agent, "type", None) == "planner_agent":
                planner = interaction.current_agent
            else:
                for a in getattr(interaction, "agents", []) or []:
                    if getattr(a, "type", None) == "planner_agent":
                        planner = a
                        break
        if planner is not None:
            plan_goal = getattr(planner, "plan_goal", None)
            plan = getattr(planner, "plan_steps", None)
            plan_current_step = getattr(planner, "plan_current_step", None)
    except Exception:
        plan = plan or None
    return {
        "is_generating": bool(is_generating),
        "queue_length": _queue_len(),
        "paused": bool(_paused),
        "current_status": current_status,
        "agent_type": agent_type,
        "agent_name": agent_name,
        "run_id": run_id,
        "output_dir": output_dir,
        "plan_goal": plan_goal,
        "plan_current_step": plan_current_step,
        "plan": plan,
        "ui_session_id": _ui_session_id,
    }


@api.post("/new_run")
async def new_run():
    """
    Reset UI-facing server state (queue/results/activity) to behave like a fresh restart,
    without requiring container restart.
    """
    global _paused, is_generating, query_resp_history, _ui_session_id
    # Stop any current agent
    try:
        if interaction and interaction.current_agent:
            interaction.current_agent.request_stop()
    except Exception:
        pass
    _paused = False
    is_generating = False
    clear_active_run()
    query_resp_history = []
    # Clear queues/results
    try:
        with _queue_lock:
            _queue.clear()
            _results.clear()
            _status.clear()
            _uid_to_run_id.clear()
            _uid_to_output_dir.clear()
            _uid_to_trace_file.clear()
    except Exception:
        pass
    # Clear activity bus
    reset_activity()
    # Clear sources registry (NotebookLM-style extracted sources)
    _reset_sources()
    _ui_session_id = str(uuid.uuid4())
    return {"ok": True, "ui_session_id": _ui_session_id}


@api.get("/sources")
async def sources(run_id: str):
    """
    Return deduped extracted sources for a run (NotebookLM-like).
    """
    rid = (run_id or "").strip()
    if not rid:
        return {"run_id": None, "updated_at": None, "sources": []}
    return _get_sources(rid)


@api.get("/sources_download")
async def sources_download(run_id: str):
    """
    Download deduped extracted sources for a run as JSON.
    """
    import json as _json
    from fastapi.responses import Response

    rid = (run_id or "").strip()
    payload = _get_sources(rid) if rid else {"run_id": None, "updated_at": None, "sources": []}
    body = _json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fname = f"sources_{rid or 'unknown'}.json"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@api.get("/sources_download_md")
async def sources_download_md(run_id: str):
    """
    Download deduped extracted sources for a run as Markdown.
    Also best-effort persists sources.md into the run output dir when available.
    """
    from fastapi.responses import Response

    rid = (run_id or "").strip()
    payload = _get_sources(rid) if rid else {"run_id": None, "updated_at": None, "sources": []}
    md = _render_sources_markdown(payload)
    body = md.encode("utf-8")
    fname = f"sources_{rid or 'unknown'}.md"

    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@api.post("/sources_upload")
async def sources_upload(run_id: str = Body(...), sources_json: str = Body(...)):
    """
    Import sources from a previously exported JSON payload.
    Merges into existing sources for the run_id (de-duplicates by URL).
    This allows loading saved sources to generate reports from.
    """
    rid = (run_id or "").strip()
    if not rid:
        return JSONResponse(status_code=400, content={"error": "run_id required"})

    try:
        data = json.loads(sources_json)
    except json.JSONDecodeError as e:
        return JSONResponse(status_code=400, content={"error": f"Invalid JSON: {e}"})

    if not isinstance(data, dict):
        return JSONResponse(status_code=400, content={"error": "Expected JSON object with 'sources' array"})

    # Determine output_dir from current run context if available
    output_dir = _uid_to_output_dir.get(rid)

    try:
        added, total = _import_sources(rid, data, output_dir=output_dir)
        logger.info(f"Imported sources for {rid}: added={added}, total={total}")
        return {"ok": True, "run_id": rid, "added": added, "total": total}
    except Exception as e:
        logger.error(f"Error importing sources: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# --- Sources Report Generation ---
# In-memory store for report generation requests and results
_report_requests: dict[str, dict] = {}  # run_id -> {status, report, error, requested_at, ...}
_report_queue: deque = deque()
_report_lock = threading.Lock()

def _emit_report_activity(run_id: str, text: str, color: str = "output") -> None:
    """Emit activity for report generation."""
    try:
        from sources.activity_bus import emit_activity
        emit_activity("print", run_id=run_id, text=text, color=color)
    except Exception:
        pass

def _generate_report_worker():
    """Background worker that processes report generation requests."""
    global _report_requests, _report_queue
    while True:
        try:
            time.sleep(0.5)
            # Check if there's a request in queue
            with _report_lock:
                if not _report_queue:
                    continue
                req = _report_queue.popleft()

            run_id = req.get("run_id", "")
            goal = req.get("goal", "")

            _emit_report_activity(run_id, "📝 Report generation starting...", "report")

            with _report_lock:
                if run_id in _report_requests:
                    _report_requests[run_id]["status"] = "generating"

            # Get sources data
            sources_data = _get_sources(run_id)
            sources_list = sources_data.get("sources", [])

            if not sources_list:
                with _report_lock:
                    _report_requests[run_id] = {
                        "status": "done",
                        "report": "# No Sources Available\n\nNo sources were collected for this query. Run a search first to gather sources.",
                        "error": None,
                    }
                _emit_report_activity(run_id, "📝 Report: No sources available", "warning")
                continue

            # Build sources context for LLM
            sources_context = []
            for i, src in enumerate(sources_list[:30], 1):  # Limit to top 30 sources
                ctx = f"## Source {i}: {src.get('title') or src.get('url', 'Unknown')}\n"
                ctx += f"- URL: {src.get('url', 'N/A')}\n"
                ctx += f"- Relevancy: {src.get('relevancy_score', 'N/A')}\n"
                if src.get("match"):
                    ctx += f"- Why it matches: {src.get('match')}\n"
                if src.get("how_helps"):
                    ctx += f"- How it helps: {src.get('how_helps')}\n"
                if src.get("data_to_collect"):
                    ctx += f"- Key data: {'; '.join(src.get('data_to_collect', [])[:5])}\n"
                if src.get("evidence_quotes"):
                    ctx += f"- Evidence: {'; '.join(src.get('evidence_quotes', [])[:3])}\n"
                sources_context.append(ctx)

            sources_text = "\n".join(sources_context)

            # Get provider for report generation
            try:
                report_provider = _get_trace_provider()
            except Exception:
                report_provider = provider  # Fall back to main provider

            system_prompt = """You are a research analyst writing a detailed report based on collected sources.

Your task is to synthesize the provided sources into a comprehensive, well-structured report that directly answers the user's original question.

REPORT FORMAT:
1. **Executive Summary** (2-3 sentences answering the question directly)
2. **Key Findings** (bulleted list of the most important discoveries)
3. **Detailed Analysis** (organized by topic/theme, with citations to sources)
4. **Source Quality Assessment** (brief note on source reliability)
5. **Recommendations/Conclusions** (actionable insights)

RULES:
- Use ONLY information from the provided sources - do NOT make up data
- Include inline citations with [Source X] references
- Link to URLs where relevant using markdown: [text](url)
- Sort findings by relevance/importance
- Be specific with numbers, prices, names when available
- If sources conflict, note the discrepancy
- Keep the report under 1500 words but make it comprehensive
- Use markdown formatting for readability"""

            user_prompt = f"""ORIGINAL QUESTION:
{goal}

COLLECTED SOURCES:
{sources_text}

Please write a detailed research report answering the original question using ONLY the information from these sources."""

            _emit_report_activity(run_id, "📝 Generating report from sources...", "report")

            try:
                report = report_provider.respond(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                report = str(report or "").strip()

                if not report:
                    raise ValueError("Empty response from LLM")

                # Add source links appendix with screenshots
                report += "\n\n---\n## Sources\n"
                for i, src in enumerate(sources_list[:30], 1):
                    title = src.get("title") or "Untitled"
                    url = src.get("url", "")
                    score = src.get("relevancy_score")
                    score_str = f" (relevancy: {score:.2f})" if score is not None else ""

                    report += f"\n### Source {i}: {title}{score_str}\n"
                    if url:
                        report += f"🔗 [{url}]({url})\n"

                    # Include screenshot images if available
                    shots = src.get("screenshot_paths") or []
                    if shots and run_id:
                        report += "\n**Screenshots:**\n"
                        for sp in shots[:3]:  # Limit to 3 screenshots per source
                            sp_clean = str(sp or "").strip()
                            if sp_clean:
                                # Build the asset download URL for the image
                                img_url = f"/run_asset_download?run_id={run_id}&path={sp_clean}"
                                report += f"\n![{title}]({img_url})\n"

                    # Include a brief evidence quote if available
                    quotes = src.get("evidence_quotes") or []
                    if quotes:
                        report += "\n**Key Quote:**\n"
                        for q in quotes[:1]:
                            report += f"> {q}\n"
                    report += "\n"

                with _report_lock:
                    _report_requests[run_id] = {
                        "status": "done",
                        "report": report,
                        "error": None,
                    }
                _emit_report_activity(run_id, "📝 Report generated successfully!", "success")

            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                with _report_lock:
                    _report_requests[run_id] = {
                        "status": "error",
                        "report": None,
                        "error": error_msg,
                    }
                _emit_report_activity(run_id, f"📝 Report generation failed: {error_msg}", "failure")

        except Exception as e:
            print(f"[Report Worker] Error: {type(e).__name__}: {e}")
            time.sleep(1)

# Start report worker thread
_report_worker_thread = threading.Thread(target=_generate_report_worker, daemon=True)
_report_worker_thread.start()


@api.post("/generate_sources_report")
async def generate_sources_report(run_id: str = Body(...), goal: str = Body(...)):
    """
    Queue a report generation request for the given run.
    Returns immediately - poll /sources_report_status for results.
    """
    rid = (run_id or "").strip()
    g = (goal or "").strip()

    if not rid:
        return JSONResponse(status_code=400, content={"error": "run_id required"})
    if not g:
        return JSONResponse(status_code=400, content={"error": "goal required"})

    with _report_lock:
        # Check if already queued/generating
        existing = _report_requests.get(rid)
        if existing and existing.get("status") in ("queued", "generating"):
            return {"status": existing["status"], "message": "Report generation already in progress"}

        # Queue the request
        _report_requests[rid] = {
            "status": "queued",
            "report": None,
            "error": None,
            "requested_at": time.time(),
        }
        _report_queue.append({"run_id": rid, "goal": g})

    _emit_report_activity(rid, "📝 Report generation queued...", "report")

    return {"status": "queued", "message": "Report generation queued"}


@api.get("/sources_report_status")
async def sources_report_status(run_id: str):
    """
    Get the status and result of a report generation request.
    """
    rid = (run_id or "").strip()
    if not rid:
        return {"status": "none", "report": None, "error": None}

    with _report_lock:
        req = _report_requests.get(rid)

    if not req:
        return {"status": "none", "report": None, "error": None}

    return {
        "status": req.get("status", "none"),
        "report": req.get("report"),
        "error": req.get("error"),
    }


@api.get("/sources_report_download")
async def sources_report_download(run_id: str):
    """
    Download the generated report as markdown.
    """
    from fastapi.responses import Response

    rid = (run_id or "").strip()
    with _report_lock:
        req = _report_requests.get(rid)

    if not req or not req.get("report"):
        return JSONResponse(status_code=404, content={"error": "No report available"})

    body = req["report"].encode("utf-8")
    fname = f"report_{rid}.md"
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@api.get("/activity")
async def activity(run_id: str | None = None, since_id: int = 0, limit: int = 200):
    """
    Returns recent activity events for UI.
    If run_id is omitted, returns global events.
    """
    try:
        limit_i = int(limit)
    except Exception:
        limit_i = 200
    limit_i = max(1, min(500, limit_i))
    try:
        since_i = int(since_id)
    except Exception:
        since_i = 0
    return get_activity(run_id=run_id, since_id=since_i, limit=limit_i)


@api.post("/amend")
async def amend_current_run(body: dict = Body(...)):
    """
    Add an amendment/note to the currently running task.
    This injects additional context without queuing a new task.
    """
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"error": "No text provided"})

    # Get the active run_id (set by worker thread when processing starts)
    run_id = get_active_run_id()

    if not run_id:
        return JSONResponse(status_code=400, content={"error": "No active run to amend. Use the send button to queue a new task."})

    amendment = add_amendment(run_id, text)

    # Emit to activity feed
    try:
        from sources.activity_bus import emit_activity
        emit_activity("amendment", run_id=run_id, text=text[:200])
    except Exception:
        pass

    print(f"[API] Amendment added to run {run_id}: {text[:50]}...")
    return {"success": True, "run_id": run_id, "amendment": amendment}


@api.get("/amendments")
async def get_amendments_endpoint(run_id: str = None):
    """Get amendments for a run."""
    if not run_id:
        try:
            from sources.runtime_context import get_run_context
            ctx = get_run_context()
            if ctx:
                run_id = ctx.run_id
        except Exception:
            pass

    if not run_id:
        return {"amendments": []}

    return {"run_id": run_id, "amendments": get_amendments(run_id)}


@api.post("/query", response_model=QueryResponse)
async def process_query(request: QueryRequest):
    global is_generating, query_resp_history
    logger.info(f"Processing query: {request.query}")
    query_resp = QueryResponse(
        done="false",
        answer="",
        reasoning="",
        agent_name="Unknown",
        success="false",
        blocks={},
        status="Ready",
        uid=str(uuid.uuid4())
    )
    # Always enqueue and return immediately (UX: never block the chat input).
    uid = query_resp.uid
    _enqueue(
        {
            "uid": uid,
            "query": request.query,
            "project_name": request.project_name,
            "mode": request.mode,
            "provider_name": request.provider_name,
            "provider_model": request.provider_model,
            "provider_server_address": request.provider_server_address,
            "provider_is_local": request.provider_is_local,
            "trace_file": request.trace_file,
            "findings_file": request.findings_file,
            "trace_config": request.trace_config,
            "run_parent_dir": request.run_parent_dir,
            "tool_config": request.tool_config,
            "agent_config": request.agent_config,
        }
    )
    query_resp.status = "queued"
    logger.info(f"Query queued: {uid} (queue_length={_queue_len()})")
    return JSONResponse(status_code=202, content=query_resp.jsonify())

    if interaction is None:
        query_resp.answer = "Error: backend not initialized"
        query_resp.reasoning = "Error: interaction system is not initialized (AGENTICSEEK_SKIP_INIT=1?)"
        return JSONResponse(status_code=503, content=query_resp.jsonify())

    # Note: processing occurs asynchronously in the background queue worker.

if __name__ == "__main__":
    # Print startup info
    if is_running_in_docker():
        print("[AgenticSeek] Starting in Docker container...")
    else:
        print("[AgenticSeek] Starting on host machine...")
    
    envport = os.getenv("BACKEND_PORT")
    if envport:
        port = int(envport)
    else:
        port = 7777
    uvicorn.run(api, host="0.0.0.0", port=7777)