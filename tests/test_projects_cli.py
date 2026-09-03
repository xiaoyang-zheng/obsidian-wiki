from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _write(vault: Path, relative: str, text: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_vault(tmp_path: Path, *, project: str = "alpha") -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    overview = _write(
        vault,
        f"projects/{project}.md",
        f"---\ntitle: {project}\n---\n# {project}\n\nManual prose.\n",
    )
    _write(
        vault,
        "references/event.md",
        (
            "---\n"
            "title: Event\n"
            f"projects: [{project}]\n"
            "created: 2026-09-01\n"
            "summary: A project event.\n"
            "---\n"
            "# Event\n"
        ),
    )
    return vault, overview


def _run(
    home: Path,
    *args: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, "-m", "obsidian_wiki.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or home,
    )


def test_cli_explicit_vault_write_then_check_clean(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    vault, overview = _make_vault(tmp_path)

    written = _run(home, "project-timelines", str(vault), "--json")
    checked = _run(home, "project-timelines", str(vault), "--check", "--json")

    assert written.returncode == 0
    assert json.loads(written.stdout) == {
        "schema_version": 1,
        "status": "updated",
        "check": False,
        "vault": str(vault.resolve()),
        "link_format": "wikilink",
        "projects_scanned": 1,
        "entries": 1,
        "changed": ["projects/alpha.md"],
        "errors": [],
    }
    assert checked.returncode == 0
    assert json.loads(checked.stdout)["status"] == "clean"
    assert "[[references/event|Event]]" in overview.read_text(encoding="utf-8")


def test_cli_check_is_read_only_and_returns_nonzero_for_drift(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    vault, overview = _make_vault(tmp_path)
    before = overview.read_bytes()

    proc = _run(home, "project-timelines", str(vault), "--check", "--json")

    assert proc.returncode == 1
    assert json.loads(proc.stdout)["status"] == "drift"
    assert overview.read_bytes() == before


def test_cli_uses_nearest_config_and_configured_markdown_links(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "work" / "nested"
    project.mkdir(parents=True)
    vault, overview = _make_vault(tmp_path)
    (home / "work" / ".env").write_text(
        f'OBSIDIAN_VAULT_PATH="{vault}"\nOBSIDIAN_LINK_FORMAT="markdown"\n',
        encoding="utf-8",
    )

    proc = _run(home, "project-timelines", "--json", cwd=project)

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["link_format"] == "markdown"
    assert "[Event](../references/event.md)" in overview.read_text(encoding="utf-8")


def test_cli_named_vault_and_pretty_json(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault, _overview = _make_vault(tmp_path)
    config = home / ".obsidian-wiki" / "config.research"
    config.parent.mkdir(parents=True)
    config.write_text(f'OBSIDIAN_VAULT_PATH="{vault}"\n', encoding="utf-8")

    proc = _run(
        home,
        "project-timelines",
        "@research",
        "--check",
        "--json",
        "--pretty",
    )

    assert proc.returncode == 1
    assert json.loads(proc.stdout)["status"] == "drift"
    assert "\n  \"status\"" in proc.stdout


def test_cli_pretty_human_output_and_link_format_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    vault, overview = _make_vault(tmp_path)

    proc = _run(
        home,
        "project-timelines",
        str(vault),
        "--link-format",
        "markdown",
    )

    assert proc.returncode == 0
    assert "project timelines: updated" in proc.stdout
    assert "projects: 1  entries: 1  changed: 1" in proc.stdout
    assert "[Event](../references/event.md)" in overview.read_text(encoding="utf-8")


def test_cli_schema_error_returns_nonzero_without_partial_writes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    vault, overview = _make_vault(tmp_path)
    second = _write(
        vault,
        "projects/beta.md",
        (
            "---\ntitle: beta\n---\n# beta\n\n"
            "<!-- BEGIN obsidian-wiki:auto-project-timeline -->\n"
            "broken\n"
        ),
    )
    before = {overview: overview.read_bytes(), second: second.read_bytes()}

    proc = _run(home, "project-timelines", str(vault), "--json")

    assert proc.returncode == 1
    report = json.loads(proc.stdout)
    assert report["status"] == "error"
    assert report["errors"][0]["code"] == "malformed_project_timeline_markers"
    assert overview.read_bytes() == before[overview]
    assert second.read_bytes() == before[second]


def test_cli_requires_a_configured_vault(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    proc = _run(home, "project-timelines")

    assert proc.returncode == 1
    assert "vault not configured" in proc.stderr
