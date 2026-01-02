from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_DROP_QUERY_KEYS_PREFIX = ("utm_",)
_DROP_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref",
    # Google SERP click tracking
    "srsltid",
}


def normalize_url(url: str) -> str:
    """
    Normalize URL for de-duplication:
    - lower-case scheme + hostname
    - drop fragments
    - drop common tracking query params
    """
    try:
        u = (url or "").strip()
        if not u:
            return ""
        p = urlparse(u)
        scheme = (p.scheme or "").lower()
        netloc = (p.netloc or "").lower()
        path = p.path or ""
        # Drop fragments
        fragment = ""
        # Drop tracking params
        q = []
        for k, v in parse_qsl(p.query or "", keep_blank_values=True):
            lk = (k or "").lower()
            if any(lk.startswith(pref) for pref in _DROP_QUERY_KEYS_PREFIX):
                continue
            if lk in _DROP_QUERY_KEYS:
                continue
            q.append((k, v))
        query = urlencode(q, doseq=True)
        return urlunparse((scheme, netloc, path, p.params or "", query, fragment))
    except Exception:
        return (url or "").strip()


class SourcesStore:
    """
    In-memory per-run source registry with URL de-duplication.
    Can optionally persist to <output_dir>/sources.json when output_dir is provided.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # run_id -> normalized_url -> record
        self._by_run: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._updated_at: Dict[str, str] = {}

    def reset(self) -> None:
        with self._lock:
            self._by_run.clear()
            self._updated_at.clear()

    def _merge_list_unique(self, a: List[str], b: List[str], limit: int = 200) -> List[str]:
        out: List[str] = []
        seen = set()
        for x in (a or []) + (b or []):
            s = str(x or "").strip()
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
            if len(out) >= limit:
                break
        return out

    def add_sources(
        self,
        run_id: str,
        sources: List[Dict[str, Any]],
        *,
        step_id: Optional[str] = None,
        agent: Optional[str] = None,
        output_dir: Optional[str] = None,
    ) -> Tuple[int, int]:
        """
        Add/merge sources for a run. Returns (added_count, total_count).
        """
        rid = (run_id or "").strip()
        if not rid:
            return (0, 0)
        added = 0
        with self._lock:
            run_map = self._by_run.setdefault(rid, {})
            for src in sources or []:
                url = str((src or {}).get("url") or "").strip()
                if not url or not url.startswith("http"):
                    continue
                nurl = normalize_url(url)
                if not nurl:
                    continue
                rec = run_map.get(nurl)
                now = _utc_iso()
                # Parse/clamp relevancy_score if provided (0.0..1.0). Latest value wins.
                rel = (src or {}).get("relevancy_score")
                try:
                    rel = float(rel) if rel is not None else None
                except Exception:
                    rel = None
                if rel is not None:
                    try:
                        rel = max(0.0, min(1.0, float(rel)))
                    except Exception:
                        rel = None
                if rec is None:
                    rec = {
                        "url": url,
                        "normalized_url": nurl,
                        "kind": (src or {}).get("kind") or "web",
                        "title": (src or {}).get("title") or "",
                        "match": (src or {}).get("match") or "",
                        "how_helps": (src or {}).get("how_helps") or "",
                        "relevancy_score": rel,
                        # Keep alternates so later runs can add better descriptions without losing prior info.
                        "title_alternates": [],
                        "match_alternates": [],
                        "how_helps_alternates": [],
                        "kind_alternates": [],
                        # Run-relative screenshot paths (e.g. screenshots/foo.png)
                        "screenshot_paths": [],
                        "data_to_collect": [],
                        "evidence_quotes": [],
                        "verbatim_context": [],
                        "first_seen": now,
                        "last_seen": now,
                        "seen_in_steps": [],
                        "agents": [],
                    }
                    run_map[nurl] = rec
                    added += 1
                else:
                    rec["last_seen"] = now
                    # Keep the first observed display URL unless the new one is longer/more specific.
                    try:
                        if len(url) > len(str(rec.get("url") or "")):
                            rec["url"] = url
                    except Exception:
                        pass
                    # Upsert string fields: keep the "best" value (longest), but also retain alternates.
                    def _upsert_str(field: str, alt_field: str) -> None:
                        cur = str(rec.get(field) or "").strip()
                        nxt = str((src or {}).get(field) or "").strip()
                        if not nxt:
                            return
                        # Keep alternates when the text differs
                        if cur and nxt != cur:
                            alts = list(rec.get(alt_field) or [])
                            if cur not in alts:
                                alts.append(cur)
                            if nxt not in alts:
                                alts.append(nxt)
                            # Cap alternates to keep payload bounded
                            rec[alt_field] = alts[-10:]
                        # Choose the better primary string (longer tends to be more informative)
                        if (not cur) or (len(nxt) > len(cur)):
                            rec[field] = nxt

                    _upsert_str("title", "title_alternates")
                    _upsert_str("match", "match_alternates")
                    _upsert_str("how_helps", "how_helps_alternates")
                    if rel is not None:
                        rec["relevancy_score"] = rel

                    # Kind: prefer non-web kinds if discovered (pdf/doc/video), but keep alternates
                    cur_kind = str(rec.get("kind") or "").strip() or "web"
                    nxt_kind = str((src or {}).get("kind") or "").strip() or ""
                    if nxt_kind and nxt_kind != cur_kind:
                        alts = list(rec.get("kind_alternates") or [])
                        if cur_kind not in alts:
                            alts.append(cur_kind)
                        if nxt_kind not in alts:
                            alts.append(nxt_kind)
                        rec["kind_alternates"] = alts[-10:]
                        # Prefer non-web kinds if available
                        if cur_kind == "web" and nxt_kind != "web":
                            rec["kind"] = nxt_kind
                        elif len(nxt_kind) > len(cur_kind):
                            rec["kind"] = nxt_kind

                rec["data_to_collect"] = self._merge_list_unique(
                    list(rec.get("data_to_collect") or []),
                    list((src or {}).get("data_to_collect") or []),
                    limit=120,
                )
                rec["evidence_quotes"] = self._merge_list_unique(
                    list(rec.get("evidence_quotes") or []),
                    list((src or {}).get("evidence_quotes") or []),
                    limit=120,
                )
                rec["verbatim_context"] = self._merge_list_unique(
                    list(rec.get("verbatim_context") or []),
                    list((src or {}).get("verbatim_context") or []),
                    limit=200,
                )
                rec["screenshot_paths"] = self._merge_list_unique(
                    list(rec.get("screenshot_paths") or []),
                    list((src or {}).get("screenshot_paths") or []),
                    limit=50,
                )

                if step_id:
                    s = str(step_id)
                    if s and s not in rec["seen_in_steps"]:
                        rec["seen_in_steps"].append(s)
                if agent:
                    a = str(agent)
                    if a and a not in rec["agents"]:
                        rec["agents"].append(a)

            self._updated_at[rid] = _utc_iso()

            # Persist best-effort (build payload here to avoid deadlock - don't call get_sources while holding lock)
            if output_dir:
                try:
                    os.makedirs(output_dir, exist_ok=True)
                    path = os.path.join(output_dir, "sources.json")
                    # Build payload directly from run_map (we already hold the lock)
                    items = list(run_map.values())
                    try:
                        items.sort(key=lambda x: str(x.get("last_seen") or ""), reverse=True)
                    except Exception:
                        pass
                    payload = {"run_id": rid, "updated_at": self._updated_at.get(rid), "sources": items}
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False, indent=2)
                    # Also persist a Markdown snapshot for easy reading/copying.
                    try:
                        md_path = os.path.join(output_dir, "sources.md")
                        with open(md_path, "w", encoding="utf-8") as f:
                            f.write(render_sources_markdown(payload))
                    except Exception:
                        pass
                except Exception:
                    pass

            return (added, len(run_map))

    def get_sources(self, run_id: str) -> Dict[str, Any]:
        rid = (run_id or "").strip()
        if not rid:
            return {"run_id": rid, "updated_at": None, "sources": []}
        with self._lock:
            run_map = self._by_run.get(rid) or {}
            updated_at = self._updated_at.get(rid)
            # Stable sort: most recently seen first
            items = list(run_map.values())
            try:
                items.sort(key=lambda x: str(x.get("last_seen") or ""), reverse=True)
            except Exception:
                pass
            return {"run_id": rid, "updated_at": updated_at, "sources": items}


_STORE = SourcesStore()


def reset_sources() -> None:
    try:
        _STORE.reset()
    except Exception:
        return


def add_sources(
    run_id: str,
    sources: List[Dict[str, Any]],
    *,
    step_id: Optional[str] = None,
    agent: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> Tuple[int, int]:
    return _STORE.add_sources(run_id, sources, step_id=step_id, agent=agent, output_dir=output_dir)


def get_sources(run_id: str) -> Dict[str, Any]:
    return _STORE.get_sources(run_id)


def import_sources(run_id: str, sources_data: Dict[str, Any], *, output_dir: Optional[str] = None) -> Tuple[int, int]:
    """
    Import sources from a previously exported JSON payload.
    Merges into existing sources for the run_id (de-duplicates by URL).
    Returns (added_count, total_count).
    """
    sources_list = sources_data.get("sources") or []
    if not isinstance(sources_list, list):
        sources_list = []
    return _STORE.add_sources(run_id, sources_list, step_id="import", agent="Import", output_dir=output_dir)


def render_sources_markdown(payload: Dict[str, Any]) -> str:
    """
    Render the sources payload (as returned by get_sources()) into a readable Markdown document.
    """
    rid = str((payload or {}).get("run_id") or "").strip()
    updated_at = str((payload or {}).get("updated_at") or "").strip()
    sources = (payload or {}).get("sources") or []
    if not isinstance(sources, list):
        sources = []

    lines: list[str] = []
    lines.append(f"# Sources{(' — ' + rid) if rid else ''}")
    if updated_at:
        lines.append("")
        lines.append(f"_Updated: {updated_at}_")
    lines.append("")
    lines.append(f"Total sources: {len(sources)}")
    lines.append("")

    for idx, s in enumerate(sources, start=1):
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()
        kind = str(s.get("kind") or "").strip()
        url = str(s.get("url") or "").strip()
        rel = s.get("relevancy_score")
        try:
            rel = float(rel) if rel is not None else None
        except Exception:
            rel = None
        match = str(s.get("match") or "").strip()
        how = str(s.get("how_helps") or "").strip()
        match_alts = s.get("match_alternates") or []
        how_alts = s.get("how_helps_alternates") or []
        steps = s.get("seen_in_steps") or []
        agents = s.get("agents") or []
        data = s.get("data_to_collect") or []
        quotes = s.get("evidence_quotes") or []
        ctxs = s.get("verbatim_context") or []
        shots = s.get("screenshot_paths") or []

        lines.append(f"## {idx}. {title or (kind or 'Source')}")
        lines.append("")
        if url:
            lines.append(f"- **URL**: {url}")
        if kind:
            lines.append(f"- **Kind**: {kind}")
        if rel is not None:
            try:
                lines.append(f"- **Relevancy score**: {max(0.0, min(1.0, float(rel))):.2f}")
            except Exception:
                pass
        if isinstance(steps, list) and steps:
            lines.append(f"- **Seen in steps**: {', '.join([str(x) for x in steps if str(x).strip()])}")
        if isinstance(agents, list) and agents:
            lines.append(f"- **Agents**: {', '.join([str(x) for x in agents if str(x).strip()])}")
        lines.append("")

        if match:
            lines.append("**Why it matches**")
            lines.append("")
            lines.append(match)
            lines.append("")
            if isinstance(match_alts, list) and match_alts:
                lines.append("_Other match notes_")
                lines.append("")
                for m in match_alts[-5:]:
                    ms = str(m or "").strip()
                    if ms and ms != match:
                        lines.append(f"- {ms}")
                lines.append("")

        if how:
            lines.append("**How it helps**")
            lines.append("")
            lines.append(how)
            lines.append("")
            if isinstance(how_alts, list) and how_alts:
                lines.append("_Other how-it-helps notes_")
                lines.append("")
                for h in how_alts[-5:]:
                    hs = str(h or "").strip()
                    if hs and hs != how:
                        lines.append(f"- {hs}")
                lines.append("")

        if isinstance(data, list) and data:
            lines.append("**Data to collect**")
            lines.append("")
            for d in data:
                ds = str(d or "").strip()
                if ds:
                    lines.append(f"- {ds}")
            lines.append("")

        if isinstance(quotes, list) and quotes:
            lines.append("**Evidence (verbatim quotes)**")
            lines.append("")
            for q in quotes:
                qs = str(q or "").strip()
                if qs:
                    # Markdown blockquote, preserve any internal newlines
                    for ln in qs.splitlines():
                        lines.append(f"> {ln}")
                    lines.append(">")
            lines.append("")

        if isinstance(ctxs, list) and ctxs:
            lines.append("**Verbatim context**")
            lines.append("")
            for c in ctxs[:8]:
                cs = str(c or "").strip()
                if not cs:
                    continue
                lines.append("```")
                lines.append(cs)
                lines.append("```")
                lines.append("")

        if isinstance(shots, list) and shots:
            lines.append("**Screenshots**")
            lines.append("")
            for sp in shots[:8]:
                sps = str(sp or "").strip()
                if sps:
                    lines.append(f"- {sps}")
            lines.append("")

    return "\n".join(lines).strip() + "\n"
