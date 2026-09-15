import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def repository_root():
    return Path(__file__).resolve().parents[3]


def git(*arguments):
    result = subprocess.run(  # noqa: S603 - arguments remain a shell-free argv vector.
        ["git", "-C", str(repository_root()), *arguments],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def repository_identity():
    status = git("status", "--porcelain", "--untracked-files=all")
    if status:
        fail("Refusing to plan or apply from a dirty infrastructure worktree.")
    return git("rev-parse", "HEAD")


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create(kind, account_id, config_paths, state):
    root = repository_root()
    return {
        "schema_version": 1,
        "kind": kind,
        "repository_revision": repository_identity(),
        "management_account_id": account_id,
        "config_sha256": {
            path.relative_to(root).as_posix(): file_digest(path)
            for path in sorted(config_paths, key=str)
        },
        "state": state,
    }


def write(path, plan):
    path = path.resolve()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(plan, file, indent=2, sort_keys=True)
            file.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def read(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        if error.errno == errno.ELOOP:
            fail("The reviewed plan file must be a regular private file.")
        raise
    try:
        path_state = os.fstat(descriptor)
        if not stat.S_ISREG(path_state.st_mode) or path_state.st_mode & 0o077:
            fail("The reviewed plan file must not be accessible by group or others.")
        with os.fdopen(descriptor, encoding="utf-8") as file:
            descriptor = None
            return json.load(file)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def require_match(reviewed, current):
    if reviewed != current:
        fail("Live state, desired state, or repository revision changed after planning.")
