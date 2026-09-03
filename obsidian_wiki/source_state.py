"""Durable runtime state for continuously polled wiki sources.

The ingest manifest records content that reached the vault.  This module keeps
the higher-frequency pull/apply state outside the vault so polling never dirties
the vault itself.  Cursors are deliberately opaque: the core only compares
them for equality and never assumes ordering or a provider-specific format.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


SCHEMA_VERSION = 1
DEFAULT_STALE_AFTER_SECONDS = 60.0
MAX_ERROR_LENGTH = 2048
_UNSET = object()


class SourceStateError(RuntimeError):
    """Base class for source-state failures."""


class SourceStateCorruptError(SourceStateError):
    """The state file exists but cannot be trusted."""


class SourceStateVersionError(SourceStateError):
    """The state file uses an unsupported schema version."""


class SourceStateLockTimeout(SourceStateError):
    """The source-state lock could not be acquired before the timeout."""


def resolve_global_config_dir(
    *,
    home: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Path:
    """Resolve the XDG config directory with the CLI's legacy fallback."""
    environment = os.environ if environ is None else environ
    user_home = Path.home() if home is None else Path(home)
    xdg_home = environment.get("XDG_CONFIG_HOME", "").strip()
    xdg_dir = (
        Path(xdg_home).expanduser() if xdg_home else user_home / ".config"
    ) / "obsidian-wiki"
    legacy_dir = user_home / ".obsidian-wiki"
    if legacy_dir.is_dir() and not xdg_dir.exists():
        return legacy_dir
    return xdg_dir


def canonical_vault_path(vault: Path) -> str:
    """Return the stable absolute identity used for one vault."""
    return str(Path(vault).expanduser().resolve())


def vault_id(vault: Path) -> str:
    """Return a collision-resistant, filesystem-safe vault identifier."""
    canonical = canonical_vault_path(vault)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _legacy_vault_id(vault: Path) -> str:
    """Match the historic shell ``echo "$vault" | md5sum`` identifier."""
    value = canonical_vault_path(vault) + "\n"
    return hashlib.md5(value.encode("utf-8")).hexdigest()[:8]  # noqa: S324


def state_dir(vault: Path, *, config_dir: Path | None = None) -> Path:
    """Return the external, vault-scoped state directory.

    Existing state directories carrying ``.vault_path`` are reused.  This keeps
    compatibility with the daily-update sidecar without trusting an 8-character
    hash collision.  New directories use a 16-character SHA-256 prefix.
    """
    root = Path(config_dir) if config_dir is not None else resolve_global_config_dir()
    state_root = root.expanduser() / "state"
    if state_root.exists() and not state_root.is_dir():
        raise SourceStateCorruptError(
            f"source-state root is not a directory: {state_root}"
        )
    canonical = canonical_vault_path(vault)
    if state_root.is_dir():
        for candidate in sorted(state_root.iterdir()):
            marker = candidate / ".vault_path"
            if not candidate.is_dir() or not marker.is_file():
                continue
            try:
                recorded = marker.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if recorded and canonical_vault_path(Path(recorded)) == canonical:
                return candidate

    legacy = state_root / _legacy_vault_id(vault)
    marker = legacy / ".vault_path"
    if legacy.is_dir():
        if marker.is_file():
            recorded = _read_vault_marker(marker)
            if canonical_vault_path(Path(recorded)) != canonical:
                raise SourceStateCorruptError(
                    f"source-state directory collision: {legacy} belongs to {recorded}"
                )
        return legacy
    target = state_root / vault_id(vault)
    if target.exists() and not target.is_dir():
        raise SourceStateCorruptError(
            f"source-state vault path is not a directory: {target}"
        )
    target_marker = target / ".vault_path"
    if target_marker.exists():
        recorded = _read_vault_marker(target_marker)
        if canonical_vault_path(Path(recorded)) != canonical:
            raise SourceStateCorruptError(
                f"source-state directory collision: {target} belongs to {recorded}"
            )
    return target


def state_path(vault: Path, *, config_dir: Path | None = None) -> Path:
    return state_dir(vault, config_dir=config_dir) / "source-state.json"


def lock_path(vault: Path, *, config_dir: Path | None = None) -> Path:
    return state_dir(vault, config_dir=config_dir) / "source-state.lock"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_vault_marker(path: Path) -> str:
    try:
        recorded = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SourceStateError(
            f"cannot read source-state vault identity {path}: {exc}"
        ) from exc
    if not recorded:
        raise SourceStateCorruptError(
            f"source-state vault identity is empty: {path}"
        )
    return recorded


def _parse_time(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise SourceStateCorruptError(f"{field} must be a non-empty RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceStateCorruptError(f"{field} is not a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise SourceStateCorruptError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _empty_state(vault: Path) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "vault_path": canonical_vault_path(vault),
        "updated_at": None,
        "sources": {},
    }


def _validate_cursor(value: object, *, field: str) -> None:
    if not isinstance(value, dict):
        raise SourceStateCorruptError(f"{field} must be an object")
    if not {"value", "at"}.issubset(value):
        raise SourceStateCorruptError(f"{field} must contain value and at")
    if not isinstance(value["value"], str) or not value["value"]:
        raise SourceStateCorruptError(f"{field}.value must be a non-empty string")
    _parse_time(value["at"], field=f"{field}.at")


def _validate_state(data: object, vault: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SourceStateCorruptError("source-state root must be a JSON object")
    version = data.get("version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise SourceStateVersionError(
            f"unsupported source-state version {version!r}; expected {SCHEMA_VERSION}"
        )
    canonical = canonical_vault_path(vault)
    recorded = data.get("vault_path")
    if not isinstance(recorded, str) or canonical_vault_path(Path(recorded)) != canonical:
        raise SourceStateCorruptError(
            f"source-state vault mismatch: expected {canonical}, found {recorded!r}"
        )
    sources = data.get("sources")
    if not isinstance(sources, dict):
        raise SourceStateCorruptError("source-state sources must be an object")
    updated_at = data.get("updated_at")
    if updated_at is not None:
        _parse_time(updated_at, field="updated_at")

    for source_id, entry in sources.items():
        if not isinstance(source_id, str) or not source_id:
            raise SourceStateCorruptError("source ids must be non-empty strings")
        if not isinstance(entry, dict):
            raise SourceStateCorruptError(f"source {source_id!r} must be an object")
        cursor_kind = entry.get("cursor_kind")
        if cursor_kind is not None and (
            not isinstance(cursor_kind, str) or not cursor_kind
        ):
            raise SourceStateCorruptError(
                f"sources.{source_id}.cursor_kind must be a non-empty string"
            )
        has_cursor = False
        for cursor_name in ("observed", "applied"):
            cursor = entry.get(cursor_name)
            if cursor is not None:
                has_cursor = True
                _validate_cursor(cursor, field=f"sources.{source_id}.{cursor_name}")
        if has_cursor and cursor_kind is None:
            raise SourceStateCorruptError(
                f"sources.{source_id}.cursor_kind is required when cursors exist"
            )
        heartbeat = entry.get("heartbeat")
        if heartbeat is not None:
            if not isinstance(heartbeat, dict):
                raise SourceStateCorruptError(
                    f"sources.{source_id}.heartbeat must be an object"
                )
            status = heartbeat.get("status")
            if status is not None and status not in ("ok", "error"):
                raise SourceStateCorruptError(
                    f"sources.{source_id}.heartbeat.status must be ok or error"
                )
            for time_field in ("last_attempt_at", "last_success_at"):
                value = heartbeat.get(time_field)
                if value is not None:
                    _parse_time(
                        value,
                        field=f"sources.{source_id}.heartbeat.{time_field}",
                    )
            stale_after = heartbeat.get("stale_after_seconds")
            if stale_after is not None and (
                isinstance(stale_after, bool)
                or not isinstance(stale_after, (int, float))
                or not math.isfinite(stale_after)
                or stale_after < 0
            ):
                raise SourceStateCorruptError(
                    f"sources.{source_id}.heartbeat.stale_after_seconds "
                    "must be a non-negative number"
                )
            error = heartbeat.get("error")
            if error is not None and not isinstance(error, str):
                raise SourceStateCorruptError(
                    f"sources.{source_id}.heartbeat.error must be a string or null"
                )
            if isinstance(error, str) and (
                not error.strip() or len(error) > MAX_ERROR_LENGTH
            ):
                raise SourceStateCorruptError(
                    f"sources.{source_id}.heartbeat.error must be a non-empty "
                    f"summary of at most {MAX_ERROR_LENGTH} characters"
                )
            attempt = heartbeat.get("last_attempt_at")
            success = heartbeat.get("last_success_at")
            if status == "ok":
                if attempt is None or success is None or error is not None:
                    raise SourceStateCorruptError(
                        f"sources.{source_id}.heartbeat ok status requires "
                        "attempt/success timestamps and no error"
                    )
            elif status == "error":
                if attempt is None or not isinstance(error, str):
                    raise SourceStateCorruptError(
                        f"sources.{source_id}.heartbeat error status requires "
                        "an attempt timestamp and error summary"
                    )
            elif any(value is not None for value in (attempt, success, error)):
                raise SourceStateCorruptError(
                    f"sources.{source_id}.heartbeat timestamps/error require status"
                )
            if attempt is not None and success is not None:
                attempt_time = _parse_time(
                    attempt,
                    field=f"sources.{source_id}.heartbeat.last_attempt_at",
                )
                success_time = _parse_time(
                    success,
                    field=f"sources.{source_id}.heartbeat.last_success_at",
                )
                if success_time > attempt_time:
                    raise SourceStateCorruptError(
                        f"sources.{source_id}.heartbeat last_success_at "
                        "cannot be later than last_attempt_at"
                    )
    return data


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite number {value} is not valid source-state JSON")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite number {value} is not valid source-state JSON")
    return parsed


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_state(vault: Path, *, config_dir: Path | None = None) -> dict[str, Any]:
    """Load and validate source state; missing state is a non-persisted empty view."""
    path = state_path(vault, config_dir=config_dir)
    if not path.exists():
        return _empty_state(vault)
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_float,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise SourceStateCorruptError(
            f"source-state is corrupt at {path}: {exc}"
        ) from exc
    except OSError as exc:
        raise SourceStateError(f"cannot read source-state {path}: {exc}") from exc
    return _validate_state(data, vault)


def _read_lock_token(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    token = payload.get("token") if isinstance(payload, dict) else None
    return token if isinstance(token, str) else None


@contextmanager
def source_state_lock(
    vault: Path,
    *,
    config_dir: Path | None = None,
    timeout: float = 10.0,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
) -> Iterator[None]:
    """Acquire an owner-token advisory lock for one vault's source state."""
    directory = state_dir(vault, config_dir=config_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SourceStateError(
            f"cannot create source-state directory {directory}: {exc}"
        ) from exc
    lock = directory / "source-state.lock"
    token = secrets.token_hex(16)
    payload = json.dumps({"pid": os.getpid(), "token": token, "created_at": _utc_now()})
    deadline = time.monotonic() + timeout
    while True:
        try:
            descriptor = os.open(
                lock,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass
                raise
            break
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > stale_after:
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise SourceStateLockTimeout(
                    f"could not acquire {lock} within {timeout}s "
                    f"(held for {age:.1f}s; stale after {stale_after}s)"
                )
            time.sleep(0.05)
        except OSError as exc:
            raise SourceStateError(
                f"cannot acquire source-state lock {lock}: {exc}"
            ) from exc
    try:
        yield
    finally:
        if _read_lock_token(lock) == token:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass


def _write_state(path: Path, state: dict[str, Any]) -> None:
    """Durably replace one state snapshot without exposing partial JSON."""
    try:
        payload = json.dumps(
            state,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise SourceStateError(f"source-state is not valid JSON data: {exc}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".source-state-{secrets.token_hex(16)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(path.parent, flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise SourceStateError(f"cannot write source-state {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_vault_marker(path: Path, vault: Path) -> None:
    """Atomically write the canonical vault identity alongside source state."""
    canonical = canonical_vault_path(vault)
    if path.exists():
        recorded = _read_vault_marker(path)
        if canonical_vault_path(Path(recorded)) != canonical:
            raise SourceStateCorruptError(
                f"source-state directory collision: {path.parent} "
                f"belongs to {recorded}"
            )
        return

    temporary = path.with_name(f".vault-path-{secrets.token_hex(16)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(canonical + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(path.parent, flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise SourceStateError(
            f"cannot write source-state vault identity {path}: {exc}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def update_state(
    vault: Path,
    mutator: Callable[[dict[str, Any]], None],
    *,
    config_dir: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Reload, mutate, validate, and atomically replace state under one lock."""
    timestamp = now or _utc_now()
    _parse_time(timestamp, field="now")
    with source_state_lock(vault, config_dir=config_dir):
        state = load_state(vault, config_dir=config_dir)
        mutator(state)
        state["updated_at"] = timestamp
        _validate_state(state, vault)
        directory = state_dir(vault, config_dir=config_dir)
        marker = directory / ".vault_path"
        _write_vault_marker(marker, vault)
        _write_state(directory / "source-state.json", state)
        return state


def _validate_nonempty(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def update_source(
    vault: Path,
    source_id: str,
    *,
    observed_cursor: object = _UNSET,
    applied_cursor: object = _UNSET,
    cursor_kind: object = _UNSET,
    heartbeat_status: object = _UNSET,
    heartbeat_error: object = _UNSET,
    stale_after_seconds: object = _UNSET,
    config_dir: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Atomically update one source and return its derived report entry."""
    source_id = _validate_nonempty(source_id, field="source_id")
    if all(
        value is _UNSET
        for value in (
            observed_cursor,
            applied_cursor,
            cursor_kind,
            heartbeat_status,
            stale_after_seconds,
        )
    ):
        raise ValueError("no source-state update requested")
    timestamp = now or _utc_now()
    _parse_time(timestamp, field="now")
    if observed_cursor is not _UNSET:
        observed_cursor = _validate_nonempty(
            observed_cursor, field="observed_cursor"
        )
    if applied_cursor is not _UNSET:
        applied_cursor = _validate_nonempty(applied_cursor, field="applied_cursor")
    if cursor_kind is not _UNSET:
        cursor_kind = _validate_nonempty(cursor_kind, field="cursor_kind")
    if heartbeat_status is not _UNSET and heartbeat_status not in ("ok", "error"):
        raise ValueError("heartbeat_status must be ok or error")
    if heartbeat_status == "error":
        if heartbeat_error is _UNSET:
            heartbeat_error = "source poll failed"
        heartbeat_error = " ".join(
            _validate_nonempty(
                heartbeat_error, field="heartbeat_error"
            ).split()
        )[:MAX_ERROR_LENGTH]
    elif heartbeat_error is not _UNSET:
        raise ValueError("heartbeat_error requires heartbeat_status='error'")
    if stale_after_seconds is not _UNSET and (
        isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, (int, float))
        or not math.isfinite(stale_after_seconds)
        or stale_after_seconds < 0
    ):
        raise ValueError("stale_after_seconds must be a non-negative number")

    def mutate(state: dict[str, Any]) -> None:
        sources = state["sources"]
        entry = sources.setdefault(source_id, {})
        if not isinstance(entry, dict):
            raise SourceStateCorruptError(f"source {source_id!r} must be an object")
        if cursor_kind is not _UNSET:
            existing_kind = entry.get("cursor_kind")
            if existing_kind is not None and existing_kind != cursor_kind:
                raise ValueError(
                    f"cursor_kind cannot change from {existing_kind!r} "
                    f"to {cursor_kind!r}"
                )
            entry["cursor_kind"] = cursor_kind
        elif ("observed" in entry or "applied" in entry) and not entry.get(
            "cursor_kind"
        ):
            entry["cursor_kind"] = "opaque"
        elif observed_cursor is not _UNSET or applied_cursor is not _UNSET:
            entry["cursor_kind"] = "opaque"

        for name, value in (
            ("observed", observed_cursor),
            ("applied", applied_cursor),
        ):
            if value is _UNSET:
                continue
            previous = entry.get(name)
            if not isinstance(previous, dict) or previous.get("value") != value:
                entry[name] = {"value": value, "at": timestamp}

        if heartbeat_status is not _UNSET or stale_after_seconds is not _UNSET:
            heartbeat = entry.setdefault("heartbeat", {})
            if not isinstance(heartbeat, dict):
                raise SourceStateCorruptError(
                    f"sources.{source_id}.heartbeat must be an object"
                )
            if stale_after_seconds is not _UNSET:
                heartbeat["stale_after_seconds"] = stale_after_seconds
            if heartbeat_status is not _UNSET:
                heartbeat["last_attempt_at"] = timestamp
                heartbeat["status"] = heartbeat_status
                if heartbeat_status == "ok":
                    heartbeat["last_success_at"] = timestamp
                    heartbeat["error"] = None
                else:
                    heartbeat["error"] = heartbeat_error

    state = update_state(vault, mutate, config_dir=config_dir, now=timestamp)
    return source_report(source_id, state["sources"][source_id], now=timestamp)


def _seconds_since(value: str, now: datetime) -> float:
    return max(0.0, (now - _parse_time(value, field="timestamp")).total_seconds())


def source_report(
    source_id: str,
    entry: dict[str, Any] | None,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Build the public view for one source, deriving debt and freshness."""
    if entry is None:
        return {
            "source": source_id,
            "tracked": False,
            "cursor_kind": None,
            "observed_cursor": None,
            "applied_cursor": None,
            "debt": {"pending": False, "reason": "untracked", "age_seconds": None},
            "heartbeat": {
                "status": "unknown",
                "last_attempt_at": None,
                "last_success_at": None,
                "error": None,
                "stale": None,
                "age_seconds": None,
                "stale_after_seconds": None,
            },
            "status": "untracked",
        }

    current = _parse_time(now or _utc_now(), field="now")
    observed = entry.get("observed")
    applied = entry.get("applied")
    observed_value = observed.get("value") if isinstance(observed, dict) else None
    applied_value = applied.get("value") if isinstance(applied, dict) else None
    if observed_value is None and applied_value is not None:
        debt_pending = True
        debt_reason = "inconsistent_state"
        debt_age = _seconds_since(applied["at"], current)
    elif observed_value is not None and applied_value is None:
        debt_pending = True
        debt_reason = "never_applied"
        debt_age = _seconds_since(observed["at"], current)
    elif observed_value != applied_value:
        debt_pending = True
        debt_reason = "cursor_mismatch"
        debt_age = _seconds_since(observed["at"], current)
    else:
        debt_pending = False
        debt_reason = "none"
        debt_age = None

    heartbeat = entry.get("heartbeat")
    heartbeat = heartbeat if isinstance(heartbeat, dict) else {}
    last_success = heartbeat.get("last_success_at")
    threshold = heartbeat.get("stale_after_seconds")
    success_age = (
        _seconds_since(last_success, current)
        if isinstance(last_success, str)
        else None
    )
    if threshold is None:
        stale = None
    else:
        stale = success_age is None or success_age > float(threshold)

    heartbeat_status = heartbeat.get("status", "unknown")
    status = "pass"
    if debt_reason == "inconsistent_state" or heartbeat_status == "error":
        status = "fail"
    elif debt_pending or stale:
        status = "warn"
    return {
        "source": source_id,
        "tracked": True,
        "cursor_kind": entry.get("cursor_kind"),
        "observed_cursor": observed_value,
        "applied_cursor": applied_value,
        "debt": {
            "pending": debt_pending,
            "reason": debt_reason,
            "age_seconds": debt_age,
        },
        "heartbeat": {
            "status": heartbeat_status,
            "last_attempt_at": heartbeat.get("last_attempt_at"),
            "last_success_at": last_success,
            "error": heartbeat.get("error"),
            "stale": stale,
            "age_seconds": success_age,
            "stale_after_seconds": threshold,
        },
        "status": status,
    }


def build_report(
    vault: Path,
    *,
    source_ids: list[str] | None = None,
    config_dir: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Load state and report debt/heartbeat health for all or selected sources."""
    state = load_state(vault, config_dir=config_dir)
    tracked = state["sources"]
    selected = sorted(tracked) if source_ids is None else list(dict.fromkeys(source_ids))
    reports = {
        source_id: source_report(source_id, tracked.get(source_id), now=now)
        for source_id in selected
    }
    summary = {
        "tracked": sum(1 for item in reports.values() if item["tracked"]),
        "healthy": sum(1 for item in reports.values() if item["status"] == "pass"),
        "debt": sum(1 for item in reports.values() if item["debt"]["pending"]),
        "stale": sum(1 for item in reports.values() if item["heartbeat"]["stale"] is True),
        "error": sum(1 for item in reports.values() if item["status"] == "fail"),
        "untracked": sum(1 for item in reports.values() if not item["tracked"]),
    }
    if summary["error"]:
        status = "fail"
    elif summary["debt"] or summary["stale"] or summary["untracked"]:
        status = "warn"
    else:
        status = "pass"
    return {
        "version": SCHEMA_VERSION,
        "status": status,
        "vault_path": state["vault_path"],
        "state_path": str(state_path(vault, config_dir=config_dir)),
        "updated_at": state["updated_at"],
        "summary": summary,
        "sources": reports,
    }
