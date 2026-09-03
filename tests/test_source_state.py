"""Unit tests for external, vault-scoped continuous source state."""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from obsidian_wiki import source_state as ss


NOW = "2026-09-01T12:00:00Z"


def _concurrent_update(
    vault: str,
    config_dir: str,
    source_id: str,
    cursor: str,
) -> None:
    ss.update_source(
        Path(vault),
        source_id,
        observed_cursor=cursor,
        config_dir=Path(config_dir),
        now=NOW,
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path / "config" / "obsidian-wiki"


def _state_file(vault: Path, config_dir: Path) -> Path:
    return ss.state_path(vault, config_dir=config_dir)


def test_missing_state_returns_empty_view_without_writing(
    vault: Path, config_dir: Path
) -> None:
    state = ss.load_state(vault, config_dir=config_dir)

    assert state == {
        "version": 1,
        "vault_path": str(vault.resolve()),
        "updated_at": None,
        "sources": {},
    }
    assert not config_dir.exists()


def test_vault_id_canonicalizes_alias_and_separates_vaults(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)

    assert ss.vault_id(first) == ss.vault_id(alias)
    assert ss.vault_id(first) != ss.vault_id(second)


def test_resolve_config_dir_matches_xdg_and_legacy_rules(tmp_path: Path) -> None:
    home = tmp_path / "home"
    legacy = home / ".obsidian-wiki"
    legacy.mkdir(parents=True)

    assert ss.resolve_global_config_dir(home=home, environ={}) == legacy

    xdg_home = tmp_path / "xdg"
    xdg = xdg_home / "obsidian-wiki"
    xdg.mkdir(parents=True)
    assert ss.resolve_global_config_dir(
        home=home,
        environ={"XDG_CONFIG_HOME": str(xdg_home)},
    ) == xdg


def test_existing_legacy_state_directory_is_reused(
    vault: Path, config_dir: Path
) -> None:
    legacy = config_dir / "state" / ss._legacy_vault_id(vault)
    legacy.mkdir(parents=True)
    (legacy / ".vault_path").write_text(str(vault) + "\n", encoding="utf-8")

    assert ss.state_dir(vault, config_dir=config_dir) == legacy


def test_legacy_hash_collision_fails_closed(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(ss, "_legacy_vault_id", lambda _vault: "collision")
    directory = config / "state" / "collision"
    directory.mkdir(parents=True)
    (directory / ".vault_path").write_text(str(first) + "\n", encoding="utf-8")

    with pytest.raises(ss.SourceStateCorruptError, match="collision"):
        ss.state_dir(second, config_dir=config)


def test_observed_and_applied_advance_independently(
    vault: Path, config_dir: Path
) -> None:
    observed = ss.update_source(
        vault,
        "feed",
        observed_cursor="opaque:115/abc",
        cursor_kind="opaque",
        config_dir=config_dir,
        now="2026-09-01T10:00:00Z",
    )

    assert observed["observed_cursor"] == "opaque:115/abc"
    assert observed["applied_cursor"] is None
    assert observed["debt"]["reason"] == "never_applied"

    applied = ss.update_source(
        vault,
        "feed",
        applied_cursor="opaque:115/abc",
        config_dir=config_dir,
        now="2026-09-01T11:00:00Z",
    )

    assert applied["observed_cursor"] == "opaque:115/abc"
    assert applied["applied_cursor"] == "opaque:115/abc"
    assert applied["debt"] == {
        "pending": False,
        "reason": "none",
        "age_seconds": None,
    }


def test_cursor_values_are_opaque_and_not_ordered(
    vault: Path, config_dir: Path
) -> None:
    result = ss.update_source(
        vault,
        "feed",
        observed_cursor="9",
        applied_cursor="10",
        config_dir=config_dir,
        now=NOW,
    )

    assert result["debt"]["pending"] is True
    assert result["debt"]["reason"] == "cursor_mismatch"


def test_rewriting_same_cursor_preserves_watermark_time(
    vault: Path, config_dir: Path
) -> None:
    ss.update_source(
        vault,
        "feed",
        observed_cursor="same",
        config_dir=config_dir,
        now="2026-09-01T10:00:00Z",
    )
    ss.update_source(
        vault,
        "feed",
        observed_cursor="same",
        heartbeat_status="ok",
        config_dir=config_dir,
        now="2026-09-01T11:00:00Z",
    )

    entry = ss.load_state(vault, config_dir=config_dir)["sources"]["feed"]
    assert entry["observed"]["at"] == "2026-09-01T10:00:00Z"
    assert entry["heartbeat"]["last_success_at"] == "2026-09-01T11:00:00Z"


def test_heartbeat_does_not_move_cursors_and_error_keeps_last_success(
    vault: Path, config_dir: Path
) -> None:
    ss.update_source(
        vault,
        "feed",
        observed_cursor="A",
        applied_cursor="A",
        heartbeat_status="ok",
        config_dir=config_dir,
        now="2026-09-01T10:00:00Z",
    )
    result = ss.update_source(
        vault,
        "feed",
        heartbeat_status="error",
        heartbeat_error="temporary failure",
        config_dir=config_dir,
        now="2026-09-01T11:00:00Z",
    )

    assert result["observed_cursor"] == "A"
    assert result["applied_cursor"] == "A"
    assert result["heartbeat"]["last_attempt_at"] == "2026-09-01T11:00:00Z"
    assert result["heartbeat"]["last_success_at"] == "2026-09-01T10:00:00Z"
    assert result["heartbeat"]["status"] == "error"
    assert result["status"] == "fail"


def test_heartbeat_only_source_has_no_cursor_debt(
    vault: Path, config_dir: Path
) -> None:
    result = ss.update_source(
        vault,
        "health-only",
        heartbeat_status="ok",
        stale_after_seconds=3600,
        config_dir=config_dir,
        now=NOW,
    )

    assert result["debt"]["pending"] is False
    assert result["observed_cursor"] is None
    assert result["applied_cursor"] is None


def test_stale_heartbeat_boundary_and_never_success(
    vault: Path, config_dir: Path
) -> None:
    ss.update_source(
        vault,
        "feed",
        heartbeat_status="ok",
        stale_after_seconds=60,
        config_dir=config_dir,
        now="2026-09-01T10:00:00Z",
    )

    at_boundary = ss.build_report(
        vault,
        config_dir=config_dir,
        now="2026-09-01T10:01:00Z",
    )
    after_boundary = ss.build_report(
        vault,
        config_dir=config_dir,
        now="2026-09-01T10:01:01Z",
    )
    ss.update_source(
        vault,
        "never-ok",
        stale_after_seconds=60,
        config_dir=config_dir,
        now="2026-09-01T10:02:00Z",
    )
    never_success = ss.build_report(
        vault,
        source_ids=["never-ok"],
        config_dir=config_dir,
        now="2026-09-01T10:02:00Z",
    )

    assert at_boundary["sources"]["feed"]["heartbeat"]["stale"] is False
    assert after_boundary["sources"]["feed"]["heartbeat"]["stale"] is True
    assert never_success["sources"]["never-ok"]["heartbeat"]["stale"] is True


def test_applied_without_observed_is_fail_closed(
    vault: Path, config_dir: Path
) -> None:
    result = ss.update_source(
        vault,
        "feed",
        applied_cursor="A",
        config_dir=config_dir,
        now=NOW,
    )

    assert result["status"] == "fail"
    assert result["debt"]["reason"] == "inconsistent_state"


@pytest.mark.parametrize(
    "payload,error_type,match",
    [
        ("{not-json", ss.SourceStateCorruptError, "corrupt"),
        (
            json.dumps(
                {
                    "version": 999,
                    "vault_path": "placeholder",
                    "updated_at": None,
                    "sources": {},
                }
            ),
            ss.SourceStateVersionError,
            "unsupported",
        ),
    ],
)
def test_corruption_and_future_version_are_not_overwritten(
    vault: Path,
    config_dir: Path,
    payload: str,
    error_type: type[Exception],
    match: str,
) -> None:
    path = _state_file(vault, config_dir)
    path.parent.mkdir(parents=True)
    if '"placeholder"' in payload:
        payload = payload.replace('"placeholder"', json.dumps(str(vault.resolve())))
    path.write_text(payload, encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(error_type, match=match):
        ss.update_source(
            vault,
            "feed",
            observed_cursor="A",
            config_dir=config_dir,
            now=NOW,
        )

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        '{"version": 1, "version": 1}',
        '{"version": 1, "vault_path": "/tmp", "updated_at": null, '
        '"sources": {}, "future": NaN}',
        '{"version": 1, "vault_path": "/tmp", "updated_at": null, '
        '"sources": {}, "future": 1e999}',
    ],
)
def test_non_standard_or_ambiguous_json_is_rejected(
    vault: Path, config_dir: Path, payload: str
) -> None:
    path = _state_file(vault, config_dir)
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ss.SourceStateCorruptError, match="corrupt"):
        ss.load_state(vault, config_dir=config_dir)


@pytest.mark.parametrize(
    "invalid_version",
    [True, False, "1", 1.0, None],
)
def test_non_integer_schema_versions_fail_closed(
    vault: Path, config_dir: Path, invalid_version: object
) -> None:
    path = _state_file(vault, config_dir)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": invalid_version,
                "vault_path": str(vault.resolve()),
                "updated_at": None,
                "sources": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ss.SourceStateVersionError, match="unsupported"):
        ss.load_state(vault, config_dir=config_dir)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_stale_threshold_is_rejected(
    vault: Path, config_dir: Path, value: float
) -> None:
    with pytest.raises(ValueError, match="non-negative number"):
        ss.update_source(
            vault,
            "feed",
            stale_after_seconds=value,
            config_dir=config_dir,
            now=NOW,
        )
    assert not _state_file(vault, config_dir).exists()


@pytest.mark.parametrize(
    "heartbeat",
    [
        {"status": "ok", "last_attempt_at": NOW, "error": None},
        {
            "status": "ok",
            "last_attempt_at": NOW,
            "last_success_at": NOW,
            "error": "stale error",
        },
        {"status": "error", "last_attempt_at": NOW, "error": None},
        {"last_attempt_at": NOW},
    ],
)
def test_inconsistent_heartbeat_fails_closed(
    vault: Path, config_dir: Path, heartbeat: dict[str, object]
) -> None:
    path = _state_file(vault, config_dir)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "vault_path": str(vault.resolve()),
                "updated_at": NOW,
                "sources": {"feed": {"heartbeat": heartbeat}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ss.SourceStateCorruptError, match="heartbeat"):
        ss.load_state(vault, config_dir=config_dir)


def test_cursor_without_kind_fails_closed(vault: Path, config_dir: Path) -> None:
    path = _state_file(vault, config_dir)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "vault_path": str(vault.resolve()),
                "updated_at": NOW,
                "sources": {
                    "feed": {"observed": {"value": "A", "at": NOW}}
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ss.SourceStateCorruptError, match="cursor_kind"):
        ss.load_state(vault, config_dir=config_dir)


def test_error_summary_is_single_line_and_bounded(
    vault: Path, config_dir: Path
) -> None:
    result = ss.update_source(
        vault,
        "feed",
        heartbeat_status="error",
        heartbeat_error=("  first line\nsecond line  " + ("x" * 3000)),
        config_dir=config_dir,
        now=NOW,
    )

    error = result["heartbeat"]["error"]
    assert "\n" not in error
    assert error.startswith("first line second line")
    assert len(error) == ss.MAX_ERROR_LENGTH


def test_unknown_fields_survive_read_modify_write(
    vault: Path, config_dir: Path
) -> None:
    ss.update_source(
        vault,
        "feed",
        observed_cursor="A",
        config_dir=config_dir,
        now=NOW,
    )
    path = _state_file(vault, config_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["future_top_level"] = {"enabled": True}
    data["sources"]["feed"]["provider_metadata"] = {"token": "redacted"}
    path.write_text(json.dumps(data), encoding="utf-8")

    ss.update_source(
        vault,
        "feed",
        applied_cursor="A",
        config_dir=config_dir,
        now="2026-09-01T13:00:00Z",
    )

    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["future_top_level"] == {"enabled": True}
    assert updated["sources"]["feed"]["provider_metadata"] == {"token": "redacted"}


def test_concurrent_updates_to_different_sources_are_merged(
    vault: Path, config_dir: Path
) -> None:
    processes = [
        multiprocessing.Process(
            target=_concurrent_update,
            args=(str(vault), str(config_dir), f"feed-{index}", str(index)),
        )
        for index in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    state = ss.load_state(vault, config_dir=config_dir)
    assert sorted(state["sources"]) == [f"feed-{index}" for index in range(6)]


def test_lock_is_released_after_exception_and_stale_lock_is_recovered(
    vault: Path, config_dir: Path
) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with ss.source_state_lock(vault, config_dir=config_dir):
            raise RuntimeError("boom")
    assert not ss.lock_path(vault, config_dir=config_dir).exists()

    lock = ss.lock_path(vault, config_dir=config_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text('{"token": "dead"}', encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))
    with ss.source_state_lock(
        vault,
        config_dir=config_dir,
        timeout=0.2,
        stale_after=60,
    ):
        assert lock.exists()
    assert not lock.exists()


def test_lock_release_does_not_remove_a_different_owners_lock(
    vault: Path, config_dir: Path
) -> None:
    lock = ss.lock_path(vault, config_dir=config_dir)
    with ss.source_state_lock(vault, config_dir=config_dir):
        lock.write_text('{"token": "new-owner"}', encoding="utf-8")

    assert lock.exists()
    lock.unlink()


def test_atomic_replace_failure_preserves_old_state_and_cleans_temp_files(
    vault: Path, config_dir: Path, monkeypatch
) -> None:
    ss.update_source(
        vault,
        "feed",
        observed_cursor="A",
        config_dir=config_dir,
        now=NOW,
    )
    path = _state_file(vault, config_dir)
    before = path.read_bytes()

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(ss.os, "replace", fail_replace)
    with pytest.raises(ss.SourceStateError, match="replace failure"):
        ss.update_source(
            vault,
            "feed",
            observed_cursor="B",
            config_dir=config_dir,
            now="2026-09-01T13:00:00Z",
        )

    assert path.read_bytes() == before
    assert list(path.parent.glob(".source-state-*.tmp")) == []


def test_state_is_external_and_manifest_is_untouched(
    vault: Path, config_dir: Path
) -> None:
    manifest = vault / ".manifest.json"
    manifest.write_bytes(b'{"sources": {}}\n')
    before = manifest.read_bytes()

    ss.update_source(
        vault,
        "feed",
        observed_cursor="A",
        config_dir=config_dir,
        now=NOW,
    )

    path = _state_file(vault, config_dir)
    assert vault.resolve() not in path.resolve().parents
    assert manifest.read_bytes() == before


def test_requested_unknown_source_is_reported_without_writing(
    vault: Path, config_dir: Path
) -> None:
    report = ss.build_report(
        vault,
        source_ids=["missing"],
        config_dir=config_dir,
        now=NOW,
    )

    assert report["status"] == "warn"
    assert report["summary"]["untracked"] == 1
    assert report["sources"]["missing"]["status"] == "untracked"
    assert not config_dir.exists()
