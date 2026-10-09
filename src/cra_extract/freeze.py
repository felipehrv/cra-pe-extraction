"""Freeze the analysis plan before test labeling (design 6, 6.1).

The work rule is: pilot on dev, fix rules and prompts, freeze, label test blind,
evaluate test once. The freeze is the git tag ``plan-v1.0``; ``evaluate.py`` refuses
to score the test split until that tag exists. This module makes the tag only when
the pre-registration is complete:

1. ``docs/analysis_plan.md`` exists and carries the headings "Scoring matrix",
   "Tolerances", "Audit design" and "Expected CI widths". When the file is missing it
   is created as a template with those headings and ``TODO`` markers; the tag is
   refused until every marker is gone.
2. The working tree is clean (``git status --porcelain`` is empty), the test split
   has been drawn (``config/corpus.csv`` has ``split == test`` rows) and the hash of
   its doc_id list (``label.split_hash``) is already recorded in ``docs/hashes.md``
   (``cra label hash --split test``).
3. ``rules.RULES_VERSION``, ``prompts.PROMPT_VERSION``,
   ``prompts.CLOSED_BOOK_PROMPT_VERSION``, the effort per backend (``llm.DEFAULT_EFFORT``,
   part of the replay key) and the sha256 of ``config/fields.yaml`` and
   ``config/headings.yaml`` are appended to
   ``docs/hashes.md`` under a dated "Freeze" heading, and that one file is committed
   so the tag points at a commit that contains the record (``--no-commit`` leaves it
   in the working tree instead).
4. The annotated tag is created with a message listing those versions. An existing
   tag is refused unless ``--force-retag`` moves it (``git tag -f``); tags are never
   deleted.

CLI::

    python -m cra_extract.freeze --tag plan-v1.0 [--check-only] [--date YYYY-MM-DD]
        [--force-retag] [--no-commit] [--root DIR]

Exit codes: 0 frozen (or all checks pass with ``--check-only``), 1 a check failed,
2 git is unavailable or the arguments are invalid.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cra_extract import prompts, rules
from cra_extract.config import ROOT
from cra_extract.label import read_manifest, split_docs, split_hash
from cra_extract.models import DocRecord

PLAN_TAG = "plan-v1.0"
PLAN_HEADINGS = ("Scoring matrix", "Tolerances", "Audit design", "Expected CI widths")
TODO_MARKER = "TODO"

# Repo-relative paths; every function takes ``root`` so tests run in a temporary repo.
PLAN_REL = Path("docs/analysis_plan.md")
HASHES_REL = Path("docs/hashes.md")
MANIFEST_REL = Path("config/corpus.csv")
FIELDS_REL = Path("config/fields.yaml")
HEADINGS_REL = Path("config/headings.yaml")

_HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*#*\s*$")


class FreezeError(RuntimeError):
    """Raised when git cannot be run or refuses an operation."""


@dataclass(frozen=True)
class FreezeRecord:
    """What the freeze pins down: the versions and hashes written to ``docs/hashes.md``
    and into the tag message."""

    tag: str
    frozen_on: date
    rules_version: str
    prompt_version: str
    fields_sha256: str
    headings_sha256: str
    n_test_docs: int
    test_doc_hash: str
    closed_book_prompt_version: str = ""
    effort: str = ""

    def lines(self) -> list[str]:
        """The version list as ``key: value`` lines (shared by the record and the tag)."""
        extra = []
        if self.closed_book_prompt_version:
            extra.append(f"closed_book_prompt_version: {self.closed_book_prompt_version}")
        if self.effort:
            extra.append(f"effort per backend: {self.effort}")
        return [
            f"rules_version: {self.rules_version}",
            f"prompt_version: {self.prompt_version}",
            *extra,
            f"fields.yaml sha256: {self.fields_sha256}",
            f"headings.yaml sha256: {self.headings_sha256}",
            f"test split: {self.n_test_docs} documents, doc_id list sha256 {self.test_doc_hash}",
        ]

    def tag_message(self) -> str:
        """Annotated tag message: a subject line, a blank line, the version list."""
        return f"Freeze {self.tag} ({self.frozen_on.isoformat()})\n\n" + "\n".join(self.lines()) + "\n"


# ----------------------------------------------------------------------------- git


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``git`` in ``root``; a missing binary or a non-zero exit raises FreezeError."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError as e:
        raise FreezeError(f"cannot run git: {e}") from e
    if check and proc.returncode != 0:
        raise FreezeError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def is_git_repo(root: Path) -> bool:
    """True when ``root`` is inside a git working tree."""
    try:
        proc = _git(root, "rev-parse", "--is-inside-work-tree", check=False)
    except FreezeError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def dirty_paths(root: Path) -> list[str]:
    """Paths reported by ``git status --porcelain`` (modified, staged or untracked)."""
    out = _git(root, "status", "--porcelain").stdout
    return [line[3:] for line in out.splitlines() if line.strip()]


def tag_exists(root: Path, tag: str) -> bool:
    """True when the tag is present in the repo at ``root``."""
    return tag in _git(root, "tag", "-l", tag).stdout.split()


def create_tag(root: Path, tag: str, message: str, force: bool = False) -> None:
    """Create the annotated tag at HEAD. With ``force`` an existing tag is moved
    (``git tag -f``); nothing is ever deleted."""
    args = ["tag", "-a"]
    if force:
        args.append("-f")
    _git(root, *args, tag, "-m", message)


def commit_paths(root: Path, paths: list[Path], message: str) -> None:
    """Stage exactly ``paths`` (repo-relative) and commit them."""
    _git(root, "add", "--", *map(str, paths))
    _git(root, "commit", "-q", "-m", message, "--", *map(str, paths))


# ----------------------------------------------------------------------------- plan


def plan_template(headings: tuple[str, ...] = PLAN_HEADINGS) -> str:
    """Markdown skeleton of the analysis plan: one section per required heading with a
    ``TODO`` marker the author replaces."""
    parts = [
        "# Analysis plan\n",
        "Pre-registered evaluation protocol (design section 6). Tagged `plan-v1.0` by "
        "`cra freeze` before test labeling; later changes go to `docs/deviations.md`.\n",
    ]
    for h in headings:
        parts.append(f"## {h}\n")
        parts.append(f"{TODO_MARKER}: write this section, then remove the marker.\n")
    return "\n".join(parts)


def ensure_plan(path: Path) -> bool:
    """Create the template at ``path`` when the file is missing. Returns True when it
    was created."""
    path = Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan_template(), encoding="utf-8")
    return True


def markdown_headings(text: str) -> list[str]:
    """ATX heading texts of a Markdown document, in order, without the hashes."""
    out = []
    for line in text.splitlines():
        m = _HEADING_LINE.match(line)
        if m:
            out.append(m.group("text").strip())
    return out


def todo_lines(text: str) -> list[int]:
    """1-based line numbers that still contain the ``TODO`` marker."""
    return [i for i, line in enumerate(text.splitlines(), start=1) if TODO_MARKER in line]


def check_plan(path: Path, headings: tuple[str, ...] = PLAN_HEADINGS) -> list[str]:
    """Problems with the analysis plan: missing file, missing headings, TODO markers."""
    path = Path(path)
    if not path.exists():
        return [f"{path.name}: file is missing"]
    text = path.read_text(encoding="utf-8")
    present = {h.casefold() for h in markdown_headings(text)}
    problems = [f"{path.name}: heading {h!r} is missing" for h in headings if h.casefold() not in present]
    todos = todo_lines(text)
    if todos:
        shown = ", ".join(map(str, todos[:10])) + (", ..." if len(todos) > 10 else "")
        problems.append(f"{path.name}: {len(todos)} {TODO_MARKER} marker(s) remain (line {shown})")
    return problems


# ----------------------------------------------------------------------------- split and hashes


def sha256_file(path: Path) -> str:
    """sha256 of the file bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_test_split(manifest: list[DocRecord], hashes_path: Path) -> list[str]:
    """Problems with the test split: no test rows, or its doc_id hash not yet recorded in
    ``docs/hashes.md`` (``cra label hash --split test``)."""
    n = len(split_docs(manifest, "test"))
    if n == 0:
        return ["corpus.csv: no rows with split == test; draw the test split first"]
    h = split_hash(manifest, "test")
    hashes_path = Path(hashes_path)
    recorded = hashes_path.exists() and h in hashes_path.read_text(encoding="utf-8")
    if not recorded:
        return [
            f"{hashes_path.name}: test doc_id hash {h[:12]}... for {n} documents is not recorded; "
            "run cra label hash --split test"
        ]
    return []


def effort_line() -> str:
    """``llm.DEFAULT_EFFORT`` as ``backend=level`` pairs (``none`` when no effort is sent),
    the effort the run stage uses unless ``--effort`` overrides it."""
    from cra_extract.llm import DEFAULT_EFFORT

    return ", ".join(f"{b}={v or 'none'}" for b, v in sorted(DEFAULT_EFFORT.items()))


def collect_record(root: Path, tag: str, frozen_on: date, manifest: list[DocRecord]) -> FreezeRecord:
    """Gather the versions and hashes that the freeze pins down."""
    return FreezeRecord(
        tag=tag,
        frozen_on=frozen_on,
        rules_version=rules.RULES_VERSION,
        prompt_version=prompts.PROMPT_VERSION,
        closed_book_prompt_version=prompts.CLOSED_BOOK_PROMPT_VERSION,
        effort=effort_line(),
        fields_sha256=sha256_file(root / FIELDS_REL),
        headings_sha256=sha256_file(root / HEADINGS_REL),
        n_test_docs=len(split_docs(manifest, "test")),
        test_doc_hash=split_hash(manifest, "test"),
    )


def record_freeze(record: FreezeRecord, hashes_path: Path) -> None:
    """Append the record to ``docs/hashes.md`` under a dated ``Freeze`` heading. The
    file is created by ``label.record_hashes``; a missing file is created bare."""
    hashes_path = Path(hashes_path)
    hashes_path.parent.mkdir(parents=True, exist_ok=True)
    existing = hashes_path.read_text(encoding="utf-8") if hashes_path.exists() else "# Committed hashes\n"
    block = (
        f"\n## Freeze {record.frozen_on.isoformat()} ({record.tag})\n\n"
        + "\n".join(f"- {line}" for line in record.lines())
        + "\n"
    )
    sep = "" if existing.endswith("\n") else "\n"
    hashes_path.write_text(existing + sep + block, encoding="utf-8")


# ----------------------------------------------------------------------------- checks


def run_checks(root: Path, tag: str, force_retag: bool = False) -> list[str]:
    """Every precondition of the freeze, as a list of problems (empty means ready).

    Creates the analysis plan template when it is missing, which itself fails the
    check (TODO markers and a dirty tree) until the author fills and commits it.
    """
    root = Path(root)
    problems: list[str] = []
    if ensure_plan(root / PLAN_REL):
        problems.append(f"{PLAN_REL}: created as a template; fill every section and commit it")
    problems.extend(check_plan(root / PLAN_REL))

    if not is_git_repo(root):
        problems.append(f"{root}: not a git repository")
        return problems
    dirty = dirty_paths(root)
    if dirty:
        shown = ", ".join(dirty[:5]) + (", ..." if len(dirty) > 5 else "")
        problems.append(f"working tree is not clean ({len(dirty)} path(s): {shown}); commit or stash first")

    manifest_path = root / MANIFEST_REL
    if not manifest_path.exists():
        problems.append(f"{MANIFEST_REL}: file is missing")
    else:
        problems.extend(check_test_split(read_manifest(manifest_path), root / HASHES_REL))
    for rel in (FIELDS_REL, HEADINGS_REL):
        if not (root / rel).exists():
            problems.append(f"{rel}: file is missing")

    if tag_exists(root, tag) and not force_retag:
        problems.append(f"tag {tag} already exists; pass --force-retag to move it (tags are never deleted)")
    return problems


def freeze(
    root: Path,
    tag: str = PLAN_TAG,
    frozen_on: date | None = None,
    check_only: bool = False,
    force_retag: bool = False,
    commit: bool = True,
) -> tuple[list[str], FreezeRecord | None]:
    """Run the checks and, when they pass and ``check_only`` is false, record the
    versions, commit the record and create the tag.

    Returns ``(problems, record)``; ``record`` is None when nothing was written.
    """
    root = Path(root)
    problems = run_checks(root, tag, force_retag)
    if problems or check_only:
        return problems, None
    manifest = read_manifest(root / MANIFEST_REL)
    record = collect_record(root, tag, frozen_on or date.today(), manifest)
    record_freeze(record, root / HASHES_REL)
    if commit:
        commit_paths(root, [HASHES_REL], f"Freeze {tag}: record versions and hashes")
    create_tag(root, tag, record.tag_message(), force=force_retag)
    return [], record


# ----------------------------------------------------------------------------- CLI


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{s!r} is not a YYYY-MM-DD date") from e


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point; returns the process exit code."""
    p = argparse.ArgumentParser(prog="python -m cra_extract.freeze", description=__doc__.split("\n\n")[0])
    p.add_argument("--tag", default=PLAN_TAG, help=f"annotated tag to create (default {PLAN_TAG})")
    p.add_argument("--check-only", action="store_true", help="run the checks and write nothing")
    p.add_argument("--date", type=_parse_date, default=None, help="freeze date for the record (default today)")
    p.add_argument("--force-retag", action="store_true", help="move an existing tag instead of refusing")
    p.add_argument("--no-commit", action="store_true", help="leave the docs/hashes.md record uncommitted")
    p.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    a = p.parse_args(argv)

    try:
        problems, record = freeze(
            a.root,
            tag=a.tag,
            frozen_on=a.date,
            check_only=a.check_only,
            force_retag=a.force_retag,
            commit=not a.no_commit,
        )
    except FreezeError as e:
        print(f"freeze refused: {e}", file=sys.stderr)
        return 2
    for pr in problems:
        print(f"- {pr}")
    if problems:
        sys.stdout.flush()
        print(f"freeze {a.tag}: {len(problems)} problem(s); nothing written", file=sys.stderr)
        return 1
    if a.check_only:
        print(f"freeze {a.tag}: all checks pass (check only, nothing written)")
        return 0
    assert record is not None
    print(f"tagged {a.tag} at HEAD; record appended to {HASHES_REL}")
    for line in record.lines():
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
