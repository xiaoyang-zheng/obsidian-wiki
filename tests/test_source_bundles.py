"""Tests for immutable source bundles, media localization, and source/entity closure."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from obsidian_wiki.lint import lint_vault
from obsidian_wiki.context_pack import load_pages
from obsidian_wiki.graph_analysis import parse_vault_graph
from obsidian_wiki.graphrag import build_index
from obsidian_wiki.source_bundles import (
    SourceBundleError,
    check_source_bundles,
    create_source_bundle,
    lint_source_bundle_closure,
    localize_bundle_media,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _page(vault: Path, relative: str, *, binding: str = "", body: str = "") -> Path:
    return _write(
        vault / relative,
        "---\n"
        f"title: {Path(relative).stem}\n"
        "category: references\n"
        "tags: [test]\n"
        "sources: [manual]\n"
        "created: 2026-09-02\n"
        "updated: 2026-09-02\n"
        "summary: Source summary.\n"
        "base_confidence: 0.8\n"
        "lifecycle: reviewed\n"
        f"{binding}"
        "---\n"
        f"# {Path(relative).stem}\n\n"
        f"{body}",
    )


def _clean_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def test_create_bundle_copies_primary_and_media_read_only(tmp_path: Path) -> None:
    vault = _clean_vault(tmp_path)
    primary = _write(tmp_path / "input" / "paper.pdf", "primary bytes")
    media = _write(tmp_path / "input" / "figure.png", "media bytes")

    report = create_source_bundle(
        vault,
        "attention-paper",
        primary,
        source_type="paper",
        original_uri="https://example.test/paper.pdf",
        media_paths=[media],
    )

    bundle = vault / "_sources" / "attention-paper"
    assert report["status"] == "pass"
    assert (bundle / "raw" / "paper.pdf").read_text() == "primary bytes"
    assert (bundle / "media" / "figure.png").read_text() == "media bytes"
    assert not ((bundle / "raw" / "paper.pdf").stat().st_mode & stat.S_IWUSR)
    manifest = json.loads((bundle / "bundle.json").read_text())
    assert manifest["id"] == "attention-paper"
    assert manifest["artifacts"][0]["path"] == "raw/paper.pdf"
    assert manifest["media"][0]["path"] == "media/figure.png"
    assert check_source_bundles(vault)["status"] == "pass"


def test_bundle_rejects_existing_id_and_does_not_overwrite(tmp_path: Path) -> None:
    vault = _clean_vault(tmp_path)
    first = _write(tmp_path / "first.pdf", "first")
    replacement = _write(tmp_path / "replacement.pdf", "replacement")
    create_source_bundle(vault, "paper", first)

    with pytest.raises(SourceBundleError, match="already exists"):
        create_source_bundle(vault, "paper", replacement)

    assert (vault / "_sources" / "paper" / "raw" / "first.pdf").read_text() == "first"
    assert not (vault / "_sources" / "paper" / "raw" / "replacement.pdf").exists()


def test_unmanaged_sources_directory_is_ignored_until_explicitly_bound(tmp_path: Path) -> None:
    vault = _clean_vault(tmp_path)
    _write(vault / "_sources" / "legacy" / "notes.md", "legacy source material")

    assert check_source_bundles(vault)["summary"] == {
        "bundles": 0,
        "valid": 0,
        "invalid": 0,
    }


@pytest.mark.parametrize("bundle_id", ["../escape", "a/b", ".hidden", "", ".."])
def test_bundle_rejects_unsafe_id(tmp_path: Path, bundle_id: str) -> None:
    vault = _clean_vault(tmp_path)
    primary = _write(tmp_path / "source.txt", "source")

    with pytest.raises(SourceBundleError, match="invalid bundle id"):
        create_source_bundle(vault, bundle_id, primary)


def test_media_localization_appends_immutable_media_and_detects_tamper(tmp_path: Path) -> None:
    vault = _clean_vault(tmp_path)
    primary = _write(tmp_path / "source.txt", "source")
    second = _write(tmp_path / "chart.svg", "chart")
    create_source_bundle(vault, "source", primary)

    report = localize_bundle_media(vault, "source", second, name="result.svg")

    bundle_media = vault / "_sources" / "source" / "media" / "result.svg"
    assert report["media"][0]["path"] == "media/result.svg"
    assert bundle_media.read_text() == "chart"
    bundle_media.chmod(bundle_media.stat().st_mode | stat.S_IWUSR)
    bundle_media.write_text("tampered", encoding="utf-8")
    checked = check_source_bundles(vault)
    assert checked["status"] == "fail"
    assert checked["bundles"]["source"]["errors"][0]["code"] == "artifact_size_mismatch"


def test_media_localization_rejects_duplicate_filename(tmp_path: Path) -> None:
    vault = _clean_vault(tmp_path)
    primary = _write(tmp_path / "source.txt", "source")
    image = _write(tmp_path / "chart.png", "one")
    other = _write(tmp_path / "other.png", "two")
    create_source_bundle(vault, "source", primary, media_paths=[image])

    with pytest.raises(SourceBundleError, match="already exists"):
        localize_bundle_media(vault, "source", other, name="chart.png")


def test_media_localization_reports_missing_bundle_as_domain_error(tmp_path: Path) -> None:
    vault = _clean_vault(tmp_path)
    media = _write(tmp_path / "chart.png", "image")

    with pytest.raises(SourceBundleError, match="source bundle not found"):
        localize_bundle_media(vault, "missing", media)


def test_source_bundle_pages_require_entities_and_two_way_links(tmp_path: Path) -> None:
    vault = _clean_vault(tmp_path)
    primary = _write(tmp_path / "source.txt", "source")
    create_source_bundle(vault, "paper", primary)
    source = _page(
        vault,
        "references/paper.md",
        binding="source_bundle: paper\nentities: [entities/attention]\n",
        body="[[entities/attention]]\n![[../_sources/paper/media/figure.png]]\n",
    )
    _page(vault, "entities/attention.md", body="[[references/paper]]\n")

    findings = lint_source_bundle_closure(vault, [source, vault / "entities" / "attention.md"])

    assert all(not items for items in findings.values())
    report = lint_vault(vault, require_trust_ledger=False)
    assert report["findings"]["missing_source_entity_links"] == []
    assert report["findings"]["missing_entity_source_backlinks"] == []
    assert report["findings"]["broken_links"] == []


def test_source_bundle_closure_reports_missing_targets_and_links(tmp_path: Path) -> None:
    vault = _clean_vault(tmp_path)
    source = _page(
        vault,
        "references/paper.md",
        binding="source_bundle: missing\nentities: [entities/attention, entities/missing]\n",
        body="",
    )
    _page(vault, "entities/attention.md", body="")

    findings = lint_source_bundle_closure(vault, [source, vault / "entities" / "attention.md"])

    assert findings["missing_source_bundle_targets"] == [{"page": "references/paper.md", "bundle": "missing"}]
    assert findings["missing_source_entities"] == [{"page": "references/paper.md", "entity": "entities/missing"}]
    assert findings["missing_source_entity_links"] == [{"page": "references/paper.md", "entity": "entities/attention"}]
    assert findings["missing_entity_source_backlinks"] == [{"page": "references/paper.md", "entity": "entities/attention"}]
    report = lint_vault(vault, require_trust_ledger=False)
    assert report["status"] == "fail"


def test_source_bundle_entities_none_is_explicit_and_does_not_require_links(tmp_path: Path) -> None:
    vault = _clean_vault(tmp_path)
    primary = _write(tmp_path / "source.txt", "source")
    create_source_bundle(vault, "admin-note", primary)
    source = _page(
        vault,
        "references/admin-note.md",
        binding="source_bundle: admin-note\nentities: none\n",
    )

    findings = lint_source_bundle_closure(vault, [source])

    assert all(not items for items in findings.values())


def test_source_bundle_invalid_entities_binding_fails_closed(tmp_path: Path) -> None:
    vault = _clean_vault(tmp_path)
    primary = _write(tmp_path / "source.txt", "source")
    create_source_bundle(vault, "paper", primary)
    source = _page(
        vault,
        "references/paper.md",
        binding="source_bundle: paper\nentities: []\n",
    )

    findings = lint_source_bundle_closure(vault, [source])

    assert findings["invalid_source_bundle_bindings"] == [
        {"page": "references/paper.md", "errors": ["use entities: none for a source without entities"]}
    ]


def test_bundle_artifacts_are_ignored_by_graph_lint_and_context_pack(tmp_path: Path) -> None:
    vault = _clean_vault(tmp_path)
    primary = _write(tmp_path / "source.md", "source")
    create_source_bundle(vault, "paper", primary)
    raw_markdown = vault / "_sources" / "paper" / "raw" / "source.md"
    raw_markdown.chmod(raw_markdown.stat().st_mode | stat.S_IWUSR)
    raw_markdown.write_text("# Raw\n\n[[missing-raw-link]]\n", encoding="utf-8")
    raw_markdown.chmod(raw_markdown.stat().st_mode & ~stat.S_IWUSR)
    _page(vault, "concepts/kept.md", body="")

    report = lint_vault(vault, require_trust_ledger=False)

    assert report["findings"]["broken_links"] == []
    outgoing, _tags = parse_vault_graph(vault)
    assert "source" not in outgoing
    assert "source" not in build_index(vault)
    assert [page.path for page in load_pages(vault)] == ["concepts/kept.md"]


def _run_cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    home.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, "-m", "obsidian_wiki.cli", *args],
        capture_output=True,
        text=True,
        env=environment,
        cwd=home,
    )


def test_source_bundle_cli_round_trip(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = _clean_vault(tmp_path)
    source = _write(tmp_path / "source.pdf", "source")
    image = _write(tmp_path / "image.png", "image")
    later_media = _write(tmp_path / "later.png", "later")

    created = _run_cli(
        home,
        "source-bundle-create",
        str(vault),
        "--id",
        "paper",
        "--source",
        str(source),
        "--source-type",
        "paper",
        "--media",
        str(image),
        "--pretty",
    )
    checked = _run_cli(home, "source-bundles", str(vault), "--id", "paper")
    localized = _run_cli(
        home,
        "source-bundle-media",
        str(vault),
        "--id",
        "paper",
        "--media",
        str(later_media),
        "--name",
        "later.png",
    )
    checked_after = _run_cli(home, "source-bundles", str(vault), "--id", "paper")

    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["id"] == "paper"
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["status"] == "pass"
    assert localized.returncode == 0, localized.stderr
    assert len(json.loads(localized.stdout)["media"]) == 2
    assert checked_after.returncode == 0, checked_after.stderr


def test_source_bundle_cli_reports_missing_source_without_traceback(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = _clean_vault(tmp_path)

    proc = _run_cli(
        home,
        "source-bundle-create",
        str(vault),
        "--id",
        "paper",
        "--source",
        str(tmp_path / "missing.pdf"),
    )

    assert proc.returncode == 1
    assert "error:" in proc.stderr
    assert "Traceback" not in proc.stderr
