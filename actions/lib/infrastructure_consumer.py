#!/usr/bin/env python3

import argparse
import dataclasses
import os
import pathlib
import re
import shutil
import subprocess
import sys

revision_pattern = re.compile(r"^[0-9a-f]{40}$")
contract_version_pattern = re.compile(r"^[1-9][0-9]*$")
action_reference_pattern = re.compile(
    r"^(?P<prefix>\s*uses:\s*"
    r"reaver-project/infrastructure/actions/[^@\s#]+@)"
    r"(?P<revision>[0-9a-f]{40})(?P<suffix>\s*(?:#.*)?)$",
    re.MULTILINE,
)
infrastructure_reference_pattern = re.compile(
    r"^\s*uses:\s*reaver-project/infrastructure/actions/[^\s#]+",
    re.MULTILINE,
)
contract_input_pattern = re.compile(
    r"^(?P<prefix>\s*expected-contract-version:\s*)"
    r"(?P<quote>['\"]?)(?P<version>[1-9][0-9]*)(?P=quote)"
    r"(?P<suffix>\s*(?:#.*)?)$",
    re.MULTILINE,
)
contract_input_reference_pattern = re.compile(
    r"^\s*expected-contract-version:\s*[^\s#]+",
    re.MULTILINE,
)


def git_local_environment_names():
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required")
    return subprocess.run(  # noqa: S603 - fixed Git introspection command.
        [git, "rev-parse", "--local-env-vars"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


git_local_environment = tuple(git_local_environment_names())


def isolated_git_environment():
    environment = os.environ.copy()
    for name in git_local_environment:
        environment.pop(name, None)
    return environment


@dataclasses.dataclass(frozen=True)
class ConsumerState:
    revision: str
    contract_version: str
    workflow_files: frozenset[str]
    action_reference_count: int
    contract_input_count: int


def relative_file(root: pathlib.Path, relative_path: str) -> pathlib.Path:
    candidate = pathlib.PurePosixPath(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValueError(f"invalid consumer path: {relative_path}")
    path = root.joinpath(*candidate.parts)
    try:
        resolved_path = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError(f"consumer path does not exist: {relative_path}") from error
    if (
        resolved_path != path.absolute()
        or not resolved_path.is_relative_to(root)
        or not path.is_file()
    ):
        raise ValueError(f"consumer path is not a regular file: {relative_path}")
    return path


def workflow_paths(root: pathlib.Path) -> list[pathlib.Path]:
    workflow_directory = root / ".github" / "workflows"
    if (
        workflow_directory.resolve() != workflow_directory.absolute()
        or not workflow_directory.is_dir()
    ):
        raise ValueError("consumer workflow directory is missing or is a symlink")
    paths = sorted([*workflow_directory.glob("*.yml"), *workflow_directory.glob("*.yaml")])
    for path in paths:
        if path.resolve() != path.absolute() or not path.is_file():
            raise ValueError(f"consumer workflow is not a regular file: {path.name}")
    return paths


def read_state(root: pathlib.Path, revision_file: str, contract_version_file: str) -> ConsumerState:
    root = root.resolve()
    revision = relative_file(root, revision_file).read_text(encoding="utf-8").strip()
    contract_version = (
        relative_file(root, contract_version_file).read_text(encoding="utf-8").strip()
    )
    if not revision_pattern.fullmatch(revision):
        raise ValueError(f"invalid recorded infrastructure revision: {revision}")
    if not contract_version_pattern.fullmatch(contract_version):
        raise ValueError(f"invalid recorded contract version: {contract_version}")

    referenced_workflows = set()
    infrastructure_reference_count = 0
    action_revisions = []
    contract_input_reference_count = 0
    contract_versions = []
    for workflow_path in workflow_paths(root):
        contents = workflow_path.read_text(encoding="utf-8")
        workflow_revisions = [
            match.group("revision") for match in action_reference_pattern.finditer(contents)
        ]
        workflow_contract_versions = [
            match.group("version") for match in contract_input_pattern.finditer(contents)
        ]
        workflow_infrastructure_reference_count = len(
            infrastructure_reference_pattern.findall(contents)
        )
        workflow_contract_input_reference_count = len(
            contract_input_reference_pattern.findall(contents)
        )
        if workflow_infrastructure_reference_count or workflow_contract_input_reference_count:
            referenced_workflows.add(workflow_path.relative_to(root).as_posix())
        infrastructure_reference_count += workflow_infrastructure_reference_count
        action_revisions.extend(workflow_revisions)
        contract_input_reference_count += workflow_contract_input_reference_count
        contract_versions.extend(workflow_contract_versions)

    if not infrastructure_reference_count:
        raise ValueError("consumer has no shared infrastructure action references")
    if len(action_revisions) != infrastructure_reference_count:
        raise ValueError("a shared infrastructure action is not pinned by full Git SHA")
    if not contract_input_reference_count:
        raise ValueError("consumer has no infrastructure contract inputs")
    if len(contract_versions) != contract_input_reference_count:
        raise ValueError("an infrastructure contract input is not a literal version")
    if any(candidate != revision for candidate in action_revisions):
        raise ValueError("a shared infrastructure action does not use the recorded revision")
    if any(candidate != contract_version for candidate in contract_versions):
        raise ValueError("an infrastructure action does not use the recorded contract version")

    return ConsumerState(
        revision=revision,
        contract_version=contract_version,
        workflow_files=frozenset(referenced_workflows),
        action_reference_count=len(action_revisions),
        contract_input_count=len(contract_versions),
    )


def require_expected_state(
    state: ConsumerState, expected_revision: str, expected_contract_version: str
):
    if not revision_pattern.fullmatch(expected_revision):
        raise ValueError(f"invalid expected infrastructure revision: {expected_revision}")
    if not contract_version_pattern.fullmatch(expected_contract_version):
        raise ValueError(f"invalid expected contract version: {expected_contract_version}")
    if state.revision != expected_revision:
        raise ValueError("recorded infrastructure revision does not match the deployed revision")
    if state.contract_version != expected_contract_version:
        raise ValueError("recorded contract version does not match the deployed contract")


def replace_workflow_values(contents: str, revision: str, contract_version: str) -> str:
    contents = action_reference_pattern.sub(
        lambda match: f"{match.group('prefix')}{revision}{match.group('suffix')}",
        contents,
    )
    return contract_input_pattern.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}{contract_version}"
            f"{match.group('quote')}{match.group('suffix')}"
        ),
        contents,
    )


def normalized_workflow(contents: str) -> str:
    return replace_workflow_values(
        contents,
        "0" * 40,
        "1",
    )


def rewrite_consumer(
    root: pathlib.Path,
    revision_file: str,
    contract_version_file: str,
    revision: str,
    contract_version: str,
) -> list[str]:
    root = root.resolve()
    read_state(root, revision_file, contract_version_file)
    if not revision_pattern.fullmatch(revision):
        raise ValueError(f"invalid replacement infrastructure revision: {revision}")
    if not contract_version_pattern.fullmatch(contract_version):
        raise ValueError(f"invalid replacement contract version: {contract_version}")

    changed_files = []
    replacements = {
        revision_file: f"{revision}\n",
        contract_version_file: f"{contract_version}\n",
    }
    for relative_path, contents in replacements.items():
        path = relative_file(root, relative_path)
        if path.read_text(encoding="utf-8") != contents:
            path.write_text(contents, encoding="utf-8")
            changed_files.append(relative_path)

    for workflow_path in workflow_paths(root):
        contents = workflow_path.read_text(encoding="utf-8")
        updated_contents = replace_workflow_values(contents, revision, contract_version)
        if contents != updated_contents:
            workflow_path.write_text(updated_contents, encoding="utf-8")
            changed_files.append(workflow_path.relative_to(root).as_posix())

    require_expected_state(
        read_state(root, revision_file, contract_version_file),
        revision,
        contract_version,
    )
    return sorted(changed_files)


def git_output(root: pathlib.Path, arguments: list[str]) -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required")
    return subprocess.run(  # noqa: S603 - arguments are internal git subcommands.
        [git, "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        env=isolated_git_environment(),
        text=True,
    ).stdout


def validate_strict_update(
    root: pathlib.Path,
    base_revision: str,
    state: ConsumerState,
    revision_file: str,
    contract_version_file: str,
):
    if not revision_pattern.fullmatch(base_revision):
        raise ValueError(f"invalid base revision: {base_revision}")
    head_revision = git_output(root, ["rev-parse", "HEAD"]).strip()
    if not revision_pattern.fullmatch(head_revision):
        raise ValueError("consumer checkout does not have a full Git head revision")

    changed_files = set(
        filter(
            None,
            git_output(
                root,
                ["diff", "--name-only", "--diff-filter=ACMR", base_revision, head_revision],
            ).splitlines(),
        )
    )
    allowed_files = {
        revision_file,
        contract_version_file,
        *state.workflow_files,
    }
    unrelated_files = changed_files - allowed_files
    if unrelated_files:
        raise ValueError(
            "infrastructure update changes unrelated files: " + ", ".join(sorted(unrelated_files))
        )
    if revision_file not in changed_files:
        raise ValueError("infrastructure update does not change its recorded revision")

    for workflow_file in changed_files & state.workflow_files:
        base_contents = git_output(root, ["show", f"{base_revision}:{workflow_file}"])
        candidate_contents = (root / workflow_file).read_text(encoding="utf-8")
        if normalized_workflow(base_contents) != normalized_workflow(candidate_contents):
            raise ValueError(f"infrastructure update makes non-contract changes to {workflow_file}")


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Rewrite or validate an infrastructure action consumer."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_consumer_arguments(subparser):
        subparser.add_argument("--root", required=True, type=pathlib.Path)
        subparser.add_argument("--revision-file", required=True)
        subparser.add_argument("--contract-version-file", required=True)

    rewrite_parser = subparsers.add_parser("rewrite")
    add_consumer_arguments(rewrite_parser)
    rewrite_parser.add_argument("--revision", required=True)
    rewrite_parser.add_argument("--contract-version", required=True)

    validate_parser = subparsers.add_parser("validate")
    add_consumer_arguments(validate_parser)
    validate_parser.add_argument("--expected-revision", required=True)
    validate_parser.add_argument("--expected-contract-version", required=True)
    validate_parser.add_argument("--base-revision")
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    try:
        if arguments.command == "rewrite":
            changed_files = rewrite_consumer(
                arguments.root,
                arguments.revision_file,
                arguments.contract_version_file,
                arguments.revision,
                arguments.contract_version,
            )
            for changed_file in changed_files:
                print(changed_file)
            return

        state = read_state(
            arguments.root,
            arguments.revision_file,
            arguments.contract_version_file,
        )
        require_expected_state(
            state,
            arguments.expected_revision,
            arguments.expected_contract_version,
        )
        if arguments.base_revision:
            validate_strict_update(
                arguments.root,
                arguments.base_revision,
                state,
                arguments.revision_file,
                arguments.contract_version_file,
            )
        print(
            "Validated "
            f"{state.action_reference_count} shared action references and "
            f"{state.contract_input_count} contract inputs."
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
