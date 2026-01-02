import asyncio

from sources.utility import pretty_print, animate_thinking
from sources.agents.agent import Agent
from sources.tools.fileFinder import FileFinder
from sources.tools.BashInterpreter import BashInterpreter
from sources.tools.appendFile import AppendFile
from sources.tools.markdownReport import MarkdownReport
from sources.tools.writeOutput import WriteOutput
from sources.memory import Memory

class FileAgent(Agent):
    def __init__(self, name, prompt_path, provider, verbose=False):
        """
        The file agent is a special agent for file operations.
        """
        super().__init__(name, prompt_path, provider, verbose, None)
        self.tools = {
            "file_finder": FileFinder(),
            "write_output": WriteOutput(),  # Primary tool for ALL file creation
            # Legacy tools (disabled by default in settings):
            "bash": BashInterpreter(),
            "append_file": AppendFile(),
            "markdown_report": MarkdownReport(),
        }
        self.work_dir = self.tools["file_finder"].get_work_dir()
        self.role = "files"
        self.type = "file_agent"
        self.memory = Memory(self.load_prompt(prompt_path),
                        recover_last_session=False, # session recovery in handled by the interaction class
                        memory_compression=False,
                        model_provider=provider.get_model_name())
    
    def _get_enabled_tools(self) -> list:
        """Get list of tools that are actually enabled for this run."""
        try:
            from sources.runtime_context import get_run_context
            ctx = get_run_context()
            if ctx and ctx.tool_config and ctx.tool_config.enabled_tools is not None:
                return list(ctx.tool_config.enabled_tools)
        except Exception:
            pass
        # Default if no config
        return ["file_finder", "write_output"]

    async def process(self, prompt, speech_module) -> str:
        exec_success = False

        # Get actually enabled tools for this run
        enabled = self._get_enabled_tools()
        enabled_str = ", ".join(enabled)

        # Build dynamic tool instructions based on what's actually enabled
        prompt += f"\nYou must work in directory: {self.work_dir}\n"
        prompt += f"\n**YOUR AVAILABLE TOOLS: {enabled_str}**\n"
        prompt += "Only use the tools listed above - others are disabled.\n\n"

        if "write_output" in enabled:
            prompt += (
                "FILE CREATION - USE write_output:\n"
                "```write_output\n"
                "format=md\n"
                "filename=my_report\n"
                "title=Report Title\n"
                "content=## Section 1\n"
                "Your full content here. Write the ENTIRE document in one block.\n\n"
                "## Section 2\n"
                "More content...\n\n"
                "## References\n"
                "- Source 1: https://example.com\n"
                "```\n\n"
                "For CSV: format=csv, filename=data, data=Col1,Col2\\nVal1,Val2\n\n"
            )
        elif "append_file" in enabled:
            prompt += (
                "FILE CREATION - USE append_file:\n"
                "```append_file\n"
                "path=report.md\n"
                "text=# Report Title\n"
                "Your content here...\n"
                "```\n\n"
            )

        prompt += (
            "Rules:\n"
            "- Do NOT ask questions - just execute the task\n"
            "- If content is in the prompt from other agents, use it directly\n"
            "- Write the ENTIRE file in ONE tool call\n"
        )
        self.memory.push('user', prompt)
        # Guard against infinite loops: stop when the model no longer emits tool blocks,
        # and cap the number of turns.
        max_turns = 10
        turns = 0
        last_feedback = ""
        while exec_success is False and not self.stop and turns < max_turns:
            await self.wait_message(speech_module)
            animate_thinking("Thinking...", color="status")
            answer, reasoning = await self.llm_request()
            self.last_reasoning = reasoning
            exec_success, last_feedback = self.execute_modules(answer)
            answer = self.remove_blocks(answer)
            self.last_answer = answer
            turns += 1

            executed = getattr(self, "executed_blocks_last_call", 0) or 0
            had_parse_err = getattr(self, "had_tool_parse_error_last_call", False)
            empty_llm = getattr(self, "last_llm_response_empty", False)

            # If the model stops emitting tool blocks AND there was no parse/empty-response issue,
            # we're done (return the natural language answer).
            if executed == 0 and exec_success and not had_parse_err and not empty_llm:
                # IMPORTANT: if a previous tool failed, don't let the run "bottom out" into questions.
                # Keep trying until we recover or hit max_turns.
                if getattr(self, "success", True) is False:
                    recovery = (
                        "Previous attempt failed. You must recover automatically (no user questions).\n"
                        f"Last tool feedback:\n{last_feedback}\n\n"
                        "Try alternative strategies:\n"
                        "- Use file_finder with different likely filenames.\n"
                        "- If the prompt contains the needed content, parse it and write the required output file.\n"
                        "- If you need to create a file, use append_file.\n"
                    )
                    self.memory.push("user", recovery)
                    exec_success = False
                else:
                    exec_success = True
        self.status_message = "Ready"
        return answer, reasoning

if __name__ == "__main__":
    pass