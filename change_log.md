## Change log (this thread)

- **Run modes + observability foundation**
  - Added/expanded **three run modes**: `standard`, `trace` (“raw”), `deep_research`.
  - Introduced **per-run context/config** via `RunContext` and `TraceConfig` so logging/outputs can be controlled per run.

- **Raw mode: single-file trace output (per run)**
  - Made raw mode default to **single-file output**: `runs/<run_id>/trace.jsonl`.
  - Set raw mode to use **`outputs_format = jsonl_only`** by default and disabled per-field truncation (configurable).
  - Prevented “file explosion” by disabling markdown artifact writing in `jsonl_only` runs.

- **Trace completeness**
  - Persisted **tool executions** into the trace:
    - Captures tool input block + full raw tool output in `tool_executed` events (and success/feedback).
  - Captures **LLM outputs** (allowed text) in the trace (`llm_answer`).
  - Captures **browser snapshots** with URL/title and **page text embedded** in `page_snapshot` events when `jsonl_only`.

- **TraceSink improvements**
  - Added configurable truncation to `TraceSink` with an option for **no truncation** (for raw/jsonl-only runs).

- **Circular import fix**
  - Fixed backend startup crash by removing a module-level import cycle between `runtime_context` and `artifacts` (lazy import in `trace_event`).

- **Tool allowlist + safer defaults**
  - Implemented tool/agent gating via config so you can **enable tools** explicitly (and avoid unintended coder/tool behavior).

- **Queueing + responsiveness**
  - Ensured `/query` is **always queued** (returns immediately) and added polling for `/result/{uid}` in the UI.
  - Added `/pause`/`/resume` behavior and a clear generating/paused state in status polling.

- **Planner robustness + handoff**
  - Planner now maintains checklist state (`plan_goal`, `plan_steps`, `plan_current_step`) and persists plan artifacts when not `jsonl_only`.
  - Improved step handoff to downstream agents and reduced “bottom out” behavior after tool failures.

- **Checklist keeps updating**
  - Fixed `/status` so it **always returns planner plan state** even when a sub-agent (browser/file/etc.) is currently executing.

- **Frontend: dockable UI + multi-pane layout**
  - Implemented a **dock-style UI** with panels: Chat, Checklist, Activity, Settings, Computer.
  - Upgraded from 2 panes to **multi-pane resizable layout**:
    - Add/remove panes (`+Pane` / `−`)
    - Move tabs between panes (move-to-next-pane).
  - Added support for **duplicate tab instances** (e.g. “Checklist (2)”).

- **Frontend: settings re-org**
  - Moved **mode selector** (Standard/Raw/Deep research) and run folder/ID display from Checklist into **Settings**.
  - Set **Raw mode as default** in the UI and made raw verbosity default to “standard”.

- **Frontend: scrolling fixes**
  - Fixed pane scrolling by ensuring dock layout CSS is always applied and the correct containers use `overflow: auto` with proper flex `min-height: 0`.

- **Frontend: big status banner**
  - Restored a prominent **WORKING / PAUSED / IDLE** banner at the top of the main UI to make progress obvious.

- **Project naming for runs**
  - Added **Project name** field in Settings (persisted in `localStorage`).
  - Backend prefixes run IDs and run folders as `<project>_<uuid>` using a filesystem-safe slug.

## Key files touched (high level)

- **Backend**
  - `api.py`
  - `sources/runtime_context.py`
  - `sources/trace_sink.py`
  - `sources/artifacts.py`
  - `sources/agents/agent.py`
  - `sources/agents/browser_agent.py`
  - `sources/agents/planner_agent.py`
  - `sources/schemas.py`
  - `config.ini`

- **Frontend**
  - `frontend/agentic-seek-front/src/App.js`
  - `frontend/agentic-seek-front/src/App.css`
  - `frontend/agentic-seek-front/src/components/DockTabs.js`
  - `frontend/agentic-seek-front/src/components/DockTabs.css`
  - `frontend/agentic-seek-front/src/components/MultiResizableLayout.js`
  - `frontend/agentic-seek-front/src/components/MultiResizableLayout.css`
  - `frontend/agentic-seek-front/src/components/ResizableLayout.css`
