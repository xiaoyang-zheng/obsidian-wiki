from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from obsidian_wiki.backlog import build_backlog, render_backlog, write_backlog
from obsidian_wiki.cache import _manifest_path, compute_hash
from obsidian_wiki.context_pack import load_pages
from obsidian_wiki.graph_analysis import parse_vault_graph
from obsidian_wiki.graphrag import build_index
from obsidian_wiki.lint import lint_vault
from obsidian_wiki.source_bundles import create_source_bundle
from obsidian_wiki.source_state import update_source


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _page(vault: Path, relative: str, *, fields: str = "", body: str = "") -> Path:
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
        f"{fields}"
        "---\n"
        f"# {Path(relative).stem}\n\n"
        f"{body}",
    )


def test_empty_backlog_is_pass(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    report = build_backlog(vault)

    assert report["status"] == "pass"
    assert report["summary"] == {
        "total": 0,
        "critical": 0,
        "needs_ingest": 0,
        "maintenance": 0,
        "reference": 0,
    }
    assert report["items"] == []


def test_backlog_aggregates_source_state_and_manifest(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    config_dir = tmp_path / "config" / "obsidian-wiki"
    source = _write(vault / "_raw" / "note.md", "before")
    _manifest_path(vault).write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "path": "_raw/note.md",
                        "content_hash": f"sha256:{compute_hash(source)}",
                    },
                    {"path": "_raw/missing.md", "content_hash": "sha256:0"},
                ]
            }
        ),
        encoding="utf-8",
    )
    source.write_text("after", encoding="utf-8")
    update_source(
        vault,
        "feed",
        observed_cursor="remote:2",
        cursor_kind="opaque",
        config_dir=config_dir,
        now="2026-09-02T10:00:00Z",
    )

    report = build_backlog(vault, config_dir=config_dir)

    assert report["status"] == "warn"
    assert report["summary"]["needs_ingest"] == 2
    assert report["summary"]["maintenance"] == 1
    assert [item["severity"] for item in report["items"]] == [
        "needs_ingest",
        "needs_ingest",
        "maintenance",
    ]
    assert {item["kind"] for item in report["items"]} == {"source-state", "manifest"}


def test_backlog_reports_corrupt_manifest_as_critical(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _manifest_path(vault).write_text("{broken", encoding="utf-8")

    report = build_backlog(vault)

    assert report["status"] == "fail"
    assert report["items"][0]["kind"] == "manifest"
    assert report["items"][0]["severity"] == "critical"
    assert report["items"][0]["subject"] == ".manifest.json"


def test_backlog_aggregates_bundle_and_closure_failures(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    primary = _write(tmp_path / "paper.pdf", "original")
    create_source_bundle(vault, "paper", primary)
    artifact = vault / "_sources" / "paper" / "raw" / "paper.pdf"
    artifact.chmod(0o600)
    artifact.write_text("tampered", encoding="utf-8")
    _page(
        vault,
        "references/paper.md",
        fields="source_bundle: paper\nentities: [entities/attention]\n",
        body="Missing entity link and backlink.\n",
    )
    _page(vault, "entities/attention.md")

    report = build_backlog(vault)

    assert report["status"] == "fail"
    assert report["summary"]["critical"] >= 3
    kinds = [item["kind"] for item in report["items"]]
    assert "source-bundle" in kinds
    assert "source-closure" in kinds


def test_backlog_reports_project_timeline_drift(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _write(vault / "projects" / "alpha.md", "---\ntitle: Alpha\n---\n# Alpha\n")
    _page(
        vault,
        "references/event.md",
        fields="projects: [alpha]\n",
        body="[[projects/alpha]]\n",
    )

    report = build_backlog(vault)

    assert report["status"] == "warn"
    assert any(item["kind"] == "project-timeline" for item in report["items"])


def test_write_backlog_and_skip_generated_page(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    report = build_backlog(vault)

    path = write_backlog(vault, report)

    assert path == vault / "_backlog.md"
    assert "No deterministic maintenance debt found." in path.read_text(encoding="utf-8")
    assert "_backlog" not in parse_vault_graph(vault)[0]
    assert "_backlog" not in build_index(vault)
    assert load_pages(vault) == []
    assert lint_vault(vault, require_trust_ledger=False)["stats"]["pages"] == 0


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, "-m", "obsidian_wiki.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=home,
    )


def test_backlog_cli_json_and_write(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    vault.mkdir()

    proc = _run(home, "backlog", str(vault), "--json", "--pretty")
    written = _run(home, "backlog", str(vault), "--write")

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["status"] == "pass"
    assert written.returncode == 0
    assert (vault / "_backlog.md").is_file()
    assert "obsidian-wiki backlog: pass" in written.stdout


def test_backlog_cli_returns_nonzero_for_critical_items(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    vault.mkdir()
    _page(
        vault,
        "references/paper.md",
        fields="source_bundle: missing\nentities: none\n",
    )

    proc = _run(home, "backlog", str(vault), "--json")

    assert proc.returncode == 1
    report = json.loads(proc.stdout)
    assert report["status"] == "fail"
    assert report["summary"]["critical"] == 1
