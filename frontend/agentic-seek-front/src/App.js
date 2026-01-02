import React, { useState, useEffect, useRef, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import axios from "axios";
import "./App.css";
import { ThemeToggle } from "./components/ThemeToggle";
import { MultiResizableLayout } from "./components/MultiResizableLayout";
import { DockTabs } from "./components/DockTabs";
import faviconPng from "./logo.png";

const BACKEND_URL =
  process.env.REACT_APP_BACKEND_URL ||
  `${window.location.protocol}//${window.location.hostname}:7777`;
console.log("Using backend URL:", BACKEND_URL);

function App() {
  const [query, setQuery] = useState("");
  const [messages, setMessages] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);
  const [currentView, setCurrentView] = useState("blocks");
  const [responseData, setResponseData] = useState(null);
  const [isOnline, setIsOnline] = useState(false);
  const [status, setStatus] = useState("Agents ready");
  // Mode: keep a single "Standard" mode (trace) to simplify UX.
  const [runMode] = useState("trace");
  const [rawVerbosity, setRawVerbosity] = useState(() => {
    const saved = localStorage.getItem("rawVerbosity");
    return saved ? parseInt(saved, 10) : 2; // default to MAX
  });
  const [rawRunParentDir, setRawRunParentDir] = useState(() => {
    return localStorage.getItem("rawRunParentDir") || "runs";
  });
  const [rawSettingsOpen, setRawSettingsOpen] = useState(false);
  const [toolSettingsOpen, setToolSettingsOpen] = useState(false);
  const [projectName, setProjectName] = useState(() => {
    return localStorage.getItem("projectName") || "";
  });
  const [llmOptions, setLlmOptions] = useState([]);
  const [llmOptionId, setLlmOptionId] = useState(() => localStorage.getItem("llmOptionId") || "gpt_oss_20b");
  const [rawOcrEnabled, setRawOcrEnabled] = useState(() => {
    const saved = localStorage.getItem("rawOcrEnabled");
    return saved ? saved === "true" : false;
  });
  const [rawSourcesEnabled, setRawSourcesEnabled] = useState(() => {
    const saved = localStorage.getItem("rawSourcesEnabled");
    return saved ? saved === "true" : false; // default OFF
  });
  const [rawSourcesLlmEnabled, setRawSourcesLlmEnabled] = useState(() => {
    const saved = localStorage.getItem("rawSourcesLlmEnabled");
    return saved ? saved === "true" : false; // default OFF (prevents GPU stalls)
  });
  // Trace chat UI state (separate from main chat; queries past runs only)
  const [traceRuns, setTraceRuns] = useState([]);
  const [traceProject, setTraceProject] = useState(() => localStorage.getItem("traceProject") || "");
  const [runFilesRunId, setRunFilesRunId] = useState("");
  const [runFiles, setRunFiles] = useState([]);
  const [runFileName, setRunFileName] = useState("");
  const [runFileView, setRunFileView] = useState({ content: "", truncated: false });
  const [queueLength, setQueueLength] = useState(0);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isPaused, setIsPaused] = useState(false);
  const [plan, setPlan] = useState(null);
  const [planGoal, setPlanGoal] = useState(null);
  const [planCurrentStep, setPlanCurrentStep] = useState(null);
  const [agentType, setAgentType] = useState(null);
  const [agentName, setAgentName] = useState(null);
  const [runId, setRunId] = useState(null);
  const [outputDir, setOutputDir] = useState(null);
  const [activitySinceId, setActivitySinceId] = useState(0);
  const [activityFeed, setActivityFeed] = useState([]);
  const [sourcesData, setSourcesData] = useState({ run_id: null, updated_at: null, sources: [] });
  const [reportStatus, setReportStatus] = useState({ status: "none", report: null, error: null });
  const [reportGoal, setReportGoal] = useState(""); // Store the goal for report generation
  const [pendingUids, setPendingUids] = useState([]); // queued jobs to poll via /result/{uid}
  const [queueItems, setQueueItems] = useState([]);
  const [powerSettings, setPowerSettings] = useState(null);
  const [postRunSummarySettings, setPostRunSummarySettings] = useState(null);
  const [postSummaryLlmOptionId, setPostSummaryLlmOptionId] = useState(
    () => localStorage.getItem("postSummaryLlmOptionId") || "deepseek_r1_32b"
  );
  const [expandedReasoning, setExpandedReasoning] = useState(new Set());
  const messagesEndRef = useRef(null);
  const [uiSessionId, setUiSessionId] = useState(null);
  const [showMobileScrollControls, setShowMobileScrollControls] = useState(false);
  // Prevent polling refresh from clobbering in-progress edits in Queue panel.
  // Shape: { [uid]: { project_name?: boolean, query?: boolean } }
  const queueEditingRef = useRef({});

  const nowIso = () => new Date().toISOString();
  const formatTs = (iso) => {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return String(iso);
    }
  };

  const formatScore = (v) => {
    if (v === null || v === undefined) return "";
    const n = Number(v);
    if (!Number.isFinite(n)) return "";
    const c = Math.max(0, Math.min(1, n));
    return c.toFixed(2);
  };

  useEffect(() => {
    const detect = () => {
      try {
        const isSmall =
          typeof window !== "undefined" &&
          window.matchMedia &&
          window.matchMedia("(max-width: 900px)").matches;
        const isTouch =
          typeof navigator !== "undefined" &&
          ((navigator.maxTouchPoints || 0) > 0 || "ontouchstart" in window);
        const isCoarse =
          typeof window !== "undefined" &&
          window.matchMedia &&
          window.matchMedia("(hover: none) and (pointer: coarse)").matches;
        setShowMobileScrollControls(!!(isSmall || isTouch || isCoarse));
      } catch {
        setShowMobileScrollControls(true);
      }
    };
    detect();
    try {
      window.addEventListener("resize", detect);
      window.addEventListener("orientationchange", detect);
      return () => {
        window.removeEventListener("resize", detect);
        window.removeEventListener("orientationchange", detect);
      };
    } catch {
      // ignore
    }
  }, []);

  const getScrollTarget = () => {
    try {
      const main = document.querySelector(".main");
      if (main && main.scrollHeight > main.clientHeight + 4) return main;
    } catch {}
    try {
      const dc = document.querySelector(".dock-content");
      if (dc && dc.scrollHeight > dc.clientHeight + 4) return dc;
    } catch {}
    try {
      const ps = document.querySelector(".panel-scroll");
      if (ps && ps.scrollHeight > ps.clientHeight + 4) return ps;
    } catch {}
    return document.scrollingElement || document.documentElement || document.body;
  };

  const mobileScrollBy = (dir) => {
    const el = getScrollTarget();
    const step = Math.max(240, Math.round(window.innerHeight * 0.75));
    const top = dir === "up" ? -step : step;
    try {
      el.scrollBy({ top, left: 0, behavior: "smooth" });
    } catch {
      try {
        el.scrollTop = (el.scrollTop || 0) + top;
      } catch {}
    }
  };

  // Dockable panels (left/right tabs) – persisted
  const ALL_PANELS = [
    { id: "chat", title: "Chat" },
    { id: "checklist", title: "Checklist" },
    { id: "activity", title: "Activity" },
    { id: "sources", title: "Sources" },
    { id: "queue", title: "Queue" },
    { id: "settings", title: "Settings" },
    { id: "computer", title: "Computer" },
    { id: "traces", title: "Traces" },
  ];

  const loadDock = () => {
    try {
      const raw2 = localStorage.getItem("dockLayoutV2");
      if (raw2) {
        const v2 = JSON.parse(raw2);
        // Migrate removed panel id "trace_chat" -> "traces"
        try {
          (v2.panes || []).forEach((p) => {
            p.tabs = (p.tabs || []).map((t) => (t === "trace_chat" ? "traces" : t));
            if (p.active === "trace_chat") p.active = "traces";
          });
        } catch {}
        return v2;
      }
      const raw1 = localStorage.getItem("dockLayoutV1");
      if (raw1) {
        const v1 = JSON.parse(raw1);
        return {
          panes: [
            { id: "pane_1", tabs: v1.left?.tabs || ["chat", "checklist"], active: v1.left?.active || "chat" },
            {
              id: "pane_2",
              tabs: (v1.right?.tabs || ["computer", "settings"]).map((t) => (t === "trace_chat" ? "traces" : t)),
              active: v1.right?.active === "trace_chat" ? "traces" : (v1.right?.active || "computer"),
            },
          ],
          widths: [50, 50],
        };
      }
    } catch {}
    return {
      panes: [
        { id: "pane_1", tabs: ["chat", "checklist"], active: "chat" },
        { id: "pane_2", tabs: ["computer", "settings"], active: "computer" },
      ],
      widths: [50, 50],
    };
  };

  const [dock, setDock] = useState(loadDock);

  useEffect(() => {
    try {
      localStorage.setItem("dockLayoutV2", JSON.stringify(dock));
    } catch {}
  }, [dock]);

  const parseTabId = (id) => {
    const s = String(id || "");
    const [base, suffix] = s.split("__");
    const n = suffix ? parseInt(suffix, 10) : 1;
    return { base: base || s, suffix: suffix || null, instance: Number.isFinite(n) ? n : null };
  };

  const getPanelMeta = (id) => {
    const { base, suffix, instance } = parseTabId(id);
    const meta = ALL_PANELS.find((p) => p.id === base) || { id: base, title: base };
    const title = suffix && instance && instance > 1 ? `${meta.title} (${instance})` : meta.title;
    return { id, title };
  };

  // Panels offered by "+ Add tab…" (we allow duplicates / multiple instances).
  const hiddenPanels = () => ALL_PANELS;

  const makeUniqueTabId = (baseId, usedSet) => {
    if (!usedSet.has(baseId)) return baseId;
    let max = 1;
    for (const tid of usedSet) {
      const { base, instance } = parseTabId(tid);
      if (base !== baseId) continue;
      const n = instance || 1;
      if (n > max) max = n;
    }
    return `${baseId}__${max + 1}`;
  };

  const closeTab = (paneIdx, id) => {
    setDock((prev) => {
      const next = JSON.parse(JSON.stringify(prev));
      next.panes[paneIdx].tabs = (next.panes[paneIdx].tabs || []).filter((t) => t !== id);
      if (next.panes[paneIdx].active === id) {
        next.panes[paneIdx].active = next.panes[paneIdx].tabs[0] || null;
      }
      return next;
    });
  };

  const moveTab = (fromPaneIdx, id) => {
    setDock((prev) => {
      const next = JSON.parse(JSON.stringify(prev));
      if (!next.panes || next.panes.length < 2) return next;
      const toPaneIdx = (fromPaneIdx + 1) % next.panes.length;
      next.panes[fromPaneIdx].tabs = (next.panes[fromPaneIdx].tabs || []).filter((t) => t !== id);
      next.panes[toPaneIdx].tabs = Array.from(new Set([...(next.panes[toPaneIdx].tabs || []), id]));
      if (next.panes[fromPaneIdx].active === id) {
        next.panes[fromPaneIdx].active = next.panes[fromPaneIdx].tabs[0] || null;
      }
      next.panes[toPaneIdx].active = id;
      return next;
    });
  };

  const openHiddenOnSide = (paneIdx, baseId) => {
    setDock((prev) => {
      const next = JSON.parse(JSON.stringify(prev));
      const used = new Set((next.panes || []).flatMap((p) => p.tabs || []));
      const tabId = makeUniqueTabId(baseId, used);
      next.panes[paneIdx].tabs = [...(next.panes[paneIdx].tabs || []), tabId];
      next.panes[paneIdx].active = tabId;
      return next;
    });
  };

  const addPaneAfter = (paneIdx) => {
    setDock((prev) => {
      const next = JSON.parse(JSON.stringify(prev));
      const panes = next.panes || [];
      const newId = `pane_${Date.now()}`;
      panes.splice(paneIdx + 1, 0, { id: newId, tabs: [], active: null });
      next.panes = panes;
      // Equalize widths for simplicity.
      const n = panes.length;
      next.widths = Array.from({ length: n }, () => 100 / n);
      return next;
    });
  };

  const removePane = (paneIdx) => {
    setDock((prev) => {
      const next = JSON.parse(JSON.stringify(prev));
      const panes = next.panes || [];
      if (panes.length <= 1) return next;
      const removed = panes.splice(paneIdx, 1)[0];
      // Merge removed tabs into previous pane (or first).
      const targetIdx = Math.max(0, paneIdx - 1);
      panes[targetIdx].tabs = Array.from(new Set([...(panes[targetIdx].tabs || []), ...((removed && removed.tabs) || [])]));
      if (!panes[targetIdx].active && panes[targetIdx].tabs.length > 0) {
        panes[targetIdx].active = panes[targetIdx].tabs[0];
      }
      next.panes = panes;
      const n = panes.length;
      next.widths = Array.from({ length: n }, () => 100 / n);
      return next;
    });
  };

  const fetchLatestAnswer = useCallback(async () => {
    try {
      const res = await axios.get(`${BACKEND_URL}/latest_answer`);
      const data = res.data;

      updateData(data);
      if (!data.answer || data.answer.trim() === "") {
        return;
      }
      const normalizedNewAnswer = normalizeAnswer(data.answer);
      const answerExists = messages.some(
        (msg) => normalizeAnswer(msg.content) === normalizedNewAnswer
      );
      if (!answerExists) {
        setMessages((prev) => [
          ...prev,
          {
            type: "agent",
            content: data.answer,
            reasoning: data.reasoning,
            agentName: data.agent_name,
            status: data.status,
            uid: data.uid,
            ts: nowIso(),
          },
        ]);
        setStatus(data.status);
        scrollToBottom();
      } else {
        console.log("Duplicate answer detected, skipping:", data.answer);
      }
    } catch (error) {
      console.error("Error fetching latest answer:", error);
    }
  }, [messages]);

  const fetchRuns = async () => {
    try {
      const params = new URLSearchParams();
      const proj = (traceProject || projectName || "").trim();
      if (proj) params.set("project_name", proj);
      params.set("run_parent_dir", rawRunParentDir || "runs");
      params.set("limit", "300");
      const res = await axios.get(`${BACKEND_URL}/runs?${params.toString()}`);
      const runs = (res.data && res.data.runs) || [];
      setTraceRuns(Array.isArray(runs) ? runs : []);
    } catch (e) {
      // ignore
    }
  };

  const fetchRunFiles = async (rid) => {
    const r = String(rid || "").trim();
    if (!r) {
      setRunFiles([]);
      return;
    }
    try {
      const params = new URLSearchParams();
      params.set("run_id", r);
      params.set("run_parent_dir", rawRunParentDir || "runs");
      params.set("limit", "200");
      const res = await axios.get(`${BACKEND_URL}/run_files?${params.toString()}`);
      const files = (res.data && res.data.files) || [];
      setRunFiles(Array.isArray(files) ? files : []);
    } catch (e) {
      setRunFiles([]);
    }
  };

  const openRunFile = async (rid, filename) => {
    const r = String(rid || "").trim();
    const f = String(filename || "").trim();
    if (!r || !f) return;
    try {
      const params = new URLSearchParams();
      params.set("run_id", r);
      params.set("file", f);
      params.set("run_parent_dir", rawRunParentDir || "runs");
      params.set("max_bytes", "250000");
      const res = await axios.get(`${BACKEND_URL}/run_file_text?${params.toString()}`);
      setRunFileView({ content: res.data?.content || "", truncated: !!res.data?.truncated });
    } catch (e) {
      setRunFileView({ content: "Failed to load file.", truncated: false });
    }
  };

  const fetchLlmOptions = async () => {
    try {
      const res = await axios.get(`${BACKEND_URL}/llm_options`);
      const opts = (res.data && res.data.options) || [];
      const defId = (res.data && res.data.default_id) || "deepseek_r1_32b";
      const list = Array.isArray(opts) ? opts : [];
      setLlmOptions(list);
      // If current selection is missing, fall back to server default.
      const has = list.some((o) => o && o.id === llmOptionId);
      if (!has) setLlmOptionId(defId);
    } catch (e) {
      // ignore; dropdown will still show the stored value
      setLlmOptions([]);
    }
  };

  // Always show a real list of local models (even if /llm_options fails).
  const effectiveLlmOptions =
    (llmOptions || []).length > 0
      ? llmOptions
      : [
          {
            id: "deepseek_r1_32b",
            label: "deepseek-r1:32b",
            provider_name: "ollama",
            provider_model: "deepseek-r1:32b",
            provider_server_address: undefined,
            provider_is_local: true,
          },
          {
            id: "gpt_oss_20b",
            label: "gpt-oss:20b",
            provider_name: "ollama",
            provider_model: "gpt-oss:20b",
            provider_server_address: undefined,
            provider_is_local: true,
          },
        ];

  useEffect(() => {
    fetchLlmOptions();
    fetchPowerSettings();
    fetchPostRunSummarySettings();
    fetchQueueItems();
    fetchRuns();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [traceProject, projectName, rawRunParentDir]);

  useEffect(() => {
    if (runFilesRunId) fetchRunFiles(runFilesRunId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runFilesRunId, rawRunParentDir]);

  useEffect(() => {
    const intervalId = setInterval(() => {
      checkHealth();
      fetchStatus();
      fetchLatestAnswer();
      fetchScreenshot();
      fetchActivity();
      fetchSources();
      pollPendingResults();
      fetchQueueItems();
      fetchPowerSettings();
    }, 3000);
    return () => clearInterval(intervalId);
  }, [fetchLatestAnswer, pendingUids, activitySinceId, runId]);

  const pollPendingResults = async () => {
    if (!pendingUids || pendingUids.length === 0) return;
    // Poll a small batch to avoid request storms
    const batch = pendingUids.slice(0, 5);
    const completed = [];
    for (const uid of batch) {
      try {
        const res = await axios.get(`${BACKEND_URL}/result/${uid}`);
        const data = res.data;
        if (data && (data.status === "done" || data.status === "failed")) {
          completed.push(uid);
          if (data.answer && data.answer.trim() !== "") {
            setMessages((prev) => {
              // De-dupe by uid if present
              const exists = prev.some((m) => m.uid && m.uid === data.uid);
              if (exists) return prev;
              return [
                ...prev,
                {
                  type: "agent",
                  content: data.answer,
                  reasoning: data.reasoning,
                  agentName: data.agent_name,
                  status: data.status,
                  uid: data.uid,
                  ts: nowIso(),
                },
              ];
            });
            scrollToBottom();
          } else {
            setMessages((prev) => [
              ...prev,
              {
                type: "agent",
                content: `Queued job ${uid} finished (${data.status}).`,
                agentName: "System",
                uid: data.uid,
                ts: nowIso(),
              },
            ]);
          }
        }
      } catch (e) {
        // 404 means not ready yet; ignore
      }
    }
    if (completed.length > 0) {
      setPendingUids((prev) => prev.filter((u) => !completed.includes(u)));
    }
  };

  const fetchQueueItems = async () => {
    try {
      const res = await axios.get(`${BACKEND_URL}/queue_items`);
      const items = (res.data && res.data.items) || [];
      const serverItems = Array.isArray(items) ? items : [];
      setQueueItems((prev) => {
        const prevArr = Array.isArray(prev) ? prev : [];
        const prevByUid = new Map(prevArr.map((x) => [x.uid, x]));
        const editing = queueEditingRef.current || {};
        return serverItems.map((srv) => {
          const uid = srv && srv.uid;
          const p = uid ? prevByUid.get(uid) : null;
          const flags = uid ? editing[uid] : null;
          if (!p) return srv;
          const merged = { ...srv };
          // Preserve currently edited fields.
          if (flags && flags.project_name) merged.project_name = p.project_name;
          if (flags && flags.query) merged.query = p.query;
          // Preserve local UI-only fields if present.
          if (p.llm_id) merged.llm_id = p.llm_id;
          return merged;
        });
      });
    } catch {
      // ignore
    }
  };

  const setQueueFieldEditing = (uid, field, isEditing) => {
    if (!uid || !field) return;
    const cur = queueEditingRef.current || {};
    const entry = { ...(cur[uid] || {}) };
    entry[field] = !!isEditing;
    // Clean up empty entries
    if (!entry.project_name && !entry.query) {
      delete cur[uid];
    } else {
      cur[uid] = entry;
    }
    queueEditingRef.current = cur;
  };

  const llmIdFromProvider = (it) => {
    const pm = (it && it.provider_model ? String(it.provider_model) : "").toLowerCase();
    if (pm.includes("gpt-oss:20b") || pm.includes("gpt_oss") || pm.includes("gpt-oss")) return "gpt_oss_20b";
    return "deepseek_r1_32b";
  };

  const providerFromLlmId = (id) => {
    const chosen = (llmOptions || []).find((o) => o && o.id === id) || null;
    if (chosen) {
      return {
        provider_name: chosen.provider_name,
        provider_model: chosen.provider_model,
        provider_server_address: chosen.provider_server_address,
        provider_is_local: chosen.provider_is_local,
      };
    }
    // If we recognize the id, we can still send a best-effort local Ollama choice.
    // Otherwise, return undefined fields so backend falls back to config.ini defaults.
    if (id === "gpt_oss_20b") {
      return {
        provider_name: "ollama",
        provider_model: "gpt-oss:20b",
        provider_server_address: undefined,
        provider_is_local: true,
      };
    }
    if (id === "deepseek_r1_32b") {
      return {
        provider_name: "ollama",
        provider_model: "deepseek-r1:32b",
        provider_server_address: undefined,
        provider_is_local: true,
      };
    }
    return {
      provider_name: undefined,
      provider_model: undefined,
      provider_server_address: undefined,
      provider_is_local: undefined,
    };
  };

  const fetchPowerSettings = async () => {
    try {
      const res = await axios.get(`${BACKEND_URL}/power_settings`);
      setPowerSettings(res.data || null);
    } catch {
      // ignore
    }
  };

  const fetchPostRunSummarySettings = async () => {
    try {
      const res = await axios.get(`${BACKEND_URL}/post_run_summary_settings`);
      setPostRunSummarySettings(res.data || null);
    } catch {
      // ignore
    }
  };

  const savePostRunSummarySettings = async (patch) => {
    try {
      const res = await axios.post(`${BACKEND_URL}/post_run_summary_settings`, patch || {});
      setPostRunSummarySettings(res.data || null);
    } catch {
      // ignore
    }
  };

  const formatActivityEvent = (ev) => {
    const fields = ev.fields || {};
    const e = ev.event;

    if (e === "print") {
      const t = (fields.text || "").trim();
      if (!t) return null;
      if (/^▂+$/g.test(t) || /^▔+$/g.test(t)) return null;
      return t;
    }
    if (e === "browser_notes") {
      const n = (fields.notes || "").trim();
      return n ? `**Note:** ${n}` : null;
    }
    if (e === "web_search_query") {
      const q = (fields.query || "").trim();
      return q ? `**Search:** ${q}` : null;
    }
    if (e === "web_navigate") {
      const u = (fields.url || "").trim();
      return u ? `**Navigate:** ${u}` : null;
    }
    if (e === "page_snapshot") {
      const u = (fields.url || "").trim();
      const t = (fields.title || "").trim();
      if (!u && !t) return null;
      return `**Page:** ${t ? t + " — " : ""}${u}`;
    }
    if (e === "plan_step") {
      const step = fields.step || {};
      const st = (fields.status || step.status || "").toString();
      const title = step.title || step.task || "";
      if (!title) return null;
      return `**Plan:** \`${st}\` ${title}`;
    }
    if (e === "tool_executed") {
      const tool = fields.tool || "";
      const ok = fields.success;
      if (!tool) return null;
      if (ok === false) return `**Tool failed:** ${tool}`;
      return null;
    }
    if (e === "selected_agent") {
      const an = fields.agent_name || fields.agent_type || "";
      return an ? `**Agent:** ${an}` : null;
    }
    if (e === "sources_update") {
      const added = typeof fields.added === "number" ? fields.added : null;
      const total = typeof fields.total === "number" ? fields.total : null;
      const sid = fields.step_id ? ` step ${fields.step_id}` : "";
      if (added != null && total != null) return `**Sources:** +${added} (total ${total})${sid}`;
      return `**Sources:** updated${sid}`;
    }
    return null;
  };

  const syncActivityCursorToLatest = async (rid) => {
    try {
      const params = new URLSearchParams();
      if (rid) params.set("run_id", rid);
      params.set("since_id", "0");
      params.set("limit", "1");
      const res = await axios.get(`${BACKEND_URL}/activity?${params.toString()}`);
      const data = res.data || {};
      if (typeof data.latest_id === "number") {
        setActivitySinceId(data.latest_id);
      }
    } catch (e) {
      // ignore
    }
  };

  const activityKind = (ev) => {
    if (!ev) return "other";
    const e = ev.event;
    const fields = ev.fields || {};
    if (e === "print") {
      const c = (fields.color || "").toString();
      return c ? c : "print";
    }
    if (e === "web_search_query" || e === "web_navigate" || e === "page_snapshot") return "web";
    if (e === "plan_step") return "plan";
    if (e === "sources_update" || e === "sources_extraction_started" || e === "sources_extraction_finished") return "sources";
    if (String(e || "").includes("error")) return "error";
    // Check for report color in print events
    if (e === "print") {
      const c = (fields.color || "").toString();
      if (c === "report") return "report";
    }
    return e || "other";
  };

  const activityBadge = (ev) => {
    if (!ev) return "EVENT";
    const e = ev.event;
    const fields = ev.fields || {};
    if (e === "amendment") return "AMEND";
    if (e === "print") {
      const c = (fields.color || "").toString();
      if (c === "verifier" || c === "verifier_done" || c === "verifier_not_done") return "VERIFIER";
      if (c === "sources" || c === "sources_done" || c === "sources_not_done") return "SOURCES";
      if (c === "report") return "REPORT";
      if (c) return c.toUpperCase();
      return "LOG";
    }
    return String(e || "event").toUpperCase();
  };

  const fetchActivity = async () => {
    try {
      const params = new URLSearchParams();
      if (runId) params.set("run_id", runId);
      params.set("since_id", String(activitySinceId || 0));
      params.set("limit", "200");
      const res = await axios.get(`${BACKEND_URL}/activity?${params.toString()}`);
      const data = res.data || {};
      const events = Array.isArray(data.events) ? data.events : [];
      if (events.length > 0) {
        const newItems = events
          .map((ev) => {
            const text = formatActivityEvent(ev);
            return { ts: ev.ts || nowIso(), event: ev.event, fields: ev.fields || {}, kind: activityKind(ev), badge: activityBadge(ev), text };
          })
          .filter((x) => x && typeof x.text === "string" && x.text.trim() !== "");

        if (newItems.length > 0) {
          // Store activity lines for the Activity panel; keep chat uncluttered.
          setActivityFeed((prev) => {
            const merged = [...prev, ...newItems];
            return merged.slice(Math.max(0, merged.length - 200));
          });
        }
      }
      if (typeof data.next_since_id === "number") {
        setActivitySinceId(data.next_since_id);
      }
    } catch (e) {
      // ignore
    }
  };

  const fetchSources = async () => {
    try {
      if (!runId) return;
      const params = new URLSearchParams();
      params.set("run_id", runId);
      const res = await axios.get(`${BACKEND_URL}/sources?${params.toString()}`);
      const data = res.data || {};
      if (data && Array.isArray(data.sources)) {
        setSourcesData({ run_id: data.run_id || runId, updated_at: data.updated_at || null, sources: data.sources });
      }
    } catch (e) {
      // ignore
    }
  };

  // Save full run state to JSON file (for later restoration)
  const saveFullRun = () => {
    const effectiveRunId = runId || sourcesData?.run_id;
    const sourcesList = Array.isArray(sourcesData?.sources) ? sourcesData.sources : [];
    if (!effectiveRunId && sourcesList.length === 0) {
      alert("Nothing to save - run a query first or load existing sources.");
      return;
    }

    const runData = {
      version: 1,
      saved_at: new Date().toISOString(),
      run_id: effectiveRunId,
      // Sources
      sources: sourcesData,
      // Activity feed
      activity: activityFeed || [],
      // Chat messages
      messages: messages || [],
      // Goal/query
      goal: reportGoal || (messages.find(m => m.sender === "user")?.text) || "",
      // Plan if any
      plan: plan,
      planGoal: planGoal,
    };

    const blob = new Blob([JSON.stringify(runData, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `agenticseek_run_${effectiveRunId || "unknown"}_${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // Load/restore full run state from JSON file
  const loadFullRun = async (file) => {
    if (!file) return;
    try {
      const text = await file.text();
      const data = JSON.parse(text);

      // Check if this is a full run save (version 1) or just sources
      const isFullRun = data.version === 1;

      // Get the run_id
      const targetRunId = data.run_id || data.sources?.run_id || `imported-${Date.now()}`;

      console.log("[Load] Loading run...", { targetRunId, isFullRun, hasActivity: !!data.activity?.length });

      // Upload sources to backend
      const sourcesToUpload = isFullRun ? data.sources : data;
      const res = await axios.post(`${BACKEND_URL}/sources_upload`, {
        run_id: targetRunId,
        sources_json: JSON.stringify(sourcesToUpload),
      });

      const result = res.data || {};
      console.log("[Load] Sources upload result:", result);

      if (result.ok || isFullRun) {
        // Restore sources
        if (isFullRun && data.sources) {
          setSourcesData({
            run_id: targetRunId,
            updated_at: data.sources.updated_at || new Date().toISOString(),
            sources: data.sources.sources || [],
          });
        } else if (data.sources) {
          setSourcesData({
            run_id: targetRunId,
            updated_at: data.updated_at || new Date().toISOString(),
            sources: data.sources || [],
          });
        }

        // Restore activity feed if present
        if (isFullRun && Array.isArray(data.activity) && data.activity.length > 0) {
          setActivityFeed(data.activity);
        }

        // Restore messages if present
        if (isFullRun && Array.isArray(data.messages) && data.messages.length > 0) {
          setMessages(data.messages);
        }

        // Restore goal
        if (isFullRun && data.goal) {
          setReportGoal(data.goal);
        }

        // Restore plan if present
        if (isFullRun && data.plan) {
          setPlan(data.plan);
          setPlanGoal(data.planGoal || "");
        }

        const sourcesCount = (isFullRun ? data.sources?.sources?.length : data.sources?.length) || 0;
        const activityCount = data.activity?.length || 0;
        const msgCount = data.messages?.length || 0;

        alert(`✅ Loaded run: ${sourcesCount} sources${activityCount ? `, ${activityCount} activity items` : ""}${msgCount ? `, ${msgCount} messages` : ""}`);
      } else {
        alert(`❌ Load failed: ${result.error || "Unknown error"}`);
      }
    } catch (e) {
      console.error("[Load] Error:", e);
      alert(`❌ Load failed: ${e.message || e}`);
    }
  };

  // Report generation functions
  // Use runId from component state, or fall back to sourcesData.run_id (for loaded/imported runs)
  const getEffectiveRunId = () => runId || sourcesData?.run_id || null;

  const requestReport = async (goal) => {
    const effectiveRunId = getEffectiveRunId();
    console.log("[Report] requestReport called", { runId, effectiveRunId, goal });
    if (!effectiveRunId) {
      console.error("[Report] No runId available");
      setReportStatus({ status: "error", report: null, error: "No run ID available. Load sources or run a query first." });
      return;
    }
    if (!goal) {
      console.error("[Report] No goal provided");
      setReportStatus({ status: "error", report: null, error: "No goal/query found. Enter a goal in the prompt below, or load a saved run." });
      return;
    }
    try {
      console.log("[Report] Queuing report generation...");
      setReportStatus({ status: "queued", report: null, error: null });
      setReportGoal(goal);
      const res = await axios.post(`${BACKEND_URL}/generate_sources_report`, { run_id: effectiveRunId, goal });
      console.log("[Report] Queue response:", res.data);
      // Start polling for status
      pollReportStatus(effectiveRunId);
    } catch (e) {
      console.error("[Report] Error queuing report:", e);
      const errMsg = e.response?.data?.error || e.message || "Failed to queue report";
      setReportStatus({ status: "error", report: null, error: errMsg });
    }
  };

  const pollReportStatus = async (overrideRunId = null) => {
    const effectiveRunId = overrideRunId || getEffectiveRunId();
    if (!effectiveRunId) return;
    try {
      const params = new URLSearchParams();
      params.set("run_id", effectiveRunId);
      const res = await axios.get(`${BACKEND_URL}/sources_report_status?${params.toString()}`);
      const data = res.data || {};
      setReportStatus({
        status: data.status || "none",
        report: data.report || null,
        error: data.error || null,
      });
      // Keep polling if still generating
      if (data.status === "queued" || data.status === "generating") {
        setTimeout(() => pollReportStatus(effectiveRunId), 1500);
      }
    } catch (e) {
      // ignore polling errors
    }
  };

  const fetchStatus = async () => {
    try {
      const res = await axios.get(`${BACKEND_URL}/status`);
      const data = res.data || {};
      setIsGenerating(!!data.is_generating);
      setQueueLength(data.queue_length || 0);
      setIsPaused(!!data.paused);
      if (data.current_status) {
        setStatus(data.current_status);
      }
      setAgentType(data.agent_type || null);
      setAgentName(data.agent_name || null);
      const nextRunId = data.run_id || null;
      if (nextRunId !== runId) {
        // New run: reset cursor to avoid replaying old activity after refresh.
        setActivitySinceId(0);
        setActivityFeed([]);
        syncActivityCursorToLatest(nextRunId);
        // New run: clear sources immediately so we don't show stale sources from a prior run.
        setSourcesData({ run_id: nextRunId, updated_at: null, sources: [] });
        // Reset report status for new run
        setReportStatus({ status: "none", report: null, error: null });
      }
      setRunId(nextRunId);
      setOutputDir(data.output_dir || null);
      setPlanGoal(data.plan_goal || null);
      setPlanCurrentStep(
        typeof data.plan_current_step === "number" ? data.plan_current_step : null
      );
      setPlan(Array.isArray(data.plan) ? data.plan : null);
      setUiSessionId(data.ui_session_id || null);
    } catch (e) {
      // ignore
    }
  };

  const handleNewRun = async () => {
    try {
      await axios.post(`${BACKEND_URL}/new_run`, {});
    } catch (e) {
      // ignore; still reset UI locally
    }
    // Reset UI state to feel like a fresh container restart
    setMessages([]);
    setResponseData(null);
    setError(null);
    setStatus("Agents ready");
    setIsLoading(false);
    setIsGenerating(false);
    setIsPaused(false);
    setQueueLength(0);
    setPendingUids([]);
    setPlan(null);
    setPlanGoal(null);
    setPlanCurrentStep(null);
    setRunId(null);
    setOutputDir(null);
    setActivityFeed([]);
    setActivitySinceId(0);
    setRunFilesRunId("");
    setRunFiles([]);
    setRunFileName("");
    setRunFileView({ content: "", truncated: false });
    setReportStatus({ status: "none", report: null, error: null });
    // also refresh runs list for trace chat
    fetchRuns();
  };

  const handleRestartServer = async () => {
    const ok = window.confirm("Restart backend server now? (current run will stop)");
    if (!ok) return;
    try {
      await axios.post(`${BACKEND_URL}/restart`, {});
      setStatus("Restarting backend…");
      setIsOnline(false);
    } catch (e) {
      setStatus("Failed to restart backend");
    }
  };

  useEffect(() => {
    localStorage.setItem("rawVerbosity", String(rawVerbosity));
  }, [rawVerbosity]);

  useEffect(() => {
    localStorage.setItem("rawRunParentDir", rawRunParentDir);
  }, [rawRunParentDir]);

  useEffect(() => {
    try {
      localStorage.setItem("rawOcrEnabled", String(rawOcrEnabled));
    } catch {}
  }, [rawOcrEnabled]);

  useEffect(() => {
    try {
      localStorage.setItem("rawSourcesEnabled", String(rawSourcesEnabled));
    } catch {}
  }, [rawSourcesEnabled]);

  useEffect(() => {
    try {
      localStorage.setItem("rawSourcesLlmEnabled", String(rawSourcesLlmEnabled));
    } catch {}
  }, [rawSourcesLlmEnabled]);

  useEffect(() => {
    try {
      localStorage.setItem("projectName", projectName || "");
    } catch {}
  }, [projectName]);

  useEffect(() => {
    try {
      localStorage.setItem("traceProject", traceProject || "");
    } catch {}
  }, [traceProject]);

  useEffect(() => {
    try {
      // Migrate old value "config_default" -> gpt-oss default.
      if ((llmOptionId || "") === "config_default") {
        setLlmOptionId("gpt_oss_20b");
        localStorage.setItem("llmOptionId", "gpt_oss_20b");
      } else {
        localStorage.setItem("llmOptionId", llmOptionId || "gpt_oss_20b");
      }
    } catch {}
  }, [llmOptionId]);

  useEffect(() => {
    try {
      if ((postSummaryLlmOptionId || "") === "config_default") {
        setPostSummaryLlmOptionId("deepseek_r1_32b");
        localStorage.setItem("postSummaryLlmOptionId", "deepseek_r1_32b");
      } else {
        localStorage.setItem("postSummaryLlmOptionId", postSummaryLlmOptionId || "deepseek_r1_32b");
      }
    } catch {}
  }, [postSummaryLlmOptionId]);

  const [defaultOutputFormat, setDefaultOutputFormat] = useState(() => {
    return localStorage.getItem("defaultOutputFormat") || "none";
  });
  const [spreadsheetFormat, setSpreadsheetFormat] = useState(() => {
    return localStorage.getItem("spreadsheetFormat") || "csv";
  });
  const [allowCoderAgent, setAllowCoderAgent] = useState(() => {
    const saved = localStorage.getItem("allowCoderAgent");
    return saved ? saved === "true" : false;  // Default OFF
  });
  const ALL_TOOL_TAGS = ["web_search", "file_finder", "write_output", "bash", "python", "go", "java", "c"];
  const DEFAULT_ENABLED_TOOLS = ["web_search", "file_finder", "write_output"]; // bash and legacy tools disabled by default
  const [enabledTools, setEnabledTools] = useState(() => {
    // Prefer new allowlist, but migrate old disabledTools if present.
    try {
      const rawEnabled = localStorage.getItem("enabledTools");
      if (rawEnabled) {
        const parsed = JSON.parse(rawEnabled);
        if (Array.isArray(parsed)) {
          // MIGRATION: Ensure write_output is enabled (added in recent update)
          // If user has old config without write_output, add it
          if (!parsed.includes("write_output") && (parsed.includes("file_finder") || parsed.includes("append_file"))) {
            parsed.push("write_output");
            localStorage.setItem("enabledTools", JSON.stringify(parsed));
          }
          return parsed;
        }
        return DEFAULT_ENABLED_TOOLS;
      }
      const rawDisabled = localStorage.getItem("disabledTools");
      if (rawDisabled) {
        const disabled = JSON.parse(rawDisabled);
        if (Array.isArray(disabled)) {
          const set = new Set(ALL_TOOL_TAGS);
          disabled.forEach((t) => set.delete(t));
          return Array.from(set);
        }
      }
    } catch {
      // ignore
    }
    return DEFAULT_ENABLED_TOOLS;
  });

  useEffect(() => {
    localStorage.setItem("spreadsheetFormat", spreadsheetFormat);
  }, [spreadsheetFormat]);

  useEffect(() => {
    localStorage.setItem("defaultOutputFormat", defaultOutputFormat);
  }, [defaultOutputFormat]);

  useEffect(() => {
    localStorage.setItem("allowCoderAgent", String(allowCoderAgent));
  }, [allowCoderAgent]);

  // If coder agent is disabled, also ensure code-execution tools aren't enabled.
  useEffect(() => {
    if (!allowCoderAgent) {
      setEnabledTools((prev) => (prev || []).filter((t) => !["python", "go", "java", "c"].includes(t)));
    }
  }, [allowCoderAgent]);

  useEffect(() => {
    localStorage.setItem("enabledTools", JSON.stringify(enabledTools || []));
  }, [enabledTools]);

  const checkHealth = async () => {
    try {
      await axios.get(`${BACKEND_URL}/health`);
      setIsOnline(true);
      console.log("System is online");
    } catch {
      setIsOnline(false);
      console.log("System is offline");
    }
  };

  const fetchScreenshot = async () => {
    try {
      const timestamp = new Date().getTime();
      const res = await axios.get(
        `${BACKEND_URL}/screenshot?timestamp=${timestamp}`,
        {
          responseType: "blob",
        }
      );
      console.log("Screenshot fetched successfully");
      const imageUrl = URL.createObjectURL(res.data);
      setResponseData((prev) => {
        if (prev?.screenshot && prev.screenshot !== "placeholder.png") {
          URL.revokeObjectURL(prev.screenshot);
        }
        return {
          ...prev,
          screenshot: imageUrl,
          screenshotTimestamp: new Date().getTime(),
        };
      });
    } catch (err) {
      console.error("Error fetching screenshot:", err);
      setResponseData((prev) => ({
        ...prev,
        screenshot: "placeholder.png",
        screenshotTimestamp: new Date().getTime(),
      }));
    }
  };

  const normalizeAnswer = (answer) => {
    return answer
      .trim()
      .toLowerCase()
      .replace(/\s+/g, " ")
      .replace(/[.,!?]/g, "");
  };

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  const toggleReasoning = (messageIndex) => {
    setExpandedReasoning((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(messageIndex)) {
        newSet.delete(messageIndex);
      } else {
        newSet.add(messageIndex);
      }
      return newSet;
    });
  };

  const updateData = (data) => {
    setResponseData((prev) => ({
      ...prev,
      blocks: data.blocks || prev.blocks || null,
      done: data.done,
      answer: data.answer,
      agent_name: data.agent_name,
      status: data.status,
      uid: data.uid,
    }));
  };

  const handleStop = async (e) => {
    e.preventDefault();
    checkHealth();
    setIsLoading(false);
    setError(null);
    try {
      if (isPaused) {
        await axios.get(`${BACKEND_URL}/resume`);
        setStatus("Resuming...");
      } else {
        await axios.get(`${BACKEND_URL}/stop`);
        setStatus("Pausing...");
      }
    } catch (err) {
      console.error("Error stopping the agent:", err);
    }
  };

  const handleAmend = async () => {
    if (!query.trim()) return;
    const amendText = query;
    try {
      const res = await axios.post(`${BACKEND_URL}/amend`, { text: amendText });
      if (res.data && res.data.success) {
        setMessages((prev) => [
          ...prev,
          { type: "amendment", content: `📝 Added to current run: "${amendText}"`, ts: nowIso() },
        ]);
        setQuery("");
      } else {
        // Unexpected response
        alert("Could not add amendment: " + JSON.stringify(res.data));
      }
    } catch (err) {
      console.error("Failed to add amendment:", err);
      const errMsg = err.response?.data?.error || "Unknown error";
      if (errMsg.includes("No active run")) {
        // No run active - show message and don't auto-queue
        alert("No task is currently running. Use the arrow button to submit a new task.");
      } else {
        alert("Failed to add amendment: " + errMsg);
      }
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    checkHealth();
    if (!query.trim()) {
      console.log("Empty query");
      return;
    }
    setMessages((prev) => [...prev, { type: "user", content: query, ts: nowIso() }]);
    // Do not block the chat input; backend queues immediately.
    setIsLoading(true);
    setError(null);

    try {
      console.log("Sending query:", query);
      const chosenLlm = (effectiveLlmOptions || []).find((o) => o && o.id === llmOptionId) || null;
      const fallbackProv = providerFromLlmId(llmOptionId);
      const res = await axios.post(`${BACKEND_URL}/query`, {
        query,
        tts_enabled: false,
        project_name: projectName || undefined,
        mode: runMode,
        // If selection is known, send explicit provider settings.
        // If not, omit provider_* so backend falls back to config.ini defaults.
        provider_name: chosenLlm ? chosenLlm.provider_name : fallbackProv.provider_name,
        provider_model: chosenLlm ? chosenLlm.provider_model : fallbackProv.provider_model,
        provider_server_address: chosenLlm ? chosenLlm.provider_server_address : fallbackProv.provider_server_address,
        provider_is_local: chosenLlm ? chosenLlm.provider_is_local : fallbackProv.provider_is_local,
        run_parent_dir: rawRunParentDir,
        tool_config: {
          spreadsheet_format: spreadsheetFormat,
          default_output_format: defaultOutputFormat,
          enabled_tools: enabledTools,
        },
        agent_config: {
          disabled_agents: allowCoderAgent ? [] : ["code_agent", "code", "coder"],
        },
        trace_config:
          runMode === "trace"
            ? rawVerbosity === 0
              ? {
                  save_query: true,
                  save_plan: true,
                  save_intermediate_outputs: false,
                  save_web_snapshots: false,
                  save_web_navigation: true,
                  save_web_page_text: false,
                  save_web_screenshots: false,
                  save_web_ocr: false,
                  save_sources: !!rawSourcesEnabled,
                  save_sources_llm: !!rawSourcesLlmEnabled,
                  save_final_answer: true,
                }
              : rawVerbosity === 1
              ? {
                  save_query: true,
                  save_plan: true,
                  save_intermediate_outputs: true,
                  // Standard raw: record navigation + URL/title snapshots, but don't embed full page text.
                  save_web_snapshots: true,
                  save_web_navigation: true,
                  save_web_page_text: false,
                  save_web_screenshots: false,
                  save_web_ocr: false,
                  save_sources: !!rawSourcesEnabled,
                  save_sources_llm: !!rawSourcesLlmEnabled,
                  save_final_answer: true,
                }
              : {
                  save_query: true,
                  save_plan: true,
                  save_intermediate_outputs: true,
                  // Max raw: record navigation + snapshots + full page text.
                  save_web_snapshots: true,
                  save_web_navigation: true,
                  save_web_page_text: true,
                  // Max raw extras: save images per snapshot + optional OCR text embedded into trace.jsonl.
                  save_web_screenshots: true,
                  save_web_ocr: !!rawOcrEnabled,
                  save_sources: !!rawSourcesEnabled,
                  save_sources_llm: !!rawSourcesLlmEnabled,
                  save_final_answer: true,
                }
            : undefined,
      });
      console.log("Response:", res.data);
      const data = res.data;
      updateData(data);

      // Show immediate backend response in chat (important after refresh / when latest_answer lags).
      if (data && data.status === "queued") {
        setPendingUids((prev) => [...prev, data.uid]);
        setMessages((prev) => [
          ...prev,
          { type: "agent", content: `Queued (id: ${data.uid})`, agentName: "System", ts: nowIso() },
        ]);
      } else if (data && data.answer && data.answer.trim() !== "") {
        setMessages((prev) => [
          ...prev,
          {
            type: "agent",
            content: data.answer,
            reasoning: data.reasoning,
            agentName: data.agent_name,
            status: data.status,
            uid: data.uid,
            ts: nowIso(),
          },
        ]);
        scrollToBottom();
      }
    } catch (err) {
      console.error("Error:", err);
      setError("Failed to process query.");
      setMessages((prev) => [
        ...prev,
        { type: "error", content: "Error: Unable to get a response.", ts: nowIso() },
      ]);
    } finally {
      console.log("Query completed");
      setIsLoading(false);
      setQuery("");
    }
  };

  const handleGetScreenshot = async () => {
    try {
      setCurrentView("screenshot");
      // Force an immediate refresh so Browser View feels responsive.
      await fetchScreenshot();
    } catch (err) {
      setError("Browser not in use");
    }
  };

  const renderPanel = (panelId) => {
    const { base } = parseTabId(panelId);
    if (base === "chat") {
      return (
        <div className="panel panel-chat">
          <div className="messages">
            {messages.length === 0 ? (
              <p className="placeholder">No messages yet. Type a question or task below. The AI will plan and execute it using web search, file operations, and more.</p>
            ) : (
              messages.map((msg, index) => (
                <div
                  key={msg.uid || index}
                  className={`message ${
                    msg.type === "user"
                      ? "user-message"
                      : msg.type === "agent"
                      ? "agent-message"
                      : msg.type === "error"
                      ? "error-message"
                      : msg.type === "amendment"
                      ? "amendment-message"
                      : "agent-message"
                  }`}
                >
                  <div className="message-header">
                    {msg.ts ? <span className="message-timestamp">{formatTs(msg.ts)}</span> : null}
                    {msg.type === "agent" && (
                      <span className="agent-name">{msg.agentName}</span>
                    )}
                    {msg.type === "agent" &&
                      msg.reasoning &&
                      expandedReasoning.has(index) && (
                        <div className="reasoning-content">
                          <ReactMarkdown>{msg.reasoning}</ReactMarkdown>
                        </div>
                      )}
                    {msg.type === "agent" && (
                      <button
                        className="reasoning-toggle"
                        onClick={() => toggleReasoning(index)}
                        title={
                          expandedReasoning.has(index)
                            ? "Hide reasoning"
                            : "Show reasoning"
                        }
                      >
                        {expandedReasoning.has(index) ? "▼" : "▶"} Reasoning
                      </button>
                    )}
                  </div>
                  <div className="message-content">
                    <ReactMarkdown>{msg.content}</ReactMarkdown>
                  </div>
                </div>
              ))
            )}
            <div ref={messagesEndRef} />
          </div>
          {isOnline && <div className="loading-animation">{status}</div>}
          {!isLoading && !isOnline && (
            <p className="loading-animation">System offline. Deploy backend first.</p>
          )}
          <form onSubmit={handleSubmit} className="input-form">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Type your query..."
              disabled={false}
            />
            <div className="action-buttons">
              <button
                type="submit"
                disabled={isLoading}
                className="icon-button"
                aria-label="Send message"
                title="Submit new query"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                  <path
                    d="M22 2L11 13M22 2L15 22L11 13M22 2L2 9L11 13"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </button>
              {isGenerating && (
                <button
                  type="button"
                  onClick={handleAmend}
                  className="icon-button amend-button"
                  aria-label="Add to current run"
                  title="Add note to current run (doesn't queue new task)"
                  style={{ background: "#6b5b95", borderColor: "#6b5b95" }}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                    <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
                  </svg>
                </button>
              )}
              <button
                type="button"
                onClick={handleStop}
                className="icon-button stop-button"
                aria-label="Pause/Resume"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                  <rect x="6" y="6" width="12" height="12" fill="currentColor" rx="2" />
                </svg>
              </button>
            </div>
          </form>
        </div>
      );
    }

    if (base === "checklist") {
      return (
        <div className="panel panel-scroll">
          {(plan && plan.length > 0) || agentType ? (
            <div className="plan-panel">
              <div className="plan-header">
                <div className="plan-title">Run checklist</div>
                <div className="plan-subtitle" style={{ marginBottom: 4 }}>
                  Multi-step plan created by the Planner agent. Each step runs in sequence.
                </div>
                <div className="plan-subtitle">
                  {agentName ? `${agentName}${agentType ? ` (${agentType})` : ""}` : ""}
                  {planGoal ? ` • ${planGoal}` : ""}
                </div>
              </div>
              {plan && plan.length > 0 ? (
                <div className="plan-steps">
                  {plan.map((step) => {
                    const st = step.status || "pending";
                    const isActive =
                      typeof planCurrentStep === "number" &&
                      step.idx === planCurrentStep &&
                      st === "running";
                    return (
                      <div
                        key={`${step.id}-${step.idx}`}
                        className={`plan-step ${st} ${isActive ? "active" : ""}`}
                      >
                        <div className="plan-step-icon">
                          {st === "completed" ? "✓" : st === "failed" ? "✕" : st === "running" ? "…" : "•"}
                        </div>
                        <div className="plan-step-body">
                          <div className="plan-step-title">
                            {step.title || step.task || "Step"}
                          </div>
                          <div className="plan-step-meta">
                            {step.agent ? `Agent: ${step.agent}` : ""}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="plan-empty">{isGenerating ? "Working…" : "Idle"}</div>
              )}
            </div>
          ) : (
            <div className="plan-empty">No plan.</div>
          )}
        </div>
      );
    }

    if (base === "activity") {
      return (
        <div className="panel panel-scroll">
          <div className="plan-header" style={{ marginBottom: 8 }}>
            <div className="plan-title">Activity</div>
            <div className="plan-subtitle">Live updates for this run.</div>
          </div>
          {activityFeed.length === 0 ? (
            <div className="plan-empty">No activity yet.</div>
          ) : (
            <div className="activity-feed">
              {activityFeed.slice().reverse().map((item, idx) => (
                <div
                  key={idx}
                  className={`activity-item kind-${(item.kind || "other").toString().replace(/[^a-z0-9_\\-]/gi, "")}`}
                >
                  <div className="activity-meta-row">
                    <span className={`activity-badge kind-${(item.kind || "other").toString().replace(/[^a-z0-9_\\-]/gi, "")}`}>
                      {item.badge || "EVENT"}
                    </span>
                    <span className="activity-timestamp">{formatTs(item.ts)}</span>
                  </div>
                  <div className="activity-text">
                    <ReactMarkdown>{item.text}</ReactMarkdown>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      );
    }

    if (base === "sources") {
      const sources = Array.isArray(sourcesData.sources) ? sourcesData.sources : [];
      const isReportLoading = reportStatus.status === "queued" || reportStatus.status === "generating";

      // Get the current goal from messages or stored reportGoal
      const getCurrentGoal = () => {
        // Try to find the most recent user query
        for (let i = messages.length - 1; i >= 0; i--) {
          const m = messages[i];
          if (m && m.sender === "user" && m.text) {
            return m.text;
          }
        }
        return reportGoal || "";
      };

      return (
        <div className="panel panel-scroll">
          <div className="plan-header" style={{ marginBottom: 8 }}>
            <div className="plan-title">Sources</div>
            <div className="plan-subtitle">
              Live, de-duplicated sources captured during web browsing (optional; toggle in Settings). Download for offline use.
            </div>
          </div>

          {/* Report Generation Section */}
          <div style={{ background: "#1f1f1f", borderRadius: 8, padding: 12, marginBottom: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
              <span style={{ fontWeight: 600 }}>📝 Generate Report</span>
              <button
                type="button"
                className="icon-button"
                style={{
                  background: isReportLoading ? "#6b7280" : "#8b5cf6",
                  color: "#fff",
                  padding: "6px 12px",
                  borderRadius: 6,
                  fontSize: 12,
                  fontWeight: 600,
                  cursor: isReportLoading ? "not-allowed" : "pointer",
                }}
                disabled={isReportLoading}
                onClick={() => {
                  const effectiveRunId = getEffectiveRunId();
                  console.log("[Report] Button clicked", { runId, effectiveRunId, sourcesLen: sources.length, isReportLoading });
                  if (!effectiveRunId) {
                    setReportStatus({ status: "error", report: null, error: "No run ID. Load sources or start a new query first." });
                    return;
                  }
                  if (sources.length === 0) {
                    setReportStatus({ status: "error", report: null, error: "No sources collected yet. Run a web search first, or load saved sources." });
                    return;
                  }
                  let goal = getCurrentGoal();
                  // If no goal found from messages, prompt user to enter one (useful for loaded sources)
                  if (!goal) {
                    goal = prompt("Enter the research question/goal for this report:", reportGoal || "");
                  }
                  if (goal && goal.trim()) {
                    requestReport(goal.trim());
                  } else {
                    setReportStatus({ status: "error", report: null, error: "No goal provided. Please enter a research question." });
                  }
                }}
                title={!getEffectiveRunId() ? "No run - load sources first" : sources.length === 0 ? "No sources available" : "Generate a detailed report from sources"}
              >
                {isReportLoading ? "⏳ Generating..." : "Generate Report"}
              </button>
              {reportStatus.status === "done" && reportStatus.report && (
                <>
                  <button
                    type="button"
                    className="icon-button"
                    title="Download report as Markdown"
                    onClick={() => {
                      const effectiveRunId = getEffectiveRunId();
                      if (!effectiveRunId) return;
                      const params = new URLSearchParams();
                      params.set("run_id", effectiveRunId);
                      window.open(`${BACKEND_URL}/sources_report_download?${params.toString()}`, "_blank", "noopener,noreferrer");
                    }}
                  >
                    ⭳ .md
                  </button>
                  <button
                    type="button"
                    className="icon-button"
                    title="Copy report to clipboard"
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(reportStatus.report);
                      } catch {}
                    }}
                  >
                    ⧉
                  </button>
                </>
              )}
            </div>

            {/* Report Status/Content */}
            {reportStatus.status === "error" && (
              <div style={{ color: "#ef4444", fontSize: 13, padding: 8, background: "#2a1a1a", borderRadius: 4 }}>
                ❌ Error: {reportStatus.error}
              </div>
            )}
            {reportStatus.status === "done" && reportStatus.report && (
              <details open style={{ marginTop: 8 }}>
                <summary style={{ cursor: "pointer", fontWeight: 500, marginBottom: 8 }}>
                  📄 View Generated Report
                </summary>
                <div
                  style={{
                    background: "#111",
                    borderRadius: 6,
                    padding: 16,
                    maxHeight: 500,
                    overflowY: "auto",
                    fontSize: 13,
                    lineHeight: 1.6,
                    whiteSpace: "pre-wrap",
                  }}
                  dangerouslySetInnerHTML={{
                    __html: reportStatus.report
                      .replace(/^### (.*$)/gm, '<h3 style="margin: 16px 0 8px; font-size: 15px; color: #a78bfa;">$1</h3>')
                      .replace(/^## (.*$)/gm, '<h2 style="margin: 20px 0 10px; font-size: 17px; color: #c4b5fd;">$1</h2>')
                      .replace(/^# (.*$)/gm, '<h1 style="margin: 24px 0 12px; font-size: 20px; color: #e9d5ff;">$1</h1>')
                      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
                      .replace(/\*(.*?)\*/g, '<em>$1</em>')
                      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer" style="color: #60a5fa;">$1</a>')
                      .replace(/^- (.*$)/gm, '<li style="margin-left: 16px;">$1</li>')
                      .replace(/^(\d+)\. (.*$)/gm, '<li style="margin-left: 16px;"><strong>$1.</strong> $2</li>')
                      .replace(/---/g, '<hr style="border: none; border-top: 1px solid #333; margin: 16px 0;">')
                  }}
                />
              </details>
            )}
            {(reportStatus.status === "none" || !reportStatus.report) && reportStatus.status !== "error" && !isReportLoading && (
              <div style={{ fontSize: 12, opacity: 0.7 }}>
                Click "Generate Report" to create a detailed analysis from the collected sources.
              </div>
            )}
          </div>

          <div className="raw-settings-row" style={{ gap: 8, marginBottom: 8 }}>
            <button type="button" className="icon-button" onClick={fetchSources} title="Refresh sources">
              ↻
            </button>
            <button
              type="button"
              className="icon-button"
              title="Download sources JSON"
              disabled={!runId}
              onClick={() => {
                if (!runId) return;
                const params = new URLSearchParams();
                params.set("run_id", runId);
                window.open(`${BACKEND_URL}/sources_download?${params.toString()}`, "_blank", "noopener,noreferrer");
              }}
            >
              ⭳
            </button>
            <button
              type="button"
              className="icon-button"
              title="Download sources Markdown"
              disabled={!runId}
              onClick={() => {
                if (!runId) return;
                const params = new URLSearchParams();
                params.set("run_id", runId);
                window.open(`${BACKEND_URL}/sources_download_md?${params.toString()}`, "_blank", "noopener,noreferrer");
              }}
            >
              .md
            </button>
            <button
              type="button"
              className="icon-button"
              title="Copy all sources JSON to clipboard"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(JSON.stringify(sourcesData, null, 2));
                } catch {}
              }}
            >
              ⧉
            </button>
            {/* Save/Load full run */}
            <input
              type="file"
              id="run-upload-input"
              accept=".json,application/json"
              style={{ display: "none" }}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) {
                  loadFullRun(file);
                  e.target.value = ""; // Reset so same file can be selected again
                }
              }}
            />
            <button
              type="button"
              className="icon-button"
              title="Save full run (sources, activity, messages) to JSON"
              onClick={saveFullRun}
              style={{ background: "#22c55e" }}
            >
              💾 Save
            </button>
            <button
              type="button"
              className="icon-button"
              title="Load saved run from JSON file"
              onClick={() => document.getElementById("run-upload-input")?.click()}
              style={{ background: "#3b82f6" }}
            >
              📂 Load
            </button>
            <div style={{ marginLeft: "auto", opacity: 0.8, fontSize: 12 }}>
              {sourcesData.updated_at ? `Updated: ${formatTs(sourcesData.updated_at)}` : ""}
              {sources.length ? ` • ${sources.length} sources` : ""}
            </div>
          </div>
          {sources.length === 0 ? (
            <div className="plan-empty">No sources yet.</div>
          ) : (
            <div className="queue-list">
              {(() => {
                // Group sources by domain
                const getDomain = (url) => {
                  try {
                    return new URL(url).hostname.replace(/^www\./, "");
                  } catch {
                    return "other";
                  }
                };
                const grouped = {};
                sources.forEach((s) => {
                  const domain = getDomain(s.url || "");
                  if (!grouped[domain]) grouped[domain] = [];
                  grouped[domain].push(s);
                });
                // Sort domains by highest relevancy in group
                const domainList = Object.keys(grouped).sort((a, b) => {
                  const maxA = Math.max(...grouped[a].map((x) => parseFloat(x.relevancy_score) || 0));
                  const maxB = Math.max(...grouped[b].map((x) => parseFloat(x.relevancy_score) || 0));
                  return maxB - maxA;
                });

                const renderSource = (s, idx) => (
                  <div key={(s.normalized_url || s.url || "") + idx} style={{ background: "#1f1f1f", borderRadius: 6, padding: 10, marginBottom: 8 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
                      <div style={{ wordBreak: "break-word", flex: 1 }}>
                        <strong style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                          <span>{s.title || (s.kind === "citation" ? "Cited Reference" : s.kind) || "Source"}</span>
                          {s.kind === "citation" ? (
                            <span style={{ background: "#8b5cf6", color: "#fff", padding: "2px 6px", borderRadius: 8, fontSize: 10, fontWeight: 600 }}>📎 cited</span>
                          ) : null}
                          {formatScore(s.relevancy_score) ? (
                            <span
                              style={{
                                background: parseFloat(s.relevancy_score) >= 0.7 ? "#22c55e" : parseFloat(s.relevancy_score) >= 0.4 ? "#eab308" : "#6b7280",
                                color: "#000",
                                padding: "2px 8px",
                                borderRadius: 12,
                                fontSize: 11,
                                fontWeight: 700,
                              }}
                            >
                              {formatScore(s.relevancy_score)}
                            </span>
                          ) : null}
                        </strong>
                        {s.url ? (
                          <div style={{ fontSize: 11, opacity: 0.7, marginTop: 2 }}>
                            <a href={s.url} target="_blank" rel="noopener noreferrer">{s.url}</a>
                          </div>
                        ) : null}
                      </div>
                      <button
                        type="button"
                        className="icon-button"
                        title="Copy source JSON"
                        style={{ flexShrink: 0 }}
                        onClick={async () => {
                          try { await navigator.clipboard.writeText(JSON.stringify(s, null, 2)); } catch {}
                        }}
                      >⧉</button>
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
                      {s.match ? (
                        <div>
                          <div className="queue-item-label">Why it matches</div>
                          <div style={{ whiteSpace: "pre-wrap", fontSize: 13, marginTop: 2 }}>{s.match}</div>
                        </div>
                      ) : null}
                      {s.how_helps ? (
                        <div>
                          <div className="queue-item-label">How it helps</div>
                          <div style={{ whiteSpace: "pre-wrap", fontSize: 13, marginTop: 2 }}>{s.how_helps}</div>
                        </div>
                      ) : null}
                      {Array.isArray(s.data_to_collect) && s.data_to_collect.length ? (
                        <div>
                          <div className="queue-item-label">Data to collect</div>
                          <ul style={{ margin: "4px 0 0 16px", padding: 0, fontSize: 13 }}>
                            {s.data_to_collect.slice(0, 6).map((d, i) => <li key={i}>{d}</li>)}
                          </ul>
                        </div>
                      ) : null}
                      {Array.isArray(s.evidence_quotes) && s.evidence_quotes.length ? (
                        <div>
                          <div className="queue-item-label">Evidence (verbatim)</div>
                          <ul style={{ margin: "4px 0 0 16px", padding: 0, fontSize: 13, fontStyle: "italic", opacity: 0.9 }}>
                            {s.evidence_quotes.slice(0, 4).map((q, i) => <li key={i}>"{q}"</li>)}
                          </ul>
                        </div>
                      ) : null}
                      {Array.isArray(s.verbatim_context) && s.verbatim_context.length ? (
                        <details style={{ marginTop: 4 }}>
                          <summary className="queue-item-label" style={{ cursor: "pointer", userSelect: "none" }}>
                            ▸ Raw page content ({s.verbatim_context.length})
                          </summary>
                          <div style={{ whiteSpace: "pre-wrap", maxHeight: 180, overflowY: "auto", background: "#111", padding: 8, borderRadius: 4, fontSize: 11, opacity: 0.85, marginTop: 4 }}>
                            {s.verbatim_context.slice(0, 2).map((txt, i) => (
                              <div key={i} style={{ marginBottom: 10 }}>{String(txt).slice(0, 1000)}{String(txt).length > 1000 ? "..." : ""}</div>
                            ))}
                          </div>
                        </details>
                      ) : null}
                      {Array.isArray(s.screenshot_paths) && s.screenshot_paths.length ? (
                        <details>
                          <summary className="queue-item-label" style={{ cursor: "pointer", userSelect: "none" }}>
                            ▸ Screenshots ({s.screenshot_paths.length})
                          </summary>
                          <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 8 }}>
                            {s.screenshot_paths.slice(0, 6).map((sp, i) => {
                              // Use sourcesData.run_id (the actual run) instead of component runId
                              const sourceRunId = sourcesData.run_id || runId;
                              if (!sourceRunId) {
                                return <span key={i} style={{ fontSize: 11, opacity: 0.6 }}>⚠️ No run ID</span>;
                              }
                              const params = new URLSearchParams();
                              params.set("run_id", String(sourceRunId));
                              params.set("path", String(sp).replace(/\\/g, "/"));
                              const href = `${BACKEND_URL}/run_asset_download?${params.toString()}`;
                              return (
                                <a key={i} href={href} target="_blank" rel="noopener noreferrer" title={String(sp)}>
                                  <img
                                    src={href}
                                    alt={`Screenshot ${i + 1}`}
                                    style={{
                                      maxWidth: 200,
                                      maxHeight: 150,
                                      borderRadius: 6,
                                      border: "1px solid #333",
                                      cursor: "pointer",
                                      background: "#111",
                                    }}
                                    onError={(e) => {
                                      e.target.style.display = "none";
                                      e.target.nextSibling && (e.target.nextSibling.style.display = "inline");
                                    }}
                                  />
                                  <span style={{ display: "none", fontSize: 11, color: "#f87171" }}>
                                    ❌ Failed to load
                                  </span>
                                </a>
                              );
                            })}
                          </div>
                        </details>
                      ) : null}
                    </div>
                  </div>
                );

                return domainList.map((domain) => {
                  const domainSources = grouped[domain];
                  const maxScore = Math.max(...domainSources.map((x) => parseFloat(x.relevancy_score) || 0));
                  return (
                    <details key={domain} open={domainSources.length <= 2} style={{ marginBottom: 12 }}>
                      <summary
                        style={{
                          cursor: "pointer",
                          userSelect: "none",
                          padding: "8px 12px",
                          background: "#2a2a2a",
                          borderRadius: 6,
                          display: "flex",
                          alignItems: "center",
                          gap: 10,
                          fontWeight: 600,
                        }}
                      >
                        <span style={{ flex: 1 }}>🌐 {domain}</span>
                        <span style={{ opacity: 0.7, fontSize: 12 }}>{domainSources.length} page{domainSources.length > 1 ? "s" : ""}</span>
                        {maxScore > 0 ? (
                          <span
                            style={{
                              background: maxScore >= 0.7 ? "#22c55e" : maxScore >= 0.4 ? "#eab308" : "#6b7280",
                              color: "#000",
                              padding: "2px 8px",
                              borderRadius: 12,
                              fontSize: 11,
                              fontWeight: 700,
                            }}
                          >
                            best: {maxScore.toFixed(2)}
                          </span>
                        ) : null}
                      </summary>
                      <div style={{ paddingLeft: 12, paddingTop: 8 }}>
                        {domainSources.map((s, idx) => renderSource(s, idx))}
                      </div>
                    </details>
                  );
                });
              })()}
            </div>
          )}
        </div>
      );
    }

    if (base === "queue") {
      const canEdit = (item) => item && item.status === "queued";
      return (
        <div className="panel panel-scroll">
          <div className="plan-header" style={{ marginBottom: 8 }}>
            <div className="plan-title">Queue</div>
            <div className="plan-subtitle">View, edit, or delete queued tasks (not started yet).</div>
          </div>
          <div className="plan-panel">
            <div className="raw-settings-row" style={{ marginBottom: 8 }}>
              <button type="button" className="icon-button" onClick={fetchQueueItems} title="Refresh queue">
                ↻
              </button>
            </div>
            {(queueItems || []).length === 0 ? (
              <div className="plan-empty">Queue is empty.</div>
            ) : (
              <div className="queue-list">
                {(queueItems || []).map((it) => (
                  <div key={it.uid} className="queue-item">
                    <div className="queue-item-header">
                      <div className="queue-item-meta">
                        <code>{it.uid}</code> • <span>{it.status}</span>
                        {it.project_name ? ` • project: ${it.project_name}` : ""}
                        {it.mode ? ` • mode: ${it.mode}` : ""}
                      </div>
                      <div className="queue-item-actions">
                        <button
                          type="button"
                          className="icon-button"
                          disabled={!canEdit(it)}
                          title="Save edits"
                          onClick={async () => {
                            try {
                              const pn = (it.project_name || "").trim();
                              const llmId = it.llm_id || llmIdFromProvider(it);
                              const llmPatch = providerFromLlmId(llmId);
                              await axios.put(`${BACKEND_URL}/queue_item/${it.uid}`, {
                                query: it.query,
                                project_name: pn ? pn : null,
                                ...llmPatch,
                              });
                              fetchQueueItems();
                            } catch {}
                          }}
                        >
                          ✓
                        </button>
                        <button
                          type="button"
                          className="icon-button"
                          disabled={!canEdit(it)}
                          title="Delete queued item"
                          onClick={async () => {
                            const ok = window.confirm("Delete this queued item?");
                            if (!ok) return;
                            try {
                              await axios.delete(`${BACKEND_URL}/queue_item/${it.uid}`);
                              fetchQueueItems();
                            } catch {}
                          }}
                        >
                          ✕
                        </button>
                      </div>
                    </div>
                    <div className="queue-item-fields">
                      <label className="queue-item-label">Project</label>
                      <input
                        className="queue-item-input"
                        type="text"
                        value={it.project_name || ""}
                        disabled={!canEdit(it)}
                        placeholder="(blank = use default project)"
                        onFocus={() => setQueueFieldEditing(it.uid, "project_name", true)}
                        onBlur={() => setQueueFieldEditing(it.uid, "project_name", false)}
                        onChange={(e) => {
                          const v = e.target.value;
                          setQueueItems((prev) =>
                            (prev || []).map((x) => (x.uid === it.uid ? { ...x, project_name: v } : x))
                          );
                        }}
                      />
                    </div>
                    <div className="queue-item-fields">
                      <label className="queue-item-label">LLM</label>
                      <select
                        className="queue-item-input"
                        value={it.llm_id || llmIdFromProvider(it)}
                        disabled={!canEdit(it)}
                        onChange={async (e) => {
                          const id = e.target.value;
                          const patch = providerFromLlmId(id);
                          setQueueItems((prev) =>
                            (prev || []).map((x) => (x.uid === it.uid ? { ...x, llm_id: id, ...patch } : x))
                          );
                          // Auto-save selection so you don't lose it.
                          try {
                            await axios.put(`${BACKEND_URL}/queue_item/${it.uid}`, patch);
                          } catch {}
                        }}
                      >
                        <option value="deepseek_r1_32b">deepseek-r1:32b</option>
                        <option value="gpt_oss_20b">gpt-oss:20b</option>
                      </select>
                    </div>
                    <textarea
                      className="queue-item-text"
                      value={it.query}
                      disabled={!canEdit(it)}
                      onFocus={() => setQueueFieldEditing(it.uid, "query", true)}
                      onBlur={() => setQueueFieldEditing(it.uid, "query", false)}
                      onChange={(e) => {
                        const v = e.target.value;
                        setQueueItems((prev) =>
                          (prev || []).map((x) => (x.uid === it.uid ? { ...x, query: v } : x))
                        );
                      }}
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      );
    }

    if (base === "traces") {
      return (
        <div className="panel panel-scroll">
          <div className="plan-header" style={{ marginBottom: 8 }}>
            <div className="plan-title">Traces</div>
            <div className="plan-subtitle">Browse, copy, and download trace files from any run.</div>
          </div>

          <div className="raw-settings-panel">
            <div className="raw-settings-row">
              <label className="raw-settings-label">Project</label>
              <input
                className="raw-settings-input"
                type="text"
                value={traceProject}
                onChange={(e) => setTraceProject(e.target.value)}
                placeholder={projectName ? projectName : "e.g. moissanite-atlanta"}
              />
              <button type="button" className="icon-button" onClick={fetchRuns} title="Refresh runs">
                ↻
              </button>
            </div>

            <div className="raw-settings-row">
              <label className="raw-settings-label">View run</label>
              <select
                className="raw-settings-input"
                value={runFilesRunId}
                onChange={(e) => {
                  const rid = e.target.value;
                  setRunFilesRunId(rid);
                  setRunFileName("");
                  setRunFileView({ content: "", truncated: false });
                  if (rid) fetchRunFiles(rid);
                }}
              >
                <option value="">Select a run to view files…</option>
                {(traceRuns || []).slice().reverse().map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {r.run_id}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="icon-button"
                onClick={() => fetchRunFiles(runFilesRunId)}
                title="Refresh run files"
                disabled={!runFilesRunId}
              >
                ↻
              </button>
            </div>

            <div className="raw-settings-row">
              <label className="raw-settings-label">File</label>
              <select
                className="raw-settings-input"
                value={runFileName}
                onChange={(e) => {
                  const fn = e.target.value;
                  setRunFileName(fn);
                  if (fn) openRunFile(runFilesRunId, fn);
                }}
                disabled={!runFilesRunId}
              >
                <option value="">Select a file…</option>
                {(runFiles || []).map((f) => (
                  <option key={f.name} value={f.name}>
                    {f.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="icon-button"
                disabled={!runFilesRunId || !runFileName}
                title="Copy file content"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(runFileView.content || "");
                  } catch {}
                }}
              >
                ⧉
              </button>
              <button
                type="button"
                className="icon-button"
                disabled={!runFilesRunId || !runFileName}
                title="Download file"
                onClick={() => {
                  const params = new URLSearchParams();
                  params.set("run_id", runFilesRunId);
                  params.set("file", runFileName);
                  params.set("run_parent_dir", rawRunParentDir || "runs");
                  window.open(`${BACKEND_URL}/run_file_download?${params.toString()}`, "_blank", "noopener,noreferrer");
                }}
              >
                ↓
              </button>
            </div>
          </div>

          <div className="plan-panel">
            {runFilesRunId && runFileName ? (
              <div style={{ marginBottom: 12 }}>
                <div className="plan-subtitle" style={{ marginBottom: 6 }}>
                  Viewing: <code>{runFilesRunId}</code> / <code>{runFileName}</code>
                  {runFileView.truncated ? " • (truncated)" : ""}
                </div>
                <pre className="run-file-view">{runFileView.content || ""}</pre>
              </div>
            ) : null}
            {!runFilesRunId ? <div className="plan-empty">Select a run to view its files.</div> : null}
          </div>
        </div>
      );
    }

    if (base === "settings") {
      return (
        <div className="panel panel-scroll">
          <div className="plan-header" style={{ marginBottom: 8 }}>
            <div className="plan-title">Settings</div>
            <div className="plan-subtitle">Configure mode, outputs, agents, tools.</div>
          </div>
          <div className="raw-settings-panel">
            <div className="raw-settings-row">
              <label className="raw-settings-label">Mode</label>
              <div className="mode-toolbar" style={{ margin: 0, padding: 0 }}>
                <button type="button" className="active" disabled>
                  Standard
                </button>
              </div>
              <span className="raw-settings-hint">Standard mode uses planning + multiple agents.</span>
            </div>
            <div className="raw-settings-row">
              <label className="raw-settings-label">LLM model</label>
              <select
                className="raw-settings-input"
                value={llmOptionId}
                onChange={(e) => setLlmOptionId(e.target.value)}
              >
                {(effectiveLlmOptions || []).map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.label || o.id}
                  </option>
                ))}
              </select>
              <button type="button" className="icon-button" onClick={fetchLlmOptions} title="Refresh LLM options">
                ↻
              </button>
              <span className="raw-settings-hint">The AI model used for all agents.</span>
            </div>

            <div className="raw-settings-row" style={{ alignItems: "flex-start" }}>
              <label className="raw-settings-label">Sleep</label>
              <div style={{ display: "flex", flexDirection: "column", gap: 8, width: "100%" }}>
                <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <input
                    type="checkbox"
                    checked={!!powerSettings?.sleep_when_queue_done}
                    onChange={async (e) => {
                      try {
                        const res = await axios.post(`${BACKEND_URL}/power_settings`, {
                          sleep_when_queue_done: !!e.target.checked,
                        });
                        setPowerSettings(res.data || null);
                      } catch {}
                    }}
                  />
                  Sleep when queue completes (off by default)
                </label>

                <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <input
                    type="checkbox"
                    checked={!!powerSettings?.sleep_after_idle_enabled}
                    onChange={async (e) => {
                      try {
                        const res = await axios.post(`${BACKEND_URL}/power_settings`, {
                          sleep_after_idle_enabled: !!e.target.checked,
                        });
                        setPowerSettings(res.data || null);
                      } catch {}
                    }}
                  />
                  Sleep after idle timer
                </label>

                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <span className="plan-subtitle" style={{ margin: 0, minWidth: 88 }}>
                    Idle hours
                  </span>
                  <input
                    className="raw-settings-input"
                    type="number"
                    min="0"
                    max="8760"
                    value={
                      powerSettings?.sleep_after_idle_seconds != null
                        ? Math.round((powerSettings.sleep_after_idle_seconds || 0) / 3600)
                        : 3
                    }
                    onChange={async (e) => {
                      const hrs = parseInt(e.target.value || "0", 10);
                      const sec = Number.isFinite(hrs) ? Math.max(0, hrs) * 3600 : 0;
                      try {
                        const res = await axios.post(`${BACKEND_URL}/power_settings`, {
                          sleep_after_idle_seconds: sec,
                        });
                        setPowerSettings(res.data || null);
                      } catch {}
                    }}
                  />
                  <span className="plan-subtitle" style={{ margin: 0 }}>
                    (0 = never)
                  </span>
                </div>

                <div className="plan-subtitle" style={{ margin: 0 }}>
                  Host sleep allowed: <b>{powerSettings?.host_sleep_allowed ? "YES" : "NO"}</b>
                  {!powerSettings?.host_sleep_allowed ? (
                    <div>
                      To enable: set env <code>AGENTICSEEK_ALLOW_HOST_SLEEP=1</code> and run backend on host (not Docker).
                    </div>
                  ) : null}
                </div>
              </div>
            </div>

            {/* Post-run summary settings removed - was confusing */}
            {outputDir ? (
              <div className="raw-settings-row" style={{ alignItems: "flex-start" }}>
                <label className="raw-settings-label">Run</label>
                <div className="run-path-hint" style={{ margin: 0 }}>
                  Folder: <code>{outputDir}</code>
                  {runId ? (
                    <span>
                      {" "}
                      • ID: <code>{runId}</code>
                    </span>
                  ) : null}
                </div>
              </div>
            ) : null}
            <div className="raw-settings-row">
              <label className="raw-settings-label">Project</label>
              <input
                className="raw-settings-input"
                type="text"
                value={projectName}
                onChange={(e) => setProjectName(e.target.value)}
                placeholder="e.g. moissanite-atlanta"
              />
              <span className="raw-settings-hint">
                Prefixes run IDs (e.g. <code>my-project_&lt;uuid&gt;</code>)
              </span>
            </div>
          </div>
          <div className="raw-settings-panel">
            <div className="raw-settings-row">
              <label className="raw-settings-label">Raw verbosity</label>
              <input
                type="range"
                min="0"
                max="2"
                value={rawVerbosity}
                onChange={(e) => setRawVerbosity(parseInt(e.target.value, 10))}
              />
              <span className="raw-settings-value">
                {rawVerbosity === 0 ? "Minimal" : rawVerbosity === 1 ? "Standard" : "Max"}
              </span>
              <span className="raw-settings-hint">How much detail to save. Max captures screenshots & page text.</span>
            </div>
            <div className="raw-settings-row">
              <label className="raw-settings-label">OCR</label>
              <label style={{ fontSize: "0.8rem" }}>
                <input
                  type="checkbox"
                  checked={rawOcrEnabled}
                  onChange={(e) => setRawOcrEnabled(e.target.checked)}
                />{" "}
                OCR screenshots in Max Raw (embed OCR text into trace)
              </label>
              <span className="raw-settings-hint">Only applies when Raw verbosity is set to Max.</span>
            </div>
              <div className="raw-settings-row">
                <label className="raw-settings-label">Sources</label>
                <label style={{ fontSize: "0.8rem" }}>
                  <input
                    type="checkbox"
                    checked={rawSourcesEnabled}
                    onChange={(e) => setRawSourcesEnabled(e.target.checked)}
                  />{" "}
                  Enable Sources tab generation (URLs/screenshots/OCR + small LLM enrichment)
                </label>
                <span className="raw-settings-hint">Default off. Turn on only when you want Sources captured.</span>
              </div>
              <div className="raw-settings-row">
                <label className="raw-settings-label">Sources LLM</label>
                <label style={{ fontSize: "0.8rem" }}>
                  <input
                    type="checkbox"
                    checked={rawSourcesLlmEnabled}
                    onChange={(e) => setRawSourcesLlmEnabled(e.target.checked)}
                    disabled={!rawSourcesEnabled}
                  />{" "}
                  Use LLM to score/enrich sources (relevancy, match, how-helps)
                </label>
                <span className="raw-settings-hint">Off by default. If sources ever “block”, leave this off.</span>
              </div>
            <div className="raw-settings-row">
              <label className="raw-settings-label">Run folder base</label>
              <input
                className="raw-settings-input"
                type="text"
                value={rawRunParentDir}
                onChange={(e) => setRawRunParentDir(e.target.value)}
                placeholder="runs"
              />
              <span className="raw-settings-hint">Where run outputs are saved (relative to workspace).</span>
            </div>
          </div>
          <div className="raw-settings-panel">
            <div className="raw-settings-row">
              <label className="raw-settings-label">Default output</label>
              <select
                className="raw-settings-input"
                value={defaultOutputFormat}
                onChange={(e) => setDefaultOutputFormat(e.target.value)}
              >
                <option value="none">None (only if requested)</option>
                <option value="md">Markdown report (.md)</option>
                <option value="csv">CSV data file (.csv)</option>
              </select>
              <span className="raw-settings-hint">Auto-suggest output format when you say "report" or similar.</span>
            </div>
            <div className="raw-settings-row">
              <label className="raw-settings-label">Spreadsheet</label>
              <select
                className="raw-settings-input"
                value={spreadsheetFormat}
                onChange={(e) => setSpreadsheetFormat(e.target.value)}
              >
                <option value="csv">CSV (recommended)</option>
                <option value="xlsx">XLSX</option>
              </select>
              <span className="raw-settings-hint">Format for data exports when CSV is chosen.</span>
            </div>
            <div className="raw-settings-row">
              <label className="raw-settings-label">Agents</label>
              <label style={{ fontSize: "0.8rem" }}>
                <input
                  type="checkbox"
                  checked={allowCoderAgent}
                  onChange={(e) => setAllowCoderAgent(e.target.checked)}
                />{" "}
                Allow Coder agent
              </label>
              <span className="raw-settings-hint">Enable code writing (Python, Go, etc). Off = research only.</span>
            </div>
            <div className="raw-settings-row">
              <label className="raw-settings-label">Enabled tools</label>
              <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
                {["web_search", "file_finder", "write_output", "bash", "python", "go", "java", "c"].map((t) => (
                  <label key={t} style={{ fontSize: "0.8rem" }}>
                    <input
                      type="checkbox"
                      checked={enabledTools.includes(t)}
                      onChange={(e) => {
                        setEnabledTools((prev) => {
                          const set = new Set(prev || []);
                          if (e.target.checked) set.add(t);
                          else set.delete(t);
                          if (!allowCoderAgent) ["python", "go", "java", "c"].forEach((x) => set.delete(x));
                          return Array.from(set);
                        });
                      }}
                    />{" "}
                    {t}
                  </label>
                ))}
              </div>
            </div>
          </div>
        </div>
      );
    }

    // computer (default)
    return (
      <div className="panel panel-scroll">
        <div className="view-selector">
          <button
            className={currentView === "blocks" ? "active" : ""}
            onClick={() => setCurrentView("blocks")}
            title="See tool outputs and file edits"
          >
            Editor View
          </button>
          <button
            className={currentView === "screenshot" ? "active" : ""}
            onClick={handleGetScreenshot}
            title="Live browser screenshot"
          >
            Browser View
          </button>
        </div>
        <span className="raw-settings-hint" style={{ display: "block", marginBottom: 8, textAlign: "center" }}>
          Editor shows tool outputs. Browser shows live webpage.
        </span>
        <div className="content">
          {error && <p className="error">{error}</p>}
          {currentView === "blocks" ? (
            <div className="blocks">
              {responseData && responseData.blocks && Object.values(responseData.blocks).length > 0 ? (
                Object.values(responseData.blocks).map((block, index) => (
                  <div key={index} className="block">
                    <p className="block-tool">Tool: {block.tool_type}</p>
                    <pre>{block.block}</pre>
                    <p className="block-feedback">Feedback: {block.feedback}</p>
                    {block.success ? (
                      <p className="block-success">Success</p>
                    ) : (
                      <p className="block-failure">Failure</p>
                    )}
                  </div>
                ))
              ) : (
                <div className="block">
                  <p className="block-tool">Tool: No tool in use</p>
                  <pre>No file opened</pre>
                </div>
              )}
            </div>
          ) : (
            <div className="screenshot">
              <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
                <button type="button" className="icon-button" onClick={fetchScreenshot} title="Refresh screenshot">
                  ↻
                </button>
              </div>
              <img
                src={responseData?.screenshot || "placeholder.png"}
                alt="Screenshot"
                onError={(e) => {
                  e.target.src = "placeholder.png";
                  console.error("Failed to load screenshot");
                }}
                key={responseData?.screenshotTimestamp || "default"}
              />
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <div className="app">
      <header className="header">
        <div className="header-brand">
          <div className="logo-container">
            <img src={faviconPng} alt="AgenticSeek" className="logo-icon" />
          </div>
          <div className="brand-text">
            <h1>AgenticSeek</h1>
          </div>
        </div>
        <div className="header-status">
          <div
            className={`status-indicator ${isOnline ? "online" : "offline"}`}
          >
            <div className="status-dot"></div>
            <span className="status-text">
              {isOnline ? "Online" : "Offline"}
            </span>
          </div>
        </div>
        <div className="header-actions">
          <button
            type="button"
            className="action-button"
            onClick={handleNewRun}
            title="Start a new run (clears UI and server state)"
            aria-label="Start new run"
          >
            <span className="action-text">New run</span>
          </button>
          <button
            type="button"
            className="action-button"
            onClick={handleRestartServer}
            title="Restart backend server"
            aria-label="Restart backend"
          >
            <span className="action-text">Restart</span>
          </button>
          <a
            href="https://github.com/Fosowl/agenticSeek"
            target="_blank"
            rel="noopener noreferrer"
            className="action-button github-link"
            aria-label="View on GitHub"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
              <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z" />
            </svg>
            <span className="action-text">GitHub</span>
          </a>
          <div>
            <ThemeToggle />
          </div>
        </div>
      </header>
      <main className="main">
        {showMobileScrollControls ? (
          <div className="mobile-scroll-controls" aria-label="Mobile scroll controls">
            <button
              type="button"
              className="mobile-scroll-btn"
              onClick={() => mobileScrollBy("up")}
              title="Scroll up"
            >
              ↑
            </button>
            <button
              type="button"
              className="mobile-scroll-btn"
              onClick={() => mobileScrollBy("down")}
              title="Scroll down"
            >
              ↓
            </button>
          </div>
        ) : null}
        <div
          className={`giant-status ${
            isPaused ? "paused" : isGenerating ? "working" : "idle"
          }`}
        >
          <div className="giant-status-title">
            {isPaused ? "PAUSED" : isGenerating ? "WORKING" : "IDLE"}
          </div>
          <div className="giant-status-subtitle">
            {status || ""}
            {typeof queueLength === "number" && queueLength > 0
              ? ` • Queue: ${queueLength}`
              : ""}
            {agentName ? ` • Agent: ${agentName}` : ""}
          </div>
        </div>
        <MultiResizableLayout
          widths={dock.widths}
          onWidthsChange={(w) => setDock((p) => ({ ...p, widths: w }))}
        >
          {(dock.panes || []).map((pane, idx) => (
            <div key={pane.id || idx} className="dock-pane">
              <DockTabs
                sideLabel={`Pane ${idx + 1}`}
                tabs={(pane.tabs || []).map(getPanelMeta)}
                activeId={pane.active}
                onSelect={(id) =>
                  setDock((p) => {
                    const next = JSON.parse(JSON.stringify(p));
                    next.panes[idx].active = id;
                    return next;
                  })
                }
                onClose={(id) => closeTab(idx, id)}
                onMove={(id) => moveTab(idx, id)}
                hiddenTabs={hiddenPanels()}
                onOpenHidden={(id) => openHiddenOnSide(idx, id)}
                onAddPane={() => addPaneAfter(idx)}
                onRemovePane={() => removePane(idx)}
                canRemovePane={(dock.panes || []).length > 1}
              />
              <div className="dock-content">{renderPanel(pane.active)}</div>
            </div>
          ))}
        </MultiResizableLayout>
      </main>
    </div>
  );
}

export default App;
