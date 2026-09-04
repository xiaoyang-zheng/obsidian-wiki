"""Regression tests for concept/entity promotion candidates."""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from obsidian_wiki import promotion


NOW = "2026-09-04T10:00:00Z"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


def _observe_worker(vault: str, index: int) -> None:
    promotion.observe_candidate(
        Path(vault),
        kind="concept",
        canonical_title=f"Concept {index}",
        source_lineage=f"source-{index}",
        evidence_path=f"_raw/source-{index}.md",
        confidence=0.72,
        now=NOW,
    )


def _hold_lock_worker(
    vault: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    with promotion.promotion_lock(Path(vault)):
        ready.set()
        release.wait(10)


def test_core_contribution_high_confidence_is_immediately_eligible(vault: Path) -> None:
    result = promotion.observe_candidate(
        vault,
        kind="concept",
        canonical_title="Evidence Graph",
        source_lineage="paper:alpha",
        evidence_path="_sources/paper-alpha.md",
        confidence=0.91,
        core_contribution=True,
        now=NOW,
    )

    assert result["status"] == "eligible"
    assert result["promotion_plan"] == {
        "action": "promote_candidate",
        "candidate_id": "concept:evidence-graph",
        "kind": "concept",
        "canonical_title": "Evidence Graph",
        "canonical_slug": "evidence-graph",
        "target_path": "concepts/evidence-graph.md",
        "aliases": [],
        "source_lineages": ["paper:alpha"],
        "evidence_paths": ["_sources/paper-alpha.md"],
        "confidence": 0.91,
        "core_contribution": True,
        "reason": "core_contribution_high_confidence",
    }


def test_independent_lineage_threshold_ignores_duplicate_lineage(vault: Path) -> None:
    first = promotion.observe_candidate(
        vault,
        kind="entity",
        canonical_title="Vector Store",
        source_lineage="repo:a",
        evidence_path="_sources/repo-a.md",
        confidence=0.75,
        now="2026-09-04T10:00:00Z",
    )
    duplicate = promotion.observe_candidate(
        vault,
        kind="entity",
        canonical_title="Vector Store",
        source_lineage="repo:a",
        evidence_path="_sources/repo-a-second.md",
        confidence=0.90,
        now="2026-09-04T10:01:00Z",
    )
    promoted = promotion.observe_candidate(
        vault,
        kind="entity",
        canonical_title="Vector Store",
        source_lineage="repo:b",
        evidence_path="_sources/repo-b.md",
        confidence=0.71,
        now="2026-09-04T10:02:00Z",
    )

    assert first["status"] == "candidate"
    assert duplicate["status"] == "candidate"
    assert duplicate["candidate"]["eligibility"]["independent_lineage_count"] == 1
    assert duplicate["candidate"]["source_lineages"]["repo:a"]["observations"] == 2
    assert promoted["status"] == "eligible"
    assert promoted["candidate"]["eligibility"]["independent_lineage_count"] == 2
    assert promoted["candidate"]["evidence_paths"] == [
        "_sources/repo-a-second.md",
        "_sources/repo-a.md",
        "_sources/repo-b.md",
    ]


def test_ambiguity_and_conflicts_block_auto_eligibility(vault: Path) -> None:
    ambiguous = promotion.observe_candidate(
        vault,
        kind="concept",
        canonical_title="Agent",
        source_lineage="doc:a",
        evidence_path="_raw/a.md",
        confidence=0.96,
        core_contribution=True,
        ambiguous=True,
        now="2026-09-04T10:00:00Z",
    )

    assert ambiguous["status"] == "candidate"
    assert ambiguous["promotion_plan"] is None
    assert ambiguous["candidate"]["eligibility"]["blocked"] == ["ambiguous"]

    first = promotion.observe_candidate(
        vault,
        kind="entity",
        canonical_title="Acme",
        source_lineage="doc:b",
        evidence_path="_raw/b.md",
        confidence=0.96,
        core_contribution=True,
        now="2026-09-04T10:01:00Z",
    )
    second = promotion.observe_candidate(
        vault,
        kind="entity",
        canonical_title="ACME",
        canonical_slug="acme-inc",
        source_lineage="doc:c",
        evidence_path="_raw/c.md",
        confidence=0.96,
        core_contribution=True,
        now="2026-09-04T10:02:00Z",
    )

    assert first["status"] == "eligible"
    assert second["status"] == "candidate"
    inspected = promotion.inspect_candidate(vault, kind="entity", canonical_slug="acme")
    assert inspected["candidate"]["state"] == "candidate"
    assert inspected["candidate"]["promotion_plan"] is None
    assert inspected["candidate"]["eligibility"]["blocked"] == ["conflicting"]


def test_terminal_states_are_not_reopened_by_observe(vault: Path) -> None:
    promotion.observe_candidate(
        vault,
        kind="concept",
        canonical_title="Atomic Ledgers",
        source_lineage="doc:a",
        evidence_path="_raw/a.md",
        confidence=0.93,
        core_contribution=True,
        now="2026-09-04T10:00:00Z",
    )
    canonical = vault / "concepts" / "atomic-ledgers.md"
    canonical.parent.mkdir()
    canonical.write_text("# Atomic Ledgers\n", encoding="utf-8")
    resolved = promotion.resolve_candidate(
        vault,
        kind="concept",
        canonical_slug="atomic-ledgers",
        resolution="promoted",
        canonical_path="concepts/atomic-ledgers.md",
        reason="canonical page created by ingest",
        resolved_by="test",
        now="2026-09-04T10:01:00Z",
    )
    observed = promotion.observe_candidate(
        vault,
        kind="concept",
        canonical_title="Atomic Ledgers",
        source_lineage="doc:b",
        evidence_path="_raw/b.md",
        confidence=0.95,
        core_contribution=True,
        now="2026-09-04T10:02:00Z",
    )

    assert resolved["status"] == "promoted"
    assert observed["status"] == "promoted"
    assert observed["promotion_plan"] is None
    assert observed["candidate"]["canonical_path"] == "concepts/atomic-ledgers.md"
    assert observed["candidate"]["source_lineages"].keys() == {"doc:a", "doc:b"}


def test_rejected_state_is_not_reopened_by_observe(vault: Path) -> None:
    promotion.observe_candidate(
        vault,
        kind="entity",
        canonical_title="Temporary Tool",
        source_lineage="doc:a",
        evidence_path="_raw/a.md",
        confidence=0.95,
        core_contribution=True,
        now="2026-09-04T10:00:00Z",
    )
    promotion.resolve_candidate(
        vault,
        kind="entity",
        canonical_slug="temporary-tool",
        resolution="rejected",
        reason="too broad",
        now="2026-09-04T10:01:00Z",
    )
    observed = promotion.observe_candidate(
        vault,
        kind="entity",
        canonical_title="Temporary Tool",
        source_lineage="doc:b",
        evidence_path="_raw/b.md",
        confidence=0.99,
        core_contribution=True,
        now="2026-09-04T10:02:00Z",
    )

    assert observed["status"] == "rejected"
    assert observed["promotion_plan"] is None


def test_concurrent_observes_are_serialized_by_lock(vault: Path) -> None:
    processes = [
        multiprocessing.Process(target=_observe_worker, args=(str(vault), index))
        for index in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    report = promotion.list_candidates(vault)
    assert [item["candidate_id"] for item in report["candidates"]] == [
        f"concept:concept-{index}" for index in range(6)
    ]


def test_unlocked_persistent_lock_file_is_reused(vault: Path) -> None:
    lock = promotion.lock_path(vault)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text('{"token": "held"}', encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))
    recovered = promotion.observe_candidate(
        vault,
        kind="concept",
        canonical_title="Locked",
        source_lineage="doc:a",
        evidence_path="_raw/a.md",
        confidence=0.9,
        timeout=0.2,
        now=NOW,
    )

    assert recovered["candidate_id"] == "concept:locked"
    assert lock.exists()


def test_live_lock_times_out_and_kernel_release_recovers(vault: Path) -> None:
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    process = multiprocessing.Process(
        target=_hold_lock_worker,
        args=(str(vault), ready, release),
    )
    process.start()
    try:
        assert ready.wait(timeout=5)
        with pytest.raises(promotion.PromotionLockTimeout):
            promotion.observe_candidate(
                vault,
                kind="concept",
                canonical_title="Locked",
                source_lineage="doc:a",
                evidence_path="_raw/a.md",
                confidence=0.9,
                timeout=0.01,
                now=NOW,
            )
    finally:
        release.set()
        process.join(timeout=10)
    assert process.exitcode == 0

    recovered = promotion.observe_candidate(
        vault,
        kind="concept",
        canonical_title="Locked",
        source_lineage="doc:a",
        evidence_path="_raw/a.md",
        confidence=0.9,
        timeout=0.2,
        now=NOW,
    )
    assert recovered["candidate_id"] == "concept:locked"


@pytest.mark.parametrize(
    "payload,error_type,match",
    [
        ("{not-json", promotion.PromotionLedgerCorruptError, "corrupt"),
        (
            json.dumps(
                {
                    "schema_version": 999,
                    "updated_at": None,
                    "policy": promotion.DEFAULT_POLICY,
                    "candidates": {},
                }
            ),
            promotion.PromotionLedgerVersionError,
            "unsupported",
        ),
        (
            '{"schema_version": 1, "schema_version": 1, "updated_at": null, '
            '"policy": {}, "candidates": {}}',
            promotion.PromotionLedgerCorruptError,
            "duplicate JSON key",
        ),
        (
            '{"schema_version": 1, "updated_at": null, "policy": {}, '
            '"candidates": {}, "future": NaN}',
            promotion.PromotionLedgerCorruptError,
            "non-finite",
        ),
    ],
)
def test_corrupt_or_unsupported_ledger_is_not_overwritten(
    vault: Path,
    payload: str,
    error_type: type[Exception],
    match: str,
) -> None:
    path = promotion.ledger_path(vault)
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(error_type, match=match):
        promotion.observe_candidate(
            vault,
            kind="concept",
            canonical_title="Safe",
            source_lineage="doc:a",
            evidence_path="_raw/a.md",
            confidence=0.9,
            now=NOW,
        )

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"canonical_slug": "../bad"}, "canonical_slug"),
        ({"evidence_path": "../bad.md"}, "evidence_path"),
        ({"confidence": float("nan")}, "confidence"),
        ({"confidence": True}, "confidence"),
    ],
)
def test_invalid_inputs_fail_closed_without_writing(
    vault: Path,
    kwargs: dict[str, object],
    match: str,
) -> None:
    params = {
        "kind": "concept",
        "canonical_title": "Safe",
        "source_lineage": "doc:a",
        "evidence_path": "_raw/a.md",
        "confidence": 0.9,
        "now": NOW,
    }
    params.update(kwargs)

    with pytest.raises(ValueError, match=match):
        promotion.observe_candidate(vault, **params)

    assert not promotion.ledger_path(vault).exists()


def test_atomic_replace_failure_preserves_old_ledger_and_cleans_temp(
    vault: Path,
    monkeypatch,
) -> None:
    promotion.observe_candidate(
        vault,
        kind="concept",
        canonical_title="Stable",
        source_lineage="doc:a",
        evidence_path="_raw/a.md",
        confidence=0.8,
        now=NOW,
    )
    path = promotion.ledger_path(vault)
    before = path.read_bytes()
    original_replace = promotion.os.replace

    def fail_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(promotion.os, "replace", fail_replace)
    with pytest.raises(promotion.PromotionError, match="replace failure"):
        promotion.observe_candidate(
            vault,
            kind="concept",
            canonical_title="Stable",
            source_lineage="doc:b",
            evidence_path="_raw/b.md",
            confidence=0.8,
            now="2026-09-04T10:01:00Z",
        )

    monkeypatch.setattr(promotion.os, "replace", original_replace)
    assert path.read_bytes() == before
    assert list(path.parent.glob(".promotion-candidates-*.tmp")) == []


def test_list_order_is_stable_by_candidate_id(vault: Path) -> None:
    promotion.observe_candidate(
        vault,
        kind="entity",
        canonical_title="Zulu",
        source_lineage="doc:z",
        evidence_path="_raw/z.md",
        confidence=0.9,
        now=NOW,
    )
    promotion.observe_candidate(
        vault,
        kind="concept",
        canonical_title="Alpha",
        source_lineage="doc:a",
        evidence_path="_raw/a.md",
        confidence=0.9,
        now=NOW,
    )
    promotion.observe_candidate(
        vault,
        kind="entity",
        canonical_title="Alpha",
        canonical_slug="alpha-entity",
        source_lineage="doc:b",
        evidence_path="_raw/b.md",
        confidence=0.9,
        now=NOW,
    )

    assert [item["candidate_id"] for item in promotion.list_candidates(vault)["candidates"]] == [
        "concept:alpha",
        "entity:alpha-entity",
        "entity:zulu",
    ]


def test_resolve_returns_planless_machine_readable_terminal_state(vault: Path) -> None:
    promotion.observe_candidate(
        vault,
        kind="concept",
        canonical_title="Promotion Plan",
        source_lineage="doc:a",
        evidence_path="_raw/a.md",
        confidence=0.99,
        core_contribution=True,
        now="2026-09-04T10:00:00Z",
    )

    canonical = vault / "concepts" / "promotion-plan.md"
    canonical.parent.mkdir()
    canonical.write_text("# Promotion Plan\n", encoding="utf-8")

    result = promotion.resolve_candidate(
        vault,
        kind="concept",
        canonical_slug="promotion-plan",
        resolution="promoted",
        now="2026-09-04T10:01:00Z",
    )

    assert result["status"] == "promoted"
    assert result["promotion_plan"] is None
    assert result["candidate"]["canonical_path"] == "concepts/promotion-plan.md"
    assert canonical.read_text(encoding="utf-8") == "# Promotion Plan\n"


def test_promoted_resolution_requires_the_exact_live_canonical_page(vault: Path) -> None:
    promotion.observe_candidate(
        vault,
        kind="concept",
        canonical_title="Live Page Gate",
        source_lineage="doc:a",
        evidence_path="_raw/a.md",
        confidence=0.99,
        core_contribution=True,
        now=NOW,
    )

    with pytest.raises(promotion.PromotionError, match="does not exist"):
        promotion.resolve_candidate(
            vault,
            kind="concept",
            canonical_slug="live-page-gate",
            resolution="promoted",
            now="2026-09-04T10:01:00Z",
        )

    wrong = vault / "concepts" / "different.md"
    wrong.parent.mkdir()
    wrong.write_text("# Different\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must match the promotion target"):
        promotion.resolve_candidate(
            vault,
            kind="concept",
            canonical_slug="live-page-gate",
            canonical_path="concepts/different.md",
            resolution="promoted",
            now="2026-09-04T10:01:00Z",
        )

    assert promotion.inspect_candidate(
        vault, kind="concept", canonical_slug="live-page-gate"
    )["candidate"]["state"] == "eligible"


def test_promoted_resolution_rejects_symlinked_canonical_page(vault: Path) -> None:
    promotion.observe_candidate(
        vault,
        kind="concept",
        canonical_title="Symlink Gate",
        source_lineage="doc:a",
        evidence_path="_raw/a.md",
        confidence=0.99,
        core_contribution=True,
        now=NOW,
    )
    outside = vault.parent / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    concepts = vault / "concepts"
    concepts.mkdir()
    try:
        (concepts / "symlink-gate.md").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(promotion.PromotionError, match="must not contain symlinks"):
        promotion.resolve_candidate(
            vault,
            kind="concept",
            canonical_slug="symlink-gate",
            resolution="promoted",
            now="2026-09-04T10:01:00Z",
        )


def test_terminal_resolution_cannot_be_reversed(vault: Path) -> None:
    promotion.observe_candidate(
        vault,
        kind="entity",
        canonical_title="Final Entity",
        source_lineage="doc:a",
        evidence_path="_raw/a.md",
        confidence=0.8,
        now=NOW,
    )
    promotion.resolve_candidate(
        vault,
        kind="entity",
        canonical_slug="final-entity",
        resolution="rejected",
        now="2026-09-04T10:01:00Z",
    )
    canonical = vault / "entities" / "final-entity.md"
    canonical.parent.mkdir()
    canonical.write_text("# Final Entity\n", encoding="utf-8")

    with pytest.raises(ValueError, match="already resolved as rejected"):
        promotion.resolve_candidate(
            vault,
            kind="entity",
            canonical_slug="final-entity",
            resolution="promoted",
            now="2026-09-04T10:02:00Z",
        )

    ledger = promotion.ledger_path(vault)
    before = ledger.read_bytes()
    duplicate = promotion.resolve_candidate(
        vault,
        kind="entity",
        canonical_slug="final-entity",
        resolution="rejected",
        now="2026-09-04T10:03:00Z",
    )
    assert duplicate["status"] == "rejected"
    assert ledger.read_bytes() == before


def test_load_rejects_state_and_canonical_target_inconsistency(vault: Path) -> None:
    promotion.observe_candidate(
        vault,
        kind="concept",
        canonical_title="Strict State",
        source_lineage="doc:a",
        evidence_path="_raw/a.md",
        confidence=0.99,
        core_contribution=True,
        now=NOW,
    )
    path = promotion.ledger_path(vault)
    ledger = json.loads(path.read_text(encoding="utf-8"))
    ledger["candidates"]["concept:strict-state"]["state"] = "candidate"
    path.write_text(json.dumps(ledger), encoding="utf-8")

    with pytest.raises(promotion.PromotionLedgerCorruptError, match="state"):
        promotion.load_ledger(vault)

    # Restore through a fresh vault and corrupt only the canonical target.
    other = vault.parent / "other-vault"
    other.mkdir()
    promotion.observe_candidate(
        other,
        kind="concept",
        canonical_title="Strict Target",
        source_lineage="doc:b",
        evidence_path="_raw/b.md",
        confidence=0.99,
        core_contribution=True,
        now=NOW,
    )
    canonical = other / "concepts" / "strict-target.md"
    canonical.parent.mkdir()
    canonical.write_text("# Strict Target\n", encoding="utf-8")
    promotion.resolve_candidate(
        other,
        kind="concept",
        canonical_slug="strict-target",
        resolution="promoted",
        now="2026-09-04T10:01:00Z",
    )
    other_path = promotion.ledger_path(other)
    other_ledger = json.loads(other_path.read_text(encoding="utf-8"))
    other_ledger["candidates"]["concept:strict-target"]["canonical_path"] = (
        "concepts/different.md"
    )
    other_path.write_text(json.dumps(other_ledger), encoding="utf-8")

    with pytest.raises(promotion.PromotionLedgerCorruptError, match="deterministic target"):
        promotion.load_ledger(other)
