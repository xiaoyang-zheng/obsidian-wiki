"""Project membership and deterministic generated timelines."""

from __future__ import annotations

import json
import os
import posixpath
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote


TIMELINE_BEGIN = "<!-- BEGIN obsidian-wiki:auto-project-timeline -->"
TIMELINE_END = "<!-- END obsidian-wiki:auto-project-timeline -->"
TIMELINE_SCHEMA_VERSION = 1

_SKIP_DIRS = frozenset(
    {"_raw", "_sources", "_archived", "_staging", "_archives", "_bootstrap", "_meta", "_readouts", ".obsidian", ".git"}
)
_SKIP_ROOT_FILES = frozenset({"index.md", "log.md", "hot.md", "_insights.md", "_backlog.md"})
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
_TOP_LEVEL_FIELD_RE = re.compile(r"^([A-Za-z_][\w-]*):(?:[ \t]*(.*))?$")
_LIST_ITEM_RE = re.compile(r"^[ \t]+-[ \t]*(.*)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_H1_RE = re.compile(r"^[ \t]{0,3}#[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_WIKILINK_RE = re.compile(r"\[\[([^]|#]+)(?:#[^]|]+)?(?:\|([^]]+))?\]\]")
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\ufeff]")


class ProjectTimelineError(ValueError):
    """A structured project timeline validation error."""

    def __init__(self, errors: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> None:
        if isinstance(errors, Mapping):
            records = (dict(errors),)
        else:
            records = tuple(dict(error) for error in errors)
        self.errors = records
        super().__init__("; ".join(str(error.get("message", error.get("code", "project timeline error"))) for error in records))


@dataclass(frozen=True)
class ProjectOverview:
    project_id: str
    path: Path
    relative_path: str
    layout: str


@dataclass(frozen=True)
class TimelineEntry:
    date: str
    path: str
    title: str
    blurb: str


@dataclass(frozen=True)
class TimelineChange:
    path: Path
    relative_path: str
    original: str
    replacement: str


@dataclass(frozen=True)
class ProjectTimelinePlan:
    vault: Path
    link_format: str
    projects_scanned: int
    entries: int
    changes: tuple[TimelineChange, ...]
    errors: tuple[dict[str, Any], ...]

    def report(self, *, status: str | None = None, check: bool = False) -> dict[str, Any]:
        resolved_status = status or ("error" if self.errors else "drift" if self.changes else "clean")
        return {
            "schema_version": TIMELINE_SCHEMA_VERSION,
            "status": resolved_status,
            "check": check,
            "vault": str(self.vault),
            "link_format": self.link_format,
            "projects_scanned": self.projects_scanned,
            "entries": self.entries,
            "changed": [change.relative_path for change in self.changes],
            "errors": [dict(error) for error in self.errors],
        }


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _frontmatter(text: str) -> str:
    match = _FRONTMATTER_RE.match(text)
    return match.group(1) if match else ""


def _without_yaml_comment(value: str) -> str:
    quote_char = ""
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote_char == '"' and character == "\\":
            escaped = True
            continue
        if quote_char:
            if character == quote_char:
                quote_char = ""
            continue
        if character in {"'", '"'}:
            quote_char = character
        elif character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _scalar(raw: str) -> str:
    value = _without_yaml_comment(raw.strip())
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else value
    return value


def _inline_list(raw: str) -> list[str]:
    value = _without_yaml_comment(raw.strip())
    if not (value.startswith("[") and value.endswith("]")):
        raise ProjectTimelineError(
            _error("invalid_projects", "projects must be a YAML list")
        )
    inner = value[1:-1].strip()
    if not inner:
        return []
    parts: list[str] = []
    start = 0
    quote_char = ""
    escaped = False
    for index, character in enumerate(inner):
        if escaped:
            escaped = False
            continue
        if quote_char == '"' and character == "\\":
            escaped = True
            continue
        if quote_char:
            if character == quote_char:
                quote_char = ""
            continue
        if character in {"'", '"'}:
            quote_char = character
        elif character == ",":
            parts.append(inner[start:index])
            start = index + 1
    if quote_char:
        raise ProjectTimelineError(
            _error("invalid_projects", "projects contains an unterminated quoted value")
        )
    parts.append(inner[start:])
    return [_scalar(part) for part in parts]


def _frontmatter_values(frontmatter: str) -> dict[str, Any]:
    lines = frontmatter.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    values: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        match = _TOP_LEVEL_FIELD_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        key = match.group(1)
        raw = (match.group(2) or "").strip()
        cursor = index + 1
        children: list[str] = []
        while cursor < len(lines):
            child = lines[cursor]
            if _TOP_LEVEL_FIELD_RE.match(child):
                break
            children.append(child)
            cursor += 1
        if key in values:
            raise ProjectTimelineError(
                _error("duplicate_frontmatter_field", f"duplicate frontmatter field: {key}", field=key)
            )
        if raw.startswith("["):
            values[key] = _inline_list(raw)
        elif raw in {"|", "|-", ">", ">-"}:
            content = [child.strip() for child in children if child.strip()]
            values[key] = ("\n" if raw.startswith("|") else " ").join(content)
        elif not raw:
            items = []
            for child in children:
                item = _LIST_ITEM_RE.match(child)
                if item is not None:
                    items.append(_scalar(item.group(1)))
            values[key] = items if items else ""
        else:
            values[key] = _scalar(raw)
        index = cursor
    return values


def normalise_project_id(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ProjectTimelineError(
            _error("invalid_project_id", "project identifiers must be strings")
        )
    project_id = _ZERO_WIDTH_RE.sub("", unicodedata.normalize("NFC", raw)).strip()
    if (
        not project_id
        or project_id in {".", ".."}
        or "/" in project_id
        or "\\" in project_id
        or any(ord(character) < 32 for character in project_id)
    ):
        raise ProjectTimelineError(
            _error("invalid_project_id", f"invalid project identifier: {raw!r}")
        )
    return project_id


def _normalise_project_list(values: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ProjectTimelineError(
            _error(
                "invalid_projects" if field == "projects" else "invalid_legacy_project",
                f"{field} must be {'a YAML list' if field == 'projects' else 'a scalar'}",
                field=field,
            )
        )
    normalised: list[str] = []
    for raw in values:
        project_id = normalise_project_id(raw)
        if project_id not in normalised:
            normalised.append(project_id)
    return tuple(normalised)


def parse_projects(frontmatter: str | Mapping[str, Any]) -> tuple[str, ...] | None:
    """Parse the preferred ``projects`` list; ``None`` means it is absent."""
    metadata = (
        dict(frontmatter)
        if isinstance(frontmatter, Mapping)
        else _frontmatter_values(_frontmatter(frontmatter) or frontmatter)
    )
    if "projects" not in metadata:
        return None
    return _normalise_project_list(metadata["projects"], field="projects")


def _registry_ids(registry: Mapping[str, Any] | Iterable[str]) -> dict[str, str]:
    raw_ids = registry.keys() if isinstance(registry, Mapping) else registry
    ids: dict[str, str] = {}
    for raw in raw_ids:
        canonical = str(raw)
        normalised = normalise_project_id(canonical)
        if normalised in ids and ids[normalised] != canonical:
            raise ProjectTimelineError(
                _error(
                    "ambiguous_project_id",
                    f"project identifiers normalise to the same value: {ids[normalised]!r}, {canonical!r}",
                    projects=[ids[normalised], canonical],
                )
            )
        ids[normalised] = canonical
    return ids


def effective_projects(
    path: str | Path,
    metadata: str | Mapping[str, Any],
    registry: Mapping[str, Any] | Iterable[str],
) -> tuple[str, ...]:
    """Resolve membership by ``projects``, legacy ``project``, then path."""
    values = (
        dict(metadata)
        if isinstance(metadata, Mapping)
        else _frontmatter_values(_frontmatter(metadata) or metadata)
    )
    canonical_ids = _registry_ids(registry)
    explicit = parse_projects(values)
    if explicit is not None:
        requested = explicit
    elif "project" in values:
        legacy = values["project"]
        if not isinstance(legacy, str) or not legacy.strip():
            raise ProjectTimelineError(
                _error("invalid_legacy_project", "legacy project must be a non-empty scalar", field="project")
            )
        requested = (normalise_project_id(legacy),)
    else:
        relative = Path(path)
        parts = relative.parts
        requested = ()
        if len(parts) >= 3 and parts[0] == "projects":
            inferred = normalise_project_id(parts[1])
            if inferred in canonical_ids:
                requested = (inferred,)

    missing = [project_id for project_id in requested if project_id not in canonical_ids]
    if missing:
        raise ProjectTimelineError(
            _error(
                "missing_project_target",
                f"unknown project membership: {', '.join(missing)}",
                projects=missing,
            )
        )
    return tuple(canonical_ids[project_id] for project_id in requested)


def _discover_projects(vault: Path) -> tuple[dict[str, ProjectOverview], list[dict[str, Any]]]:
    project_root = vault / "projects"
    if not project_root.is_dir():
        return {}, []

    candidates: dict[str, list[ProjectOverview]] = {}
    for path in sorted(project_root.glob("*.md")):
        if path.name.startswith("."):
            continue
        project_id = normalise_project_id(path.stem)
        candidates.setdefault(project_id, []).append(
            ProjectOverview(project_id, path, path.relative_to(vault).as_posix(), "flat")
        )
    for folder in sorted(path for path in project_root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        path = folder / f"{folder.name}.md"
        if not path.is_file():
            continue
        project_id = normalise_project_id(folder.name)
        candidates.setdefault(project_id, []).append(
            ProjectOverview(project_id, path, path.relative_to(vault).as_posix(), "folder")
        )

    registry: dict[str, ProjectOverview] = {}
    errors: list[dict[str, Any]] = []
    for project_id, matches in sorted(candidates.items()):
        if len(matches) != 1:
            paths = [match.relative_path for match in matches]
            errors.append(
                _error(
                    "ambiguous_project_overview",
                    f"project {project_id!r} has multiple overview pages: {', '.join(paths)}",
                    project=project_id,
                    paths=paths,
                )
            )
            continue
        registry[project_id] = matches[0]
    return registry, errors


def discover_projects(vault: Path) -> dict[str, ProjectOverview]:
    """Discover flat and folder-note project overviews."""
    root = Path(vault).expanduser().resolve()
    registry, errors = _discover_projects(root)
    if errors:
        raise ProjectTimelineError(errors)
    return registry


def _iter_pages(vault: Path) -> Iterable[Path]:
    for path in sorted(vault.rglob("*.md")):
        relative = path.relative_to(vault)
        if path.name in _SKIP_ROOT_FILES and len(relative.parts) == 1:
            continue
        if any(part in _SKIP_DIRS for part in relative.parts):
            continue
        yield path


def _clean_text(value: Any) -> str:
    text = str(value)
    text = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1), text)
    text = _WIKILINK_RE.sub(lambda match: match.group(2) or match.group(1), text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("`", "")
    return " ".join(text.split())


def _entry_date(metadata: Mapping[str, Any]) -> str:
    field = "timeline_date" if "timeline_date" in metadata else "created"
    raw = metadata.get(field, "")
    value = str(raw).strip() if isinstance(raw, str) else ""
    if not value:
        raise ProjectTimelineError(
            _error(
                "missing_timeline_date",
                "project timeline entries require timeline_date or created",
            )
        )
    date_value = value if _DATE_RE.fullmatch(value) else value[:10]
    if not _DATE_RE.fullmatch(date_value):
        raise ProjectTimelineError(
            _error(
                "invalid_timeline_date",
                f"{field} must use YYYY-MM-DD or an ISO-8601 timestamp",
                field=field,
                value=value,
            )
        )
    try:
        parsed_date = date.fromisoformat(date_value)
    except ValueError as exc:
        raise ProjectTimelineError(
            _error("invalid_timeline_date", f"{field} is not a valid date", field=field, value=value)
        ) from exc
    if field == "created" and value != date_value:
        timestamp = value.replace("Z", "+00:00")
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ProjectTimelineError(
                _error("invalid_timeline_date", "created must be an ISO-8601 timestamp", field=field, value=value)
            ) from exc
        if parsed_timestamp.date() != parsed_date:
            raise ProjectTimelineError(
                _error("invalid_timeline_date", "created timestamp date is inconsistent", field=field, value=value)
            )
    elif value != date_value:
        raise ProjectTimelineError(
            _error("invalid_timeline_date", "timeline_date must use YYYY-MM-DD", field=field, value=value)
        )
    return date_value


def _entry_title(path: Path, metadata: Mapping[str, Any], text: str) -> str:
    raw = metadata.get("title", "")
    if isinstance(raw, str) and raw.strip():
        return _clean_text(raw)
    body_match = _FRONTMATTER_RE.match(text)
    body = text[body_match.end():] if body_match else text
    heading = _H1_RE.search(body)
    return _clean_text(heading.group(1) if heading else path.stem)


def _entry_blurb(metadata: Mapping[str, Any], title: str) -> str:
    for field in ("timeline_blurb", "summary", "title"):
        raw = metadata.get(field, "")
        if isinstance(raw, str) and raw.strip():
            return _clean_text(raw)
    return title


def _collect_timeline_entries(
    vault: Path,
    registry: Mapping[str, ProjectOverview],
) -> tuple[dict[str, list[TimelineEntry]], list[dict[str, Any]]]:
    grouped = {project_id: [] for project_id in registry}
    overview_paths = {overview.path.resolve() for overview in registry.values()}
    errors: list[dict[str, Any]] = []

    for path in _iter_pages(vault):
        if path.resolve() in overview_paths:
            continue
        relative = path.relative_to(vault).as_posix()
        try:
            text = _read_text(path)
            metadata = _frontmatter_values(_frontmatter(text))
            memberships = effective_projects(relative, metadata, registry)
            if not memberships:
                continue
            entry_date = _entry_date(metadata)
            title = _entry_title(path, metadata, text)
            entry = TimelineEntry(entry_date, relative, title, _entry_blurb(metadata, title))
        except (OSError, UnicodeError, ProjectTimelineError) as exc:
            records = exc.errors if isinstance(exc, ProjectTimelineError) else (
                _error("project_page_read_error", str(exc)),
            )
            for record in records:
                errors.append({**record, "path": relative})
            continue
        for project_id in memberships:
            grouped[project_id].append(entry)

    for entries in grouped.values():
        entries.sort(key=lambda entry: entry.path)
        entries.sort(key=lambda entry: entry.date, reverse=True)
    return grouped, errors


def collect_timeline_entries(
    vault: Path,
    registry: Mapping[str, ProjectOverview] | None = None,
) -> dict[str, list[TimelineEntry]]:
    """Collect dated entries under their effective project memberships."""
    root = Path(vault).expanduser().resolve()
    resolved_registry = dict(registry) if registry is not None else discover_projects(root)
    grouped, errors = _collect_timeline_entries(root, resolved_registry)
    if errors:
        raise ProjectTimelineError(errors)
    return grouped


def _quarter(value: str) -> str:
    return f"{value[:4]} Q{(int(value[5:7]) - 1) // 3 + 1}"


def _markdown_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _entry_link(overview: ProjectOverview, entry: TimelineEntry, link_format: str) -> str:
    if link_format == "wikilink":
        target = entry.path[:-3] if entry.path.lower().endswith(".md") else entry.path
        label = entry.title.replace("|", "¦").replace("]", "）").replace("[", "（")
        return f"[[{target}|{label}]]"
    if link_format == "markdown":
        relative = posixpath.relpath(entry.path, posixpath.dirname(overview.relative_path))
        return f"[{_markdown_label(entry.title)}]({quote(relative, safe='/._~-')})"
    raise ProjectTimelineError(
        _error("invalid_link_format", "link format must be wikilink or markdown", value=link_format)
    )


def render_timeline(
    project: ProjectOverview,
    entries: Iterable[TimelineEntry],
    link_format: str = "wikilink",
) -> str:
    """Render a complete generated timeline block."""
    ordered = sorted(entries, key=lambda entry: entry.path)
    ordered.sort(key=lambda entry: entry.date, reverse=True)
    lines = [TIMELINE_BEGIN, "## Timeline", ""]
    if not ordered:
        lines.append("_No dated project entries._")
    else:
        current_quarter = ""
        for entry in ordered:
            quarter = _quarter(entry.date)
            if quarter != current_quarter:
                if current_quarter:
                    lines.append("")
                lines.extend([f"### {quarter}", ""])
                current_quarter = quarter
            lines.append(
                f"- **{entry.date}** — {_entry_link(project, entry, link_format)} — {entry.blurb}"
            )
    lines.append(TIMELINE_END)
    return "\n".join(lines)


def _timeline_span(text: str) -> tuple[int, int] | None:
    begin_count = text.count(TIMELINE_BEGIN)
    end_count = text.count(TIMELINE_END)
    if begin_count == end_count == 0:
        return None
    begin = text.find(TIMELINE_BEGIN)
    end = text.find(TIMELINE_END)
    if begin_count != 1 or end_count != 1 or begin < 0 or end < begin:
        raise ProjectTimelineError(
            _error(
                "malformed_project_timeline_markers",
                "generated project timeline markers must be one ordered pair",
            )
        )
    return begin, end + len(TIMELINE_END)


def strip_generated_project_timeline(text: str, *, strict: bool = False) -> str:
    """Remove one valid generated block; malformed markers remain visible."""
    try:
        span = _timeline_span(text)
    except ProjectTimelineError:
        if strict:
            raise
        return text
    if span is None:
        return text
    return text[:span[0]] + text[span[1]:]


def _with_timeline(text: str, block: str) -> str:
    span = _timeline_span(text)
    newline = "\r\n" if "\r\n" in text and text.count("\n") == text.count("\r\n") else "\n"
    rendered = block.replace("\n", newline)
    if span is not None:
        return text[:span[0]] + rendered + text[span[1]:]
    prefix = text.rstrip("\r\n")
    return (prefix + newline * 2 if prefix else "") + rendered + newline


def plan_project_timelines(
    vault: Path,
    *,
    link_format: str = "wikilink",
) -> ProjectTimelinePlan:
    """Validate and calculate every project timeline without writing."""
    root = Path(vault).expanduser().resolve()
    if not root.is_dir():
        error = _error("vault_not_found", f"vault not found: {root}", path=str(root))
        return ProjectTimelinePlan(root, link_format, 0, 0, (), (error,))
    if link_format not in {"wikilink", "markdown"}:
        error = _error(
            "invalid_link_format",
            "link format must be wikilink or markdown",
            value=link_format,
        )
        return ProjectTimelinePlan(root, link_format, 0, 0, (), (error,))

    registry, discovery_errors = _discover_projects(root)
    if discovery_errors:
        projects_scanned = len(registry) + len(discovery_errors)
        return ProjectTimelinePlan(root, link_format, projects_scanned, 0, (), tuple(discovery_errors))
    grouped, collection_errors = _collect_timeline_entries(root, registry)
    changes: list[TimelineChange] = []
    marker_errors: list[dict[str, Any]] = []
    for project_id, overview in registry.items():
        try:
            if overview.path.is_symlink():
                raise ProjectTimelineError(
                    _error("unsafe_project_overview", "project overview must not be a symlink")
                )
            original = _read_text(overview.path)
            replacement = _with_timeline(
                original,
                render_timeline(overview, grouped[project_id], link_format),
            )
        except (OSError, UnicodeError, ProjectTimelineError) as exc:
            records = exc.errors if isinstance(exc, ProjectTimelineError) else (
                _error("project_overview_read_error", str(exc)),
            )
            for record in records:
                marker_errors.append(
                    {**record, "project": project_id, "path": overview.relative_path}
                )
            continue
        if replacement != original:
            changes.append(
                TimelineChange(overview.path, overview.relative_path, original, replacement)
            )

    errors = tuple(collection_errors + marker_errors)
    return ProjectTimelinePlan(
        root,
        link_format,
        len(registry),
        sum(len(entries) for entries in grouped.values()),
        tuple(changes),
        errors,
    )


def _stage_text(path: Path, text: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, stat.S_IMODE(path.stat().st_mode))
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def _commit_changes(changes: tuple[TimelineChange, ...]) -> None:
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for change in changes:
            staged[change.path] = _stage_text(change.path, change.replacement)
            backups[change.path] = _stage_text(change.path, change.original)
        for change in changes:
            if _read_text(change.path) != change.original:
                raise RuntimeError(f"project overview changed while timelines were being prepared: {change.relative_path}")
        for change in changes:
            os.replace(staged[change.path], change.path)
            replaced.append(change.path)
        for directory in {change.path.parent for change in changes}:
            try:
                descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                pass
    except BaseException as exc:
        rollback_errors: list[str] = []
        for target in reversed(replaced):
            try:
                os.replace(backups[target], target)
            except OSError as rollback_error:
                rollback_errors.append(f"{target}: {rollback_error}")
        detail = f"; rollback failed: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise ProjectTimelineError(
            _error("timeline_write_failed", f"{exc}{detail}")
        ) from exc
    finally:
        for temporary in (*staged.values(), *backups.values()):
            temporary.unlink(missing_ok=True)


def write_project_timelines(
    vault: Path,
    *,
    link_format: str = "wikilink",
) -> dict[str, Any]:
    """Plan all timelines, then atomically replace every changed overview."""
    plan = plan_project_timelines(vault, link_format=link_format)
    if plan.errors:
        return plan.report(status="error")
    if not plan.changes:
        return plan.report(status="clean")
    try:
        _commit_changes(plan.changes)
    except ProjectTimelineError as exc:
        failed = ProjectTimelinePlan(
            plan.vault,
            plan.link_format,
            plan.projects_scanned,
            plan.entries,
            plan.changes,
            exc.errors,
        )
        return failed.report(status="error")
    return plan.report(status="updated")


def check_project_timelines(
    vault: Path,
    *,
    link_format: str = "wikilink",
) -> dict[str, Any]:
    """Return timeline drift and validation errors without writing."""
    return plan_project_timelines(vault, link_format=link_format).report(check=True)


PROJECT_LINT_FINDINGS = (
    "invalid_project_memberships",
    "missing_project_targets",
    "conflicting_project_membership",
    "redundant_legacy_project_field",
    "ambiguous_project_overviews",
    "invalid_timeline_metadata",
    "project_timeline_drift",
    "malformed_project_timeline_markers",
)


def lint_project_metadata(
    vault: Path,
    *,
    link_format: str = "wikilink",
) -> dict[str, list[dict[str, Any]]]:
    """Return opt-in project membership and generated-timeline findings.

    Legacy vaults that have never used ``projects:`` or generated timeline
    markers remain unaffected. Once the feature is used, explicit membership
    and the generated projection are checked together.
    """
    root = Path(vault).expanduser().resolve()
    findings: dict[str, list[dict[str, Any]]] = {name: [] for name in PROJECT_LINT_FINDINGS}
    opted_in = False

    for path in _iter_pages(root):
        relative = path.relative_to(root).as_posix()
        try:
            text = _read_text(path)
            frontmatter = _frontmatter(text)
            has_projects = re.search(r"^projects:", frontmatter, re.MULTILINE) is not None
            if has_projects or TIMELINE_BEGIN in text or TIMELINE_END in text:
                opted_in = True
            metadata = _frontmatter_values(frontmatter)
        except (OSError, UnicodeError, ProjectTimelineError):
            # The planner below owns canonical parsing/read errors and reports
            # them once with a structured code.
            continue

        if has_projects and "project" in metadata:
            try:
                explicit = parse_projects(metadata) or ()
                legacy_raw = metadata["project"]
                if not isinstance(legacy_raw, str) or not legacy_raw.strip():
                    raise ProjectTimelineError(
                        _error("invalid_legacy_project", "legacy project must be a non-empty scalar")
                    )
                legacy = normalise_project_id(legacy_raw)
            except ProjectTimelineError:
                # The planner will surface invalid values as a hard finding.
                continue
            record = {
                "page": relative,
                "projects": list(explicit),
                "legacy_project": legacy,
            }
            if explicit == (legacy,):
                findings["redundant_legacy_project_field"].append(record)
            else:
                findings["conflicting_project_membership"].append(record)

        if "timeline_blurb" in metadata:
            blurb = metadata["timeline_blurb"]
            issue = None
            if not isinstance(blurb, str) or not blurb.strip():
                issue = "timeline_blurb must be a non-empty scalar"
            elif "\n" in blurb or _WIKILINK_RE.search(blurb) or _MARKDOWN_LINK_RE.search(blurb):
                issue = "timeline_blurb must be plain single-line text without links"
            elif len(blurb) > 200:
                issue = "timeline_blurb must be at most 200 characters"
            if issue:
                findings["invalid_timeline_metadata"].append(
                    {"page": relative, "field": "timeline_blurb", "issue": issue}
                )

    if not opted_in:
        return findings

    plan = plan_project_timelines(root, link_format=link_format)
    category_by_code = {
        "ambiguous_project_overview": "ambiguous_project_overviews",
        "malformed_project_timeline_markers": "malformed_project_timeline_markers",
        "missing_project_target": "missing_project_targets",
        "missing_timeline_date": "invalid_timeline_metadata",
        "invalid_timeline_date": "invalid_timeline_metadata",
        "invalid_projects": "invalid_project_memberships",
        "invalid_project_id": "invalid_project_memberships",
        "invalid_legacy_project": "invalid_project_memberships",
        "duplicate_frontmatter_field": "invalid_project_memberships",
    }
    for error in plan.errors:
        category = category_by_code.get(error.get("code"), "invalid_timeline_metadata")
        findings[category].append(dict(error))
    findings["project_timeline_drift"] = [
        {"page": relative_path} for relative_path in plan.report()["changed"]
    ]
    return findings
