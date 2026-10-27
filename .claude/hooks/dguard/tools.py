"""Инструменты, которые проверяет страж, и путь файла из `tool_input`."""


FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")
GUARDED_TOOLS = SHELL_TOOLS + FILE_TOOLS
FILE_KEYS = ("file_path", "notebook_path", "path", "filePath")


def file_target(tool_input):
    """Путь, который меняет файловый инструмент (Write/Edit/MultiEdit: `file_path`, NotebookEdit: `notebook_path`)."""
    for k in FILE_KEYS:
        v = tool_input.get(k) if isinstance(tool_input, dict) else None
        if isinstance(v, str) and v.strip():
            return v
    return None
