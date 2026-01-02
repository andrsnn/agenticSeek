
import os, sys
import re
from io import StringIO
import subprocess
import shlex

if __name__ == "__main__": # if running as a script for individual testing
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sources.tools.tools import Tools
from sources.tools.safety import is_any_unsafe

class BashInterpreter(Tools):
    """
    This class is a tool to allow agent for bash code execution.
    """
    def __init__(self):
        super().__init__()
        self.tag = "bash"
        self.name = "Bash Interpreter"
        self.description = "This tool allows the agent to execute bash commands."
        # IMPORTANT: lock down bash by default. This prevents the LLM from running
        # process-control/network/system commands (which can stop the server/container).
        self.safe_mode = True
        self._allowed_cmds = {
            # read-only
            "pwd", "ls", "cat", "head", "tail", "wc", "stat",
            # search / inspect
            "find", "grep",
            # basic file ops (within work_dir only)
            "mkdir", "touch", "cp", "mv",
            # misc
            "echo",
        }
        # Reject shell metacharacters entirely to avoid chaining / redirection / subshell.
        self._forbidden_shell_chars = set(";|&><`$(){}[]")
        self._forbidden_tokens = {"-exec", "xargs", "sudo", "su"}

    def _is_safe_rel_path(self, p: str) -> bool:
        """
        Only allow paths within the tool's work_dir. Disallow absolute paths and parent traversal.
        """
        if p is None:
            return False
        s = str(p).strip()
        if s == "" or s.startswith("-"):
            return True  # option, not a path
        # disallow absolute paths / drive paths
        if os.path.isabs(s):
            return False
        if ":" in s and sys.platform.startswith("win"):
            return False
        # normalize and ensure stays under work_dir
        wd = os.path.abspath(self.work_dir or os.getcwd())
        target = os.path.abspath(os.path.join(wd, s))
        try:
            return os.path.commonpath([wd, target]) == wd
        except Exception:
            return False

    def _validate_command(self, command: str) -> tuple[bool, str]:
        """
        Validate a single command string for safe execution.
        """
        cmd = (command or "").strip()
        if not cmd:
            return False, "Empty command."
        if any(ch in cmd for ch in self._forbidden_shell_chars) or "\n" in cmd or "\r" in cmd:
            return False, (
                "Shell metacharacters are not allowed in safe bash mode "
                "(no chaining/redirection like &&, >>, >, |). "
                "Use one simple command at a time, or use append_file for writing files."
            )
        try:
            argv = shlex.split(cmd, posix=True)
        except Exception:
            return False, "Failed to parse command."
        if not argv:
            return False, "Empty command."
        prog = argv[0]
        if prog not in self._allowed_cmds:
            return False, f"Command '{prog}' is not allowed (safe bash mode)."
        for tok in argv[1:]:
            if tok in self._forbidden_tokens:
                return False, f"Token '{tok}' is not allowed."
        # Path restrictions for commands that accept paths
        if prog in {"cat", "head", "tail", "stat", "wc"}:
            for a in argv[1:]:
                if not self._is_safe_rel_path(a):
                    return False, "Path outside work_dir is not allowed."
        if prog == "ls":
            # allow options and an optional path
            for a in argv[1:]:
                if not self._is_safe_rel_path(a):
                    return False, "Path outside work_dir is not allowed."
        if prog == "find":
            # disallow -exec, and restrict any explicit path args
            for a in argv[1:]:
                if a == "-exec":
                    return False, "find -exec is not allowed."
                if not self._is_safe_rel_path(a):
                    return False, "Path outside work_dir is not allowed."
        if prog == "grep":
            # allow grep PATTERN <file...> but restrict file paths; no recursive flags assumed safe.
            for a in argv[1:]:
                if a.startswith("-"):
                    continue
                # heuristic: treat non-flag args after pattern as file/path; restrict
                if not self._is_safe_rel_path(a):
                    return False, "Path outside work_dir is not allowed."
        if prog in {"mkdir", "touch"}:
            for a in argv[1:]:
                if not self._is_safe_rel_path(a):
                    return False, "Path outside work_dir is not allowed."
        if prog in {"cp", "mv"}:
            # require at least 2 operands and keep them within work_dir
            ops = [a for a in argv[1:] if not a.startswith("-")]
            if len(ops) < 2:
                return False, f"{prog} requires source and destination paths."
            for a in ops:
                if not self._is_safe_rel_path(a):
                    return False, "Path outside work_dir is not allowed."
        return True, ""
    
    def language_bash_attempt(self, command: str):
        """
        Detect if AI attempt to run the code using bash.
        If so, return True, otherwise return False.
        Code written by the AI will be executed automatically, so it should not use bash to run it.
        """
        lang_interpreter = ["python", "gcc", "g++", "mvn", "go", "java", "javac", "rustc", "clang", "clang++", "rustc", "rustc++", "rustc++"]
        for word in command.split():
            if any(word.startswith(lang) for lang in lang_interpreter):
                return True
        return False
    
    def execute(self, commands: str, safety=False, timeout=300):
        """
        Execute bash commands and display output in real-time.
        """
        if safety and input("Execute command? y/n ") != "y":
            return "Command rejected by user."
    
        concat_output = ""
        for command in commands:
            command = command.replace('\n', '')
            if self.safe_mode:
                ok, reason = self._validate_command(command)
                if not ok:
                    return f"Unsafe command blocked (safe bash mode): {command}\nReason: {reason}"
            if self.language_bash_attempt(command) and self.allow_language_exec_bash == False:
                continue
            try:
                argv = shlex.split(command, posix=True)
                res = subprocess.run(
                    argv,
                    cwd=self.work_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    timeout=timeout,
                    shell=False,
                )
                out = res.stdout or ""
                if res.returncode != 0:
                    return f"Command {command} failed with return code {res.returncode}:\n{out}"
                concat_output += f"Output of {command}:\n{out.strip()}\n"
            except subprocess.TimeoutExpired:
                return f"Command {command} timed out."
            except Exception as e:
                return f"Command {command} failed:\n{str(e)}"
        return concat_output

    def interpreter_feedback(self, output):
        """
        Provide feedback based on the output of the bash interpreter
        """
        if self.execution_failure_check(output):
            feedback = f"[failure] Error in execution:\n{output}"
        else:
            feedback = "[success] Execution success, code output:\n" + output
        return feedback

    def execution_failure_check(self, output):
        """
        Check if a bash command failed.

        IMPORTANT: Do not use broad keyword matching; normal command output can contain
        words like "not found" (e.g., search results) even when the command succeeded.
        We rely on the explicit error strings produced by this tool's execute().
        """
        if output is None:
            return True
        text = str(output)

        # Explicit error formats returned by execute()
        if text.startswith("Command rejected by user."):
            return True
        if "Unsafe command:" in text:
            return True
        if " failed with return code " in text:
            return True
        if " timed out." in text or " timed out" in text:
            return True
        if "Command " in text and " failed:\n" in text:
            return True

        return False

if __name__ == "__main__":
    bash = BashInterpreter()
    print(bash.execute(["ls", "pwd", "ip a", "nmap -sC 127.0.0.1"]))
