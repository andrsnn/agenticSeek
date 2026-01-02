#!/usr/bin python3

import sys
import argparse
import configparser
import asyncio
import os

from sources.llm_provider import Provider
from sources.interaction import Interaction
from sources.agents import Agent, CoderAgent, CasualAgent, FileAgent, PlannerAgent, BrowserAgent, McpAgent
from sources.browser import Browser, create_driver
from sources.utility import pretty_print
from sources.runtime_context import RunContext, RunMode, set_run_context
from sources.trace_sink import TraceSink
from sources.workdir import resolve_work_dir

import warnings
warnings.filterwarnings("ignore")

config = configparser.ConfigParser()
config.read('config.ini')

async def main():
    parser = argparse.ArgumentParser(description="AgenticSeek CLI")
    parser.add_argument("--mode", choices=[m.value for m in RunMode], default=None, help="Run mode: standard, trace, deep_research")
    parser.add_argument("--work-dir", default=None, help="Workspace dir for outputs (defaults to WORK_DIR or config.ini [MAIN].work_dir)")
    parser.add_argument("--trace-file", default=None, help="Trace output file (relative to work-dir unless absolute)")
    parser.add_argument("--findings-file", default=None, help="Findings output file for deep research (relative to work-dir unless absolute)")
    args = parser.parse_args()

    pretty_print("Initializing...", color="status")
    stealth_mode = config.getboolean('BROWSER', 'stealth_mode')
    personality_folder = "jarvis" if config.getboolean('MAIN', 'jarvis_personality') else "base"
    languages = config["MAIN"]["languages"].split(' ')

    work_dir = args.work_dir or resolve_work_dir()
    mode_str = args.mode or config.get("MAIN", "run_mode", fallback=RunMode.STANDARD.value)
    mode = RunMode(mode_str) if mode_str in [m.value for m in RunMode] else RunMode.STANDARD

    trace_file = args.trace_file
    findings_file = args.findings_file
    if mode in (RunMode.TRACE, RunMode.DEEP_RESEARCH):
        if not trace_file:
            trace_file = os.path.join(work_dir, "agenticseek_trace.jsonl")
        else:
            if not os.path.isabs(trace_file):
                trace_file = os.path.join(work_dir, trace_file)
    if mode == RunMode.DEEP_RESEARCH:
        if not findings_file:
            findings_file = os.path.join(work_dir, "deep_research_findings.md")
        else:
            if not os.path.isabs(findings_file):
                findings_file = os.path.join(work_dir, findings_file)

    trace_sink = TraceSink(trace_file) if trace_file else None
    set_run_context(RunContext(mode=mode, work_dir=work_dir, trace_file=trace_file, findings_file=findings_file, trace_sink=trace_sink))

    provider = Provider(provider_name=config["MAIN"]["provider_name"],
                        model=config["MAIN"]["provider_model"],
                        server_address=config["MAIN"]["provider_server_address"],
                        is_local=config.getboolean('MAIN', 'is_local'))

    browser = Browser(
        create_driver(headless=config.getboolean('BROWSER', 'headless_browser'), stealth_mode=stealth_mode, lang=languages[0]),
        anticaptcha_manual_install=stealth_mode
    )

    agents = [
        CasualAgent(name=config["MAIN"]["agent_name"],
                    prompt_path=f"prompts/{personality_folder}/casual_agent.txt",
                    provider=provider, verbose=False),
        CoderAgent(name="coder",
                   prompt_path=f"prompts/{personality_folder}/coder_agent.txt",
                   provider=provider, verbose=False),
        FileAgent(name="File Agent",
                  prompt_path=f"prompts/{personality_folder}/file_agent.txt",
                  provider=provider, verbose=False),
        BrowserAgent(name="Browser",
                     prompt_path=f"prompts/{personality_folder}/browser_agent.txt",
                     provider=provider, verbose=False, browser=browser),
        PlannerAgent(name="Planner",
                     prompt_path=f"prompts/{personality_folder}/planner_agent.txt",
                     provider=provider, verbose=False, browser=browser),
        #McpAgent(name="MCP Agent",
        #            prompt_path=f"prompts/{personality_folder}/mcp_agent.txt",
        #            provider=provider, verbose=False), # NOTE under development
    ]

    interaction = Interaction(agents,
                              tts_enabled=config.getboolean('MAIN', 'speak'),
                              stt_enabled=config.getboolean('MAIN', 'listen'),
                              recover_last_session=config.getboolean('MAIN', 'recover_last_session'),
                              langs=languages
                            )
    try:
        while interaction.is_active:
            interaction.get_user()
            # Simple queue command for CLI: ":queue <prompt>" (or "/queue <prompt>")
            if interaction.last_query and (interaction.last_query.startswith(":queue ") or interaction.last_query.startswith("/queue ")):
                q = interaction.last_query.split(" ", 1)[1] if " " in interaction.last_query else ""
                qlen = interaction.enqueue(q)
                pretty_print(f"Queued ({qlen}): {q}", color="status")
                continue
            if await interaction.think():
                interaction.show_answer()
                interaction.speak_answer()
                # After each completion, automatically drain any queued prompts.
                await interaction.drain_queue()
    except Exception as e:
        if config.getboolean('MAIN', 'save_session'):
            interaction.save_session()
        raise e
    finally:
        if config.getboolean('MAIN', 'save_session'):
            interaction.save_session()

if __name__ == "__main__":
    asyncio.run(main())