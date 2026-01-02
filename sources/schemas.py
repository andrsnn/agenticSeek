
from typing import Tuple, Callable
from pydantic import BaseModel
from sources.utility import pretty_print

class QueryRequest(BaseModel):
    query: str
    tts_enabled: bool = True
    project_name: str | None = None
    mode: str | None = None
    # Optional per-run LLM override
    provider_name: str | None = None
    provider_model: str | None = None
    provider_server_address: str | None = None
    provider_is_local: bool | None = None
    trace_file: str | None = None
    findings_file: str | None = None
    # Raw/trace settings (all optional, used when mode=trace/deep_research)
    trace_config: dict | None = None
    run_parent_dir: str | None = None
    tool_config: dict | None = None
    agent_config: dict | None = None

    def __str__(self):
        return f"Query: {self.query}, TTS: {self.tts_enabled}, mode: {self.mode}"

    def jsonify(self):
        return {
            "query": self.query,
            "tts_enabled": self.tts_enabled,
            "project_name": self.project_name,
            "mode": self.mode,
            "provider_name": self.provider_name,
            "provider_model": self.provider_model,
            "provider_server_address": self.provider_server_address,
            "provider_is_local": self.provider_is_local,
            "trace_file": self.trace_file,
            "findings_file": self.findings_file,
            "trace_config": self.trace_config,
            "run_parent_dir": self.run_parent_dir,
            "tool_config": self.tool_config,
            "agent_config": self.agent_config,
        }

class QueryResponse(BaseModel):
    done: str
    answer: str
    reasoning: str
    agent_name: str
    success: str
    blocks: dict
    status: str
    uid: str
    run_id: str | None = None
    output_dir: str | None = None
    trace_file: str | None = None

    def __str__(self):
        return f"Done: {self.done}, Answer: {self.answer}, Agent Name: {self.agent_name}, Success: {self.success}, Blocks: {self.blocks}, Status: {self.status}, UID: {self.uid}"

    def jsonify(self):
        return {
            "done": self.done,
            "answer": self.answer,
            "reasoning": self.reasoning,
            "agent_name": self.agent_name,
            "success": self.success,
            "blocks": self.blocks,
            "status": self.status,
            "uid": self.uid,
            "run_id": self.run_id,
            "output_dir": self.output_dir,
            "trace_file": self.trace_file,
        }


class executorResult:
    """
    A class to store the result of a tool execution.
    """
    def __init__(self, block: str, feedback: str, success: bool, tool_type: str):
        """
        Initialize an agent with execution results.

        Args:
            block: The content or code block processed by the agent.
            feedback: Feedback or response information from the execution.
            success: Boolean indicating whether the agent's execution was successful.
            tool_type: The type of tool used by the agent for execution.
        """
        self.block = block
        self.feedback = feedback
        self.success = success
        self.tool_type = tool_type
    
    def __str__(self):
        return f"Tool: {self.tool_type}\nBlock: {self.block}\nFeedback: {self.feedback}\nSuccess: {self.success}"
    
    def jsonify(self):
        return {
            "block": self.block,
            "feedback": self.feedback,
            "success": self.success,
            "tool_type": self.tool_type
        }

    def show(self):
        pretty_print('▂'*64, color="status")
        pretty_print(self.feedback, color="success" if self.success else "failure")
        pretty_print('▂'*64, color="status")