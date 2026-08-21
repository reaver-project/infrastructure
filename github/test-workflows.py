import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
yaml_files = [
    *sorted((root / ".github" / "workflows").glob("*.yml")),
    *sorted((root / "actions").glob("*/action.yml")),
]
if not yaml_files:
    sys.exit("No GitHub Actions workflows found.")

for yaml_file in yaml_files:
    contents = yaml_file.read_text(encoding="utf-8")
    for line_number, line in enumerate(contents.splitlines(), start=1):
        if "uses:" not in line:
            continue
        action = line.split("uses:", 1)[1].split("#", 1)[0].strip()
        if action.startswith("./"):
            continue
        if "@" not in action or len(action.rsplit("@", 1)[1]) != 40:
            sys.exit(f"{yaml_file}:{line_number}: action is not pinned by SHA: {action}")

print("GitHub Actions workflow tests passed.")
