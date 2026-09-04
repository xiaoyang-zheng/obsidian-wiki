"""Documentation contracts for deterministic paper and promotion flows."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_wiki_ingest_runs_paper_inspection_and_verifies_candidates() -> None:
    skill = _read(".skills/wiki-ingest/SKILL.md")

    assert "obsidian-wiki paper-inspect" in skill
    assert "identity.work_id" in skill
    assert "identity.edition_id" in skill
    assert "The source is read-only" in skill
    assert "Verify each crop visually" in skill
    assert "Never link to the temporary inspect output" in skill
    assert "exact `paper_work_id`" in skill
    assert "paper_editions" in skill


def test_wiki_ingest_promotion_is_plan_then_commit() -> None:
    skill = _read(".skills/wiki-ingest/SKILL.md")

    observe = skill.index("obsidian-wiki promotion-observe")
    list_eligible = skill.index("obsidian-wiki promotion-candidates")
    resolve = skill.index("obsidian-wiki promotion-resolve")
    assert observe < list_eligible < resolve
    assert "not create Markdown" in skill
    assert "Do not resolve a partial ingest" in skill
    assert "leave the candidate eligible" in skill


def test_stage_commit_owns_resolution_for_accepted_staged_pages() -> None:
    stage = _read(".skills/wiki-stage-commit/SKILL.md")

    assert "obsidian-wiki promotion-resolve" in stage
    assert "promotion_plan.target_path" in stage
    assert "do not rely on conversational memory" in stage
    assert "Do this last" in stage
    assert "leave the\n   candidate eligible" in stage
    assert "Rejected staged" in stage
    assert "not automatically a semantic rejection" in stage


def test_cli_and_architecture_document_new_contracts() -> None:
    cli = _read("docs/cli.md")
    architecture = _read("docs/architecture.md")
    install = _read("docs/installation.md")
    foundation = _read(".skills/llm-wiki/SKILL.md")

    for command in (
        "paper-inspect",
        "promotion-candidates",
        "promotion-observe",
        "promotion-resolve",
    ):
        assert command in cli
    assert "work_id" in architecture and "edition_id" in architecture
    assert "_meta/promotion-candidates.json" in architecture
    assert "obsidian-wiki[paper]" in install
    for field in (
        "paper_work_id",
        "paper_edition_id",
        "paper_editions",
        "paper_source_sha256",
    ):
        assert field in foundation
