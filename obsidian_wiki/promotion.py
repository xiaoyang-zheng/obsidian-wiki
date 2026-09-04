"""Candidate promotion ledger for concept/entity pages.

The ingest flow can observe repeated mentions of concepts and entities before a
canonical page should exist.  This module keeps that intermediate state in one
strict JSON ledger and returns promotion plans for callers to consume.  It does
not create Markdown pages directly.
"""

from __future__ import annotations

import builtins
import copy
import errno
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


PROMOTION_LEDGER_RELATIVE_PATH = Path("_meta/promotion-candidates.json")
PROMOTION_LOCK_RELATIVE_PATH = Path("_meta/promotion-candidates.lock")
PROMOTION_LEDGER_SCHEMA_VERSION = 1

ALLOWED_KINDS = frozenset({"concept", "entity"})
ALLOWED_STATES = frozenset({"candidate", "eligible", "promoted", "rejected"})
TERMINAL_STATES = frozenset({"promoted", "rejected"})
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
DEFAULT_POLICY = {
    "core_contribution_confidence": 0.85,
    "independent_lineage_count": 2,
    "lineage_confidence": 0.70,
}
_LEDGER_KEYS = frozenset({"schema_version", "updated_at", "policy", "candidates"})
_CANDIDATE_KEYS = frozenset(
    {
        "kind",
        "canonical_title",
        "canonical_slug",
        "aliases",
        "state",
        "created_at",
        "updated_at",
        "confidence",
        "core_contribution",
        "source_lineages",
        "evidence_paths",
        "ambiguous",
        "conflicts",
        "eligibility",
        "promotion_plan",
        "canonical_path",
        "resolved_at",
        "resolution_reason",
        "resolved_by",
    }
)
_LINEAGE_KEYS = frozenset(
    {
        "lineage",
        "first_seen_at",
        "last_seen_at",
        "observations",
        "max_confidence",
        "core_contribution",
        "evidence_paths",
    }
)
_ELIGIBILITY_KEYS = frozenset(
    {
        "eligible",
        "reason",
        "independent_lineage_count",
        "eligible_lineage_count",
        "independent_lineage_threshold",
        "blocked",
    }
)

_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?")
_UNSET = object()


class PromotionError(RuntimeError):
    """Base class for promotion ledger failures."""


class PromotionLedgerCorruptError(PromotionError):
    """The promotion ledger exists but cannot be trusted."""


class PromotionLedgerVersionError(PromotionError):
    """The promotion ledger uses an unsupported schema version."""


class PromotionLockTimeout(PromotionError):
    """The promotion ledger lock could not be acquired before the timeout."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise PromotionLedgerCorruptError(
            f"{field} must be a non-empty RFC3339 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PromotionLedgerCorruptError(
            f"{field} is not a valid RFC3339 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise PromotionLedgerCorruptError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_now(value: str) -> str:
    _parse_time(value, field="now")
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite number {value} is not valid promotion JSON")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite number {value} is not valid promotion JSON")
    return parsed


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _canonical_vault(vault: Path) -> Path:
    try:
        return Path(vault).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise PromotionError(f"vault does not exist: {vault}") from exc


def ledger_path(vault: Path) -> Path:
    """Return the canonical vault-local promotion candidate ledger path."""
    return _canonical_vault(vault) / PROMOTION_LEDGER_RELATIVE_PATH


def lock_path(vault: Path) -> Path:
    """Return the canonical vault-local promotion candidate lock path."""
    return _canonical_vault(vault) / PROMOTION_LOCK_RELATIVE_PATH


def _empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": PROMOTION_LEDGER_SCHEMA_VERSION,
        "updated_at": None,
        "policy": dict(DEFAULT_POLICY),
        "candidates": {},
    }


def _validate_kind(value: object) -> str:
    if not isinstance(value, str) or value not in ALLOWED_KINDS:
        raise ValueError("kind must be concept or entity")
    return value


def _validate_state(value: object) -> str:
    if not isinstance(value, str) or value not in ALLOWED_STATES:
        raise ValueError("state must be candidate, eligible, promoted, or rejected")
    return value


def slug_from_title(title: str) -> str:
    """Derive a deterministic ASCII slug from a title."""
    title = _validate_text(title, field="canonical_title")
    normalised = unicodedata.normalize("NFKD", title)
    ascii_title = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise ValueError("canonical_slug is required when title has no ASCII slug")
    return _validate_slug(slug)


def _validate_slug(value: object) -> str:
    if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
        raise ValueError(
            "canonical_slug must contain only lowercase letters, digits, and hyphens"
        )
    return value


def _validate_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    candidate = " ".join(value.strip().split())
    if not candidate:
        raise ValueError(f"{field} must be a non-empty string")
    if any(ord(character) < 32 for character in candidate):
        raise ValueError(f"{field} must not contain control characters")
    return candidate


def _validate_lineage(value: object) -> str:
    lineage = _validate_text(value, field="source_lineage")
    if len(lineage) > 512:
        raise ValueError("source_lineage must be at most 512 characters")
    return lineage


def _validate_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be a number in [0.0, 1.0]")
    confidence = float(value)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be a number in [0.0, 1.0]")
    return confidence


def _validate_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _validate_relpath(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"{field} must be a safe vault-relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or "." in path.parts
        or ".." in path.parts
        or value.endswith("/")
    ):
        raise ValueError(f"{field} must be a safe vault-relative POSIX path")
    return value


def _normalise_label(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def _unique_sorted_text(values: list[str]) -> list[str]:
    by_key: dict[str, str] = {}
    for value in values:
        text = _validate_text(value, field="alias")
        key = _normalise_label(text)
        by_key.setdefault(key, text)
    return [by_key[key] for key in sorted(by_key)]


def _validate_aliases(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, builtins.list):
        raise ValueError("aliases must be a list of strings")
    return _unique_sorted_text(value)


def _candidate_key(kind: str, slug: str) -> str:
    return f"{kind}:{slug}"


def _target_path(kind: str, slug: str) -> str:
    directory = "concepts" if kind == "concept" else "entities"
    return f"{directory}/{slug}.md"


def _validate_policy(value: object) -> dict[str, Any]:
    if value is None:
        value = DEFAULT_POLICY
    if not isinstance(value, dict):
        raise PromotionLedgerCorruptError("policy must be an object")
    policy = dict(DEFAULT_POLICY)
    for key, raw in value.items():
        if key not in DEFAULT_POLICY:
            raise PromotionLedgerCorruptError(f"unsupported policy field: {key}")
        if key == "independent_lineage_count":
            if isinstance(raw, bool) or type(raw) is not int or raw < 1:
                raise PromotionLedgerCorruptError(
                    "policy.independent_lineage_count must be a positive integer"
                )
            policy[key] = raw
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise PromotionLedgerCorruptError(f"policy.{key} must be a finite number")
        number = float(raw)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise PromotionLedgerCorruptError(
                f"policy.{key} must be a finite number in [0.0, 1.0]"
            )
        policy[key] = number
    return policy


def _reject_unknown_keys(
    value: dict[str, Any],
    allowed: frozenset[str],
    *,
    field: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PromotionLedgerCorruptError(
            f"{field} contains unsupported field(s): {', '.join(unknown)}"
        )


def _validate_lineage_entry(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PromotionLedgerCorruptError(f"{field} must be an object")
    _reject_unknown_keys(value, _LINEAGE_KEYS, field=field)
    lineage = _validate_lineage(value.get("lineage"))
    first_seen_at = value.get("first_seen_at")
    last_seen_at = value.get("last_seen_at")
    _parse_time(first_seen_at, field=f"{field}.first_seen_at")
    _parse_time(last_seen_at, field=f"{field}.last_seen_at")
    observations = value.get("observations")
    if isinstance(observations, bool) or type(observations) is not int or observations < 1:
        raise PromotionLedgerCorruptError(
            f"{field}.observations must be a positive integer"
        )
    max_confidence = _validate_confidence(value.get("max_confidence"))
    core_contribution = _validate_bool(
        value.get("core_contribution"),
        field=f"{field}.core_contribution",
    )
    evidence_paths = value.get("evidence_paths")
    if not isinstance(evidence_paths, builtins.list) or not evidence_paths:
        raise PromotionLedgerCorruptError(
            f"{field}.evidence_paths must be a non-empty list"
        )
    validated_paths = sorted(
        set(_validate_relpath(path, field=f"{field}.evidence_paths") for path in evidence_paths)
    )
    if len(validated_paths) != len(evidence_paths):
        raise PromotionLedgerCorruptError(
            f"{field}.evidence_paths must not contain duplicates"
        )
    return {
        "lineage": lineage,
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "observations": observations,
        "max_confidence": max_confidence,
        "core_contribution": core_contribution,
        "evidence_paths": validated_paths,
    }


def _validate_eligibility(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PromotionLedgerCorruptError(f"{field} must be an object")
    _reject_unknown_keys(value, _ELIGIBILITY_KEYS, field=field)
    eligible = value.get("eligible")
    if not isinstance(eligible, bool):
        raise PromotionLedgerCorruptError(f"{field}.eligible must be a boolean")
    reason = _validate_text(value.get("reason"), field=f"{field}.reason")
    independent_count = value.get("independent_lineage_count")
    eligible_count = value.get("eligible_lineage_count")
    threshold = value.get("independent_lineage_threshold")
    for name, raw in (
        ("independent_lineage_count", independent_count),
        ("eligible_lineage_count", eligible_count),
        ("independent_lineage_threshold", threshold),
    ):
        if isinstance(raw, bool) or type(raw) is not int or raw < 0:
            raise PromotionLedgerCorruptError(f"{field}.{name} must be a non-negative integer")
    if threshold < 1:
        raise PromotionLedgerCorruptError(
            f"{field}.independent_lineage_threshold must be positive"
        )
    blocked = value.get("blocked")
    if not isinstance(blocked, builtins.list) or not all(
        isinstance(item, str) and item for item in blocked
    ):
        raise PromotionLedgerCorruptError(f"{field}.blocked must be a list of strings")
    return {
        "eligible": eligible,
        "reason": reason,
        "independent_lineage_count": independent_count,
        "eligible_lineage_count": eligible_count,
        "independent_lineage_threshold": threshold,
        "blocked": sorted(set(blocked)),
    }


def _copy_plan(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    return copy.deepcopy(plan) if plan is not None else None


def _validate_candidate_payload(
    value: dict[str, Any],
    *,
    key: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    _reject_unknown_keys(value, _CANDIDATE_KEYS, field=f"candidates.{key}")
    kind = _validate_kind(value.get("kind"))
    slug = _validate_slug(value.get("canonical_slug"))
    if key != _candidate_key(kind, slug):
        raise PromotionLedgerCorruptError(f"candidate key mismatch for {key!r}")
    title = _validate_text(value.get("canonical_title"), field="canonical_title")
    aliases = _validate_aliases(value.get("aliases", []))
    state = _validate_state(value.get("state"))
    created_at = value.get("created_at")
    updated_at = value.get("updated_at")
    _parse_time(created_at, field=f"candidates.{key}.created_at")
    _parse_time(updated_at, field=f"candidates.{key}.updated_at")
    confidence = _validate_confidence(value.get("confidence"))
    core_contribution = _validate_bool(
        value.get("core_contribution"),
        field=f"candidates.{key}.core_contribution",
    )
    ambiguous = _validate_bool(
        value.get("ambiguous", False),
        field=f"candidates.{key}.ambiguous",
    )
    source_lineages = value.get("source_lineages")
    if not isinstance(source_lineages, dict) or not source_lineages:
        raise PromotionLedgerCorruptError(
            f"candidates.{key}.source_lineages must be a non-empty object"
        )
    validated_lineages: dict[str, dict[str, Any]] = {}
    evidence_paths: set[str] = set()
    max_confidence = 0.0
    any_core = False
    for raw_lineage, entry in source_lineages.items():
        lineage = _validate_lineage(raw_lineage)
        if raw_lineage != lineage:
            raise PromotionLedgerCorruptError(
                f"candidates.{key}.source_lineages has a non-canonical lineage key"
            )
        validated = _validate_lineage_entry(
            entry,
            field=f"candidates.{key}.source_lineages.{lineage}",
        )
        if validated["lineage"] != lineage:
            raise PromotionLedgerCorruptError(
                f"candidates.{key}.source_lineages.{lineage}.lineage mismatch"
            )
        validated_lineages[lineage] = validated
        evidence_paths.update(validated["evidence_paths"])
        max_confidence = max(max_confidence, validated["max_confidence"])
        any_core = any_core or validated["core_contribution"]
    top_level_evidence = value.get("evidence_paths")
    if not isinstance(top_level_evidence, builtins.list):
        raise PromotionLedgerCorruptError(f"candidates.{key}.evidence_paths must be a list")
    validated_evidence = sorted(
        _validate_relpath(path, field=f"candidates.{key}.evidence_paths")
        for path in top_level_evidence
    )
    if validated_evidence != sorted(evidence_paths):
        raise PromotionLedgerCorruptError(
            f"candidates.{key}.evidence_paths must match lineage evidence paths"
        )
    if abs(confidence - max_confidence) > 1e-12:
        raise PromotionLedgerCorruptError(
            f"candidates.{key}.confidence must equal the max lineage confidence"
        )
    if core_contribution != any_core:
        raise PromotionLedgerCorruptError(
            f"candidates.{key}.core_contribution must summarize source lineages"
        )
    conflicts = value.get("conflicts", [])
    if not isinstance(conflicts, builtins.list):
        raise PromotionLedgerCorruptError(f"candidates.{key}.conflicts must be a list")
    validated_conflicts = sorted(
        {
            _validate_text(item, field=f"candidates.{key}.conflicts")
            for item in conflicts
        }
    )
    eligibility = _validate_eligibility(
        value.get("eligibility"),
        field=f"candidates.{key}.eligibility",
    )
    promotion_plan = value.get("promotion_plan")
    if promotion_plan is not None and not isinstance(promotion_plan, dict):
        raise PromotionLedgerCorruptError(
            f"candidates.{key}.promotion_plan must be an object or null"
        )
    resolved_at = value.get("resolved_at")
    if resolved_at is not None:
        _parse_time(resolved_at, field=f"candidates.{key}.resolved_at")
    canonical_path = value.get("canonical_path")
    if canonical_path is not None:
        canonical_path = _validate_relpath(
            canonical_path, field=f"candidates.{key}.canonical_path"
        )
    resolution_reason = value.get("resolution_reason")
    resolved_by = value.get("resolved_by")
    if state in TERMINAL_STATES:
        if resolved_at is None or resolution_reason is None:
            raise PromotionLedgerCorruptError(
                f"candidates.{key} terminal state requires resolved_at and resolution_reason"
            )
        if state == "promoted" and canonical_path is None:
            raise PromotionLedgerCorruptError(
                f"candidates.{key} promoted state requires canonical_path"
            )
        if state == "rejected" and canonical_path is not None:
            raise PromotionLedgerCorruptError(
                f"candidates.{key} rejected state must not have canonical_path"
            )
        if state == "promoted" and canonical_path != _target_path(kind, slug):
            raise PromotionLedgerCorruptError(
                f"candidates.{key}.canonical_path must match its deterministic target"
            )
    elif any(item is not None for item in (resolved_at, resolution_reason, resolved_by, canonical_path)):
        raise PromotionLedgerCorruptError(
            f"candidates.{key} unresolved state contains terminal resolution fields"
        )
    normalised = {
        "kind": kind,
        "canonical_title": title,
        "canonical_slug": slug,
        "aliases": aliases,
        "state": state,
        "created_at": created_at,
        "updated_at": updated_at,
        "confidence": confidence,
        "core_contribution": core_contribution,
        "source_lineages": {
            lineage: validated_lineages[lineage]
            for lineage in sorted(validated_lineages)
        },
        "evidence_paths": validated_evidence,
        "ambiguous": ambiguous,
        "conflicts": validated_conflicts,
        "eligibility": eligibility,
        "promotion_plan": _copy_plan(promotion_plan),
    }
    if canonical_path is not None:
        normalised["canonical_path"] = canonical_path
    if resolved_at is not None:
        normalised["resolved_at"] = resolved_at
    if resolution_reason is not None:
        normalised["resolution_reason"] = _validate_text(
            resolution_reason,
            field=f"candidates.{key}.resolution_reason",
        )
    if resolved_by is not None:
        normalised["resolved_by"] = _validate_text(
            resolved_by,
            field=f"candidates.{key}.resolved_by",
        )
    computed_eligibility, computed_plan = _evaluate_candidate(normalised, policy)
    if state in TERMINAL_STATES:
        computed_plan = None
    else:
        expected_state = "eligible" if computed_eligibility["eligible"] else "candidate"
        if state != expected_state:
            raise PromotionLedgerCorruptError(
                f"candidates.{key}.state does not match eligibility"
            )
    if eligibility != computed_eligibility:
        raise PromotionLedgerCorruptError(
            f"candidates.{key}.eligibility does not match source lineages"
        )
    if _copy_plan(promotion_plan) != computed_plan:
        raise PromotionLedgerCorruptError(
            f"candidates.{key}.promotion_plan does not match eligibility"
        )
    return normalised


def _validate_candidate(value: object, *, key: str, policy: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PromotionLedgerCorruptError(f"candidate {key!r} must be an object")
    try:
        return _validate_candidate_payload(value, key=key, policy=policy)
    except ValueError as exc:
        raise PromotionLedgerCorruptError(str(exc)) from exc


def _validate_ledger(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise PromotionLedgerCorruptError("promotion ledger root must be a JSON object")
    _reject_unknown_keys(data, _LEDGER_KEYS, field="promotion ledger")
    version = data.get("schema_version")
    if type(version) is not int or version != PROMOTION_LEDGER_SCHEMA_VERSION:
        raise PromotionLedgerVersionError(
            f"unsupported promotion ledger version {version!r}; "
            f"expected {PROMOTION_LEDGER_SCHEMA_VERSION}"
        )
    updated_at = data.get("updated_at")
    if updated_at is not None:
        _parse_time(updated_at, field="updated_at")
    policy = _validate_policy(data.get("policy", DEFAULT_POLICY))
    candidates = data.get("candidates")
    if not isinstance(candidates, dict):
        raise PromotionLedgerCorruptError("candidates must be an object")
    validated_candidates = {
        key: _validate_candidate(value, key=key, policy=policy)
        for key, value in sorted(candidates.items())
    }
    return {
        "schema_version": PROMOTION_LEDGER_SCHEMA_VERSION,
        "updated_at": updated_at,
        "policy": policy,
        "candidates": validated_candidates,
    }


def load_ledger(vault: Path) -> dict[str, Any]:
    """Load and validate the promotion ledger; missing ledger is an empty view."""
    path = ledger_path(vault)
    if not path.exists():
        return _empty_ledger()
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_float,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise PromotionLedgerCorruptError(
            f"promotion ledger is corrupt at {path}: {exc}"
        ) from exc
    except OSError as exc:
        raise PromotionError(f"cannot read promotion ledger {path}: {exc}") from exc
    return _validate_ledger(data)


def _open_lock_file(path: Path) -> int:
    if path.is_symlink():
        raise PromotionError("promotion lock must not be a symlink")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            named.st_dev,
            named.st_ino,
        ):
            raise PromotionError("promotion lock path changed while opening")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _try_platform_lock(descriptor: int) -> bool:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI.
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise


def _platform_unlock(descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI.
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _prepare_meta_dir(vault: Path) -> Path:
    root = _canonical_vault(vault)
    meta = root / "_meta"
    if meta.exists() and meta.is_symlink():
        raise PromotionError("promotion ledger parent must not be a symlink")
    meta.mkdir(parents=True, exist_ok=True)
    if meta.resolve(strict=True) != root / "_meta":
        raise PromotionError("promotion ledger parent resolves outside the vault")
    return meta


@contextmanager
def promotion_lock(
    vault: Path,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Acquire a process-scoped OS advisory lock for the promotion ledger.

    The lock file is intentionally persistent: a crashed process releases its
    kernel lock automatically, so recovery never unlinks another owner's lock.
    """
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("timeout must be a non-negative finite number")
    meta = _prepare_meta_dir(vault)
    lock = meta / PROMOTION_LOCK_RELATIVE_PATH.name
    deadline = time.monotonic() + timeout
    try:
        descriptor = _open_lock_file(lock)
    except OSError as exc:
        raise PromotionError(f"cannot open promotion lock {lock}: {exc}") from exc
    acquired = False
    try:
        while not acquired:
            try:
                acquired = _try_platform_lock(descriptor)
            except OSError as exc:
                raise PromotionError(
                    f"cannot acquire promotion lock {lock}: {exc}"
                ) from exc
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise PromotionLockTimeout(
                    f"could not acquire {lock} within {timeout}s"
                )
            time.sleep(0.05)

        yield
    finally:
        if acquired:
            try:
                _platform_unlock(descriptor)
            except OSError:
                pass
        os.close(descriptor)


def _write_ledger(path: Path, ledger: dict[str, Any], *, vault: Path) -> None:
    root = _canonical_vault(vault)
    expected = root / PROMOTION_LEDGER_RELATIVE_PATH
    if path.expanduser().resolve(strict=False) != expected:
        raise PromotionError("promotion ledger destination resolves outside the vault")
    if expected.exists() and expected.is_symlink():
        raise PromotionError("promotion ledger destination must not be a symlink")
    meta = _prepare_meta_dir(root)
    if expected.parent != meta:
        raise PromotionError("promotion ledger parent mismatch")

    try:
        payload = json.dumps(
            ledger,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise PromotionError(f"promotion ledger is not valid JSON data: {exc}") from exc

    if os.name == "posix":
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            directory_fd = os.open(meta, directory_flags)
        except OSError as exc:
            raise PromotionError(
                f"cannot securely open promotion ledger directory: {exc}"
            ) from exc
        temporary_name = f".promotion-candidates-{secrets.token_hex(16)}.tmp"
        temporary_created = False
        try:
            try:
                destination_stat = os.stat(
                    expected.name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                destination_stat = None
            if destination_stat is not None and stat.S_ISLNK(destination_stat.st_mode):
                raise PromotionError("promotion ledger destination must not be a symlink")
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            temporary_created = True
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary_name,
                expected.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temporary_created = False
            os.fsync(directory_fd)
        except OSError as exc:
            raise PromotionError(f"cannot securely write promotion ledger: {exc}") from exc
        finally:
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            os.close(directory_fd)
        return

    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=meta,
            prefix=".promotion-candidates-",
            suffix=".tmp",
            text=True,
        )
    except OSError as exc:
        raise PromotionError(
            f"cannot create promotion ledger temporary file: {exc}"
        ) from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, expected)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise PromotionError(f"cannot write promotion ledger: {exc}") from exc


def _promotion_plan(candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "action": "promote_candidate",
        "candidate_id": _candidate_key(candidate["kind"], candidate["canonical_slug"]),
        "kind": candidate["kind"],
        "canonical_title": candidate["canonical_title"],
        "canonical_slug": candidate["canonical_slug"],
        "target_path": _target_path(candidate["kind"], candidate["canonical_slug"]),
        "aliases": builtins.list(candidate["aliases"]),
        "source_lineages": sorted(candidate["source_lineages"]),
        "evidence_paths": builtins.list(candidate["evidence_paths"]),
        "confidence": candidate["confidence"],
        "core_contribution": candidate["core_contribution"],
        "reason": reason,
    }


def _evaluate_candidate(
    candidate: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    blocked: list[str] = []
    if candidate.get("ambiguous"):
        blocked.append("ambiguous")
    if candidate.get("conflicts"):
        blocked.append("conflicting")

    lineages = candidate["source_lineages"]
    independent_count = len(lineages)
    eligible_count = sum(
        1
        for lineage in lineages.values()
        if lineage["max_confidence"] >= policy["lineage_confidence"]
    )
    threshold = policy["independent_lineage_count"]
    reason = "below_threshold"
    eligible = False
    if blocked:
        reason = "blocked_ambiguous_or_conflicting"
    elif (
        candidate["core_contribution"]
        and candidate["confidence"] >= policy["core_contribution_confidence"]
    ):
        reason = "core_contribution_high_confidence"
        eligible = True
    elif eligible_count >= threshold:
        reason = "independent_lineage_threshold"
        eligible = True

    eligibility = {
        "eligible": eligible,
        "reason": reason,
        "independent_lineage_count": independent_count,
        "eligible_lineage_count": eligible_count,
        "independent_lineage_threshold": threshold,
        "blocked": sorted(blocked),
    }
    plan = _promotion_plan(candidate, reason) if eligible else None
    return eligibility, plan


def _refresh_candidate(candidate: dict[str, Any], policy: dict[str, Any]) -> None:
    evidence_paths: set[str] = set()
    max_confidence = 0.0
    core_contribution = False
    for lineage in candidate["source_lineages"].values():
        evidence_paths.update(lineage["evidence_paths"])
        max_confidence = max(max_confidence, lineage["max_confidence"])
        core_contribution = core_contribution or lineage["core_contribution"]
    candidate["evidence_paths"] = sorted(evidence_paths)
    candidate["confidence"] = max_confidence
    candidate["core_contribution"] = core_contribution
    eligibility, plan = _evaluate_candidate(candidate, policy)
    candidate["eligibility"] = eligibility
    candidate["promotion_plan"] = None if candidate["state"] in TERMINAL_STATES else plan
    if candidate["state"] not in TERMINAL_STATES:
        candidate["state"] = "eligible" if eligibility["eligible"] else "candidate"


def _recompute_conflicts(candidates: dict[str, dict[str, Any]]) -> None:
    label_index: dict[str, set[str]] = {}
    for key, candidate in candidates.items():
        labels = [candidate["canonical_title"], *candidate["aliases"]]
        for label in labels:
            label_index.setdefault(_normalise_label(label), set()).add(key)
    conflicts = {
        key: sorted(other for other in keys if other != key)
        for keys in label_index.values()
        if len(keys) > 1
        for key in keys
    }
    for key, candidate in candidates.items():
        candidate["conflicts"] = conflicts.get(key, [])


def _candidate_view(candidate: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(candidate)


def _mutate_ledger(
    vault: Path,
    mutator: Any,
    *,
    now: str | None = None,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    timestamp = _validate_now(now or _utc_now())
    with promotion_lock(vault, timeout=timeout):
        ledger = load_ledger(vault)
        before = copy.deepcopy(ledger["candidates"])
        mutator(ledger, timestamp)
        _recompute_conflicts(ledger["candidates"])
        for candidate in ledger["candidates"].values():
            _refresh_candidate(candidate, ledger["policy"])
        for key, candidate in ledger["candidates"].items():
            previous = before.get(key)
            if previous is None:
                candidate["updated_at"] = timestamp
                continue
            previous_material = copy.deepcopy(previous)
            current_material = copy.deepcopy(candidate)
            previous_material.pop("updated_at", None)
            current_material.pop("updated_at", None)
            if current_material != previous_material:
                candidate["updated_at"] = timestamp
        if ledger["candidates"] == before:
            return ledger
        ledger["updated_at"] = timestamp
        ledger = _validate_ledger(ledger)
        _write_ledger(ledger_path(vault), ledger, vault=vault)
        return ledger


def observe_candidate(
    vault: Path,
    *,
    kind: str,
    canonical_title: str,
    source_lineage: str,
    evidence_path: str,
    confidence: float,
    canonical_slug: str | None = None,
    aliases: list[str] | None = None,
    core_contribution: bool = False,
    ambiguous: bool = False,
    now: str | None = None,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Observe evidence for one candidate and return its machine-readable state."""
    kind = _validate_kind(kind)
    canonical_title = _validate_text(canonical_title, field="canonical_title")
    canonical_slug = _validate_slug(canonical_slug or slug_from_title(canonical_title))
    source_lineage = _validate_lineage(source_lineage)
    evidence_path = _validate_relpath(evidence_path, field="evidence_path")
    confidence = _validate_confidence(confidence)
    aliases = _validate_aliases(aliases or [])
    core_contribution = _validate_bool(core_contribution, field="core_contribution")
    ambiguous = _validate_bool(ambiguous, field="ambiguous")
    key = _candidate_key(kind, canonical_slug)

    def mutate(ledger: dict[str, Any], timestamp: str) -> None:
        candidates = ledger["candidates"]
        candidate = candidates.get(key)
        if candidate is None:
            candidate = {
                "kind": kind,
                "canonical_title": canonical_title,
                "canonical_slug": canonical_slug,
                "aliases": aliases,
                "state": "candidate",
                "created_at": timestamp,
                "updated_at": timestamp,
                "confidence": confidence,
                "core_contribution": core_contribution,
                "source_lineages": {},
                "evidence_paths": [],
                "ambiguous": ambiguous,
                "conflicts": [],
                "eligibility": {
                    "eligible": False,
                    "reason": "below_threshold",
                    "independent_lineage_count": 0,
                    "eligible_lineage_count": 0,
                    "independent_lineage_threshold": ledger["policy"][
                        "independent_lineage_count"
                    ],
                    "blocked": [],
                },
                "promotion_plan": None,
            }
            candidates[key] = candidate
        elif candidate["kind"] != kind or candidate["canonical_slug"] != canonical_slug:
            raise PromotionLedgerCorruptError(f"candidate key collision for {key}")

        if _normalise_label(candidate["canonical_title"]) != _normalise_label(
            canonical_title
        ):
            merged_aliases = [*candidate["aliases"], canonical_title]
        else:
            merged_aliases = builtins.list(candidate["aliases"])
        candidate["aliases"] = _unique_sorted_text([*merged_aliases, *aliases])
        candidate["ambiguous"] = bool(candidate.get("ambiguous")) or ambiguous
        candidate["updated_at"] = timestamp

        lineage = candidate["source_lineages"].get(source_lineage)
        if lineage is None:
            candidate["source_lineages"][source_lineage] = {
                "lineage": source_lineage,
                "first_seen_at": timestamp,
                "last_seen_at": timestamp,
                "observations": 1,
                "max_confidence": confidence,
                "core_contribution": core_contribution,
                "evidence_paths": [evidence_path],
            }
            return
        lineage["last_seen_at"] = timestamp
        lineage["observations"] += 1
        lineage["max_confidence"] = max(lineage["max_confidence"], confidence)
        lineage["core_contribution"] = lineage["core_contribution"] or core_contribution
        lineage["evidence_paths"] = sorted({*lineage["evidence_paths"], evidence_path})

    ledger = _mutate_ledger(
        vault,
        mutate,
        now=now,
        timeout=timeout,
    )
    candidate = ledger["candidates"][key]
    return {
        "status": candidate["state"],
        "candidate_id": key,
        "candidate": _candidate_view(candidate),
        "promotion_plan": _copy_plan(candidate["promotion_plan"]),
        "ledger_path": str(ledger_path(vault)),
    }


def inspect_candidate(
    vault: Path,
    *,
    kind: str,
    canonical_slug: str | None = None,
    canonical_title: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic read-only view for one candidate."""
    kind = _validate_kind(kind)
    if canonical_slug is None:
        if canonical_title is None:
            raise ValueError("canonical_slug or canonical_title is required")
        canonical_slug = slug_from_title(canonical_title)
    canonical_slug = _validate_slug(canonical_slug)
    key = _candidate_key(kind, canonical_slug)
    ledger = load_ledger(vault)
    candidate = ledger["candidates"].get(key)
    return {
        "found": candidate is not None,
        "candidate_id": key,
        "candidate": _candidate_view(candidate) if candidate is not None else None,
        "promotion_plan": (
            _copy_plan(candidate["promotion_plan"]) if candidate is not None else None
        ),
        "ledger_path": str(ledger_path(vault)),
    }


def list_candidates(
    vault: Path,
    *,
    state: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """List candidates in stable order, optionally filtered by state and kind."""
    if state is not None:
        state = _validate_state(state)
    if kind is not None:
        kind = _validate_kind(kind)
    ledger = load_ledger(vault)
    items = []
    for key, candidate in sorted(ledger["candidates"].items()):
        if state is not None and candidate["state"] != state:
            continue
        if kind is not None and candidate["kind"] != kind:
            continue
        items.append({"candidate_id": key, **_candidate_view(candidate)})
    return {
        "status": "pass",
        "ledger_path": str(ledger_path(vault)),
        "updated_at": ledger["updated_at"],
        "candidates": items,
        "counts": {
            state_name: sum(
                1
                for candidate in ledger["candidates"].values()
                if candidate["state"] == state_name
                and (kind is None or candidate["kind"] == kind)
            )
            for state_name in sorted(ALLOWED_STATES)
        },
    }


def resolve_candidate(
    vault: Path,
    *,
    kind: str,
    resolution: str,
    canonical_slug: str | None = None,
    canonical_title: str | None = None,
    canonical_path: str | None = None,
    reason: str | None = None,
    resolved_by: str | None = None,
    now: str | None = None,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Mark a candidate promoted or rejected without creating Markdown."""
    kind = _validate_kind(kind)
    if canonical_slug is None:
        if canonical_title is None:
            raise ValueError("canonical_slug or canonical_title is required")
        canonical_slug = slug_from_title(canonical_title)
    canonical_slug = _validate_slug(canonical_slug)
    if resolution not in TERMINAL_STATES:
        raise ValueError("resolution must be promoted or rejected")
    expected_path = _target_path(kind, canonical_slug)
    if canonical_path is not None:
        canonical_path = _validate_relpath(canonical_path, field="canonical_path")
    if resolution == "promoted" and canonical_path not in {None, expected_path}:
        raise ValueError(f"canonical_path must match the promotion target {expected_path}")
    if resolution == "rejected" and canonical_path is not None:
        raise ValueError("canonical_path is only valid for a promoted resolution")
    reason = _validate_text(reason or resolution, field="reason")
    if resolved_by is not None:
        resolved_by = _validate_text(resolved_by, field="resolved_by")
    key = _candidate_key(kind, canonical_slug)

    def mutate(ledger: dict[str, Any], timestamp: str) -> None:
        candidate = ledger["candidates"].get(key)
        if candidate is None:
            raise KeyError(f"candidate not found: {key}")
        if candidate["state"] in TERMINAL_STATES:
            if candidate["state"] != resolution:
                raise ValueError(
                    f"candidate {key} is already resolved as {candidate['state']}"
                )
            if resolution == "promoted":
                _require_canonical_page(vault, candidate["canonical_path"])
            return
        if resolution == "promoted":
            resolved_path = canonical_path or expected_path
            _require_canonical_page(vault, resolved_path)
        candidate["state"] = resolution
        candidate["updated_at"] = timestamp
        candidate["resolved_at"] = timestamp
        candidate["resolution_reason"] = reason
        if resolved_by is not None:
            candidate["resolved_by"] = resolved_by
        if resolution == "promoted":
            candidate["canonical_path"] = canonical_path or expected_path
        elif "canonical_path" in candidate:
            del candidate["canonical_path"]

    ledger = _mutate_ledger(
        vault,
        mutate,
        now=now,
        timeout=timeout,
    )
    candidate = ledger["candidates"][key]
    return {
        "status": candidate["state"],
        "candidate_id": key,
        "candidate": _candidate_view(candidate),
        "promotion_plan": _copy_plan(candidate["promotion_plan"]),
        "ledger_path": str(ledger_path(vault)),
    }


def _require_canonical_page(vault: Path, relative_path: str) -> Path:
    """Require a real, vault-contained Markdown page before promotion resolves."""
    root = _canonical_vault(vault)
    candidate = root / PurePosixPath(relative_path)
    current = root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise PromotionError(
                f"canonical page path must not contain symlinks: {relative_path}"
            )
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise PromotionError(
            f"canonical page does not exist inside the vault: {relative_path}"
        ) from exc
    if not resolved.is_file() or resolved.suffix.lower() != ".md":
        raise PromotionError(
            f"canonical page must be a Markdown file: {relative_path}"
        )
    return resolved


def observe(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for observe_candidate."""
    return observe_candidate(*args, **kwargs)


def inspect(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for inspect_candidate."""
    return inspect_candidate(*args, **kwargs)


def list(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for list_candidates."""
    return list_candidates(*args, **kwargs)


def resolve(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for resolve_candidate."""
    return resolve_candidate(*args, **kwargs)
