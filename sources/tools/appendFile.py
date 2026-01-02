import os
import sys

if __name__ == "__main__":  # pragma: no cover
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sources.tools.tools import Tools


class AppendFile(Tools):
    """
    Append text to a file under the configured work_dir.

    Tool block format:
    ```append_file
    path=some_folder/findings.md
    text=...text to append...
    ```
    """

    def __init__(self):
        super().__init__()
        self.tag = "append_file"
        self.name = "Append File"
        self.description = "Append text to a file under work_dir. Parameters: path=..., text=..."

    def _safe_target_path(self, requested_path: str) -> str:
        if requested_path is None or str(requested_path).strip() == "":
            raise ValueError("Missing required parameter: path")
        work_dir_abs = os.path.abspath(self.work_dir)
        p = str(requested_path).replace("\\", os.sep).replace("/", os.sep)
        p = p.lstrip(os.sep)
        target_path = os.path.abspath(os.path.join(work_dir_abs, p))
        if os.path.commonpath([work_dir_abs, target_path]) != work_dir_abs:
            raise ValueError(f"Refusing to write outside work_dir. path={requested_path}")
        return target_path

    def execute(self, blocks: list, safety: bool = False) -> str:
        if not blocks:
            return "Error: No append_file blocks provided."

        results = []
        for block in blocks:
            try:
                path = self.get_parameter_value(block, "path")
                text = self.get_parameter_value(block, "text")
                if text is None:
                    # Allow "content=" as alias
                    text = self.get_parameter_value(block, "content")
                if text is None:
                    raise ValueError("Missing required parameter: text")

                target = self._safe_target_path(path)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "a", encoding="utf-8") as f:
                    f.write(str(text))
                    if not str(text).endswith("\n"):
                        f.write("\n")
                results.append(f"Appended {len(str(text))} chars to {target}")
            except Exception as e:
                return f"Error: append_file failed: {type(e).__name__}: {str(e)}"
        return "\n".join(results)

    def execution_failure_check(self, output: str) -> bool:
        return output is None or str(output).startswith("Error:")

    def interpreter_feedback(self, output: str) -> str:
        if self.execution_failure_check(output):
            return f"[failure] {output}"
        return f"[success] {output}"
