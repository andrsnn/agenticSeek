import json
import os
import re
import asyncio
from typing import List, Tuple, Type, Dict
from sources.utility import pretty_print, animate_thinking
from sources.agents.agent import Agent
from sources.agents.code_agent import CoderAgent
from sources.agents.file_agent import FileAgent
from sources.agents.browser_agent import BrowserAgent
from sources.agents.casual_agent import CasualAgent
from sources.text_to_speech import Speech
from sources.tools.tools import Tools
from sources.logger import Logger
from sources.memory import Memory
from sources.runtime_context import trace_event
from sources import artifacts
from sources.workdir import resolve_work_dir
from sources.runtime_context import get_run_context
from sources.sources_store import add_sources as _add_sources
from sources.sources_store import normalize_url as _normalize_url

class PlannerAgent(Agent):
    def __init__(self, name, prompt_path, provider, verbose=False, browser=None):
        """
        The planner agent is a special agent that divides and conquers the task.
        """
        super().__init__(name, prompt_path, provider, verbose, None)
        self.tools = {
            "json": Tools()
        }
        self.tools['json'].tag = "json"
        self.browser = browser
        self.agents = {
            "coder": CoderAgent(name, "prompts/base/coder_agent.txt", provider, verbose=False),
            "file": FileAgent(name, "prompts/base/file_agent.txt", provider, verbose=False),
            "web": BrowserAgent(name, "prompts/base/browser_agent.txt", provider, verbose=False, browser=browser),
            "casual": CasualAgent(name, "prompts/base/casual_agent.txt", provider, verbose=False)
        }
        self.role = "planification"
        self.type = "planner_agent"
        # Plan tracking for UI/status (Cursor-like checklist)
        self.plan_goal = None
        self.plan_steps = []  # list[dict]: {idx, id, agent, task, title, status}
        self.plan_current_step = None
        self.memory = Memory(self.load_prompt(prompt_path),
                                recover_last_session=False, # session recovery in handled by the interaction class
                                memory_compression=False,
                                model_provider=provider.get_model_name())
        self.logger = Logger("planner_agent.log")
        # Safety: if a step stalls/loops, force-advance instead of blocking the entire queue.
        # Default: 10 minutes. Override with env var if needed.
        try:
            self.step_timeout_seconds = int(os.getenv("AGENTICSEEK_PLAN_STEP_TIMEOUT_SECONDS", "600"))
        except Exception:
            self.step_timeout_seconds = 600
        # How many times we are allowed to retry a step if the verifier says it's incomplete.
        try:
            self.step_max_retries = int(os.getenv("AGENTICSEEK_PLAN_STEP_MAX_RETRIES", "6"))
        except Exception:
            self.step_max_retries = 6

    def _get_amendments(self) -> list:
        """Get any amendments added to the current run."""
        try:
            from sources.runtime_context import get_run_context
            ctx = get_run_context()
            if not ctx or not ctx.run_id:
                return []

            # Direct import of api module
            try:
                import api as api_module
                amendments = api_module.get_amendments(ctx.run_id)
                return amendments
            except ImportError as ie:
                self._emit_activity(f"Could not import api: {ie}", color="warning")
                return []
        except Exception as e:
            self._emit_activity(f"Error getting amendments: {e}", color="warning")
        return []

    def _format_amendments(self, original_goal: str = "") -> str:
        """Format amendments as a string to inject into prompts, including original context."""
        amendments = self._get_amendments()
        if not amendments:
            return ""
        lines = ["\n\n[USER AMENDMENTS - Additional requests added during this run]"]
        # Include original prompt context so references like "this" make sense
        if original_goal:
            lines.append(f"Original user request: \"{original_goal.strip()[:500]}\"")
        lines.append("Additional points to incorporate:")
        for i, a in enumerate(amendments, 1):
            lines.append(f"  {i}. {a.get('text', '')}")
        lines.append("[Incorporate these additional points into your work, understanding they relate to the original request above.]\n")
        return "\n".join(lines)

    def _emit_activity(self, text: str, color: str = "output") -> None:
        """
        Emit an Activity feed line for the UI. Best-effort (never raise).
        """
        try:
            from sources.activity_bus import emit_activity
        except Exception:
            return
        try:
            ctx = get_run_context()
        except Exception:
            ctx = None
        rid = getattr(ctx, "run_id", None) if ctx is not None else None
        try:
            emit_activity("print", run_id=rid, text=str(text), color=color)
        except Exception:
            return

    def _extract_sources_from_output(self, text: str, goal: str, step_id: str, agent_name: str) -> None:
        """
        Extract URLs and citations mentioned in agent output and add them as sources.
        This captures references the LLM mentions even if the browser didn't visit them.
        """
        try:
            ctx = get_run_context()
            if ctx is None:
                return
            if not getattr(ctx.trace_config, "save_sources", False):
                return
            rid = getattr(ctx, "run_id", None)
            out_dir = getattr(ctx, "output_dir", None)
            if not rid:
                return

            txt = str(text or "")

            # Extract URLs from text with surrounding context
            url_pattern = r'https?://[^\s<>\"\'\)\]\}]+'
            url_with_context = {}
            for match in re.finditer(url_pattern, txt):
                url = match.group(0).rstrip(".,;:!?)>]}")
                if len(url) <= 10 or not url.startswith("http"):
                    continue
                # Get ~150 chars before and after for context
                start = max(0, match.start() - 150)
                end = min(len(txt), match.end() + 150)
                context = txt[start:end].strip()
                norm = _normalize_url(url)
                if norm and norm not in url_with_context:
                    url_with_context[norm] = {"url": url, "context": context}

            # Also look for PubMed IDs and create URLs
            pubmed_pattern = r'(?:PubMed\s*(?:ID)?[:\s]*|PMID[:\s]*)(\d{7,9})'
            for match in re.finditer(pubmed_pattern, txt, re.IGNORECASE):
                pmid = match.group(1)
                url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                # Get context around the PubMed mention
                start = max(0, match.start() - 200)
                end = min(len(txt), match.end() + 200)
                context = txt[start:end].strip()
                norm = _normalize_url(url)
                if norm and norm not in url_with_context:
                    url_with_context[norm] = {"url": url, "context": context, "pmid": pmid}

            if not url_with_context:
                return

            # Build source records for cited URLs
            sources_to_add = []
            for norm, data in list(url_with_context.items())[:20]:  # Limit to 20 per step
                ctx_text = data.get("context", "")
                # Try to extract a title-like phrase near the URL
                title = ""
                if data.get("pmid"):
                    # Look for study title pattern near PubMed ID
                    title_match = re.search(r'"([^"]{20,200})"', ctx_text)
                    if title_match:
                        title = title_match.group(1)

                sources_to_add.append({
                    "url": data["url"],
                    "kind": "citation",
                    "title": title,
                    "relevancy_score": None,
                    "match": "Cited in agent output",
                    "how_helps": "",
                    "data_to_collect": [],
                    "evidence_quotes": [ctx_text[:400]] if ctx_text else [],
                    "verbatim_context": [],
                    "screenshot_paths": [],
                })

            if sources_to_add:
                added, total = _add_sources(
                    rid,
                    sources_to_add,
                    step_id=step_id,
                    agent=agent_name,
                    output_dir=out_dir,
                )
                if added > 0:
                    print(f"[Sources] Extracted {added} citation(s) from {agent_name} output")
        except Exception as e:
            print(f"[Sources] Citation extraction error (ignored): {type(e).__name__}: {e}")

    def _set_plan(self, goal: str, agents_tasks: List[dict]) -> None:
        self.plan_goal = goal
        self.plan_steps = []
        self.plan_current_step = 0 if agents_tasks else None
        for idx, (task_name, task) in enumerate(agents_tasks):
            self.plan_steps.append({
                "idx": idx,
                "id": str(task.get("id", idx + 1)),
                "agent": str(task.get("agent", "")),
                "task": str(task.get("task", "")),
                "title": str(task_name),
                "deliverable": str(task.get("deliverable") or ""),
                "definition_of_done": task.get("definition_of_done") or task.get("dod") or [],
                "status": "pending",
            })
        trace_event("plan_created", goal=goal, steps=self.plan_steps)
        # Persist plan artifacts early
        ctx = None
        try:
            from sources.runtime_context import get_run_context
            ctx = get_run_context()
        except Exception:
            ctx = None
        if ctx is not None and ctx.is_trace_enabled() and ctx.trace_config.save_plan:
            artifacts.write_json("plan.json", {"goal": goal, "steps": self.plan_steps})
            md_lines = [f"# Plan\n\n## Goal\n\n{goal}\n\n## Steps\n"]
            for s in self.plan_steps:
                md_lines.append(f"- [{s['status']}] **{s.get('title') or s.get('task')}** (agent: {s.get('agent')})")
            artifacts.write_text("plan.md", "\n".join(md_lines) + "\n", append=False)

    def _update_step(self, idx: int, status: str) -> None:
        try:
            if idx is None:
                return
            self.plan_current_step = idx
            if 0 <= idx < len(self.plan_steps):
                self.plan_steps[idx]["status"] = status
                trace_event("plan_step", step_idx=idx, status=status, step=self.plan_steps[idx])
                # Update plan.md incrementally
                try:
                    from sources.runtime_context import get_run_context
                    ctx = get_run_context()
                except Exception:
                    ctx = None
                if ctx is not None and ctx.is_trace_enabled() and ctx.trace_config.save_plan:
                    md_lines = [f"# Plan\n\n## Goal\n\n{self.plan_goal or ''}\n\n## Steps\n"]
                    for s in self.plan_steps:
                        md_lines.append(f"- [{s['status']}] **{s.get('title') or s.get('task')}** (agent: {s.get('agent')})")
                    artifacts.write_text("plan.md", "\n".join(md_lines) + "\n", append=False)
        except Exception:
            return
    
    def get_task_names(self, text: str) -> List[str]:
        """
        Extracts task names from the given text.
        This method processes a multi-line string, where each line may represent a task name.
        containing '##' or starting with a digit. The valid task names are collected and returned.
        Args:
            text (str): A string containing potential task titles (eg: Task 1: I will...).
        Returns:
            List[str]: A list of extracted task names that meet the specified criteria.
        """
        tasks_names = []
        lines = text.strip().split('\n')
        for line in lines:
            if line is None:
                continue
            line = line.strip()
            if len(line) == 0:
                continue
            if '##' in line or line[0].isdigit():
                tasks_names.append(line)
                continue
        self.logger.info(f"Found {len(tasks_names)} tasks names.")
        return tasks_names

    def parse_agent_tasks(self, text: str) -> List[Tuple[str, str]]:
        """
        Parses agent tasks from the given LLM text.
        This method extracts task information from a JSON. It identifies task names and their details.
        Args:
            text (str): The input text containing task information in a JSON-like format.
        Returns:
            List[Tuple[str, str]]: A list of tuples containing task names and their details.
        """
        def _extract_fenced_json_blocks(txt: str) -> list[str]:
            """
            Extract ```json ... ``` blocks in a tolerant way (case-insensitive tag).
            Tools.load_exec_block() is case-sensitive and only triggers on exact ```json.
            """
            if not txt:
                return []
            blocks_local = []
            for m in re.finditer(r"```(?:json|JSON)\s*(.*?)\s*```", txt, flags=re.DOTALL):
                blocks_local.append(m.group(1).strip())
            return blocks_local

        def _iter_top_level_json_objects(txt: str) -> list[str]:
            """
            Strict JSON parsing, tolerant extraction: return candidate top-level {...} objects found
            in the text (ignoring braces inside JSON strings). Caller can json.loads() each candidate
            and take the first that parses.
            """
            if not txt:
                return []
            depth = 0
            in_str = False
            esc = False
            start = None
            out = []
            for i in range(0, len(txt)):
                ch = txt[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                    continue
                if ch == "{":
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and start is not None:
                        out.append(txt[start : i + 1])
                        start = None
            return out

        tasks = []
        tasks_names = self.get_task_names(text)

        blocks, _ = self.tools["json"].load_exec_block(text)
        if blocks is None:
            blocks = _extract_fenced_json_blocks(text)
        # If we still have no fenced blocks, try to extract a raw JSON object from surrounding prose.
        if not blocks:
            candidates = _iter_top_level_json_objects(text)
            for cand in candidates:
                try:
                    parsed = json.loads(cand)
                except Exception:
                    continue
                # Only accept objects with a "plan" key
                if isinstance(parsed, dict) and "plan" in parsed:
                    blocks = [cand]
                    break
            if not blocks:
                return []

        def normalize_agent_name(raw: str) -> str:
            """
            Normalize/alias LLM-provided agent names to the known agent keys.
            The planner prompt sometimes causes the LLM to output tool names like "bash".
            """
            if raw is None:
                return None
            a = str(raw).strip().lower()
            aliases = {
                # Common drift: model uses tool/language names instead of agent names
                "bash": "file",
                "shell": "file",
                "terminal": "file",
                "filesystem": "file",
                "files": "file",
                "fileagent": "file",
                "file_agent": "file",

                "python": "coder",
                "code": "coder",
                "coding": "coder",
                "developer": "coder",
                "coderagent": "coder",
                "code_agent": "coder",

                "browser": "web",
                "search": "web",
                "websearch": "web",

                "talk": "casual",
                "chat": "casual",
            }
            return aliases.get(a, a)

        for block in blocks:
            try:
                # Strict JSON parsing: extraction is tolerant, but JSON must be valid.
                line_json = json.loads(block)
            except Exception as e:
                # Malformed/partial JSON is common with smaller or flaky providers; retry upstream.
                try:
                    trace_event(
                        "planner_plan_parse_error",
                        {"error": f"{type(e).__name__}: {str(e)}", "snippet": str(block)[:800]},
                    )
                except Exception:
                    pass
                return []
            if 'plan' in line_json:
                for task in line_json['plan']:
                    normalized_agent = normalize_agent_name(task.get('agent'))
                    if normalized_agent not in [ag_name.lower() for ag_name in self.agents.keys()]:
                        self.logger.warning(f"Agent {task.get('agent')} does not exist.")
                        pretty_print(f"Agent {task.get('agent')} does not exist.", color="warning")
                        return []
                    try:
                        agent = {
                            'agent': normalized_agent,
                            'id': task['id'],
                            'task': task['task'],
                            'need': task.get('need', []) or [],
                            # Optional "definition of done" fields (verifier will use these).
                            'deliverable': task.get('deliverable') or "",
                            'definition_of_done': task.get('definition_of_done') or task.get('dod') or [],
                        }
                    except:
                        self.logger.warning("Missing field in json plan.")
                        return []
                    self.logger.info(f"Created agent {task['agent']} with task: {task['task']}")
                    if 'need' in task:
                        self.logger.info(f"Agent {task['agent']} was given info:\n {task['need']}")
                    tasks.append(agent)
        if len(tasks_names) != len(tasks):
            names = [task['task'] for task in tasks]
            return list(map(list, zip(names, tasks)))
        return list(map(list, zip(tasks_names, tasks)))
    
    def make_prompt(self, task: str, agent_infos_dict: dict) -> str:
        """
        Generates a prompt for the agent based on the task and previous agents work information.
        Args:
            task (str): The task to be performed.
            agent_infos_dict (dict): A dictionary containing information from other agents.
        Returns:
            str: The formatted prompt for the agent.
        """
        infos = ""
        if agent_infos_dict is None or len(agent_infos_dict) == 0:
            infos = "No needed informations."
        else:
            for agent_id, info in agent_infos_dict.items():
                infos += f"\t- According to agent {agent_id}:\n{info}\n\n"
        prompt = f"""
        You are given informations from your AI friends work:
        {infos}
        Your task is:
        {task}

        Critical rules:
        - Do NOT ask the user questions or request clarification.
        - If required info seems missing, recover automatically using tools and best-effort assumptions.
        - If a task mentions an input/list produced by another agent, use the content provided above as the source of truth.
        """
        self.logger.info(f"Prompt for agent:\n{prompt}")
        return prompt
    
    def show_plan(self, agents_tasks: List[dict], answer: str) -> None:
        """
        Displays the plan made by the agent.
        Args:
            agents_tasks (dict): The tasks assigned to each agent.
            answer (str): The answer from the LLM.
        """
        if agents_tasks == []:
            pretty_print(answer, color="warning")
            pretty_print("Failed to make a plan. This can happen with (too) small LLM. Clarify your request and insist on it making a plan within ```json.", color="failure")
            return
        pretty_print("\n▂▘ P L A N ▝▂", color="status")
        for task_name, task in agents_tasks:
            pretty_print(f"{task['agent']} -> {task['task']}", color="info")
        pretty_print("▔▗ E N D ▖▔", color="status")

    async def make_plan(self, prompt: str) -> str:
        """
        Asks the LLM to make a plan.
        Args:
            prompt (str): The prompt to be sent to the LLM.
        Returns:
            str: The plan made by the LLM.
        """
        ok = False
        answer = None
        while not ok:
            animate_thinking("Thinking...", color="status")
            self.memory.push('user', prompt)
            answer, reasoning = await self.llm_request()
            if "NO_UPDATE" in answer:
                return []
            agents_tasks = self.parse_agent_tasks(answer)
            if agents_tasks == []:
                self.show_plan(agents_tasks, answer)
                prompt = f"Failed to parse the tasks. Please write down your task followed by a json plan within ```json. Do not ask for clarification.\n"
                pretty_print("Failed to make plan. Retrying...", color="warning")
                continue
            self.show_plan(agents_tasks, answer)
            ok = True
        self.logger.info(f"Plan made:\n{answer}")
        return self.parse_agent_tasks(answer)

    def _plan_schema_instructions(self) -> str:
        """
        Hard requirements for the plan JSON schema, to prevent "step 1 loops" and enable verification.
        """
        return (
            "\n\nPlan JSON requirements:\n"
            "- Output ONLY one ```json block with {\"plan\": [...]}.\n"
            "- Each item must include: id, agent, task, deliverable, definition_of_done (array).\n"
            "- definition_of_done must be concrete checks (no vague wording).\n"
            "- JSON must be STRICTLY valid: strings must not contain raw newlines; use \\n if needed.\n"
        )
    
    async def update_plan(self, goal: str, agents_tasks: List[dict], agents_work_result: dict, id: str, success: bool) -> dict:
        """
        Updates the plan with the results of the agents work.
        Args:
            goal (str): The goal to be achieved.
            agents_tasks (list): The tasks assigned to each agent.
            agents_work_result (dict): The results of the agents work.
        Returns:
            dict: The updated plan.
        """
        self.status_message = "Updating plan..."
        last_agent_work = agents_work_result[id]
        tool_success_str = "success" if success else "failure"
        pretty_print(f"Agent {id} work {tool_success_str}.", color="success" if success else "failure")
        try:
            id_int = int(id)
        except Exception as e:
            return agents_tasks
        if id_int == len(agents_tasks):
            next_task = "No task follow, this was the last step. If it failed add a task to recover."
        else:
            next_task = f"Next task is: {agents_tasks[int(id)][0]}."
        #if success:
        #    return agents_tasks # we only update the plan if last task failed, for now
        update_prompt = f"""
        Your goal is : {goal}
        You previously made a plan, agents are currently working on it.
        The last agent working on task: {id}, did the following work:
        {last_agent_work}
        Agent {id} work was a {tool_success_str} according to system interpreter.
        {next_task}
        Is the work done for task {id} leading to success or failure ? Did an agent fail with a task?
        If agent work was good: answer "NO_UPDATE"
        If agent work is leading to failure: update the plan.
        If a task failed add a task to try again or recover from failure. You might have near identical task twice.
        plan should be within ```json like before.
        You need to rewrite the whole plan, but only change the tasks after task {id}.
        Make the plan the same length as the original one or with only one additional step.
        Do not change past tasks. Change next tasks.
        """
        pretty_print("Updating plan...", color="status")
        plan = await self.make_plan(update_prompt + self._plan_schema_instructions())
        if plan == []:
            pretty_print("No plan update required.", color="info")
            return agents_tasks
        self.logger.info(f"Plan updated:\n{plan}")
        return plan

    async def _verify_step(
        self,
        goal: str,
        task_name: str,
        task: dict,
        step_output: str,
        success: bool,
        elapsed_s: int,
        attempt_idx: int,
        remaining_s: int,
    ) -> dict:
        """
        Strict verifier that decides if a step is done, based on the step's Definition of Done.
        Returns a dict:
          {is_done: bool, confidence: float, missing: [str], recommended_action: "advance"|"retry", rationale: str}
        """
        def _extract_url_contexts(text_blob: str, max_urls: int = 50) -> list[dict]:
            """
            Extract URLs plus a small amount of verbatim surrounding context from step output.
            This is purely string-based (no hallucination) and feeds the LLM a bounded set of candidates.
            """
            t = str(text_blob or "")
            urls = []
            for m in re.finditer(r"https?://\S+", t):
                u = m.group(0).strip().rstrip(").,;]")
                if not u or "..." in u:
                    continue
                urls.append((u, m.start(), m.end()))
                if len(urls) >= max_urls:
                    break
            if not urls:
                return []
            lines = t.splitlines()
            # Build an index from char offset to line number for context extraction
            line_starts = []
            pos = 0
            for ln in lines:
                line_starts.append(pos)
                pos += len(ln) + 1

            def offset_to_line(off: int) -> int:
                # naive linear scan is fine for small outputs
                idx = 0
                for i, s in enumerate(line_starts):
                    if s <= off:
                        idx = i
                    else:
                        break
                return idx

            out = []
            seen = set()
            for u, s, _e in urls:
                if u in seen:
                    continue
                seen.add(u)
                li = offset_to_line(s)
                a = max(0, li - 2)
                b = min(len(lines), li + 3)
                ctx = "\n".join(lines[a:b]).strip()
                out.append({"url": u, "verbatim_context": ctx})
            return out

        # NOTE: Source extraction/enrichment is intentionally NOT done here anymore.
        # It used to run as a big blocking step and could stall the whole run.
        # Sources are now upserted incrementally by BrowserAgent as it visits pages / emits notes.

        try:
            self._emit_activity(
                f"Verifier running: step {task.get('id')} (attempt {attempt_idx}) — checking Definition of Done…",
                color="verifier",
            )
        except Exception:
            pass
        deliverable = str(task.get("deliverable") or "").strip()
        dod = task.get("definition_of_done") or task.get("dod") or []
        if not isinstance(dod, list):
            dod = [str(dod)]
        dod_lines = [str(x).strip() for x in dod if str(x).strip()]
        # If there is no DoD, be conservative: only accept if the agent reported success and output is non-empty.
        if not dod_lines:
            is_done = bool(success and (step_output or "").strip() != "")
            return {
                "is_done": is_done,
                "confidence": 0.5 if is_done else 0.0,
                "missing": [] if is_done else ["No definition_of_done provided in plan"],
                "recommended_action": "advance" if is_done else ("retry" if remaining_s > 60 and attempt_idx <= self.step_max_retries else "advance"),
                "rationale": "No DoD available; used conservative fallback.",
            }

        system = (
            "You are a STRICT verifier checking if a plan step is complete.\n"
            "You MUST NOT mark a step done unless ALL Definition-of-Done checks are explicitly satisfied.\n"
            "If you are unsure or evidence is missing, set is_done=false.\n\n"
            "Return JSON with these keys:\n"
            "- is_done: boolean\n"
            "- confidence: 0.0-1.0\n"
            "- missing: array of SPECIFIC, ACTIONABLE fix instructions (see below)\n"
            "- recommended_action: 'advance' or 'retry'\n"
            "- rationale: brief explanation of your decision\n\n"
            "CRITICAL - Each item in 'missing' MUST be a specific instruction the agent can follow:\n"
            "BAD missing items (vague, unhelpful):\n"
            "  - 'Report is incomplete'\n"
            "  - 'Sources not cited'\n"
            "  - 'Missing content'\n\n"
            "GOOD missing items (specific, actionable):\n"
            "  - 'Add a section titled \"Bryan Johnson Advice\" with his specific recommendations'\n"
            "  - 'Add a References section listing the URLs visited: https://site1.com, https://site2.com'\n"
            "  - 'The Executive Summary is empty - add 2-3 sentences summarizing the key findings'\n"
            "  - 'Expand Section 2 with more detail - currently only 1 sentence, needs 3-4'\n\n"
            "FOR REPORTS specifically, check:\n"
            "  - Does it have a References/Sources section with actual URLs? If not, mark missing.\n"
            "  - Is it comprehensive (4+ sections, detailed content)? If too short, mark missing.\n"
            "  - Does it use specific data/evidence from research, not vague statements?\n\n"
            "If a file was created, evaluate the ACTUAL CONTENT in the output, not just that it was created.\n"
            "Do NOT recommend retry for minor polish - only for substantive missing requirements.\n"
        )
        # Smart sampling for long outputs - the content is already in step_output
        # (agent passed it to write_output, so it's in the conversation)
        def smart_sample(text: str, max_chars: int = 12000) -> str:
            """Sample long text: beginning, middle sections, and end."""
            text = (text or "").strip()
            if len(text) <= max_chars:
                return text

            # For very long outputs, sample strategically
            chunk_size = max_chars // 4

            # Beginning (includes headers, structure)
            beginning = text[:chunk_size]

            # Find middle sections (look for ## headers if markdown)
            middle = ""
            lines = text.split("\n")
            section_starts = [i for i, line in enumerate(lines) if line.startswith("## ") or line.startswith("### ")]
            if section_starts and len(section_starts) > 2:
                # Get a middle section
                mid_idx = section_starts[len(section_starts) // 2]
                mid_start = sum(len(lines[j]) + 1 for j in range(mid_idx))
                middle = "\n... [middle sections sampled] ...\n" + text[mid_start:mid_start + chunk_size]
            else:
                # Just take middle chunk
                mid_point = len(text) // 2
                middle = "\n... [middle sampled] ...\n" + text[mid_point:mid_point + chunk_size]

            # End (includes conclusions, references)
            end = "\n... [end section] ...\n" + text[-chunk_size:]

            return beginning + middle + end

        sampled_output = smart_sample(step_output or "", max_chars=14000)

        user = (
            f"GOAL:\n{goal}\n\n"
            f"STEP NAME:\n{task_name}\n\n"
            f"STEP TASK:\n{task.get('task')}\n\n"
            f"DELIVERABLE:\n{deliverable}\n\n"
            f"DEFINITION_OF_DONE (ALL must be satisfied):\n- " + "\n- ".join(dod_lines) + "\n\n"
            f"STEP OUTPUT (content the agent produced):\n{sampled_output}\n\n"
            f"EXECUTION CONTEXT:\n- success_flag: {bool(success)}\n- attempt: {attempt_idx}\n- elapsed_seconds: {elapsed_s}\n- remaining_seconds_before_forced_advance: {remaining_s}\n\n"
            "Verify completion based on the output above. The output contains what the agent produced.\n"
            "If items are missing, list them with SPECIFIC, ACTIONABLE instructions to fix.\n"
        )

        # Use the planner provider but do NOT pollute long-term memory; call provider directly.
        raw = ""
        try:
            raw = await asyncio.to_thread(self.llm.respond, [{"role": "system", "content": system}, {"role": "user", "content": user}], False)
        except Exception:
            raw = ""
        txt = str(raw or "").strip()

        # Parse JSON strictly; if parse fails, default to NOT done.
        for _ in range(2):
            try:
                data = json.loads(txt)
                break
            except Exception:
                data = None
                # Try to extract a JSON object substring
                m = re.search(r"\{[\s\S]*\}", txt)
                txt = m.group(0) if m else ""
        if not isinstance(data, dict):
            return {
                "is_done": False,
                "confidence": 0.0,
                "missing": ["Verifier returned invalid JSON"],
                "recommended_action": "retry" if remaining_s > 60 and attempt_idx <= self.step_max_retries else "advance",
                "rationale": "Verifier output could not be parsed.",
            }

        is_done = bool(data.get("is_done"))
        try:
            conf = float(data.get("confidence") or 0.0)
        except Exception:
            conf = 0.0
        missing = data.get("missing")
        if not isinstance(missing, list):
            missing = [str(missing)] if missing else []
        missing = [str(x).strip() for x in missing if str(x).strip()]
        rec = str(data.get("recommended_action") or "").strip().lower()
        if rec not in ("advance", "retry"):
            rec = "advance" if is_done else ("retry" if remaining_s > 60 and attempt_idx <= self.step_max_retries else "advance")
        rationale = str(data.get("rationale") or "").strip()
        # Never allow retry if no time remains.
        if remaining_s <= 60:
            rec = "advance"
        out = {"is_done": is_done, "confidence": max(0.0, min(1.0, conf)), "missing": missing, "recommended_action": rec, "rationale": rationale}
        try:
            miss_preview = "; ".join((missing or [])[:4])
            miss_note = f" Missing: {miss_preview}" if miss_preview else ""
            self._emit_activity(
                f"Verifier result: step {task.get('id')} — {'DONE' if is_done else 'NOT DONE'} (conf={out['confidence']:.2f}, action={rec}).{miss_note}",
                color="verifier_done" if is_done else "verifier_not_done",
            )
        except Exception:
            pass

        # Sources: handled elsewhere (BrowserAgent).
        return out
    
    async def start_agent_process(self, task: dict, required_infos: dict | None, timeout_s: int | None = None) -> str:
        """
        Starts the agent process for a given task.
        Args:
            task (dict): The task to be performed.
            required_infos (dict | None): The required information for the task.
        Returns:
            str: The result of the agent process.
        """
        self.status_message = f"Starting task {task['task']}..."
        ctx = get_run_context()
        # Enforce agent policy: if a planned agent is disabled, fail fast so update_plan can repair.
        if ctx is not None and getattr(ctx, "agent_config", None) is not None:
            agent_key = str(task.get("agent", "")).lower()
            agent_obj = self.agents.get(agent_key)
            if agent_obj is not None:
                allowed = ctx.agent_config.allow_agent(agent_obj.type, agent_obj.role)
                if not allowed:
                    msg = f"Agent '{agent_obj.type}' is disabled by user settings. Planner must update the plan to use allowed agents."
                    trace_event("agent_disabled", agent=agent_obj.type)
                    return msg, False
        agent_prompt = self.make_prompt(task['task'], required_infos)
        # Include Definition of Done so the sub-agent knows when to stop and move on.
        try:
            deliverable = str(task.get("deliverable") or "").strip()
            dod = task.get("definition_of_done") or task.get("dod") or []
            if not isinstance(dod, list):
                dod = [str(dod)]
            dod_lines = [str(x).strip() for x in dod if str(x).strip()]
            if deliverable or dod_lines:
                agent_prompt += "\n\nDefinition of Done:\n"
                if deliverable:
                    agent_prompt += f"- Deliverable: {deliverable}\n"
                for d in dod_lines:
                    agent_prompt += f"- {d}\n"
                agent_prompt += "\nIf satisfied, stop and proceed.\n"
        except Exception:
            pass
        pretty_print(f"Agent {task['agent']} started working...", color="status")
        self.logger.info(f"Agent {task['agent']} started working on {task['task']}.")
        # Reset per-task success to avoid leaking failures across tasks.
        try:
            if hasattr(self.agents[task['agent'].lower()], "reset_run_state"):
                self.agents[task['agent'].lower()].reset_run_state()
            else:
                self.agents[task['agent'].lower()].success = True
                self.agents[task['agent'].lower()].stop = False
        except Exception:
            pass
        # Apply run output dir to sub-agent tools so any files go into runs/<run_id>/...
        try:
            if hasattr(self.agents[task['agent'].lower()], "apply_run_context"):
                self.agents[task['agent'].lower()].apply_run_context()
        except Exception:
            pass
        # Loop detector: enforce a hard time budget per step, then force-advance.
        base_timeout = max(30, int(self.step_timeout_seconds or 600))
        step_timeout = base_timeout
        if timeout_s is not None:
            try:
                step_timeout = max(15, int(timeout_s))
            except Exception:
                step_timeout = base_timeout
        agent_key = str(task.get("agent", "")).lower()
        agent_obj = self.agents.get(agent_key)
        agent_task = None
        try:
            agent_task = asyncio.create_task(agent_obj.process(agent_prompt, None))
            answer, reasoning = await asyncio.wait_for(agent_task, timeout=step_timeout)
        except asyncio.TimeoutError:
            try:
                if agent_task is not None:
                    agent_task.cancel()
            except Exception:
                pass
            try:
                if hasattr(agent_obj, "request_stop"):
                    agent_obj.request_stop()
            except Exception:
                pass
            # Best-effort salvage: use last_answer/notes if present so downstream steps can continue.
            partial = ""
            try:
                partial = str(getattr(agent_obj, "last_answer", "") or "").strip()
            except Exception:
                partial = ""
            if not partial and agent_key == "web":
                try:
                    notes = getattr(agent_obj, "notes", []) or []
                    partial = "\n".join([str(n) for n in notes[-12:]]).strip()
                except Exception:
                    partial = ""
            msg = (
                f"[system] Loop detector: step timed out after {step_timeout}s. "
                f"Forcing advance to the next step so the queue can continue."
            )
            try:
                self._emit_activity(
                    f"Timeout hit: step {task.get('id')} timed out after {step_timeout}s — forcing advance.",
                    color="warning",
                )
            except Exception:
                pass
            trace_event(
                "plan_step_timeout_forced_advance",
                task_id=str(task.get("id")),
                agent=agent_key,
                timeout_s=step_timeout,
                task=str(task.get("task") or ""),
            )
            combined = (partial + "\n\n" if partial else "") + msg
            return combined, True
        self.last_answer = answer
        self.last_reasoning = reasoning
        self.blocks_result = self.agents[task['agent'].lower()].blocks_result
        agent_answer = self.agents[task['agent'].lower()].raw_answer_blocks(answer)
        success = self.agents[task['agent'].lower()].get_success
        self.agents[task['agent'].lower()].show_answer()
        pretty_print(f"Agent {task['agent']} completed task.", color="status")
        self.logger.info(f"Agent {task['agent']} finished working on {task['task']}. Success: {success}")

        # If this was a Web/Browser step, append auto-captured sources so downstream steps never rely on
        # LLM-generated placeholder/ellipsis links.
        try:
            agent_key = str(task.get("agent", "")).lower()
            if agent_key == "web":
                web_agent = self.agents.get(agent_key)
                urls: list[str] = []
                # From browser agent state
                if web_agent is not None:
                    for u in getattr(web_agent, "search_history", []) or []:
                        if isinstance(u, str) and u.startswith("http"):
                            urls.append(u.strip())
                    cur = getattr(web_agent, "current_page", None)
                    if isinstance(cur, str) and cur.startswith("http"):
                        urls.append(cur.strip())
                    # From notes (these are prefixed with On <url>, when possible)
                    for n in getattr(web_agent, "notes", []) or []:
                        if not isinstance(n, str):
                            continue
                        urls.extend(re.findall(r"https?://\\S+", n))
                # Normalize + de-dupe
                cleaned = []
                seen = set()
                for u in urls:
                    u2 = u.strip().rstrip(").,;]")
                    if not u2 or not u2.startswith("http"):
                        continue
                    if "..." in u2:
                        continue
                    if u2 in seen:
                        continue
                    seen.add(u2)
                    cleaned.append(u2)
                if cleaned:
                    agent_answer += "\n\n## Sources (auto-captured)\n" + "\n".join([f"- {u}" for u in cleaned[:50]]) + "\n"
                    trace_event("web_sources_captured", task_id=str(task.get("id")), sources=cleaned[:200])
        except Exception:
            pass

        # Persist step output to a deterministic file under the per-run output dir so downstream steps can consume it.
        try:
            ctx = get_run_context()
            base_dir = (ctx.output_dir if (ctx and getattr(ctx, "output_dir", None)) else resolve_work_dir())
            # In single-file trace mode, do not create extra files.
            if ctx is not None and getattr(ctx.trace_config, "outputs_format", "jsonl_only") == "jsonl_only":
                raise Exception("skipped (jsonl_only)")
            step_dir = os.path.join(base_dir, "planner_outputs")
            os.makedirs(step_dir, exist_ok=True)
            step_file = os.path.join(step_dir, f"task_{task.get('id')}_{task.get('agent')}.md")
            with open(step_file, "w", encoding="utf-8") as f:
                f.write(agent_answer or "")
                f.write("\n")
            agent_answer += f"\n\n[system] Saved task output to: {step_file}"
            trace_event("planner_step_saved", task_id=str(task.get("id")), path=step_file)
        except Exception as e:
            # Don't spam errors for jsonl_only; just note if real error
            if "jsonl_only" not in str(e):
                trace_event("planner_step_save_failed", task_id=str(task.get("id")), error=str(e))

        agent_answer += "\nAgent succeeded with task." if success else "\nAgent failed with task (Error detected)."
        return agent_answer, success
    
    def get_work_result_agent(self, task_needs, agents_work_result):
        res = {k: agents_work_result[k] for k in task_needs if k in agents_work_result}
        self.logger.info(f"Next agent needs: {task_needs}.\n Match previous agent result: {res}")
        return res

    async def process(self, goal: str, speech_module: Speech) -> Tuple[str, str]:
        """
        Process the goal by dividing it into tasks and assigning them to agents.
        Args:
            goal (str): The goal to be achieved (user prompt).
            speech_module (Speech): The speech module for text-to-speech.
        Returns:
            Tuple[str, str]: The result of the agent process and empty reasoning string.
        """
        agents_tasks = []
        required_infos = None
        agents_work_result = dict()

        self.status_message = "Making a plan..."
        # Inject tool/agent policy so the planner doesn't assume disabled agents/tools or bad file types.
        try:
            from sources.runtime_context import get_run_context
            ctx = get_run_context()
        except Exception:
            ctx = None
        if ctx is not None and getattr(ctx, "tool_config", None) is not None:
            tc = ctx.tool_config
            output_format = getattr(tc, "default_output_format", "none") or "none"
            tool_policy = f"\n\nTool policy (must follow):\n- spreadsheet_format: {tc.spreadsheet_format}\n"
            tool_policy += f"- default_output_format: {output_format}\n"
            if tc.enabled_tools is not None:
                tool_policy += f"- enabled_tools: {sorted(list(tc.enabled_tools))}\n"
            if tc.disabled_tools:
                tool_policy += f"- disabled_tools: {sorted(list(tc.disabled_tools))}\n"
            tool_policy += (
                "- If spreadsheet_format is csv, instruct File agent to write CSV (do NOT require LibreOffice).\n"
                "- Do not assign tasks requiring disabled tools.\n"
            )
            # Output format guidance
            if output_format == "none":
                tool_policy += "- default_output_format is 'none': Do NOT create output files unless user EXPLICITLY requests one.\n"
            elif output_format == "md":
                tool_policy += "- default_output_format is 'md': If user says 'report' or 'summary', use markdown_report tool (not CSV).\n"
            elif output_format == "csv":
                tool_policy += "- default_output_format is 'csv': If user wants data output, create CSV file.\n"
            goal = goal + tool_policy

        if ctx is not None and getattr(ctx, "agent_config", None) is not None:
            ac = ctx.agent_config
            agent_policy = "\n\nAgent policy (must follow):\n"
            if ac.enabled_agents is not None:
                agent_policy += f"- enabled_agents: {sorted(list(ac.enabled_agents))}\n"
            if ac.disabled_agents:
                agent_policy += f"- disabled_agents: {sorted(list(ac.disabled_agents))}\n"
            agent_policy += "- Do not assign tasks to disabled agents.\n"
            goal = goal + agent_policy

        agents_tasks = await self.make_plan(goal + self._plan_schema_instructions())

        if agents_tasks == []:
            return "Failed to parse the tasks.", ""
        self._set_plan(goal, agents_tasks)
        i = 0
        steps = len(agents_tasks)
        step_started_ts: dict[int, float] = {}
        step_attempts: dict[int, int] = {}
        while i < steps and not self.stop:
            task_name, task = agents_tasks[i][0], agents_tasks[i][1]
            self.status_message = "Starting agents..."
            self._update_step(i, "running")
            pretty_print(f"I will {task_name}.", color="info")
            self.last_answer = f"I will {task_name.lower()}."
            pretty_print(f"Assigned agent {task['agent']} to {task_name}", color="info")
            if speech_module: speech_module.speak(f"I will {task_name}. I assigned the {task['agent']} agent to the task.")

            if agents_work_result is not None:
                required_infos = self.get_work_result_agent(task.get('need', []), agents_work_result)
            # Fallback: if the plan forgot to wire need, but we're handing off to file/coder/casual,
            # include the most recent previous outputs so it doesn't hunt for missing files.
            if (required_infos is None or len(required_infos) == 0) and agents_work_result:
                current_agent = str(task.get("agent", "")).lower()
                if current_agent in ("file", "coder", "casual"):
                    keys = list(agents_work_result.keys())
                    tail = keys[-2:] if len(keys) >= 2 else keys
                    required_infos = {k: agents_work_result[k] for k in tail if k in agents_work_result}

            # Step-level timer + verifier-driven retry loop.
            try:
                loop_now = asyncio.get_event_loop().time()
            except Exception:
                loop_now = 0.0
            if i not in step_started_ts:
                step_started_ts[i] = loop_now
            if i not in step_attempts:
                step_attempts[i] = 0

            answer = ""
            success = False
            # Keep attempting this step until verified done, retries exhausted, or time budget exceeded.
            while True:
                step_attempts[i] = int(step_attempts.get(i, 0)) + 1
                attempt = int(step_attempts[i])
                try:
                    now2 = asyncio.get_event_loop().time()
                except Exception:
                    now2 = step_started_ts.get(i, 0.0)
                elapsed = int(max(0, now2 - step_started_ts.get(i, now2)))
                remaining = int(max(0, int(self.step_timeout_seconds or 600) - elapsed))

                # If we ran out of step time budget, force advance.
                if remaining <= 0:
                    msg = f"[system] Step time budget exceeded ({int(self.step_timeout_seconds)}s). Forcing advance to keep queue moving."
                    try:
                        trace_event("plan_step_time_budget_forced_advance", step_idx=i, task_id=str(task.get("id")), elapsed_s=elapsed)
                    except Exception:
                        pass
                    try:
                        self._emit_activity(
                            f"Step budget hit: step {task.get('id')} exceeded {int(self.step_timeout_seconds)}s — forcing advance.",
                            color="warning",
                        )
                    except Exception:
                        pass
                    answer = (answer + "\n\n" if answer else "") + msg
                    success = True
                    break

                # Check for amendments added by user during run
                amendments = self._get_amendments()
                if amendments:
                    amendments_str = self._format_amendments(original_goal=goal)
                    task = dict(task)  # Copy to avoid mutating original
                    task["task"] = str(task.get("task") or "") + amendments_str
                    # Clear amendments so they don't re-apply to every retry/step
                    try:
                        from sources.runtime_context import get_run_context
                        ctx = get_run_context()
                        if ctx and ctx.run_id and "api" in sys.modules:
                            api_module = sys.modules["api"]
                            if hasattr(api_module, "clear_amendments"):
                                api_module.clear_amendments(ctx.run_id)
                    except Exception:
                        pass
                    # Log prominently
                    print(f"[Planner] *** INCORPORATING {len(amendments)} AMENDMENT(S) INTO STEP ***", flush=True)
                    for a in amendments:
                        print(f"[Planner] Amendment: {a.get('text', '')[:100]}", flush=True)
                        self._emit_activity(f"AMEND: {a.get('text', '')[:100]}", color="info")

                try:
                    answer, success = await self.start_agent_process(task, required_infos, timeout_s=remaining)
                except Exception as e:
                    raise e

                # Mid-step amendment check: if user added amendments while agent was working,
                # force a retry so the agent can incorporate them (don't run verifier yet)
                self._emit_activity(f"Checking for mid-step amendments...", color="status")
                mid_step_amendments = self._get_amendments()
                self._emit_activity(f"Mid-step check: {len(mid_step_amendments)} amendments found", color="status")
                if mid_step_amendments:
                    self._emit_activity(f"Amendment detected! Retrying step to incorporate user feedback.", color="info")
                    # Don't clear yet - let the next iteration's pre-agent check handle it
                    # Just skip verifier and continue to next retry iteration
                    continue

                # Verifier runs after the step returns (the natural yield point).
                try:
                    verdict = await self._verify_step(
                        goal=goal,
                        task_name=str(task_name),
                        task=task,
                        step_output=str(answer or ""),
                        success=bool(success),
                        elapsed_s=elapsed,
                        attempt_idx=attempt,
                        remaining_s=remaining,
                    )
                except Exception:
                    verdict = {"is_done": False, "confidence": 0.0, "missing": ["verifier_failed"], "recommended_action": "advance", "rationale": "verifier_failed"}

                try:
                    trace_event("plan_step_verdict", step_idx=i, task_id=str(task.get("id")), verdict=verdict)
                except Exception:
                    pass

                # If verifier says done, proceed.
                if bool(verdict.get("is_done")):
                    answer = (answer or "") + f"\n\n[system] Verifier: step complete (confidence={verdict.get('confidence')})."
                    success = True
                    break

                # Not done: retry only if allowed and time remains.
                rec = str(verdict.get("recommended_action") or "retry").lower()
                missing = verdict.get("missing") or []
                if not isinstance(missing, list):
                    missing = [str(missing)]
                missing = [str(x).strip() for x in missing if str(x).strip()]
                conf = float(verdict.get("confidence") or 0.0)

                can_retry = attempt <= int(self.step_max_retries or 0)

                # Track progress: stop retrying if not improving
                prev_missing_count = step_attempts.get(f"{i}_missing", 999)
                prev_conf = step_attempts.get(f"{i}_conf", 0.0)
                curr_missing_count = len(missing)
                step_attempts[f"{i}_missing"] = curr_missing_count
                step_attempts[f"{i}_conf"] = conf

                # Not improving: same or more missing items AND confidence not increasing
                not_improving = (curr_missing_count >= prev_missing_count) and (conf <= prev_conf) and (attempt > 1)

                if rec != "retry" or (not can_retry) or remaining <= 60 or not_improving:
                    reason = "max retries" if not can_retry else ("no progress" if not_improving else "time/verifier")
                    answer = (answer or "") + f"\n\n[system] Verifier: incomplete, but advancing ({reason})."
                    try:
                        miss_preview = "; ".join(missing[:4])
                        miss_note = f" Missing: {miss_preview}" if miss_preview else ""
                        self._emit_activity(
                            f"Verifier: step {task.get('id')} advancing ({reason}).{miss_note}",
                            color="warning",
                        )
                    except Exception:
                        pass
                    success = True
                    break

                # Build detailed retry instructions with previous output context
                rationale = str(verdict.get("rationale") or "").strip()
                prev_output_preview = str(answer or "").strip()[-2000:]  # Last 2000 chars of previous output

                retry_note = "\n\n" + "="*60 + "\n"
                retry_note += f"VERIFIER FEEDBACK - ATTEMPT {attempt}/{self.step_max_retries} INCOMPLETE\n"
                retry_note += "="*60 + "\n\n"
                if rationale:
                    retry_note += f"REASON: {rationale}\n\n"
                retry_note += "ITEMS TO FIX (be specific, address each one):\n"
                for idx, item in enumerate(missing[:8], 1):
                    retry_note += f"  {idx}. {item}\n"
                retry_note += "\n"

                if prev_output_preview:
                    retry_note += "YOUR PREVIOUS OUTPUT (build on this, don't start over):\n"
                    retry_note += "-"*40 + "\n"
                    retry_note += prev_output_preview + "\n"
                    retry_note += "-"*40 + "\n\n"

                retry_note += "INSTRUCTIONS:\n"
                retry_note += "- Fix EACH numbered item above\n"
                retry_note += "- For files: write COMPLETE file in ONE write_output block (include previous content + fixes)\n"
                retry_note += "- Do NOT browse unless you need NEW information\n"
                retry_note += "="*60 + "\n"

                try:
                    miss_preview = "; ".join(missing[:3])
                    self._emit_activity(
                        f"Verifier retry: step {task.get('id')} attempt {attempt}/{self.step_max_retries} — Fix: {miss_preview}",
                        color="status",
                    )
                except Exception:
                    pass
                task = dict(task)
                task["task"] = str(task.get("task") or "") + retry_note
                # Continue loop and re-run agent with remaining time budget.
            if self.stop:
                pretty_print(f"Requested stop.", color="failure")
            agents_work_result[task['id']] = answer
            # Extract URLs/citations from agent output and add as sources
            try:
                self._extract_sources_from_output(
                    text=str(answer or ""),
                    goal=goal,
                    step_id=str(task.get("id", i)),
                    agent_name=str(task.get("agent", "Planner")),
                )
            except Exception:
                pass
            self._update_step(i, "completed" if success else "failed")
            agents_tasks = await self.update_plan(goal, agents_tasks, agents_work_result, task['id'], success)
            steps = len(agents_tasks)
            i += 1

        return answer, ""
