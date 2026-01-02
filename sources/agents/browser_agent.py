import re
import time
import json
from datetime import date
from typing import List, Tuple, Type, Dict
from enum import Enum
import asyncio
import os
import uuid
import shutil
import threading

from sources.utility import pretty_print, animate_thinking
from sources.agents.agent import Agent
from sources.tools.searxSearch import searxSearch
from sources.tools.appendFile import AppendFile
from sources.browser import Browser
from sources.logger import Logger
from sources.memory import Memory
from sources.runtime_context import trace_event, get_run_context
from sources.ocr import ocr_image
from sources.sources_store import add_sources as _add_sources, normalize_url as _normalize_url

from sources.activity_bus import emit_activity

class Action(Enum):
    REQUEST_EXIT = "REQUEST_EXIT"
    FORM_FILLED = "FORM_FILLED"
    GO_BACK = "GO_BACK"
    NAVIGATE = "NAVIGATE"
    SEARCH = "SEARCH"
    
class BrowserAgent(Agent):
    def __init__(self, name, prompt_path, provider, verbose=False, browser=None):
        """
        The Browser agent is an agent that navigate the web autonomously in search of answer
        """
        super().__init__(name, prompt_path, provider, verbose, browser)
        self.tools = {
            "web_search": searxSearch(),
            "append_file": AppendFile(),
        }
        self.role = "web"
        self.type = "browser_agent"
        self.browser = browser
        self.current_page = ""
        self.search_history = []
        self.navigable_links = []
        self.last_action = Action.NAVIGATE.value
        self.notes = []
        # Goal/prompt for this run (used for per-page source enrichment).
        self._run_goal: str = ""
        # Avoid spamming mode logs.
        self._sources_mode_logged: bool = False
        self.date = self.get_today_date()
        self.logger = Logger("browser_agent.log")
        self._step = 0
        self.memory = Memory(self.load_prompt(prompt_path),
                        recover_last_session=False, # session recovery in handled by the interaction class
                        memory_compression=False,
                        model_provider=provider.get_model_name() if provider else None)

    def _safe_trace_write(self, rel_path: str, content: str) -> str | None:
        """
        Best-effort write under WORK_DIR, returns absolute path on success.
        """
        ctx = get_run_context()
        if ctx is None or not ctx.is_trace_enabled():
            return None
        try:
            work_dir_abs = os.path.abspath(ctx.work_dir or os.getcwd())
            rel = str(rel_path).replace("\\", os.sep).replace("/", os.sep).lstrip(os.sep)
            target = os.path.abspath(os.path.join(work_dir_abs, rel))
            if os.path.commonpath([work_dir_abs, target]) != work_dir_abs:
                return None
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content or "")
            return target
        except Exception:
            return None

    def _emit_activity(self, text: str, color: str = "output") -> None:
        """
        Emit an Activity feed line for the UI. Best-effort (never raise).
        """
        try:
            ctx = get_run_context()
        except Exception:
            ctx = None
        rid = getattr(ctx, "run_id", None) if ctx is not None else None
        try:
            emit_activity("print", run_id=rid, text=str(text), color=str(color))
        except Exception:
            return

    def _snapshot_page(self, page_text: str | None = None) -> None:
        """
        In trace/raw mode, capture URL + title + screenshot + page text file.
        Also upserts to Sources store if save_sources is enabled.
        """
        ctx = get_run_context()
        if ctx is None or self.browser is None:
            print(f"[_snapshot_page] Early return: ctx={ctx is not None}, browser={self.browser is not None}")
            return

        save_sources = getattr(ctx.trace_config, "save_sources", False)
        trace_enabled = ctx.is_trace_enabled()
        print(f"[_snapshot_page] trace_enabled={trace_enabled}, save_sources={save_sources}")

        # Run if tracing is on OR sources is on
        if not trace_enabled and not save_sources:
            print(f"[_snapshot_page] Skipping: neither trace nor sources enabled")
            return
        try:
            url = self.browser.get_current_url()
            title = self.browser.get_page_title()
        except Exception:
            url, title = None, None

        self._step += 1
        outputs_format = getattr(ctx.trace_config, "outputs_format", "jsonl_only")
        shot_name = None
        shot_path = None
        run_shot_path = None
        text_path = None
        ocr_text = None
        ocr_error = None

        # Screenshot capture:
        # - In jsonl_only mode, only capture if save_web_screenshots is enabled (Max raw).
        # - In markdown mode, capture by default (legacy behavior).
        want_screenshot = (
            getattr(ctx.trace_config, "save_sources", False)
            or (outputs_format != "jsonl_only")
            or getattr(ctx.trace_config, "save_web_screenshots", False)
        )
        if want_screenshot:
            shot_name = f"trace_step_{self._step:04d}_{uuid.uuid4().hex[:8]}.png"
            try:
                shot_path = self.browser.screenshot_named(shot_name)
            except Exception:
                shot_path = None
            if shot_path and ctx.output_dir:
                try:
                    shots_dir = os.path.join(ctx.output_dir, "screenshots")
                    os.makedirs(shots_dir, exist_ok=True)
                    run_shot_path = os.path.join(shots_dir, shot_name)
                    shutil.copy2(shot_path, run_shot_path)
                except Exception:
                    run_shot_path = None

            # OCR: embed extracted text into trace.jsonl (optional, Max raw).
            if run_shot_path and getattr(ctx.trace_config, "save_web_ocr", False) and outputs_format == "jsonl_only":
                ocr_text, ocr_error = ocr_image(run_shot_path)

        # Page text file: only for markdown mode
        if outputs_format != "jsonl_only" and page_text:
                text_path = self._safe_trace_write(
                    rel_path=os.path.join("trace_pages", f"trace_step_{self._step:04d}_{uuid.uuid4().hex[:8]}.md"),
                    content=page_text,
                )

        trace_event(
            "page_snapshot",
            step=self._step,
            url=url,
            title=title,
            screenshot_file=shot_name if shot_path else None,
            screenshot_path=shot_path,
            run_screenshot_path=run_shot_path,
            page_text=(
                page_text
                if (outputs_format == "jsonl_only" and getattr(ctx.trace_config, "save_web_page_text", False))
                else None
            ),
            page_text_path=text_path,
            ocr_text=ocr_text,
            ocr_error=ocr_error,
        )

        # --- SOURCES: Simple upsert if enabled (never affects navigation) ---
        try:
            if getattr(ctx.trace_config, "save_sources", False) and url and url.startswith("http"):
                rid = getattr(ctx, "run_id", None)
                out_dir = getattr(ctx, "output_dir", None)
                if rid:
                    # Build simple source record
                    screenshot_paths = []
                    if shot_name and run_shot_path:
                        screenshot_paths = [os.path.join("screenshots", shot_name).replace("\\", "/")]
                    verbatim_context = []
                    if ocr_text:
                        verbatim_context.append(str(ocr_text).strip()[:1500])
                    if page_text:
                        verbatim_context.append(str(page_text).strip()[:1500])

                    # Default source record (raw) - upsert immediately so we have data even if LLM fails
                    source_rec = {
                        "url": url,
                        "kind": "web",
                        "title": title or "",
                        "relevancy_score": None,
                        "match": "",
                        "how_helps": "",
                        "data_to_collect": [],
                        "evidence_quotes": [],
                        "verbatim_context": verbatim_context[:2],
                        "screenshot_paths": screenshot_paths[:4],
                    }

                    print(f"[Sources] Upserting (raw): {url[:80]}...")
                    added, total = _add_sources(
                        rid,
                        [source_rec],
                        step_id=f"web_{self._step}",
                        agent="Web",
                        output_dir=out_dir,
                    )
                    print(f"[Sources] Done: added={added}, total={total}")

                    # Optional LLM enrichment in BACKGROUND THREAD (fire-and-forget, never blocks navigation)
                    if getattr(ctx.trace_config, "save_sources_llm", False) and self._run_goal:
                        def _enrich_source_bg():
                            try:
                                print(f"[Sources BG] LLM enrichment for: {url[:60]}...")
                                raw_ctx = "\n".join(verbatim_context)[:2500]
                                system = (
                                    "You score and summarize web sources. Return ONLY valid JSON.\n"
                                    "Schema: {\"relevancy_score\": 0.0, \"match\": \"...\", \"how_helps\": \"...\", "
                                    "\"data_to_collect\": [\"...\"], \"evidence_quotes\": [\"...\"]}\n"
                                    "Rules:\n"
                                    "- relevancy_score: 0.0 (irrelevant) to 1.0 (directly answers goal)\n"
                                    "- match: 1 sentence on why this source matches the goal\n"
                                    "- how_helps: 1 sentence on how this helps answer the goal\n"
                                    "- data_to_collect: 2-4 bullet points of key data found\n"
                                    "- evidence_quotes: 1-3 short verbatim quotes from the page\n"
                                    "Do NOT invent data. Use ONLY what's in the page content."
                                )
                                user_msg = f"GOAL: {self._run_goal}\n\nURL: {url}\nTITLE: {title}\n\nPAGE CONTENT:\n{raw_ctx}"
                                llm_out = self.llm.respond([{"role": "system", "content": system}, {"role": "user", "content": user_msg}], False)
                                # Parse JSON from response
                                txt = str(llm_out or "").strip()
                                import re as _re
                                m = _re.search(r"\{[\s\S]*\}", txt)
                                if m:
                                    data = json.loads(m.group(0))
                                    if isinstance(data, dict):
                                        enriched = {"url": url}
                                        if "relevancy_score" in data:
                                            try:
                                                enriched["relevancy_score"] = max(0.0, min(1.0, float(data["relevancy_score"])))
                                            except:
                                                pass
                                        if data.get("match"):
                                            enriched["match"] = str(data["match"])[:500]
                                        if data.get("how_helps"):
                                            enriched["how_helps"] = str(data["how_helps"])[:500]
                                        if isinstance(data.get("data_to_collect"), list):
                                            enriched["data_to_collect"] = [str(x)[:200] for x in data["data_to_collect"][:6]]
                                        if isinstance(data.get("evidence_quotes"), list):
                                            enriched["evidence_quotes"] = [str(x)[:300] for x in data["evidence_quotes"][:4]]
                                        # Upsert enriched data (merges with existing raw record)
                                        _add_sources(rid, [enriched], step_id=f"web_{self._step}_enriched", agent="Web", output_dir=out_dir)
                                        print(f"[Sources BG] LLM enrichment done: score={enriched.get('relevancy_score')}")
                            except Exception as bg_err:
                                print(f"[Sources BG] LLM enrichment failed: {type(bg_err).__name__}: {bg_err}")

                        # Spawn daemon thread - will be killed on process exit, never blocks main thread
                        t = threading.Thread(target=_enrich_source_bg, daemon=True)
                        t.start()
                        print(f"[Sources] LLM enrichment spawned in background thread")
        except Exception as e:
            print(f"[Sources] Error (ignored): {type(e).__name__}: {e}")
    
    def get_today_date(self) -> str:
        """Get the date"""
        date_time = date.today()
        return date_time.strftime("%B %d, %Y")

    def extract_links(self, search_result: str) -> List[str]:
        """Extract all links from a sentence."""
        pattern = r'(https?://\S+|www\.\S+)'
        matches = re.findall(pattern, search_result)
        trailing_punct = ".,!?;:)"
        cleaned_links = [link.rstrip(trailing_punct) for link in matches]
        self.logger.info(f"Extracted links: {cleaned_links}")
        return self.clean_links(cleaned_links)
    
    def extract_form(self, text: str) -> List[str]:
        """Extract form written by the LLM in format [input_name](value)"""
        inputs = []
        matches = re.findall(r"\[\w+\]\([^)]+\)", text)
        return matches
        
    def clean_links(self, links: List[str]) -> List[str]:
        """Ensure no '.' at the end of link"""
        links_clean = []
        for link in links:
            link = link.strip()
            if not (link[-1].isalpha() or link[-1].isdigit()):
                links_clean.append(link[:-1])
            else:
                links_clean.append(link)
        return links_clean

    def get_unvisited_links(self) -> List[str]:
        visited_norm = set(_normalize_url(h) for h in self.search_history if h)
        return "\n".join([f"[{i}] {link}" for i, link in enumerate(self.navigable_links) if _normalize_url(link) not in visited_norm])

    def make_newsearch_prompt(self, prompt: str, search_result: dict) -> str:
        search_choice = self.stringify_search_results(search_result)
        self.logger.info(f"Search results: {search_choice}")
        return f"""
        Based on the search result:
        {search_choice}
        Your goal is to find accurate and complete information to satisfy the user’s request.
        User request: {prompt}
        To proceed, choose a relevant link from the search results. Announce your choice by saying: "I will navigate to <link>"
        Do not explain your choice.
        """
    
    def make_navigation_prompt(self, user_prompt: str, page_text: str) -> str:
        remaining_links = self.get_unvisited_links() 
        remaining_links_text = remaining_links if remaining_links is not None else "No links remaining, do a new search." 
        inputs_form = self.browser.get_form_inputs()
        inputs_form_text = '\n'.join(inputs_form)
        notes = '\n'.join(self.notes)
        self.logger.info(f"Making navigation prompt with page text: {page_text[:100]}...\nremaining links: {remaining_links_text}")
        self.logger.info(f"Inputs form: {inputs_form_text}")
        self.logger.info(f"Notes: {notes}")

        return f"""
        You are navigating the web.

        **Current Context**

        Webpage ({self.current_page}) content:
        {page_text}

        Allowed Navigation Links:
        {remaining_links_text}

        Inputs forms:
        {inputs_form_text}

        End of webpage ({self.current_page}.

        # Instruction

        1. **Evaluate if the page is relevant for user’s query and document finding:**
          - If the page is relevant, extract and summarize key information in concise notes (Note: <your note>)
          - Include a short "Match:" clause in the Note that explicitly states what matches the user request (e.g. Match: mentions moissanite tennis necklace, price shown, ships to Atlanta).
          - If page not relevant, state: "Error: <specific reason the page does not address the query>" and either return to the previous page or navigate to a new link.
          - Notes should be factual, useful summaries of relevant content, they should always include specific names or link. Written as: "On <website URL>, <key fact 1>. <Key fact 2>. <Additional insight>." Avoid phrases like "the page provides" or "I found that."
        2. **Navigate to a link by either: **
          - Saying I will navigate to (write down the full URL) www.example.com/cats
          - Going back: If no link seems helpful, say: {Action.GO_BACK.value}.
        3. **Fill forms on the page:**
          - Fill form only when relevant.
          - Use Login if username/password specified by user. For quick task create account, remember password in a note.
          - You can fill a form using [form_name](value). Don't {Action.GO_BACK.value} when filling form.
          - If a form is irrelevant or you lack informations (eg: don't know user email) leave it empty.
        4. **Decide if you completed the task**
          - Check your notes. Do they fully answer the question? Did you verify with multiple pages?
          - Are you sure it’s correct?
          - If yes to all, say {Action.REQUEST_EXIT}.
          - If no, or a page lacks info, go to another link.
          - Never stop or ask the user for help.
        
        **Rules:**
        - Do not write "The page talk about ...", write your finding on the page and how they contribute to an answer.
        - Put note in a single paragraph.
        - When you exit, explain why.
        
        # Example:
        
        Example 1 (useful page, no need go futher):
        Note: According to karpathy site LeCun net is ...
        No link seem useful to provide futher information.
        Action: {Action.GO_BACK.value}

        Example 2 (not useful, see useful link on page):
        Error: reddit.com/welcome does not discuss anything related to the user’s query.
        There is a link that could lead to the information.
        Action: navigate to http://reddit.com/r/locallama

        Example 3 (not useful, no related links):
        Error: x.com does not discuss anything related to the user’s query and no navigation link are usefull.
        Action: {Action.GO_BACK.value}

        Example 3 (clear definitive query answer found or enought notes taken):
        I took 10 notes so far with enought finding to answer user question.
        Therefore I should exit the web browser.
        Action: {Action.REQUEST_EXIT.value}

        Example 4 (loging form visible):

        Note: I am on the login page, I will type the given username and password. 
        Action:
        [username_field](David)
        [password_field](edgerunners77)

        Remember, user asked:
        {user_prompt}
        You previously took these notes:
        {notes}
        Do not Step-by-Step explanation. Write comprehensive Notes or Error as a long paragraph followed by your action.
        You must always take notes.
        """
    
    async def llm_decide(self, prompt: str, show_reasoning: bool = False) -> Tuple[str, str]:
        animate_thinking("Thinking...", color="status")
        self.memory.push('user', prompt)
        answer, reasoning = await self.llm_request()
        self.last_reasoning = reasoning
        if show_reasoning:
            pretty_print(reasoning, color="failure")
        pretty_print(answer, color="output")
        return answer, reasoning
    
    def select_unvisited(self, search_result: List[str]) -> List[str]:
        # Build normalized set of visited URLs
        visited_norm = set(_normalize_url(h) for h in self.search_history if h)
        results_unvisited = []
        for res in search_result:
            link = res.get("link") or ""
            if _normalize_url(link) not in visited_norm:
                results_unvisited.append(res) 
        self.logger.info(f"Unvisited links: {results_unvisited}")
        return results_unvisited

    def jsonify_search_results(self, results_string: str) -> List[str]:
        result_blocks = results_string.split("\n\n")
        parsed_results = []
        for block in result_blocks:
            if not block.strip():
                continue
            lines = block.split("\n")
            result_dict = {}
            for line in lines:
                if line.startswith("Title:"):
                    result_dict["title"] = line.replace("Title:", "").strip()
                elif line.startswith("Snippet:"):
                    result_dict["snippet"] = line.replace("Snippet:", "").strip()
                elif line.startswith("Link:"):
                    result_dict["link"] = line.replace("Link:", "").strip()
            if result_dict:
                parsed_results.append(result_dict)
        return parsed_results 
    
    def stringify_search_results(self, results_arr: List[str]) -> str:
        return '\n\n'.join([f"Link: {res['link']}\nPreview: {res['snippet']}" for res in results_arr])
    
    def parse_answer(self, text):
        lines = text.split('\n')
        saving = False
        buffer = []
        links = []
        for line in lines:
            if line == '' or 'action:' in line.lower():
                saving = False
            if "note" in line.lower():
                saving = True
            if saving:
                buffer.append(line.replace("notes:", ''))
            else:
                links.extend(self.extract_links(line))
        note = '. '.join(buffer).strip()
        # Ensure notes carry a real URL so downstream summaries/reports can include correct links.
        try:
            current_url = self.browser.get_current_url() if self.browser else None
        except Exception:
            current_url = None
        if current_url and note and ("http://" not in note and "https://" not in note and "www." not in note):
            note = f"On {current_url}, {note}"
        self.notes.append(note)
        return links
    
    def select_link(self, links: List[str]) -> str | None:
        """
        Select the first unvisited link that is not the current page.
        Preference is given to links not in search_history.
        Uses normalized URLs to avoid loops caused by tracking params (srsltid, utm_*, etc).
        """
        # Build normalized set of visited URLs for fast lookup
        visited_norm = set()
        for h in self.search_history:
            if h:
                visited_norm.add(_normalize_url(h))
        current_norm = _normalize_url(self.current_page) if self.current_page else ""

        for lk in links:
            lk_norm = _normalize_url(lk)
            if lk_norm == current_norm or lk_norm in visited_norm:
                self.logger.info(f"Skipping already visited or current link: {lk}")
                continue
            self.logger.info(f"Selected link: {lk}")
            return lk
        self.logger.warning("No suitable link selected.")
        return None
    
    def get_page_text(self, limit_to_model_ctx = False) -> str:
        """Get the text content of the current page."""
        page_text = self.browser.get_text()
        if limit_to_model_ctx:
            #page_text = self.memory.compress_text_to_max_ctx(page_text)
            page_text = self.memory.trim_text_to_max_ctx(page_text)
        return page_text
    
    def conclude_prompt(self, user_query: str) -> str:
        annotated_notes = [f"{i+1}: {note.lower()}" for i, note in enumerate(self.notes)]
        search_note = '\n'.join(annotated_notes)
        pretty_print(f"AI notes:\n{search_note}", color="success")
        return f"""
        Following a human request:
        {user_query}
        A web browsing AI made the following finding across different pages:
        {search_note}

        Expand on the finding or step that lead to success, and provide a conclusion that answer the request. Include link when possible.
        Do not give advices or try to answer the human. Just structure the AI finding in a structured and clear way.
        You should answer in the same language as the user.
        """
    
    def search_prompt(self, user_prompt: str) -> str:
        return f"""
        Current date: {self.date}
        Make a efficient search engine query to help users with their request:
        {user_prompt}
        Example:
        User: "go to twitter, login with username toto and password pass79 to my twitter and say hello everyone "
        You: search: Twitter login page. 

        User: "I need info on the best laptops for AI this year."
        You: "search: best laptops 2025 to run Machine Learning model, reviews"

        User: "Search for recent news about space missions."
        You: "search: Recent space missions news, {self.date}"

        Do not explain, do not write anything beside the search query.
        Except if query does not make any sense for a web search then explain why and say {Action.REQUEST_EXIT.value}
        Do not try to answer query. you can only formulate search term or exit.
        """
    
    def handle_update_prompt(self, user_prompt: str, page_text: str, fill_success: bool) -> str:
        prompt = f"""
        You are a web browser.
        You just filled a form on the page.
        Now you should see the result of the form submission on the page:
        Page text:
        {page_text}
        The user asked: {user_prompt}
        Does the page answer the user’s query now? Are you still on a login page or did you get redirected?
        If it does, take notes of the useful information, write down result and say {Action.FORM_FILLED.value}.
        if it doesn’t, say: Error: Attempt to fill form didn't work {Action.GO_BACK.value}.
        If you were previously on a login form, no need to take notes.
        """
        if not fill_success:
            prompt += f"""
            According to browser feedback, the form was not filled correctly. Is that so? you might consider other strategies.
            """
        return prompt
    
    def show_search_results(self, search_result: List[str]):
        pretty_print("\nSearch results:", color="output")
        for res in search_result:
            pretty_print(f"Title: {res['title']} - ", color="info", no_newline=True)
            pretty_print(f"Link: {res['link']}", color="status")
    
    def stuck_prompt(self, user_prompt: str, unvisited: List[str]) -> str:
        """
        Prompt for when the agent repeat itself, can happen when fail to extract a link.
        """
        prompt = self.make_newsearch_prompt(user_prompt, unvisited)
        prompt += f"""
        You previously said:
        {self.last_answer}
        You must consider other options. Choose other link.
        """
        return prompt
    
    async def process(self, user_prompt: str, speech_module: type) -> Tuple[str, str]:
        """
        Process the user prompt to conduct an autonomous web search.
        Start with a google search with searxng using web_search tool.
        Then enter a navigation logic to find the answer or conduct required actions.
        Args:
          user_prompt: The user's input query
          speech_module: Optional speech output module
        Returns:
            tuple containing the final answer and reasoning
        """
        complete = False

        animate_thinking(f"Thinking...", color="status")
        trace_event("browser_agent_start", user_prompt=user_prompt)
        self._run_goal = str(user_prompt or "")
        mem_begin_idx = self.memory.push('user', self.search_prompt(user_prompt))
        ai_prompt, reasoning = await self.llm_request()
        if Action.REQUEST_EXIT.value in ai_prompt:
            pretty_print(f"Web agent requested exit.\n{reasoning}\n\n{ai_prompt}", color="failure")
            return ai_prompt, "" 
        animate_thinking(f"Searching...", color="status")
        self.status_message = "Searching..."
        ctx = get_run_context()
        if ctx is None or getattr(ctx.trace_config, "save_web_navigation", True):
            trace_event("web_search_query", query=ai_prompt)
        search_result_raw = self.tools["web_search"].execute([ai_prompt], False)
        search_result = self.jsonify_search_results(search_result_raw)[:16]
        # Persist structured search results so full URLs are always available in the trace,
        # even if an LLM later abbreviates links in a summary.
        ctx_nav = get_run_context()
        if ctx_nav is None or getattr(ctx_nav.trace_config, "save_web_navigation", True):
            trace_event("web_search_results", results=search_result)
        self.show_search_results(search_result)
        prompt = self.make_newsearch_prompt(user_prompt, search_result)
        unvisited = [None]
        while not complete and len(unvisited) > 0 and not self.stop:
            self.memory.clear()
            unvisited = self.select_unvisited(search_result)
            answer, reasoning = await self.llm_decide(prompt, show_reasoning = False)
            if self.stop:
                pretty_print(f"Requested stop.", color="failure")
                break
            if self.last_answer == answer:
                prompt = self.stuck_prompt(user_prompt, unvisited)
                continue
            self.last_answer = answer
            pretty_print('▂'*32, color="status")

            extracted_form = self.extract_form(answer)
            if len(extracted_form) > 0:
                self.status_message = "Filling web form..."
                pretty_print(f"Filling inputs form...", color="status")
                fill_success = self.browser.fill_form(extracted_form)
                page_text = self.get_page_text(limit_to_model_ctx=True)
                self._snapshot_page(page_text=page_text)
                answer = self.handle_update_prompt(user_prompt, page_text, fill_success)
                answer, reasoning = await self.llm_decide(prompt)

            if Action.FORM_FILLED.value in answer:
                pretty_print(f"Filled form. Handling page update.", color="status")
                page_text = self.get_page_text(limit_to_model_ctx=True)
                self.navigable_links = self.browser.get_navigable()
                self._snapshot_page(page_text=page_text)
                prompt = self.make_navigation_prompt(user_prompt, page_text)
                continue

            links = self.parse_answer(answer)
            # Emit any new notes taken this turn.
            if self.notes:
                try:
                    cur_url = self.browser.get_current_url() if self.browser else None
                    cur_title = self.browser.get_page_title() if self.browser else None
                except Exception:
                    cur_url, cur_title = None, None
                trace_event("browser_notes", notes=self.notes[-1], url=cur_url, title=cur_title)
            link = self.select_link(links)
            if link == self.current_page:
                pretty_print(f"Already visited {link}. Search callback.", color="status")
                prompt = self.make_newsearch_prompt(user_prompt, unvisited)
                self.search_history.append(link)
                continue

            if Action.REQUEST_EXIT.value in answer:
                self.status_message = "Exiting web browser..."
                pretty_print(f"Agent requested exit.", color="status")
                complete = True
                break

            # Check if link is already visited (using normalized URL to handle tracking params)
            link_already_visited = link and any(_normalize_url(link) == _normalize_url(h) for h in self.search_history if h)
            if (link == None and len(extracted_form) < 3) or Action.GO_BACK.value in answer or link_already_visited:
                pretty_print(f"Going back to results. Still {len(unvisited)}", color="status")
                self.status_message = "Going back to search results..."
                request_prompt = user_prompt
                if link is None:
                    request_prompt += f"\nYou previously choosen:\n{self.last_answer} but the website is unavailable. Consider other options."
                prompt = self.make_newsearch_prompt(request_prompt, unvisited)
                self.search_history.append(link)
                self.current_page = link
                continue

            animate_thinking(f"Navigating to {link}", color="status")
            if speech_module: speech_module.speak(f"Navigating to {link}")
            ctx = get_run_context()
            if ctx is None or getattr(ctx.trace_config, "save_web_navigation", True):
                trace_event("web_navigate", url=link)
            nav_ok = self.browser.go_to(link)
            # Always store the *actual* URL post-navigation so sources are never placeholders.
            actual_url = None
            try:
                actual_url = self.browser.get_current_url()
            except Exception:
                actual_url = None
            self.search_history.append(actual_url or link)
            if not nav_ok:
                pretty_print(f"Failed to navigate to {link}.", color="failure")
                prompt = self.make_newsearch_prompt(user_prompt, unvisited)
                continue
            self.current_page = actual_url or link
            page_text = self.get_page_text(limit_to_model_ctx=True)
            self.navigable_links = self.browser.get_navigable()
            self._snapshot_page(page_text=page_text)
            prompt = self.make_navigation_prompt(user_prompt, page_text)
            self.status_message = "Navigating..."
            self.browser.screenshot()

        pretty_print("Exited navigation, starting to summarize finding...", color="status")
        # Emit consolidated browser history (easy to copy out of a single JSONL event).
        try:
            ctx = get_run_context()
            if ctx is None or getattr(ctx.trace_config, "save_web_history", True):
                urls = []
                for u in (self.search_history or []):
                    if isinstance(u, str) and u.startswith("http") and "..." not in u:
                        urls.append(u.strip().rstrip(").,;]"))
                if isinstance(self.current_page, str) and self.current_page.startswith("http") and "..." not in self.current_page:
                    urls.append(self.current_page.strip().rstrip(").,;]"))
                # de-dupe, preserve order
                seen = set()
                deduped = []
                for u in urls:
                    if not u or u in seen:
                        continue
                    seen.add(u)
                    deduped.append(u)
                if deduped:
                    trace_event("browser_history", urls=deduped)
        except Exception:
            pass
        prompt = self.conclude_prompt(user_prompt)
        mem_last_idx = self.memory.push('user', prompt)
        self.status_message = "Summarizing findings..."
        answer, reasoning = await self.llm_request()
        pretty_print(answer, color="output")
        self.status_message = "Ready"
        self.last_answer = answer
        return answer, reasoning

if __name__ == "__main__":
    pass
