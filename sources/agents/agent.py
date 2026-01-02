
from typing import Tuple, Callable
from abc import abstractmethod
import os
import random
import time

import asyncio
from concurrent.futures import ThreadPoolExecutor

from sources.memory import Memory
from sources.utility import pretty_print
from sources.schemas import executorResult
from sources.runtime_context import trace_event
from sources.runtime_context import get_run_context
from sources import artifacts

random.seed(time.time())

class Agent():
    """
    An abstract class for all agents.
    """
    def __init__(self, name: str,
                       prompt_path:str,
                       provider,
                       verbose=False,
                       browser=None) -> None:
        """
        Args:
            name (str): Name of the agent.
            prompt_path (str): Path to the prompt file for the agent.
            provider: The provider for the LLM.
            recover_last_session (bool, optional): Whether to recover the last conversation. 
            verbose (bool, optional): Enable verbose logging if True. Defaults to False.
            browser: The browser class for web navigation (only for browser agent).
        """
            
        self.agent_name = name
        self.browser = browser
        self.role = None
        self.type = None
        self.current_directory = os.getcwd()
        self.llm = provider 
        self.memory = None
        self.tools = {}
        self.blocks_result = []
        self.success = True
        self.executed_blocks_last_call = 0
        self.had_tool_parse_error_last_call = False
        self.last_llm_response_empty = False
        self.last_answer = ""
        self.last_reasoning = ""
        self.status_message = "Haven't started yet"
        self.stop = False
        self.verbose = verbose
        self.executor = ThreadPoolExecutor(max_workers=1)
    
    @property
    def get_agent_name(self) -> str:
        return self.agent_name
    
    @property
    def get_agent_type(self) -> str:
        return self.type
    
    @property
    def get_agent_role(self) -> str:
        return self.role
    
    @property
    def get_last_answer(self) -> str:
        return self.last_answer
    
    @property
    def get_last_reasoning(self) -> str:
        return self.last_reasoning
    
    @property
    def get_blocks(self) -> list:
        return self.blocks_result
    
    @property
    def get_status_message(self) -> str:
        return self.status_message

    @property
    def get_tools(self) -> dict:
        return self.tools
    
    @property
    def get_success(self) -> bool:
        return self.success
    
    def get_blocks_result(self) -> list:
        return self.blocks_result

    def add_tool(self, name: str, tool: Callable) -> None:
        if tool is not Callable:
            raise TypeError("Tool must be a callable object (a method)")
        self.tools[name] = tool
    
    def get_tools_name(self) -> list:
        """
        Get the list of tools names.
        """
        return list(self.tools.keys())
    
    def get_tools_description(self) -> str:
        """
        Get the list of tools names and their description.
        """
        description = ""
        for name in self.get_tools_name():
            description += f"{name}: {self.tools[name].description}\n"
        return description
    
    def load_prompt(self, file_path: str) -> str:
        try:
            with open(file_path, 'r', encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"Prompt file not found at path: {file_path}")
        except PermissionError:
            raise PermissionError(f"Permission denied to read prompt file at path: {file_path}")
        except Exception as e:
            raise e
    
    def request_stop(self) -> None:
        """
        Request the agent to stop.
        """
        self.stop = True
        self.status_message = "Stopped"

    def reset_run_state(self) -> None:
        """
        Reset volatile per-run state so a previous stop/failure doesn't poison future tasks.
        Does NOT clear long-term memory.
        """
        self.stop = False
        self.status_message = "Ready"
        self.success = True
        self.executed_blocks_last_call = 0
        self.had_tool_parse_error_last_call = False
        self.last_llm_response_empty = False

    def apply_run_context(self) -> None:
        """
        Apply the current RunContext to this agent so all tool/file writes go under the per-run output dir.
        """
        ctx = get_run_context()
        if ctx is None:
            return
        out_dir = getattr(ctx, "output_dir", None)
        if not out_dir:
            return
        # Update agent working directory
        try:
            self.current_directory = out_dir
        except Exception:
            pass
        # Update all tool work dirs (Tools subclasses use tool.work_dir for writes/cwd)
        try:
            for tool in (self.tools or {}).values():
                try:
                    setattr(tool, "work_dir", out_dir)
                except Exception:
                    continue
        except Exception:
            return
    
    @abstractmethod
    def process(self, prompt, speech_module) -> str:
        """
        abstract method, implementation in child class.
        Process the prompt and return the answer of the agent.
        """
        pass

    def remove_reasoning_text(self, text: str) -> None:
        """
        Remove the reasoning block of reasoning model like deepseek.
        """
        end_tag = "</think>"
        end_idx = text.rfind(end_tag)
        if end_idx == -1:
            return text
        return text[end_idx+8:]
    
    def extract_reasoning_text(self, text: str) -> None:
        """
        Extract the reasoning block of a reasoning model like deepseek.
        """
        start_tag = "<think>"
        end_tag = "</think>"
        if text is None:
            return None
        start_idx = text.find(start_tag)
        end_idx = text.rfind(end_tag)+8
        return text[start_idx:end_idx]
    
    async def llm_request(self) -> Tuple[str, str]:
        """
        Asynchronously ask the LLM to process the prompt.
        """
        self.status_message = "Thinking..."
        trace_event("llm_working", agent=self.type, state="start")
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(self.executor, self.sync_llm_request)
        finally:
            trace_event("llm_working", agent=self.type, state="end")
    
    def sync_llm_request(self) -> Tuple[str, str]:
        """
        Ask the LLM to process the prompt and return the answer and the reasoning.
        """
        memory = self.memory.get()
        try:
            # Defensive: providers can occasionally return empty/None responses.
            thought = None
            for _ in range(3):
                thought = self.llm.respond(memory, self.verbose)
                if thought is not None and str(thought).strip() != "":
                    break
                time.sleep(0.2)
        except Exception as e:
            # Defensive: provider failures should not crash the whole agentic process.
            # Convert to a normal agent failure that can be surfaced to the user/UI.
            self.success = False
            err_summary = f"Error: LLM provider failed ({type(e).__name__}): {str(e)}"
            # Don't push provider stack/garbage into memory; just return a stable message.
            return "Error: LLM provider failed. Please retry.", err_summary

        if thought is None or str(thought).strip() == "":
            # Treat as transient; let the calling agent loop retry (bounded by max turns).
            self.last_llm_response_empty = True
            self.success = False
            return "Error: LLM returned an empty response. Retrying...", "Error: empty LLM response"
        self.last_llm_response_empty = False

        reasoning = self.extract_reasoning_text(thought)
        answer = self.remove_reasoning_text(thought)
        # Trace the model output *excluding* internal reasoning.
        ctx = get_run_context()
        if ctx is not None and ctx.is_trace_enabled() and getattr(ctx.trace_config, "outputs_format", "jsonl_only") == "jsonl_only":
            trace_event("llm_answer", agent=self.type, answer=answer)
        else:
            trace_event("llm_answer", agent=self.type, answer_preview=answer[:2000])
        # Persist intermediate outputs (useful for mid-run structured reports).
        if ctx is not None and ctx.is_trace_enabled() and ctx.trace_config.save_intermediate_outputs:
            # Store full answer as a snapshot (markdown) under the run folder
            snap_path = artifacts.write_markdown_snapshot(
                kind=f"llm_{self.type}",
                title=f"LLM output ({self.type})",
                body=answer,
            )
            # Also append a pointer into chat transcript (instead of dumping huge content).
            if snap_path and getattr(ctx.trace_config, "save_chat_transcript", False):
                artifacts.append_chat(f"[LLM_OUTPUT] ({self.type}) saved: {snap_path}")
        self.memory.push('assistant', answer)
        return answer, reasoning
    
    async def wait_message(self, speech_module):
        if speech_module is None:
            return
        messages = ["Please be patient, I am working on it.",
                    "Computing... I recommand you have a coffee while I work.",
                    "Hold on, I’m crunching numbers.",
                    "Working on it, please let me think."]
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self.executor, lambda: speech_module.speak(messages[random.randint(0, len(messages)-1)]))
    
    def get_last_tool_type(self) -> str:
        return self.blocks_result[-1].tool_type if len(self.blocks_result) > 0 else None
    
    def raw_answer_blocks(self, answer: str) -> str:
        """
        Return the answer with all the blocks inserted, as text.
        """
        if self.last_answer is None:
            return
        raw = ""
        lines = self.last_answer.split("\n")
        for line in lines:
            if "block:" in line:
                block_idx = int(line.split(":")[1])
                if block_idx < len(self.blocks_result):
                    raw += self.blocks_result[block_idx].__str__()
            else:
                raw += line + "\n"
        return raw

    def show_answer(self):
        """
        Show the answer in a pretty way.
        Show code blocks and their respective feedback by inserting them in the ressponse.
        """
        if self.last_answer is None:
            return
        lines = self.last_answer.split("\n")
        for line in lines:
            if "block:" in line:
                block_idx = int(line.split(":")[1])
                if block_idx < len(self.blocks_result):
                    self.blocks_result[block_idx].show()
            else:
                pretty_print(line, color="output")

    def remove_blocks(self, text: str) -> str:
        """
        Remove all code/query blocks within a tag from the answer text.
        """
        tag = f'```'
        lines = text.split('\n')
        post_lines = []
        in_block = False
        block_idx = 0
        for line in lines:
            if tag in line and not in_block:
                in_block = True
                continue
            if not in_block:
                post_lines.append(line)
            if tag in line:
                in_block = False
                post_lines.append(f"block:{block_idx}")
                block_idx += 1
        return "\n".join(post_lines)
    
    def show_block(self, block: str) -> None:
        """
        Show the block in a pretty way.
        """
        pretty_print('▂'*64, color="status")
        pretty_print(block, color="code")
        pretty_print('▂'*64, color="status")

    def execute_modules(self, answer: str) -> Tuple[bool, str]:
        """
        Execute all the tools the agent has and return the result.
        """
        # Track whether any new tool blocks were executed during this call.
        # Some agents use this to decide when to stop looping.
        start_blocks_len = len(self.blocks_result)
        self.had_tool_parse_error_last_call = False
        parse_errors = []
        feedback = ""
        success = True
        blocks = None
        if answer.startswith("```"):
            answer = "I will execute:\n" + answer # there should always be a text before blocks for the function that display answer

        # Do NOT reset self.success here.
        # self.success should reflect overall task success for the current agent run.
        # If a tool fails, we set self.success=False and keep it False unless a later tool run succeeds.
        for name, tool in self.tools.items():
            feedback = ""
            blocks, save_path = tool.load_exec_block(answer)
            ctx = get_run_context()
            # Enforce per-run tool config (if present)
            if ctx is not None and hasattr(ctx, "tool_config") and ctx.tool_config is not None:
                tool_tag = getattr(tool, "tag", None)
                allowed = ctx.tool_config.allow_tool(tool_key=name, tool_tag=tool_tag)
                if blocks and not allowed:
                    feedback = f"Tool '{name}' is disabled by user settings. Choose another allowed tool."
                    self.success = False
                    self.memory.push('user', feedback)
                    trace_event("tool_disabled", agent=self.type, tool=name, tool_tag=tool_tag)
                    self.executed_blocks_last_call = len(self.blocks_result) - start_blocks_len
                    return False, feedback
            tool_parse_err = getattr(tool, "last_parse_error", None)
            if tool_parse_err:
                self.had_tool_parse_error_last_call = True
                parse_errors.append(f"{name}: {tool_parse_err}")
                trace_event("tool_parse_error", agent=self.type, tool=name, error=tool_parse_err)

            if blocks:
                pretty_print(f"Executing {len(blocks)} {name} blocks...", color="status")
                trace_event("tool_execute_start", agent=self.type, tool=name, blocks_count=len(blocks))
                for block in blocks:
                    self.show_block(block)
                    output = tool.execute([block])
                    feedback = tool.interpreter_feedback(output) # tool interpreter feedback
                    success = not tool.execution_failure_check(output)
                    self.blocks_result.append(executorResult(block, feedback, success, name))
                    # Persist raw tool output to per-run artifacts (Raw mode).
                    out_path = None
                    ctx2 = get_run_context()
                    if ctx2 is not None and ctx2.is_trace_enabled() and getattr(ctx2.trace_config, "save_tool_outputs", False):
                        # In jsonl_only mode, keep everything in trace.jsonl instead of extra files.
                        if getattr(ctx2.trace_config, "outputs_format", "jsonl_only") != "jsonl_only":
                            out_path = artifacts.write_tool_output(name, output)
                            if out_path and getattr(ctx2.trace_config, "save_chat_transcript", False):
                                artifacts.append_chat(f"[TOOL_OUTPUT] {name} saved: {out_path}")
                    trace_event(
                        "tool_executed",
                        agent=self.type,
                        tool=name,
                        success=success,
                        feedback=feedback,
                        block=block,
                        output=output,
                        output_path=out_path,
                    )
                    if not success:
                        self.success = False
                        self.memory.push('user', feedback)
                        self.executed_blocks_last_call = len(self.blocks_result) - start_blocks_len
                        return False, feedback
                self.memory.push('user', feedback)
                # If we executed blocks and none failed, mark the agent as successful (recovery possible).
                self.success = True
                if save_path != None:
                    tool.save_block(blocks, save_path)
        self.executed_blocks_last_call = len(self.blocks_result) - start_blocks_len

        # If the model attempted tool usage but the tool blocks were malformed, ask it to retry.
        if self.executed_blocks_last_call == 0 and self.had_tool_parse_error_last_call:
            feedback = (
                "Tool block parse error(s) detected:\n- "
                + "\n- ".join(parse_errors)
                + f"\n\nPlease resend the tool block(s) with correct fenced code blocks like ```{self.get_last_tool_type() or '<tool>'}``` ... ```."
            )
            self.success = False
            self.memory.push('user', feedback)
            return False, feedback
        return True, feedback
