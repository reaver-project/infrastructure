#!/usr/bin/env python3

import ast
import pathlib

root = pathlib.Path(__file__).resolve().parent
excluded_directories = {".git", ".ruff_cache", ".venv", "__pycache__", "build"}
forbidden_commands = {"aws", "gh"}


def command_name(node):
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return None
    executable = node.elts[0]
    if not isinstance(executable, ast.Constant) or not isinstance(executable.value, str):
        return None
    return pathlib.PurePath(executable.value).name


violations = []
for path in root.rglob("*.py"):
    if excluded_directories.intersection(path.relative_to(root).parts):
        continue
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        function = call.func
        if (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "subprocess"
            and call.args
            and command_name(call.args[0]) in forbidden_commands
        ):
            violations.append(f"{path.relative_to(root)}:{call.lineno}")

if violations:
    raise SystemExit(
        "Python must use AWS and GitHub APIs directly instead of spawning aws or gh:\n"
        + "\n".join(violations)
    )

print("Python API boundary tests passed.")
