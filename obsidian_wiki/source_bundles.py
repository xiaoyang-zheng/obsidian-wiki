"""Immutable source bundles and local media provenance for wiki sources."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import secrets
import shutil
import stat
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


BUNDLE_ROOT = "_sources"
BUNDLE_MANIFEST = "bundle.json"
BUNDLE_SCHEMA_VERSION = 1
_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^]|#]+)(?:[|#][^\]]*)?\]\]")
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
_FIELD_RE = re.compile(r"^([A-Za-z_][\w-]*):(?:[ \t]*(.*))?$")
_LIST_ITEM_RE = re.compile(r"^[ \t]+-[ \t]*(.*)$")


class SourceBundleError(RuntimeError):
    """Raised for invalid or unsafe source-bundle operations."""


@contextmanager
def _bundle_lock(bundle_dir: Path, *, timeout: float = 10.0, stale_after: float = 60.0):
    """Serialize bundle manifest/media updates without locking the whole vault."""
    lock = bundle_dir / ".bundle.lock"
    deadline = time.monotonic() + timeout
    token = secrets.token_hex(16)
    descriptor: int | None = None
    while True:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, token.encode("ascii"))
            os.close(descriptor)
            descriptor = None
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
                raise SourceBundleError(
                    f"could not acquire bundle lock {lock} within {timeout}s"
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if lock.read_text(encoding="ascii") == token:
                lock.unlink()
        except FileNotFoundError:
            pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _remove_zero_width(value: str) -> str:
    return "".join(
        character
        for character in value
        if ord(character) not in {0x200B, 0x200C, 0x200D, 0xFEFF}
    )


def normalise_bundle_id(raw: object) -> str:
    """Return a stable id that is safe as one bundle directory name."""
    if not isinstance(raw, str):
        raise SourceBundleError("bundle id must be a string")
    value = _remove_zero_width(unicodedata.normalize("NFC", raw)).strip()
    if (
        not value
        or value in {".", ".."}
        or value.startswith(".")
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise SourceBundleError(f"invalid bundle id: {raw!r}")
    return value


def source_bundles_root(vault: Path) -> Path:
    return Path(vault) / BUNDLE_ROOT


def source_bundle_path(vault: Path, bundle_id: str) -> Path:
    return source_bundles_root(vault) / normalise_bundle_id(bundle_id)


def is_source_bundle_artifact(path: Path, vault: Path) -> bool:
    """Return whether path is below the immutable bundle tree."""
    try:
        relative = path.relative_to(vault)
    except ValueError:
        return False
    return bool(relative.parts and relative.parts[0] == BUNDLE_ROOT)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _safe_filename(raw: object) -> str:
    if not isinstance(raw, str):
        raise SourceBundleError("artifact filename must be a string")
    name = raw.strip()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise SourceBundleError(f"invalid artifact filename: {raw!r}")
    return name


def _relative_artifact_path(raw: object, expected_parent: str) -> PurePosixPath:
    if not isinstance(raw, str):
        raise SourceBundleError("bundle artifact path must be a string")
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != expected_parent
        or relative.parts[1] in {"", ".", ".."}
    ):
        raise SourceBundleError(
            f"bundle artifact path must be {expected_parent}/<filename>: {raw!r}"
        )
    _safe_filename(relative.parts[1])
    return relative


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceBundleError(f"cannot read bundle manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SourceBundleError(f"bundle manifest must be a JSON object: {path}")
    return data


def _validate_manifest(bundle_dir: Path, bundle_id: str, manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        errors.append({"code": "invalid_schema_version", "message": "unsupported bundle schema version"})
    if manifest.get("id") != bundle_id:
        errors.append({"code": "bundle_id_mismatch", "message": "bundle manifest id does not match directory"})
    if not isinstance(manifest.get("source_type"), str) or not manifest["source_type"].strip():
        errors.append({"code": "invalid_source_type", "message": "source_type must be a non-empty string"})
    if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"]:
        errors.append({"code": "invalid_created_at", "message": "created_at must be a non-empty timestamp"})

    for key, expected_parent, minimum in (("artifacts", "raw", 1), ("media", "media", 0)):
        records = manifest.get(key)
        if not isinstance(records, list) or len(records) < minimum:
            errors.append({"code": f"invalid_{key}", "message": f"{key} must contain at least {minimum} entries"})
            continue
        seen: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                errors.append({"code": f"invalid_{key}", "message": f"{key} entries must be objects"})
                continue
            try:
                relative = _relative_artifact_path(record.get("path"), expected_parent)
            except SourceBundleError as exc:
                errors.append({"code": "invalid_artifact_path", "message": str(exc)})
                continue
            relative_text = relative.as_posix()
            if relative_text in seen:
                errors.append({"code": "duplicate_artifact_path", "message": f"duplicate bundle artifact: {relative_text}"})
                continue
            seen.add(relative_text)
            digest = record.get("sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                errors.append({"code": "invalid_artifact_hash", "message": f"invalid hash for {relative_text}"})
                continue
            size = record.get("size_bytes")
            if type(size) is not int or size < 0:
                errors.append({"code": "invalid_artifact_size", "message": f"invalid size for {relative_text}"})
                continue
            target = bundle_dir / relative
            if not target.is_file() or target.is_symlink():
                errors.append({"code": "missing_artifact", "message": f"missing immutable artifact: {relative_text}"})
                continue
            if target.stat().st_size != size:
                errors.append({"code": "artifact_size_mismatch", "message": f"size changed for {relative_text}"})
                continue
            if _sha256(target) != digest:
                errors.append({"code": "artifact_hash_mismatch", "message": f"content changed for {relative_text}"})
    return errors


def _load_valid_bundle(vault: Path, bundle_id: str) -> tuple[Path, dict[str, Any]]:
    canonical_id = normalise_bundle_id(bundle_id)
    bundle_dir = source_bundle_path(vault, canonical_id)
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        raise SourceBundleError(f"source bundle not found: {canonical_id}")
    manifest_path = bundle_dir / BUNDLE_MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise SourceBundleError(f"bundle manifest not found: {canonical_id}")
    manifest = _read_manifest(manifest_path)
    errors = _validate_manifest(bundle_dir, canonical_id, manifest)
    if errors:
        raise SourceBundleError(
            f"source bundle is invalid: {canonical_id}: "
            + "; ".join(error["message"] for error in errors)
        )
    return bundle_dir, manifest


def _write_manifest(bundle_dir: Path, manifest: Mapping[str, Any]) -> None:
    target = bundle_dir / BUNDLE_MANIFEST
    temporary = bundle_dir / f".{BUNDLE_MANIFEST}.{secrets.token_hex(12)}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _copy_immutable(source: Path, bundle_dir: Path, relative: PurePosixPath) -> dict[str, Any]:
    destination = bundle_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as input_handle, destination.open("xb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=1 << 20)
            output_handle.flush()
            os.fsync(output_handle.fileno())
    except OSError as exc:
        raise SourceBundleError(f"cannot localize {source}: {exc}") from exc
    try:
        destination.chmod(destination.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except OSError as exc:
        raise SourceBundleError(f"cannot make bundle artifact read-only: {destination}: {exc}") from exc
    return {
        "path": relative.as_posix(),
        "sha256": _sha256(destination),
        "size_bytes": destination.stat().st_size,
    }


def _bundle_report(bundle_dir: Path, bundle_id: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": bundle_id,
        "path": bundle_dir.as_posix(),
        "status": "pass",
        "source_type": manifest["source_type"],
        "artifacts": list(manifest["artifacts"]),
        "media": list(manifest["media"]),
        "errors": [],
    }


def create_source_bundle(
    vault: Path,
    bundle_id: str,
    source_path: Path,
    *,
    source_type: str = "file",
    original_uri: str | None = None,
    media_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    """Capture a primary local source plus optional local media as one new bundle."""
    canonical_id = normalise_bundle_id(bundle_id)
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise SourceBundleError(f"primary source must be a readable file: {source}")
    normalized_type = source_type.strip()
    if not normalized_type:
        raise SourceBundleError("source_type must not be empty")
    if original_uri is not None and not original_uri.strip():
        raise SourceBundleError("original_uri must not be empty when provided")

    media = [Path(item).expanduser().resolve() for item in media_paths]
    for item in media:
        if not item.is_file():
            raise SourceBundleError(f"media must be a readable file: {item}")
    media_names = [_safe_filename(item.name) for item in media]
    if len(set(media_names)) != len(media_names):
        raise SourceBundleError("media filenames must be unique within a bundle")

    root = source_bundles_root(vault)
    root.mkdir(parents=True, exist_ok=True)
    target = source_bundle_path(vault, canonical_id)
    if target.exists() or target.is_symlink():
        raise SourceBundleError(f"source bundle already exists: {canonical_id}")
    temporary = root / f".{canonical_id}.creating-{secrets.token_hex(12)}"
    try:
        temporary.mkdir()
        primary_relative = PurePosixPath("raw") / _safe_filename(source.name)
        primary = _copy_immutable(source, temporary, primary_relative)
        media_records = [
            _copy_immutable(item, temporary, PurePosixPath("media") / name)
            for item, name in zip(media, media_names)
        ]
        manifest: dict[str, Any] = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "id": canonical_id,
            "created_at": _utc_now(),
            "source_type": normalized_type,
            "artifacts": [primary],
            "media": media_records,
        }
        if original_uri is not None:
            manifest["original_uri"] = original_uri.strip()
        _write_manifest(temporary, manifest)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return _bundle_report(target, canonical_id, manifest)


def localize_bundle_media(
    vault: Path,
    bundle_id: str,
    media_path: Path,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Copy one local media file into an existing bundle without replacing artifacts."""
    source = Path(media_path).expanduser().resolve()
    if not source.is_file():
        raise SourceBundleError(f"media must be a readable file: {source}")
    filename = _safe_filename(name if name is not None else source.name)
    bundle_dir = source_bundle_path(vault, bundle_id)
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        raise SourceBundleError(f"source bundle not found: {normalise_bundle_id(bundle_id)}")
    with _bundle_lock(bundle_dir):
        bundle_dir, manifest = _load_valid_bundle(vault, bundle_id)
        relative = PurePosixPath("media") / filename
        destination = bundle_dir / relative
        if destination.exists() or destination.is_symlink():
            raise SourceBundleError(f"bundle media already exists: {relative.as_posix()}")
        record = _copy_immutable(source, bundle_dir, relative)
        updated = dict(manifest)
        updated["media"] = [*manifest["media"], record]
        try:
            _write_manifest(bundle_dir, updated)
        except BaseException:
            try:
                destination.chmod(destination.stat().st_mode | stat.S_IWUSR)
                destination.unlink()
            except OSError:
                pass
            raise
    return _bundle_report(bundle_dir, normalise_bundle_id(bundle_id), updated)


def check_source_bundles(
    vault: Path,
    *,
    bundle_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Verify every requested bundle manifest and registered artifact hash."""
    if bundle_ids is None:
        root = source_bundles_root(vault)
        identifiers = [
            path.name
            for path in sorted(root.iterdir())
            if (
                path.is_dir()
                and not path.is_symlink()
                and not path.name.startswith(".")
                and (path / BUNDLE_MANIFEST).is_file()
            )
        ] if root.is_dir() else []
    else:
        identifiers = [normalise_bundle_id(item) for item in bundle_ids]
    reports: dict[str, dict[str, Any]] = {}
    for bundle_id in identifiers:
        bundle_dir = source_bundle_path(vault, bundle_id)
        manifest: dict[str, Any] | None = None
        errors: list[dict[str, str]] = []
        if not bundle_dir.is_dir() or bundle_dir.is_symlink():
            errors.append({"code": "missing_bundle", "message": f"source bundle not found: {bundle_id}"})
        else:
            manifest_path = bundle_dir / BUNDLE_MANIFEST
            if not manifest_path.is_file() or manifest_path.is_symlink():
                errors.append({"code": "missing_manifest", "message": f"bundle manifest not found: {bundle_id}"})
            else:
                try:
                    manifest = _read_manifest(manifest_path)
                    errors.extend(_validate_manifest(bundle_dir, bundle_id, manifest))
                except SourceBundleError as exc:
                    errors.append({"code": "invalid_manifest", "message": str(exc)})
        reports[bundle_id] = {
            "id": bundle_id,
            "path": bundle_dir.as_posix(),
            "status": "pass" if not errors else "fail",
            "source_type": manifest.get("source_type") if manifest else None,
            "artifacts": list(manifest.get("artifacts", [])) if manifest else [],
            "media": list(manifest.get("media", [])) if manifest else [],
            "errors": errors,
        }
    invalid = sum(1 for report in reports.values() if report["status"] == "fail")
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "status": "fail" if invalid else "pass",
        "vault": str(Path(vault).resolve()),
        "summary": {"bundles": len(reports), "valid": len(reports) - invalid, "invalid": invalid},
        "bundles": reports,
    }


def _strip_comment(value: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _scalar(raw: str) -> str:
    value = _strip_comment(raw.strip())
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else value
    return value


def _split_inline_list(raw: str) -> list[str]:
    value = _strip_comment(raw.strip())
    if not (value.startswith("[") and value.endswith("]")):
        raise SourceBundleError("entities must be a YAML list or the scalar none")
    inner = value[1:-1].strip()
    if not inner:
        return []
    result: list[str] = []
    start = 0
    quote = ""
    escaped = False
    for index, character in enumerate(inner):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == ",":
            result.append(_scalar(inner[start:index]))
            start = index + 1
    if quote:
        raise SourceBundleError("entities contains an unterminated quoted value")
    result.append(_scalar(inner[start:]))
    return result


def normalise_entity_reference(raw: object) -> str:
    """Normalize an entity reference to entities/<name> without a suffix."""
    if not isinstance(raw, str):
        raise SourceBundleError("entity references must be strings")
    value = raw.strip()
    if value.startswith("[[") and value.endswith("]]" ):
        value = value[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
    if value.endswith(".md"):
        value = value[:-3]
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] != "entities"
        or len(path.parts) < 2
    ):
        raise SourceBundleError("entity references must use entities/<name> paths")
    return "/".join(_remove_zero_width(unicodedata.normalize("NFC", part)).strip() for part in path.parts)


def parse_source_page_binding(text: str) -> dict[str, Any] | None:
    """Parse opt-in source_bundle and entities frontmatter from a wiki page."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    lines = match.group(1).replace("\r\n", "\n").replace("\r", "\n").splitlines()
    fields: dict[str, tuple[str, list[str]]] = {}
    index = 0
    while index < len(lines):
        field = _FIELD_RE.match(lines[index])
        if field is None:
            index += 1
            continue
        key = field.group(1)
        raw = (field.group(2) or "").strip()
        cursor = index + 1
        children: list[str] = []
        while cursor < len(lines) and _FIELD_RE.match(lines[cursor]) is None:
            children.append(lines[cursor])
            cursor += 1
        if key in fields:
            fields[key] = ("__duplicate__", [])
        else:
            fields[key] = (raw, children)
        index = cursor
    if "source_bundle" not in fields:
        return None

    errors: list[str] = []
    bundle_id: str | None = None
    raw_bundle, bundle_children = fields["source_bundle"]
    if raw_bundle == "__duplicate__" or bundle_children or not raw_bundle:
        errors.append("source_bundle must be one scalar bundle id")
    else:
        try:
            bundle_id = normalise_bundle_id(_scalar(raw_bundle))
        except SourceBundleError as exc:
            errors.append(str(exc))

    entities: tuple[str, ...] | None = None
    entities_none = False
    if "entities" not in fields:
        errors.append("source_bundle pages must declare entities: [...] or entities: none")
    else:
        raw_entities, entity_children = fields["entities"]
        if raw_entities == "__duplicate__":
            errors.append("duplicate frontmatter field: entities")
        elif _scalar(raw_entities) == "none" and not entity_children:
            entities_none = True
            entities = ()
        else:
            try:
                if raw_entities:
                    values = _split_inline_list(raw_entities)
                else:
                    values = []
                    for child in entity_children:
                        item = _LIST_ITEM_RE.match(child)
                        if item is None:
                            if child.strip():
                                raise SourceBundleError("entities block list may contain only list items")
                            continue
                        values.append(_scalar(item.group(1)))
                normalized: list[str] = []
                for value in values:
                    entity = normalise_entity_reference(value)
                    if entity not in normalized:
                        normalized.append(entity)
                if not normalized:
                    raise SourceBundleError("use entities: none for a source without entities")
                entities = tuple(normalized)
            except SourceBundleError as exc:
                errors.append(str(exc))
    return {
        "bundle_id": bundle_id,
        "entities": entities,
        "entities_none": entities_none,
        "errors": errors,
    }


def _node_from_wikilink(raw: str) -> str | None:
    value = raw.strip()
    if value.endswith(".md"):
        value = value[:-3]
    return _normalise_vault_node(value)


def _normalise_vault_node(raw: str) -> str | None:
    value = raw.split("#", 1)[0].split("?", 1)[0].strip()
    if value.endswith(".md"):
        value = value[:-3]
    if not value or value.startswith("/") or "\\" in value:
        return None
    normalized = posixpath.normpath(value)
    if normalized in {".", ".."} or normalized.startswith("../"):
        return None
    return "/".join(_remove_zero_width(unicodedata.normalize("NFC", part)).strip() for part in normalized.split("/"))


def _resolve_markdown_node(raw: str, source_page: str) -> str | None:
    target = raw.strip().strip("<>")
    if "://" in target or target.startswith("#"):
        return None
    combined = posixpath.join(posixpath.dirname(source_page), target)
    return _normalise_vault_node(combined)


def page_links_to(text: str, source_page: str, target_node: str) -> bool:
    """Return whether page text contains a canonical wiki or markdown link."""
    for raw in _WIKILINK_RE.findall(text):
        if _node_from_wikilink(raw) == target_node:
            return True
    for raw in _MARKDOWN_LINK_RE.findall(text):
        if _resolve_markdown_node(raw, source_page) == target_node:
            return True
    return False


def lint_source_bundle_closure(vault: Path, page_paths: Iterable[Path]) -> dict[str, list[dict[str, Any]]]:
    """Verify bundle integrity and two-way source/entity links for bound pages."""
    bundle_report = check_source_bundles(vault)
    invalid_bundles = [
        {"bundle": bundle_id, "errors": report["errors"]}
        for bundle_id, report in bundle_report["bundles"].items()
        if report["status"] == "fail"
    ]
    invalid_bindings: list[dict[str, Any]] = []
    missing_entities: list[dict[str, str]] = []
    missing_source_entity_links: list[dict[str, str]] = []
    missing_entity_source_backlinks: list[dict[str, str]] = []
    missing_bundle_targets: list[dict[str, str]] = []

    for page_path in page_paths:
        relative = page_path.relative_to(vault).as_posix()
        text = page_path.read_text(encoding="utf-8", errors="replace")
        binding = parse_source_page_binding(text)
        if binding is None:
            continue
        if binding["errors"]:
            invalid_bindings.append({"page": relative, "errors": binding["errors"]})
            continue
        bundle_id = binding["bundle_id"]
        assert isinstance(bundle_id, str)
        bundle = bundle_report["bundles"].get(bundle_id)
        if bundle is None:
            missing_bundle_targets.append({"page": relative, "bundle": bundle_id})
        if binding["entities_none"]:
            continue
        entities = binding["entities"]
        assert isinstance(entities, tuple)
        for entity in entities:
            entity_path = vault / f"{entity}.md"
            if not entity_path.is_file():
                missing_entities.append({"page": relative, "entity": entity})
                continue
            if not page_links_to(text, relative, entity):
                missing_source_entity_links.append({"page": relative, "entity": entity})
            entity_text = entity_path.read_text(encoding="utf-8", errors="replace")
            source_node = _normalise_vault_node(relative)
            assert source_node is not None
            if not page_links_to(entity_text, entity_path.relative_to(vault).as_posix(), source_node):
                missing_entity_source_backlinks.append({"page": relative, "entity": entity})
    return {
        "invalid_source_bundles": invalid_bundles,
        "invalid_source_bundle_bindings": invalid_bindings,
        "missing_source_bundle_targets": missing_bundle_targets,
        "missing_source_entities": missing_entities,
        "missing_source_entity_links": missing_source_entity_links,
        "missing_entity_source_backlinks": missing_entity_source_backlinks,
    }
