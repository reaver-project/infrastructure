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

all_workflows = "\n".join(
    workflow.read_text(encoding="utf-8")
    for workflow in sorted((root / ".github" / "workflows").glob("*.yml"))
)
for public_artifact_action in ("actions/upload-artifact@", "actions/download-artifact@"):
    if public_artifact_action in all_workflows:
        sys.exit(f"AWS deployment plans must not use {public_artifact_action}")

plan_workflow = (root / ".github" / "workflows" / "aws-infrastructure.yml").read_text(
    encoding="utf-8"
)
if "change_set_arn" in plan_workflow or "change_set_type" in plan_workflow:
    sys.exit("The public planning workflow exposes private change-set metadata.")

deploy_workflow = (
    root / ".github" / "workflows" / "aws-infrastructure-deploy.yml"
).read_text(encoding="utf-8")
if "plan_run_id" in deploy_workflow or "plan_key" not in deploy_workflow:
    sys.exit("The deployment workflow does not consume the opaque plan key.")

print("GitHub Actions workflow tests passed.")
