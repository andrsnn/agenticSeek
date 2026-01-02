import os
import asyncio

from sources.utility import pretty_print, animate_thinking
from sources.agents.agent import Agent
from sources.tools.mcpFinder import MCP_finder
from sources.memory import Memory

# NOTE MCP agent is an active work in progress, not functional yet.

class McpAgent(Agent):

    def __init__(self, name, prompt_path, provider, verbose=False):
        """
        The mcp agent is a special agent for using MCPs.
        MCP agent will be disabled if the user does not explicitly set the MCP_FINDER_API_KEY in environment variable.
        """
        super().__init__(name, prompt_path, provider, verbose, None)
        keys = self.get_api_keys()
        self.tools = {
            "mcp_finder": MCP_finder(keys["mcp_finder"]),
            # add mcp tools here
        }
        self.role = "mcp"
        self.type = "mcp_agent"
        self.memory = Memory(self.load_prompt(prompt_path),
                                recover_last_session=False, # session recovery in handled by the interaction class
                                memory_compression=False,
                                model_provider=provider.get_model_name())
        self.enabled = True
    
    def get_api_keys(self) -> dict:
        """
        Returns the API keys for the tools.
        """
        api_key_mcp_finder = os.getenv("MCP_FINDER_API_KEY")
        if not api_key_mcp_finder or api_key_mcp_finder == "":
            pretty_print("MCP Finder disabled.", color="warning")
            self.enabled = False
        return {
            "mcp_finder": api_key_mcp_finder
        }
    
    def expand_prompt(self, prompt):
        """
        Expands the prompt with the tools available.
        """
        tools_str = self.get_tools_description()
        prompt += f"""
        You can use the following tools and MCPs:
        {tools_str}
        """
        return prompt
    
    async def process(self, prompt, speech_module) -> str:
        if self.enabled == False:
            return "MCP Agent is disabled."
        prompt = self.expand_prompt(prompt)
        self.memory.push('user', prompt)
        # Guard against infinite loops: stop when the model no longer emits tool blocks.
        # (blocks_result persists across turns, so we must track "new blocks executed this turn".)
        working = True
        max_turns = 8
        turns = 0
        while working == True and not self.stop and turns < max_turns:
            animate_thinking("Thinking...", color="status")
            answer, reasoning = await self.llm_request()
            exec_success, _ = self.execute_modules(answer)
            answer = self.remove_blocks(answer)
            self.last_answer = answer
            self.status_message = "Ready"
            turns += 1

            executed = getattr(self, "executed_blocks_last_call", 0) or 0
            had_parse_err = getattr(self, "had_tool_parse_error_last_call", False)
            empty_llm = getattr(self, "last_llm_response_empty", False)

            # Stop when the model no longer emits tool blocks AND we didn't detect parse/empty-response issues.
            if executed == 0 and exec_success and not had_parse_err and not empty_llm:
                working = False

            # If a tool failed for a real reason (not just formatting/empty response), stop and surface it.
            if not exec_success and not had_parse_err and not empty_llm:
                working = False
        return answer, reasoning

if __name__ == "__main__":
    pass