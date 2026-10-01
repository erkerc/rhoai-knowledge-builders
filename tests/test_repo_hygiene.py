"""Keeps private material out of this repository.

The `.gitignore` is an allowlist: it ignores everything and re-includes only the
tool's own code. This script asks git which files that allowlist would ignore
(`git check-ignore --no-index`, which looks at the rules rather than the index)
and fails if any of them are tracked, staged, or anywhere in history. The
`.gitignore` therefore stays the single source of truth.

    python tests/test_repo_hygiene.py             # self-test + check tracked files (CI)
    python tests/test_repo_hygiene.py --staged    # what is about to be committed (pre-commit hook)
    python tests/test_repo_hygiene.py --history   # every path ever committed, on any branch
    python tests/test_repo_hygiene.py --filter-repo-args   # the command that purges everything else
"""

import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Set

ROOT = Path(__file__).resolve().parents[1]

#: The same allowlist as .gitignore, as regexes, for rewriting history with
#: `git filter-repo --path-regex`. A self-test below checks the two agree, so
#: they cannot drift apart unnoticed.
KEEP_REGEXES = [
    r"^rhkb/.*\.py$",
    r"^tests/.*\.py$",
    r"^\.github/workflows/[^/]*\.yml$",
    r"^\.githooks/[^/]*$",
    r"^(\.gitignore|LICENSE|README\.md|CLAUDE\.md|pyproject\.toml|requirements\.txt|sources\.yaml)$",
]


def kept_by_regex(path: str) -> bool:
    return any(re.search(rx, path) for rx in KEEP_REGEXES)


def filter_repo_command() -> str:
    """The git filter-repo invocation that keeps only allowlisted paths, in all history."""
    parts = ["git", "filter-repo", "--force"]
    for rx in KEEP_REGEXES:
        parts += ["--path-regex", rx]
    return " ".join(shlex.quote(x) for x in parts)


def git(args: Sequence[str], cwd: Path = ROOT, stdin: Optional[bytes] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), input=stdin, capture_output=True)


def in_git_repo(cwd: Path = ROOT) -> bool:
    return shutil.which("git") is not None and git(["rev-parse", "--git-dir"], cwd).returncode == 0


def ignored_by_allowlist(paths: Sequence[str], cwd: Path = ROOT) -> List[str]:
    """Paths the .gitignore allowlist would ignore, i.e. paths that must not be committed."""
    paths = [p for p in dict.fromkeys(paths) if p]
    if not paths:
        return []
    proc = git(["check-ignore", "--no-index", "--stdin", "-z"], cwd,
               stdin=("\0".join(paths) + "\0").encode("utf-8"))
    # exit 0 = some ignored, 1 = none ignored; anything else is a real error
    if proc.returncode not in (0, 1):
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip())
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


def _names(proc: subprocess.CompletedProcess) -> List[str]:
    return [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]


def tracked_files(cwd: Path = ROOT) -> List[str]:
    return _names(git(["ls-files", "-z"], cwd))


def staged_files(cwd: Path = ROOT) -> List[str]:
    return _names(git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], cwd))


def historic_files(cwd: Path = ROOT) -> List[str]:
    return _names(git(["log", "--all", "--name-only", "--pretty=format:", "-z"], cwd))


def report(title: str, offenders: List[str]) -> int:
    if not offenders:
        return 0
    print(f"\n{title}", file=sys.stderr)
    for path in sorted(offenders):
        print(f"  {path}", file=sys.stderr)
    return 1


# ------------------------------------------------------------------ modes ---
def run_staged() -> int:
    if not in_git_repo():
        return 0
    bad = ignored_by_allowlist(staged_files())
    code = report("refusing to commit files outside the .gitignore allowlist:", bad)
    if code:
        print("\nThis repository holds the rhkb tool only. Keep raw/, wiki/, notes and exports in\n"
              "your RHKB_HOME workspace. To track a new kind of file on purpose, add it to the\n"
              "allowlist in .gitignore first.", file=sys.stderr)
    return code


def run_history() -> int:
    if not in_git_repo():
        print("not a git repository", file=sys.stderr)
        return 2
    bad = ignored_by_allowlist(historic_files())
    if not bad:
        print("history is clean: every path ever committed is inside the allowlist")
        return 0
    report("paths in history that are outside the allowlist:", bad)
    print(f"\n{len(bad)} path(s). Removing them from a clone is not enough; the history has to be rewritten.",
          file=sys.stderr)
    return 1


def check(label: str, condition: bool, detail: object = "") -> bool:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}{('  ' + str(detail)) if not condition else ''}")
    return bool(condition)


def self_test() -> bool:
    """Prove the allowlist does what it says, using the real .gitignore."""
    ok = True
    if not shutil.which("git"):
        print("[SKIP] git is not installed")
        return True

    code_files = [
        ".gitignore", "LICENSE", "README.md", "CLAUDE.md", "pyproject.toml", "requirements.txt",
        "sources.yaml", "rhkb/__init__.py", "rhkb/cli.py", "rhkb/sources/deep.py",
        "tests/test_offline.py", ".github/workflows/tests.yml", ".githooks/pre-commit",
    ]
    private_files = [
        # the workspace layout
        "raw/rhoai/3.5/serving.md", "wiki/index.md", "wiki/log.md", "wiki/pages/customer-acme.md",
        "state.json", "brief.md", "briefs/session-4.md",
        # things people actually drop into a repo
        "notes/chats/call-with-customer.md", "conversations/transcript.txt", "customers/acme.md",
        "chat-export.json", "Untitled.docx", "export.pdf", "secrets.txt",
        # agent and environment state
        ".env", ".env.local", ".claude/settings.local.json", "CLAUDE.local.md", ".vscode/settings.json",
        # hidden inside directories that DO hold code
        "rhkb/notes.md", "rhkb/data.json", "rhkb/sources/transcript.txt",
        "tests/fixture-chat.json", "tests/data/customer.yaml",
        # lookalikes
        "README.txt", "sources.yml", "wiki.py", "raw.py.bak",
    ]

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        git(["init", "-q", "."], repo)
        shutil.copyfile(ROOT / ".gitignore", repo / ".gitignore")
        for name in code_files + private_files:
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text("x\n", encoding="utf-8")
        git(["add", "-A"], repo)
        tracked = set(tracked_files(repo))

        missing = [f for f in code_files if f not in tracked]
        leaked = [f for f in private_files if f in tracked]
        ok &= check("every code file is tracked by `git add -A`", not missing, missing)
        ok &= check("no private file is tracked by `git add -A`", not leaked, leaked)
        ok &= check("check-ignore --no-index flags exactly the private files",
                    set(ignored_by_allowlist(code_files + private_files, repo)) == set(private_files),
                    sorted(set(ignored_by_allowlist(code_files + private_files, repo)) ^ set(private_files)))

        everything = code_files + private_files
        disagree = [f for f in everything
                    if kept_by_regex(f) != (f not in set(ignored_by_allowlist(everything, repo)))]
        ok &= check("the filter-repo keep-regexes agree with the .gitignore allowlist",
                    not disagree, disagree)

        # `git add -f` defeats .gitignore; the staged check is what catches it.
        git(["add", "-f", "wiki/pages/customer-acme.md", "notes/chats/call-with-customer.md"], repo)
        forced = ignored_by_allowlist(staged_files(repo), repo)
        ok &= check("a force-added private file is caught by the staged check",
                    set(forced) == {"wiki/pages/customer-acme.md", "notes/chats/call-with-customer.md"}, forced)

    return bool(ok)


def check_tracked() -> bool:
    if not in_git_repo():
        print("[SKIP] not a git checkout, so there is no tracked-file list to audit")
        return True
    bad = ignored_by_allowlist(tracked_files())
    return check("every tracked file is inside the .gitignore allowlist", not bad, bad)


def main(argv: Sequence[str]) -> int:
    if "--staged" in argv:
        return run_staged()
    if "--history" in argv:
        return run_history()
    if "--filter-repo-args" in argv:
        print(filter_repo_command())
        return 0
    ok = self_test()
    ok = check_tracked() and ok
    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
