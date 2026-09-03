"""CLI contract tests for generic continuous source state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import REPO_ROOT


def _run(
    home: Path,
    *args: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    home.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment.pop("XDG_CONFIG_HOME", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, "-m", "obsidian_wiki.cli", *args],
        cwd=cwd or home,
        env=environment,
        capture_output=True,
        text=True,
    )


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".manifest.json").write_text('{"sentinel": true}\n', encoding="utf-8")
    return vault


def _state_files(home: Path) -> list[Path]:
    return [
        *home.glob(".config/obsidian-wiki/state/*/source-state.json"),
        *home.glob(".obsidian-wiki/state/*/source-state.json"),
    ]


def test_source_state_help_surfaces_parse() -> None:
    for command in ("source-state", "source-state-update"):
        proc = subprocess.run(
            [sys.executable, "-m", "obsidian_wiki.cli", command, "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        assert "usage:" in proc.stdout
        assert "Traceback" not in proc.stderr


def test_source_state_requires_a_configured_vault(tmp_path: Path) -> None:
    proc = _run(tmp_path / "home", "source-state")

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "vault not configured" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_empty_state_is_a_stable_read_only_report(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = _make_vault(tmp_path)

    proc = _run(home, "source-state", str(vault), "--pretty")

    assert proc.returncode == 0
    report = json.loads(proc.stdout)
    assert report["version"] == 1
    assert report["status"] == "pass"
    assert report["summary"]["tracked"] == 0
    assert report["sources"] == {}
    assert "\n  " in proc.stdout
    assert _state_files(home) == []


def test_update_and_read_round_trip_opaque_cursors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = _make_vault(tmp_path)
    manifest_before = (vault / ".manifest.json").read_bytes()

    observed = _run(
        home,
        "source-state-update",
        str(vault),
        "--source",
        "generic-feed",
        "--observed-cursor",
        "remote:page/115?etag=a:b",
        "--cursor-kind",
        "opaque",
        "--heartbeat-ok",
        "--stale-after-seconds",
        "3600",
    )
    assert observed.returncode == 0, observed.stderr
    observed_data = json.loads(observed.stdout)
    assert observed_data["debt"]["reason"] == "never_applied"
    assert observed_data["heartbeat"]["status"] == "ok"

    applied = _run(
        home,
        "source-state-update",
        str(vault),
        "--source",
        "generic-feed",
        "--applied-cursor",
        "remote:page/115?etag=a:b",
        "--pretty",
    )
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["debt"]["pending"] is False

    report = _run(
        home,
        "source-state",
        str(vault),
        "--source",
        "generic-feed",
        "--strict",
    )
    assert report.returncode == 0, report.stderr
    entry = json.loads(report.stdout)["sources"]["generic-feed"]
    assert entry["observed_cursor"] == "remote:page/115?etag=a:b"
    assert entry["applied_cursor"] == "remote:page/115?etag=a:b"
    assert (vault / ".manifest.json").read_bytes() == manifest_before
    assert not (vault / "source-state.json").exists()
    assert len(_state_files(home)) == 1


def test_heartbeat_error_preserves_cursors_and_strict_fails(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = _make_vault(tmp_path)
    setup = _run(
        home,
        "source-state-update",
        str(vault),
        "--source",
        "feed",
        "--observed-cursor",
        "A",
        "--applied-cursor",
        "A",
        "--heartbeat-ok",
    )
    assert setup.returncode == 0, setup.stderr

    failed = _run(
        home,
        "source-state-update",
        str(vault),
        "--source",
        "feed",
        "--heartbeat-error",
        "temporary authentication failure",
    )
    assert failed.returncode == 0, failed.stderr
    failure = json.loads(failed.stdout)
    assert failure["observed_cursor"] == "A"
    assert failure["applied_cursor"] == "A"
    assert failure["heartbeat"]["status"] == "error"

    strict = _run(home, "source-state", str(vault), "--strict")
    assert strict.returncode == 1
    assert json.loads(strict.stdout)["status"] == "fail"


def test_update_without_mutation_is_a_clean_business_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = _make_vault(tmp_path)

    proc = _run(
        home,
        "source-state-update",
        str(vault),
        "--source",
        "feed",
    )

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "error: no source-state update requested" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_requested_untracked_source_is_strict_failure_without_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    vault = _make_vault(tmp_path)

    normal = _run(home, "source-state", str(vault), "--source", "unknown")
    strict = _run(
        home,
        "source-state",
        str(vault),
        "--source",
        "unknown",
        "--strict",
    )

    assert normal.returncode == 0
    assert strict.returncode == 1
    assert json.loads(normal.stdout)["sources"]["unknown"]["status"] == "untracked"
    assert _state_files(home) == []


def test_corrupt_state_fails_closed_without_traceback_or_overwrite(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    vault = _make_vault(tmp_path)
    created = _run(
        home,
        "source-state-update",
        str(vault),
        "--source",
        "feed",
        "--observed-cursor",
        "A",
    )
    assert created.returncode == 0, created.stderr
    state_file = _state_files(home)[0]
    state_file.write_text("{broken", encoding="utf-8")

    proc = _run(home, "source-state", str(vault))
    update = _run(
        home,
        "source-state-update",
        str(vault),
        "--source",
        "feed",
        "--applied-cursor",
        "A",
    )

    for result in (proc, update):
        assert result.returncode == 1
        assert result.stdout == ""
        assert "error:" in result.stderr
        assert "Traceback" not in result.stderr
    assert state_file.read_text(encoding="utf-8") == "{broken"


def test_configured_vault_and_nearest_env_are_supported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    global_vault = tmp_path / "global-vault"
    local_vault = tmp_path / "local-vault"
    global_vault.mkdir()
    local_vault.mkdir()
    config = home / ".obsidian-wiki" / "config"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'OBSIDIAN_VAULT_PATH="{global_vault}"\n',
        encoding="utf-8",
    )
    project = home / "project"
    project.mkdir()
    (project / ".env").write_text(
        f'OBSIDIAN_VAULT_PATH="{local_vault}"\n',
        encoding="utf-8",
    )

    proc = _run(
        home,
        "source-state-update",
        "--source",
        "feed",
        "--heartbeat-ok",
        cwd=project,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["tracked"] is True
    state = json.loads(_state_files(home)[0].read_text(encoding="utf-8"))
    assert state["vault_path"] == str(local_vault.resolve())


def test_named_vault_is_supported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = _make_vault(tmp_path)
    config = home / ".obsidian-wiki" / "config.research"
    config.parent.mkdir(parents=True)
    config.write_text(f'OBSIDIAN_VAULT_PATH="{vault}"\n', encoding="utf-8")

    update = _run(
        home,
        "source-state-update",
        "@research",
        "--source",
        "feed",
        "--observed-cursor",
        "A",
    )
    report = _run(home, "source-state", "@research", "--source", "feed")

    assert update.returncode == 0, update.stderr
    assert report.returncode == 0, report.stderr
    assert json.loads(report.stdout)["sources"]["feed"]["observed_cursor"] == "A"
