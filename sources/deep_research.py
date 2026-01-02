from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

from sources.runtime_context import trace_event
from sources.tools.searxSearch import searxSearch


@dataclass
class Finding:
    website: str
    product: str
    price: str
    url: str
    evidence: str = ""
    location_hint: str = ""


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _uniq_by_domain(urls: List[str]) -> List[str]:
    seen = set()
    out = []
    for u in urls:
        d = _domain(u)
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(u)
    return out


def _extract_jsonld_products(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, Any]] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            raw = tag.string or tag.text
            if not raw:
                continue
            data = json.loads(raw)
        except Exception:
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            # Some sites wrap in @graph
            if "@graph" in item and isinstance(item["@graph"], list):
                items.extend([x for x in item["@graph"] if isinstance(x, dict)])
                continue
            t = str(item.get("@type", "")).lower()
            if "product" in t:
                results.append(item)
    return results


def _extract_price_from_jsonld(product_obj: Dict[str, Any]) -> Optional[str]:
    offers = product_obj.get("offers")
    if offers is None:
        return None
    offers_list = offers if isinstance(offers, list) else [offers]
    for off in offers_list:
        if not isinstance(off, dict):
            continue
        price = off.get("price") or off.get("lowPrice")
        currency = off.get("priceCurrency")
        if price is None:
            continue
        if currency:
            return f"{currency} {price}"
        return str(price)
    return None


def _extract_title(soup: BeautifulSoup) -> str:
    for sel in [
        ("meta", {"property": "og:title"}),
        ("meta", {"name": "twitter:title"}),
    ]:
        el = soup.find(sel[0], attrs=sel[1])
        if el and el.get("content"):
            return el["content"].strip()
    if soup.title and soup.title.text:
        return soup.title.text.strip()
    return ""


def _extract_price_heuristic(text: str) -> Optional[str]:
    # Common price patterns: $1,234.56, USD 1234, etc.
    patterns = [
        r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?",
        r"\bUSD\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(0)
    return None


def _location_hint(text: str) -> str:
    # Very lightweight: just surface if page mentions Atlanta/GA.
    low = text.lower()
    hints = []
    if "atlanta" in low:
        hints.append("mentions Atlanta")
    if re.search(r"\bga\b", low) or "georgia" in low:
        hints.append("mentions GA/Georgia")
    return ", ".join(hints)


class DeepResearchOrchestrator:
    def __init__(self, work_dir: str, findings_relpath: str = "deep_research_findings.md"):
        self.work_dir = work_dir
        # Safety: keep findings under work_dir even if caller passes an absolute path.
        rel = findings_relpath or "deep_research_findings.md"
        rel = str(rel).replace("\\", os.sep).replace("/", os.sep)
        rel = rel.lstrip(os.sep)
        self.findings_path = os.path.abspath(os.path.join(work_dir, rel))
        work_dir_abs = os.path.abspath(work_dir)
        if os.path.commonpath([work_dir_abs, self.findings_path]) != work_dir_abs:
            self.findings_path = os.path.join(work_dir_abs, "deep_research_findings.md")
        os.makedirs(os.path.dirname(self.findings_path), exist_ok=True)
        self.search_tool = searxSearch()

    def _append_findings(self, text: str) -> None:
        with open(self.findings_path, "a", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")

    def discover_urls(self, user_prompt: str, limit: int = 30) -> List[str]:
        queries = [
            f"{user_prompt}",
            "Atlanta GA jeweler moissanite tennis necklace price",
            "moissanite tennis necklace Atlanta jewelry store",
            "site:.com moissanite tennis necklace Atlanta",
        ]
        all_urls: List[str] = []
        for q in queries:
            trace_event("deep_research_search", query=q)
            raw = self.search_tool.execute([q], False)
            urls = []
            for block in str(raw).split("\n\n"):
                for line in block.splitlines():
                    if line.startswith("Link:"):
                        urls.append(line.replace("Link:", "").strip())
            all_urls.extend(urls)

        # Basic filtering: keep likely product-ish URLs
        filtered = []
        for u in all_urls:
            lu = u.lower()
            if any(x in lu for x in ["moissanite", "tennis", "necklace"]):
                filtered.append(u)
        # Prefer unique domains so we get 5 different jewelers
        filtered = _uniq_by_domain(filtered)
        return filtered[:limit]

    def investigate_url(self, url: str, timeout: int = 12) -> Optional[Finding]:
        trace_event("deep_research_investigate_start", url=url)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        }
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code != 200 or not resp.text:
                trace_event("deep_research_investigate_failed", url=url, status=resp.status_code)
                return None
        except Exception as e:
            trace_event("deep_research_investigate_failed", url=url, error=str(e))
            return None

        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        title = _extract_title(soup)
        text = soup.get_text(" ", strip=True)[:20000]

        # Ensure relevance
        low = (title + " " + text).lower()
        if not ("moissanite" in low and "tennis" in low and "necklace" in low):
            trace_event("deep_research_irrelevant", url=url, title=title)
            return None

        price = None
        product_name = title

        # Prefer JSON-LD product schema
        for prod in _extract_jsonld_products(html):
            nm = prod.get("name")
            if nm and isinstance(nm, str):
                product_name = nm.strip()
            p = _extract_price_from_jsonld(prod)
            if p:
                price = p
                break

        if not price:
            price = _extract_price_heuristic(text)

        if not price:
            trace_event("deep_research_missing_price", url=url, title=title)
            return None

        website = _domain(url) or url
        hint = _location_hint(text)
        finding = Finding(
            website=website,
            product=product_name or "Unknown product",
            price=price,
            url=url,
            evidence=(title or "")[:300],
            location_hint=hint,
        )
        trace_event("deep_research_finding", **finding.__dict__)
        self._append_findings(
            f"- **Website**: {finding.website}\n"
            f"  - **Product**: {finding.product}\n"
            f"  - **Price**: {finding.price}\n"
            f"  - **URL**: {finding.url}\n"
            f"  - **Location hint**: {finding.location_hint or 'n/a'}\n"
        )
        return finding

    def run(self, user_prompt: str, want: int = 5) -> Tuple[str, str]:
        self._append_findings(f"\n## Run: {user_prompt}\n")
        urls = self.discover_urls(user_prompt, limit=40)
        if not urls:
            return "No candidate sites found from search.", ""

        findings: List[Finding] = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = [ex.submit(self.investigate_url, u) for u in urls]
            for fut in as_completed(futures):
                res = fut.result()
                if res:
                    findings.append(res)
                if len(findings) >= want:
                    break

        # De-dup by domain again, keep first 5
        unique: List[Finding] = []
        seen = set()
        for f in findings:
            d = f.website
            if d in seen:
                continue
            seen.add(d)
            unique.append(f)
        unique = unique[:want]

        if not unique:
            return "Found candidate pages, but could not extract price/product reliably. Check the findings file for partial traces.", ""

        # Deterministic “good report”: table + references
        lines = [
            "Here are 5 moissanite tennis necklace options (with price) found via web search:",
            "",
            "| # | Website | Product | Price | URL | Location hint |",
            "|---:|---|---|---|---|---|",
        ]
        for i, f in enumerate(unique, 1):
            lines.append(f"| {i} | {f.website} | {f.product} | {f.price} | {f.url} | {f.location_hint or 'n/a'} |")

        report = "\n".join(lines)
        self._append_findings("\n## Final selection\n\n" + report + "\n")
        return report, ""
