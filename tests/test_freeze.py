"""Tests for cra_extract.freeze in a temporary git repository.

Every test builds its own repo under ``tmp_path`` (git init, one commit, a fake
manifest with dev and test rows, a fake analysis plan, fake config files) and runs git
there through ``cwd``; the real checkout's tags are never read or written. The global
and system git configs are disabled so signing or hooks on the developer's machine do
not change the outcome.
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

from cra_extract import freeze, label, prompts, rules

TEST_IDS = ["FDIC_2001_20240101", "OCC_1002_20240202", "FRB_3003_20240303"]
DEV_IDS = ["OCC_1001_20240404"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def _manifest_csv(test_ids: list[str] = TEST_IDS, dev_ids: list[str] = DEV_IDS) -> str:
    lines = ["doc_id,regulator,bank_id,bank_name,exam_date,method,overall_rating,url,split"]
    for split, ids in (("dev", dev_ids), ("test", test_ids)):
        for doc_id in ids:
            reg, bank_id, ymd = doc_id.split("_")
            exam = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
            lines.append(f"{doc_id},{reg},{bank_id},{reg} Bank {bank_id},{exam},small,S,https://example.invalid/{bank_id}.pdf,{split}")
    return "\n".join(lines) + "\n"


def _plan(complete: bool = True, drop: str | None = None) -> str:
    parts = ["# Analysis plan\n"]
    for h in freeze.PLAN_HEADINGS:
        if h == drop:
            continue
        parts.append(f"## {h}\n")
        parts.append("Written.\n" if complete else "TODO: write this.\n")
    return "\n".join(parts)


def _write(repo: Path, rel: str, text: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _commit_all(repo: Path, msg: str = "commit") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A ready-to-freeze repository: clean tree, complete plan, test split drawn and
    its hash recorded, config files present, no tags."""
    empty_cfg = tmp_path / "gitconfig"
    empty_cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "tester")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "tester@example.invalid")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "config/corpus.csv", _manifest_csv())
    _write(repo, "config/fields.yaml", "fields: {}\n")
    _write(repo, "config/headings.yaml", "OCC: []\n")
    _write(repo, "docs/analysis_plan.md", _plan(complete=True))
    manifest = label.read_manifest(repo / "config/corpus.csv")
    label.record_hashes("test", 3, label.split_hash(manifest, "test"), None, None, repo / "docs/hashes.md")
    _commit_all(repo, "init")
    return repo


# --------------------------------------------------------------------------- plan


def test_plan_template_has_headings_and_todos():
    text = freeze.plan_template()
    assert freeze.markdown_headings(text) == ["Analysis plan", *freeze.PLAN_HEADINGS]
    assert len(freeze.todo_lines(text)) == len(freeze.PLAN_HEADINGS)
    assert "TODO" not in "".join(freeze.markdown_headings(text))


def test_ensure_plan_creates_only_when_missing(tmp_path: Path):
    p = tmp_path / "docs" / "analysis_plan.md"
    assert freeze.ensure_plan(p) is True
    assert p.exists()
    assert "TODO" in p.read_text()
    p.write_text("custom")
    assert freeze.ensure_plan(p) is False
    assert p.read_text() == "custom"


def test_check_plan_reasons(tmp_path: Path):
    p = tmp_path / "analysis_plan.md"
    assert freeze.check_plan(p) == ["analysis_plan.md: file is missing"]

    p.write_text(_plan(complete=True))
    assert freeze.check_plan(p) == []

    p.write_text(_plan(complete=False))
    [msg] = freeze.check_plan(p)
    assert "4 TODO marker(s)" in msg
    assert "line 5, 9, 13, 17" in msg

    p.write_text(_plan(complete=True, drop="Tolerances"))
    assert freeze.check_plan(p) == ["analysis_plan.md: heading 'Tolerances' is missing"]

    # Heading match ignores case and trailing hashes; a TODO in prose still blocks.
    p.write_text("# Plan\n\n## scoring MATRIX ##\n\n## Tolerances\n\n## Audit design\n\n## Expected CI widths\n\nfill TODO later\n")
    [msg] = freeze.check_plan(p)
    assert "1 TODO marker(s)" in msg


# --------------------------------------------------------------------------- checks


def test_check_only_passes_on_ready_repo(repo: Path, capsys):
    before = (repo / "docs/hashes.md").read_text()
    rc = freeze.main(["--check-only", "--root", str(repo)])
    assert rc == 0
    assert "all checks pass" in capsys.readouterr().out
    assert _git(repo, "tag", "-l").strip() == ""
    assert (repo / "docs/hashes.md").read_text() == before


def test_check_only_fails_on_todo_markers(repo: Path, capsys):
    _write(repo, "docs/analysis_plan.md", _plan(complete=False))
    _commit_all(repo, "plan with todos")
    rc = freeze.main(["--check-only", "--root", str(repo)])
    out = capsys.readouterr()
    assert rc == 1
    assert "TODO marker(s) remain" in out.out
    assert "not clean" not in out.out
    assert "nothing written" in out.err


def test_missing_plan_is_created_as_template_and_refused(repo: Path, capsys):
    (repo / "docs/analysis_plan.md").unlink()
    _commit_all(repo, "drop plan")
    rc = freeze.main(["--check-only", "--root", str(repo)])
    out = capsys.readouterr().out
    assert rc == 1
    plan = repo / "docs/analysis_plan.md"
    assert plan.exists()
    assert set(freeze.PLAN_HEADINGS) <= set(freeze.markdown_headings(plan.read_text()))
    assert "created as a template" in out
    assert "TODO marker(s) remain" in out
    assert "not clean" in out  # the new file is untracked
    # A second check does not overwrite the template.
    plan.write_text(_plan(complete=True))
    _commit_all(repo, "plan done")
    assert freeze.main(["--check-only", "--root", str(repo)]) == 0


def test_check_fails_without_test_split(repo: Path):
    _write(repo, "config/corpus.csv", _manifest_csv(test_ids=[]))
    _commit_all(repo, "no test rows")
    problems = freeze.run_checks(repo, "plan-v1.0")
    assert problems == ["corpus.csv: no rows with split == test; draw the test split first"]


def test_check_fails_when_test_hash_not_recorded(repo: Path):
    # Re-draw the test split so the recorded hash no longer matches.
    _write(repo, "config/corpus.csv", _manifest_csv(test_ids=TEST_IDS[:2]))
    _commit_all(repo, "redraw test")
    [msg] = freeze.run_checks(repo, "plan-v1.0")
    assert msg.startswith("hashes.md: test doc_id hash")
    assert "2 documents" in msg and "cra label hash --split test" in msg
    # Recording the new hash clears the problem.
    manifest = label.read_manifest(repo / "config/corpus.csv")
    label.record_hashes("test", 2, label.split_hash(manifest, "test"), None, None, repo / "docs/hashes.md")
    _commit_all(repo, "record hash")
    assert freeze.run_checks(repo, "plan-v1.0") == []


def test_check_fails_when_hashes_file_missing(repo: Path):
    (repo / "docs/hashes.md").unlink()
    _commit_all(repo, "drop hashes")
    [msg] = freeze.run_checks(repo, "plan-v1.0")
    assert "not recorded" in msg


def test_check_fails_on_dirty_tree(repo: Path, capsys):
    (repo / "config/fields.yaml").write_text("fields: {changed: 1}\n")
    rc = freeze.main(["--check-only", "--root", str(repo)])
    assert rc == 1
    assert "working tree is not clean (1 path(s): config/fields.yaml)" in capsys.readouterr().out


def test_check_fails_outside_git_repo(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "plain"
    root.mkdir()
    _write(root, "docs/analysis_plan.md", _plan(complete=True))
    problems = freeze.run_checks(root, "plan-v1.0")
    assert problems == [f"{root}: not a git repository"]


# --------------------------------------------------------------------------- freeze


def test_freeze_creates_tag_record_and_commit(repo: Path, capsys):
    head_before = _git(repo, "rev-parse", "HEAD").strip()
    rc = freeze.main(["--tag", "plan-v1.0", "--date", "2026-10-13", "--root", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tagged plan-v1.0 at HEAD" in out

    manifest = label.read_manifest(repo / "config/corpus.csv")
    expected_hash = label.split_hash(manifest, "test")
    fields_sha = freeze.sha256_file(repo / "config/fields.yaml")
    headings_sha = freeze.sha256_file(repo / "config/headings.yaml")

    # The tag is annotated and its message lists the versions.
    assert _git(repo, "cat-file", "-t", "plan-v1.0").strip() == "tag"
    msg = _git(repo, "for-each-ref", "--format=%(contents)", "refs/tags/plan-v1.0")
    assert msg.startswith("Freeze plan-v1.0 (2026-10-13)\n\n")
    assert f"rules_version: {rules.RULES_VERSION}" in msg
    assert f"prompt_version: {prompts.PROMPT_VERSION}" in msg
    assert f"fields.yaml sha256: {fields_sha}" in msg
    assert f"headings.yaml sha256: {headings_sha}" in msg
    assert f"test split: 3 documents, doc_id list sha256 {expected_hash}" in msg

    # The record sits under a dated Freeze heading in docs/hashes.md, in the tagged commit.
    text = (repo / "docs/hashes.md").read_text()
    assert "## Freeze 2026-10-13 (plan-v1.0)\n" in text
    assert f"- fields.yaml sha256: {fields_sha}" in text
    assert text.index("| test | 3 |") < text.index("## Freeze")
    tagged_file = _git(repo, "show", "plan-v1.0:docs/hashes.md")
    assert "## Freeze 2026-10-13 (plan-v1.0)" in tagged_file
    assert _git(repo, "rev-parse", "plan-v1.0^{commit}").strip() != head_before
    assert _git(repo, "log", "-1", "--format=%s").strip() == "Freeze plan-v1.0: record versions and hashes"
    assert _git(repo, "status", "--porcelain") == ""


def test_freeze_no_commit_leaves_record_in_tree(repo: Path):
    head_before = _git(repo, "rev-parse", "HEAD").strip()
    problems, record = freeze.freeze(repo, frozen_on=date(2026, 10, 13), commit=False)
    assert problems == [] and record is not None
    assert record.frozen_on == date(2026, 10, 13)
    assert _git(repo, "rev-parse", "plan-v1.0^{commit}").strip() == head_before
    assert _git(repo, "status", "--porcelain").split() == ["M", "docs/hashes.md"]


def test_freeze_refuses_dirty_tree(repo: Path, capsys):
    (repo / "scratch.txt").write_text("untracked")
    hashes_before = (repo / "docs/hashes.md").read_text()
    rc = freeze.main(["--root", str(repo)])
    assert rc == 1
    assert "working tree is not clean" in capsys.readouterr().out
    assert _git(repo, "tag", "-l").strip() == ""
    assert (repo / "docs/hashes.md").read_text() == hashes_before


def test_freeze_refuses_existing_tag_unless_forced(repo: Path, capsys):
    _git(repo, "tag", "-a", "plan-v1.0", "-m", "earlier")
    first_target = _git(repo, "rev-parse", "plan-v1.0^{commit}").strip()
    rc = freeze.main(["--root", str(repo)])
    assert rc == 1
    assert "tag plan-v1.0 already exists; pass --force-retag" in capsys.readouterr().out
    assert _git(repo, "for-each-ref", "--format=%(contents:subject)", "refs/tags/plan-v1.0").strip() == "earlier"

    rc = freeze.main(["--root", str(repo), "--force-retag", "--date", "2026-10-14"])
    assert rc == 0
    # Moved, not deleted: still exactly one tag, now at the freeze commit with the new message.
    assert _git(repo, "tag", "-l").split() == ["plan-v1.0"]
    assert _git(repo, "rev-parse", "plan-v1.0^{commit}").strip() != first_target
    assert "Freeze plan-v1.0 (2026-10-14)" in _git(repo, "for-each-ref", "--format=%(contents)", "refs/tags/plan-v1.0")


def test_freeze_custom_tag_name(repo: Path):
    problems, record = freeze.freeze(repo, tag="plan-v1.1", frozen_on=date(2026, 10, 20))
    assert problems == [] and record is not None and record.tag == "plan-v1.1"
    assert _git(repo, "tag", "-l").split() == ["plan-v1.1"]
    assert "## Freeze 2026-10-20 (plan-v1.1)" in (repo / "docs/hashes.md").read_text()


def test_check_only_never_writes_even_when_ready(repo: Path):
    problems, record = freeze.freeze(repo, check_only=True)
    assert problems == [] and record is None
    assert _git(repo, "tag", "-l").strip() == ""
    assert _git(repo, "status", "--porcelain") == ""


def test_cli_rejects_bad_date(repo: Path, capsys):
    with pytest.raises(SystemExit) as e:
        freeze.main(["--check-only", "--date", "13-10-2026", "--root", str(repo)])
    assert e.value.code == 2
    assert "not a YYYY-MM-DD date" in capsys.readouterr().err


def test_record_freeze_creates_bare_file(tmp_path: Path):
    rec = freeze.FreezeRecord(
        tag="plan-v1.0", frozen_on=date(2026, 10, 13), rules_version="0.1.0", prompt_version="v1",
        fields_sha256="a" * 64, headings_sha256="b" * 64, n_test_docs=30, test_doc_hash="c" * 64,
    )
    p = tmp_path / "docs" / "hashes.md"
    freeze.record_freeze(rec, p)
    text = p.read_text()
    assert text.startswith("# Committed hashes\n\n## Freeze 2026-10-13 (plan-v1.0)\n\n- rules_version: 0.1.0\n")
    assert rec.tag_message().splitlines()[0] == "Freeze plan-v1.0 (2026-10-13)"
    assert rec.tag_message().endswith(f"doc_id list sha256 {'c' * 64}\n")
