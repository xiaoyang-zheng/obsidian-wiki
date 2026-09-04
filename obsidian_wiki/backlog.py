"""Deterministic maintenance backlog for an obsidian-wiki vault."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from obsidian_wiki.cache import _is_file_key, _iter_entries, _load_manifest, _strip_algo, compute_hash
from obsidian_wiki.graph_analysis import iter_pages
from obsidian_wiki.projects import check_project_timelines
from obsidian_wiki.promotion import (
    PROMOTION_LEDGER_RELATIVE_PATH,
    PromotionError,
    load_ledger,
)
from obsidian_wiki.source_bundles import check_source_bundles, lint_source_bundle_closure
from obsidian_wiki.source_state import build_report


BACKLOG_SCHEMA_VERSION = 1
BACKLOG_PATH = Path("_backlog.md")
SEVERITY_ORDER = {"critical": 0, "needs_ingest": 1, "maintenance": 2, "reference": 3}


def _line(value: object) -> str:
    return " ".join(str(value).split())


def _item(
    severity: str,
    kind: str,
    title: str,
    *,
    subject: str,
    detail: str,
    action: str,
) -> dict[str, str]:
    item_id = hashlib.sha256(
        f"{severity}\0{kind}\0{subject}\0{detail}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "id": item_id,
        "severity": severity,
        "kind": kind,
        "subject": _line(subject),
        "title": _line(title),
        "detail": _line(detail),
        "action": _line(action),
    }


def _sort_items(items: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        items,
        key=lambda item: (
            SEVERITY_ORDER.get(item["severity"], 99),
            item["kind"],
            item["subject"],
            item["id"],
        ),
    )


def _source_state_items(vault: Path, *, config_dir: Path | None = None) -> list[dict[str, str]]:
    try:
        report = build_report(vault, config_dir=config_dir)
    except RuntimeError as exc:
        return [
            _item(
                "critical",
                "source-state",
                "Source-state sidecar cannot be read",
                subject="source-state",
                detail=str(exc),
                action="Inspect the source-state JSON under the obsidian-wiki config directory.",
            )
        ]
    items: list[dict[str, str]] = []
    for source_id, source in report["sources"].items():
        heartbeat = source["heartbeat"]
        debt = source["debt"]
        if source["status"] == "fail":
            items.append(
                _item(
                    "critical",
                    "source-state",
                    f"Source {source_id} has a failing heartbeat or inconsistent cursor state",
                    subject=source_id,
                    detail=heartbeat.get("error") or debt.get("reason") or "source-state failure",
                    action="Fix the adapter failure, then update source-state after the source check succeeds.",
                )
            )
        elif debt.get("pending"):
            items.append(
                _item(
                    "needs_ingest",
                    "source-state",
                    f"Source {source_id} has observed data that is not applied",
                    subject=source_id,
                    detail=f"debt={debt.get('reason')}",
                    action="Run the relevant ingest/update workflow, then advance the applied cursor.",
                )
            )
        elif heartbeat.get("stale") is True:
            items.append(
                _item(
                    "maintenance",
                    "source-state",
                    f"Source {source_id} heartbeat is stale",
                    subject=source_id,
                    detail=f"last_success_at={heartbeat.get('last_success_at')}",
                    action="Run the source check or inspect the scheduler for this source.",
                )
            )
    return items


def _manifest_source_path(vault: Path, key: str | None) -> Path | None:
    if not _is_file_key(key):
        return None
    path = Path(str(key))
    return path if path.is_absolute() else vault / path


def _manifest_items(vault: Path) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    manifest_path = vault / ".manifest.json"
    if manifest_path.is_file():
        try:
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [
                _item(
                    "critical",
                    "manifest",
                    "Manifest cannot be parsed",
                    subject=".manifest.json",
                    detail=str(exc),
                    action="Repair .manifest.json before relying on ingest delta state.",
                )
            ]
        if not isinstance(raw_manifest, dict):
            return [
                _item(
                    "critical",
                    "manifest",
                    "Manifest root is not a JSON object",
                    subject=".manifest.json",
                    detail="manifest root must be an object",
                    action="Repair .manifest.json before relying on ingest delta state.",
                )
            ]
    for key, entry in _iter_entries(_load_manifest(vault)):
        path = _manifest_source_path(vault, key)
        if path is None:
            continue
        subject = str(key)
        if not path.exists():
            items.append(
                _item(
                    "maintenance",
                    "manifest",
                    f"Manifest source is missing: {subject}",
                    subject=subject,
                    detail="manifest entry points to a filesystem source that is no longer present",
                    action="Restore the source file, update the manifest entry, or archive the stale provenance.",
                )
            )
            continue
        expected = _strip_algo(entry.get("content_hash"))
        if expected:
            try:
                actual = _strip_algo(compute_hash(path))
            except OSError as exc:
                items.append(
                    _item(
                        "maintenance",
                        "manifest",
                        f"Manifest source cannot be hashed: {subject}",
                        subject=subject,
                        detail=str(exc),
                        action="Inspect file permissions or remove the stale manifest entry.",
                    )
                )
                continue
            if actual != expected:
                items.append(
                    _item(
                        "needs_ingest",
                        "manifest",
                        f"Manifest source changed: {subject}",
                        subject=subject,
                        detail="content_hash differs from the current file content",
                        action="Re-ingest the source or update the manifest after confirming no wiki changes are needed.",
                    )
                )
    return items


def _bundle_items(vault: Path) -> list[dict[str, str]]:
    report = check_source_bundles(vault)
    items: list[dict[str, str]] = []
    for bundle_id, bundle in report["bundles"].items():
        if bundle["status"] == "pass":
            continue
        for error in bundle["errors"]:
            items.append(
                _item(
                    "critical",
                    "source-bundle",
                    f"Source bundle {bundle_id} is invalid",
                    subject=bundle_id,
                    detail=error.get("message", error.get("code", "bundle integrity failure")),
                    action="Restore the captured artifact or create a replacement bundle with a new id.",
                )
            )
    return items


def _closure_items(vault: Path) -> list[dict[str, str]]:
    findings = lint_source_bundle_closure(vault, iter_pages(vault))
    items: list[dict[str, str]] = []
    for finding in findings["invalid_source_bundle_bindings"]:
        items.append(
            _item(
                "critical",
                "source-closure",
                f"Source page {finding['page']} has invalid bundle metadata",
                subject=finding["page"],
                detail="; ".join(finding["errors"]),
                action="Fix source_bundle and entities frontmatter.",
            )
        )
    for finding in findings["missing_source_bundle_targets"]:
        items.append(
            _item(
                "critical",
                "source-closure",
                f"Source page {finding['page']} references a missing bundle",
                subject=finding["page"],
                detail=f"bundle={finding['bundle']}",
                action="Create the missing bundle or correct the source_bundle id.",
            )
        )
    for finding in findings["missing_source_entities"]:
        items.append(
            _item(
                "critical",
                "source-closure",
                f"Source page {finding['page']} declares a missing entity",
                subject=finding["page"],
                detail=f"entity={finding['entity']}",
                action="Create the entity page or correct the entities list.",
            )
        )
    for finding in findings["missing_source_entity_links"]:
        items.append(
            _item(
                "critical",
                "source-closure",
                f"Source page {finding['page']} does not link a declared entity",
                subject=finding["page"],
                detail=f"entity={finding['entity']}",
                action="Add a body link to the declared entity or remove the declaration.",
            )
        )
    for finding in findings["missing_entity_source_backlinks"]:
        items.append(
            _item(
                "critical",
                "source-closure",
                f"Entity {finding['entity']} does not link back to source {finding['page']}",
                subject=finding["entity"],
                detail=f"source={finding['page']}",
                action="Add a backlink from the entity page to the source page.",
            )
        )
    return items


def _project_timeline_items(vault: Path, *, link_format: str) -> list[dict[str, str]]:
    report = check_project_timelines(vault, link_format=link_format)
    items: list[dict[str, str]] = []
    for error in report["errors"]:
        subject = str(error.get("path") or error.get("project") or error.get("code"))
        items.append(
            _item(
                "critical",
                "project-timeline",
                "Project timeline cannot be checked cleanly",
                subject=subject,
                detail=str(error.get("message") or error.get("code")),
                action="Fix the project metadata or generated timeline markers.",
            )
        )
    for path in report["changed"]:
        items.append(
            _item(
                "maintenance",
                "project-timeline",
                f"Project timeline is out of date: {path}",
                subject=path,
                detail="generated project timeline differs from source metadata",
                action="Run obsidian-wiki project-timelines to rebuild generated blocks.",
            )
        )
    return items


def _promotion_items(vault: Path) -> list[dict[str, str]]:
    """Surface trusted eligible plans and fail closed on an invalid ledger."""
    try:
        ledger = load_ledger(vault)
    except PromotionError as exc:
        return [
            _item(
                "critical",
                "promotion-ledger",
                "Promotion candidate ledger cannot be trusted",
                subject=PROMOTION_LEDGER_RELATIVE_PATH.as_posix(),
                detail=str(exc),
                action=(
                    "Restore or repair the promotion ledger before observing or "
                    "resolving candidates."
                ),
            )
        ]

    items: list[dict[str, str]] = []
    for candidate_id, candidate in sorted(ledger["candidates"].items()):
        if candidate["state"] == "promoted":
            canonical_path = candidate["canonical_path"]
            target = vault / canonical_path
            try:
                if any(
                    path.is_symlink()
                    for path in [
                        vault / Path(*Path(canonical_path).parts[:index])
                        for index in range(1, len(Path(canonical_path).parts) + 1)
                    ]
                ):
                    raise ValueError("canonical path contains a symlink")
                resolved = target.resolve(strict=True)
                resolved.relative_to(vault)
                if not resolved.is_file() or resolved.suffix.lower() != ".md":
                    raise ValueError("canonical path is not a Markdown file")
            except (FileNotFoundError, ValueError):
                items.append(
                    _item(
                        "critical",
                        "promotion-ledger",
                        f"Promoted candidate page is missing: {candidate_id}",
                        subject=candidate_id,
                        detail=f"canonical_path={canonical_path}",
                        action=(
                            "Restore the canonical page or repair the terminal "
                            "promotion record after reviewing its provenance."
                        ),
                    )
                )
            if candidate["eligibility"]["blocked"]:
                items.append(
                    _item(
                        "reference",
                        "promotion-review",
                        f"Promoted candidate has a later identity conflict: {candidate['canonical_title']}",
                        subject=candidate_id,
                        detail=(
                            "blocked="
                            + ",".join(candidate["eligibility"]["blocked"])
                        ),
                        action=(
                            "Review the conflicting candidates together; keep the "
                            "canonical page unchanged until identity is resolved."
                        ),
                    )
                )
            continue
        if candidate["state"] == "rejected":
            continue
        if candidate["state"] != "eligible":
            blocked = candidate["eligibility"]["blocked"]
            if blocked:
                items.append(
                    _item(
                        "reference",
                        "promotion-review",
                        f"Promotion candidate requires review: {candidate['canonical_title']}",
                        subject=candidate_id,
                        detail=f"blocked={','.join(blocked)}",
                        action=(
                            "Review ambiguity or identity conflicts in one batch; "
                            "then deliberately promote or reject the candidate."
                        ),
                    )
                )
            continue
        plan = candidate["promotion_plan"]
        if not isinstance(plan, dict):
            # load_ledger validates this invariant. Keep the guard local so a
            # future schema change cannot silently hide actionable debt.
            items.append(
                _item(
                    "critical",
                    "promotion-ledger",
                    f"Eligible promotion candidate has no plan: {candidate_id}",
                    subject=candidate_id,
                    detail="eligible candidate is missing a validated promotion_plan",
                    action="Repair the promotion ledger before running wiki-ingest.",
                )
            )
            continue
        items.append(
            _item(
                "maintenance",
                "promotion-candidate",
                f"Promotion candidate is eligible: {candidate['canonical_title']}",
                subject=candidate_id,
                detail=(
                    f"reason={plan['reason']}; target={plan['target_path']}; "
                    f"lineages={len(plan['source_lineages'])}"
                ),
                action=(
                    "Run wiki-ingest with the promotion plan, then resolve it as "
                    "promoted only after the canonical page and required metadata "
                    "updates succeed."
                ),
            )
        )
    return items


def build_backlog(
    vault: Path,
    *,
    link_format: str = "wikilink",
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Aggregate deterministic maintenance debt across existing validators."""
    root = Path(vault).expanduser().resolve()
    items = _sort_items(
        [
            *_source_state_items(root, config_dir=config_dir),
            *_manifest_items(root),
            *_bundle_items(root),
            *_closure_items(root),
            *_project_timeline_items(root, link_format=link_format),
            *_promotion_items(root),
        ]
    )
    summary = {
        "total": len(items),
        "critical": sum(1 for item in items if item["severity"] == "critical"),
        "needs_ingest": sum(1 for item in items if item["severity"] == "needs_ingest"),
        "maintenance": sum(1 for item in items if item["severity"] == "maintenance"),
        "reference": sum(1 for item in items if item["severity"] == "reference"),
    }
    status = "fail" if summary["critical"] else "warn" if summary["total"] else "pass"
    return {
        "schema_version": BACKLOG_SCHEMA_VERSION,
        "status": status,
        "vault": str(root),
        "summary": summary,
        "items": items,
    }


def render_backlog(report: dict[str, Any]) -> str:
    """Render a compact Markdown backlog suitable for _backlog.md."""
    generated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    lines = [
        "---",
        "title: Wiki Backlog",
        "generated_by: obsidian-wiki backlog",
        f"generated_at: {generated}",
        f"status: {report['status']}",
        "---",
        "",
        "# Wiki Backlog",
        "",
        (
            f"Total: {report['summary']['total']} | Critical: {report['summary']['critical']} | "
            f"Needs ingest: {report['summary']['needs_ingest']} | "
            f"Maintenance: {report['summary']['maintenance']} | "
            f"Reference: {report['summary']['reference']}"
        ),
        "",
    ]
    groups = (
        ("critical", "Critical"),
        ("needs_ingest", "Needs Ingest"),
        ("maintenance", "Maintenance"),
        ("reference", "Reference"),
    )
    for severity, title in groups:
        group = [item for item in report["items"] if item["severity"] == severity]
        if not group:
            continue
        lines.extend([f"## {title}", ""])
        for item in group:
            lines.append(f"- [ ] {item['title']}")
            lines.append(f"  - kind: {item['kind']}")
            lines.append(f"  - subject: `{item['subject']}`")
            lines.append(f"  - detail: {item['detail']}")
            lines.append(f"  - action: {item['action']}")
        lines.append("")
    if not report["items"]:
        lines.append("No deterministic maintenance debt found.")
        lines.append("")
    return "\n".join(lines)


def write_backlog(vault: Path, report: dict[str, Any]) -> Path:
    """Write the generated backlog report to the vault root."""
    path = Path(vault).expanduser().resolve() / BACKLOG_PATH
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(render_backlog(report))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return path
