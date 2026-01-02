import os
import readline
from typing import List, Tuple, Type, Dict

from sources.text_to_speech import Speech
from sources.utility import pretty_print, animate_thinking
from sources.router import AgentRouter
from sources.speech_to_text import AudioTranscriber, AudioRecorder
import threading
from sources.runtime_context import get_run_context, trace_event, RunMode

from sources.deep_research import DeepResearchOrchestrator
from sources.workdir import resolve_work_dir
from sources import artifacts

class Interaction:
    """
    Interaction is a class that handles the interaction between the user and the agents.
    """
    def __init__(self, agents,
                 tts_enabled: bool = True,
                 stt_enabled: bool = True,
                 recover_last_session: bool = False,
                 langs: List[str] = ["en", "zh"]
                ):
        self.is_active = True
        self.current_agent = None
        self.last_query = None
        self.last_answer = None
        self.last_reasoning = None
        self.agents = agents
        self.tts_enabled = tts_enabled
        self.stt_enabled = stt_enabled
        self.recover_last_session = recover_last_session
        self.router = AgentRouter(self.agents, supported_language=langs)
        self.ai_name = self.find_ai_name()
        self.speech = None
        self.transcriber = None
        self.recorder = None
        self.is_generating = False
        self._queued_queries = []
        self.languages = langs
        if tts_enabled:
            self.initialize_tts()
        if stt_enabled:
            self.initialize_stt()
        if recover_last_session:
            self.load_last_session()
        self.emit_status()

    def enqueue(self, query: str) -> int:
        """
        Queue a query to be executed after the current one finishes.
        Returns the new queue length.
        """
        if query is None or str(query).strip() == "":
            return len(self._queued_queries)
        self._queued_queries.append(str(query))
        trace_event("queued", query=query, queue_len=len(self._queued_queries))
        return len(self._queued_queries)

    def queued_len(self) -> int:
        return len(self._queued_queries)

    def pop_next_queued(self) -> str | None:
        if not self._queued_queries:
            return None
        return self._queued_queries.pop(0)
    
    def get_spoken_language(self) -> str:
        """Get the primary TTS language."""
        lang = self.languages[0]
        return lang

    def initialize_tts(self):
        """Initialize TTS."""
        if not self.speech:
            animate_thinking("Initializing text-to-speech...", color="status")
            self.speech = Speech(enable=self.tts_enabled, language=self.get_spoken_language(), voice_idx=1)

    def initialize_stt(self):
        """Initialize STT."""
        if not self.transcriber or not self.recorder:
            animate_thinking("Initializing speech recognition...", color="status")
            self.transcriber = AudioTranscriber(self.ai_name, verbose=False)
            self.recorder = AudioRecorder()
    
    def emit_status(self):
        """Print the current status of agenticSeek."""
        if self.stt_enabled:
            pretty_print(f"Text-to-speech trigger is {self.ai_name}", color="status")
        if self.tts_enabled:
            self.speech.speak("Hello, we are online and ready. What can I do for you ?")
        pretty_print("AgenticSeek is ready.", color="status")
    
    def find_ai_name(self) -> str:
        """Find the name of the default AI. It is required for STT as a trigger word."""
        ai_name = "jarvis"
        for agent in self.agents:
            if agent.type == "casual_agent":
                ai_name = agent.agent_name
                break
        return ai_name
    
    def get_last_blocks_result(self) -> List[Dict]:
        """Get the last blocks result."""
        if self.current_agent is None:
            return []
        blks = []
        for agent in self.agents:
            blks.extend(agent.get_blocks_result())
        return blks
    
    def load_last_session(self):
        """Recover the last session."""
        for agent in self.agents:
            if agent.type == "planner_agent":
                continue
            agent.memory.load_memory(agent.type)
    
    def save_session(self):
        """Save the current session."""
        for agent in self.agents:
            agent.memory.save_memory(agent.type)

    def is_active(self) -> bool:
        return self.is_active
    
    def read_stdin(self) -> str:
        """Read the input from the user."""
        buffer = ""

        PROMPT = "\033[1;35m➤➤➤ \033[0m"
        while not buffer:
            try:
                buffer = input(PROMPT)
            except EOFError:
                return None
            if buffer == "exit" or buffer == "goodbye":
                return None
        return buffer
    
    def transcription_job(self) -> str:
        """Transcribe the audio from the microphone."""
        self.recorder = AudioRecorder(verbose=True)
        self.transcriber = AudioTranscriber(self.ai_name, verbose=True)
        self.transcriber.start()
        self.recorder.start()
        self.recorder.join()
        self.transcriber.join()
        query = self.transcriber.get_transcript()
        if query == "exit" or query == "goodbye":
            return None
        return query

    def get_user(self) -> str:
        """Get the user input from the microphone or the keyboard."""
        if self.stt_enabled:
            query = "TTS transcription of user: " + self.transcription_job()
        else:
            query = self.read_stdin()
        if query is None:
            self.is_active = False
            self.last_query = None
            return None
        self.last_query = query
        return query
    
    def set_query(self, query: str) -> None:
        """Set the query"""
        self.is_active = True
        self.last_query = query
    
    async def think(self) -> bool:
        """Request AI agents to process the user input."""
        push_last_agent_memory = False
        if self.last_query is None or len(self.last_query) == 0:
            return False
        ctx = get_run_context()
        trace_event("user_query", query=self.last_query)
        # Persist original query early (so you get it even mid-run)
        if ctx is not None and ctx.is_trace_enabled() and ctx.trace_config.save_query:
            artifacts.write_text("query.txt", self.last_query + "\n", append=False)

        # Deep research mode bypasses the normal router/agent loop.
        if ctx is not None and ctx.mode == RunMode.DEEP_RESEARCH:
            work_dir = (ctx.work_dir if ctx.work_dir else resolve_work_dir())
            if ctx.findings_file:
                ff = str(ctx.findings_file)
                if os.path.isabs(ff):
                    try:
                        findings_rel = os.path.relpath(ff, work_dir)
                    except Exception:
                        findings_rel = os.path.basename(ff)
                else:
                    findings_rel = ff
            else:
                findings_rel = "deep_research_findings.md"
            orch = DeepResearchOrchestrator(work_dir=work_dir, findings_relpath=findings_rel)
            self.current_agent = None
            self.is_generating = True
            trace_event("interaction_working", state="start", mode="deep_research")
            self.last_answer, self.last_reasoning = orch.run(self.last_query)
            trace_event("interaction_working", state="end", mode="deep_research")
            self.is_generating = False
            trace_event("final_answer", answer=self.last_answer)
            if ctx.trace_config.save_final_answer and self.last_answer:
                artifacts.write_text("final_answer.md", self.last_answer + "\n", append=False)
            return True

        agent = self.router.select_agent(self.last_query)
        if agent is None:
            return False
        # Reset per-run state so previous stop/failure doesn't leak into a new user query.
        try:
            if hasattr(agent, "reset_run_state"):
                agent.reset_run_state()
            else:
                agent.success = True
                agent.stop = False
        except Exception:
            pass
        # Ensure all tool/file writes for this query go under runs/<run_id>/...
        try:
            if hasattr(agent, "apply_run_context"):
                agent.apply_run_context()
        except Exception:
            pass
        if self.current_agent != agent and self.last_answer is not None:
            push_last_agent_memory = True
        tmp = self.last_answer
        self.current_agent = agent
        self.is_generating = True
        trace_event("interaction_working", state="start", mode="standard")
        trace_event("selected_agent", agent_type=agent.type, agent_name=agent.agent_name)
        self.last_answer, self.last_reasoning = await agent.process(self.last_query, self.speech)
        trace_event("interaction_working", state="end", mode="standard")
        self.is_generating = False
        if push_last_agent_memory:
            self.current_agent.memory.push('user', self.last_query)
            self.current_agent.memory.push('assistant', self.last_answer)
        if self.last_answer == tmp:
            self.last_answer = None
        trace_event("final_answer", answer=self.last_answer)
        # Persist final answer (even in standard mode if trace is on)
        if ctx is not None and ctx.is_trace_enabled() and ctx.trace_config.save_final_answer and self.last_answer:
            artifacts.write_text("final_answer.md", self.last_answer + "\n", append=False)
        return True

    async def drain_queue(self) -> None:
        """
        Process all queued queries sequentially.
        Intended for CLI usage.
        """
        while self.is_active and self.queued_len() > 0:
            nxt = self.pop_next_queued()
            if nxt is None:
                break
            self.set_query(nxt)
            await self.think()
    
    def get_updated_process_answer(self) -> str:
        """Get the answer from the last agent."""
        if self.current_agent is None:
            return None
        return self.current_agent.get_last_answer()
    
    def get_updated_block_answer(self) -> str:
        """Get the answer from the last agent."""
        if self.current_agent is None:
            return None
        return self.current_agent.get_last_block_answer()
    
    def speak_answer(self) -> None:
        """Speak the answer to the user in a non-blocking thread."""
        if self.last_query is None:
            return
        if self.tts_enabled and self.last_answer and self.speech:
            def speak_in_thread(speech_instance, text):
                speech_instance.speak(text)
            thread = threading.Thread(target=speak_in_thread, args=(self.speech, self.last_answer))
            thread.start()
    
    def show_answer(self) -> None:
        """Show the answer to the user."""
        if self.last_query is None:
            return
        if self.current_agent is not None:
            self.current_agent.show_answer()

