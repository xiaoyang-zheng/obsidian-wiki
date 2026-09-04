"""Deterministic, read-only inspection for paper ingestion candidates.

The core API is intentionally independent from the CLI and the vault writer. It
fingerprints a source PDF, derives a canonical paper identity from stable clues,
and asks an optional PyMuPDF-compatible backend for bounded page-level signals.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


ARXIV_RE = re.compile(
    r"(?ix)"
    r"(?:"
    r"arxiv\s*:\s*|"
    r"arxiv(?:\s+id)?\s+|"
    r"arxiv\.org/(?:abs|pdf|html)/|"
    r"doi\.org/10\.48550/arxiv\."
    r")"
    r"("
    r"\d{4}\.\d{4,5}|"
    r"(?:[a-z][a-z.-]*/)?[a-z-]+(?:\.[A-Z]{2})?/\d{7}"
    r")"
    r"(v\d+)?"
)
ARXIV_BARE_RE = re.compile(
    r"(?ix)"
    r"(?<![A-Za-z0-9])"
    r"("
    r"\d{4}\.\d{4,5}|"
    r"(?:[a-z][a-z.-]*/)?[a-z-]+(?:\.[A-Z]{2})?/\d{7}"
    r")"
    r"(v\d+)?"
    r"(?:\.pdf)?"
    r"(?![A-Za-z0-9])"
)
DOI_RE = re.compile(r"(?i)\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)")
CAPTION_RE = re.compile(r"(?i)^\s*(fig(?:ure)?\.?|table)\s*[\w.-]+[\s:.-]")
TABLE_RE = re.compile(r"(?i)^\s*table\s+[\w.-]+[\s:.-]")
FORMULA_RE = re.compile(
    "(?x)"
    "(?:"
    r"[A-Za-z]\s*=\s*[^=]+|"
    r"\\(?:sum|prod|int|frac|alpha|beta|gamma)|"
    "[" + "".join(chr(codepoint) for codepoint in (0x2211, 0x222B, 0x221A, 0x2248, 0x2264, 0x2265, 0x00B1, 0x00D7, 0x00F7)) + "]|"
    r"\([0-9]{1,3}\)\s*$"
    ")"
)

DEFAULT_LIMITS = {
    "max_file_bytes": 100 * 1024 * 1024,
    "max_pages": 250,
    "max_candidates": 2_000,
    "max_text_chars_per_page": 200_000,
    "max_candidate_text_chars": 2_000,
    "max_export_image_bytes": 20 * 1024 * 1024,
    "max_total_export_bytes": 100 * 1024 * 1024,
}
_CANDIDATE_TYPES = ("pages", "images", "captions", "tables", "formulas")
_TYPE_ORDER = {name: index for index, name in enumerate(_CANDIDATE_TYPES)}


class PaperInspectError(RuntimeError):
    """Structured fail-closed error raised by :func:`inspect_paper`."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {"status": "error", "error": {"code": self.code, "message": self.message, "details": self.details}}


def inspect_paper(
    pdf_path: str | os.PathLike[str],
    *,
    source_url: str | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    backend: Any | None = None,
    limits: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Inspect a PDF without modifying the source file or any vault.

    Parameters
    ----------
    pdf_path:
        Source PDF to read.
    source_url:
        Optional original URL used as an identity clue.
    output_dir:
        Optional new directory for ``inspect.json`` and candidate images. The
        directory must not already exist.
    backend:
        Optional PyMuPDF-compatible module/object. When omitted, ``fitz`` is
        imported lazily and a structured ``PaperInspectError`` is raised if it is
        unavailable.
    limits:
        Resource ceilings. Missing keys inherit ``DEFAULT_LIMITS``.
    """

    effective_limits = _validate_limits(limits)
    source = _resolve_source_path(Path(pdf_path), effective_limits)
    output_root = _validate_output_dir(output_dir) if output_dir is not None else None
    source_sha256 = _sha256_file(source)
    module = backend if backend is not None else _load_pymupdf_backend()

    image_exports: list[dict[str, Any]] = []
    document = _open_document(module, source)
    try:
        metadata = _metadata(document)
        page_count = _page_count(document)
        if page_count > effective_limits["max_pages"]:
            raise PaperInspectError(
                "page_limit_exceeded",
                "PDF page count exceeds the configured limit",
                details={"pages": page_count, "limit": effective_limits["max_pages"]},
            )

        identity = _paper_identity(
            sha256=source_sha256,
            source_url=source_url,
            filename=source.name,
            metadata=metadata,
        )
        candidates = _extract_candidates(
            document,
            identity=identity,
            limits=effective_limits,
            image_exports=image_exports,
        )
    finally:
        _close_document(document)

    report: dict[str, Any] = {
        "status": "ok",
        "source": {
            "filename": source.name,
            "size_bytes": source.stat().st_size,
            "sha256": source_sha256,
            "source_url": source_url,
        },
        "identity": identity,
        "metadata": metadata,
        "limits": dict(sorted(effective_limits.items())),
        "backend": _backend_name(module),
        "page_count": page_count,
        "candidate_counts": {name: len(candidates[name]) for name in _CANDIDATE_TYPES},
        "candidate_budget_exhausted": (
            sum(len(candidates[name]) for name in _CANDIDATE_TYPES)
            >= effective_limits["max_candidates"]
        ),
        "candidates": candidates,
    }

    if output_root is not None:
        report["exports"] = _write_outputs(report, output_root, image_exports, effective_limits)
    return report


def _validate_limits(overrides: Mapping[str, int] | None) -> dict[str, int]:
    values = dict(DEFAULT_LIMITS)
    if overrides:
        unknown = sorted(set(overrides) - set(DEFAULT_LIMITS))
        if unknown:
            raise PaperInspectError(
                "invalid_limits",
                "unknown paper inspection limit",
                details={"keys": unknown},
            )
        invalid_types = {
            key: value
            for key, value in overrides.items()
            if isinstance(value, bool) or type(value) is not int
        }
        if invalid_types:
            raise PaperInspectError(
                "invalid_limits",
                "paper inspection limits must be positive integers",
                details=invalid_types,
            )
        values.update(overrides)
    invalid = {key: value for key, value in values.items() if value < 1}
    if invalid:
        raise PaperInspectError(
            "invalid_limits",
            "paper inspection limits must be positive integers",
            details=invalid,
        )
    return values


def _resolve_source_path(path: Path, limits: Mapping[str, int]) -> Path:
    try:
        source = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise PaperInspectError(
            "source_not_found",
            "source PDF was not found",
            details={"path": str(path)},
        ) from exc
    if not source.is_file():
        raise PaperInspectError(
            "source_not_file",
            "source path must be a regular file",
            details={"path": str(source)},
        )
    if source.stat().st_size > limits["max_file_bytes"]:
        raise PaperInspectError(
            "file_limit_exceeded",
            "source PDF exceeds the configured byte limit",
            details={"size_bytes": source.stat().st_size, "limit": limits["max_file_bytes"]},
        )
    return source


def _validate_output_dir(raw: str | os.PathLike[str]) -> Path:
    output = Path(raw).expanduser()
    if output.name in {"", ".", ".."} or any(part == ".." for part in output.parts):
        raise PaperInspectError(
            "unsafe_output_dir",
            "output directory must not contain parent-directory traversal",
            details={"path": str(output)},
        )
    if output.exists():
        raise PaperInspectError(
            "output_dir_exists",
            "output directory already exists; refusing to overwrite",
            details={"path": str(output)},
        )
    parent = output.parent if output.parent != Path("") else Path(".")
    try:
        parent_resolved = parent.resolve(strict=True)
    except OSError as exc:
        raise PaperInspectError(
            "output_parent_missing",
            "output directory parent does not exist",
            details={"path": str(parent)},
        ) from exc
    if not parent_resolved.is_dir():
        raise PaperInspectError(
            "output_parent_not_dir",
            "output directory parent must be a directory",
            details={"path": str(parent_resolved)},
        )
    return parent_resolved / output.name


def _load_pymupdf_backend() -> Any:
    errors: list[str] = []
    for module_name in ("pymupdf", "fitz"):
        try:
            return importlib.import_module(module_name)
        except ImportError as exc:
            errors.append(f"{module_name}: {exc}")
    raise PaperInspectError(
        "py_mupdf_unavailable",
        "PyMuPDF backend is not installed; install obsidian-wiki[paper] or pass a compatible backend",
        details={"modules": ["pymupdf", "fitz"], "extra": "paper", "errors": errors},
    )


def _open_document(module: Any, source: Path) -> Any:
    opener = getattr(module, "open", None)
    if opener is None:
        raise PaperInspectError(
            "invalid_backend",
            "paper inspection backend must provide open(path)",
            details={"backend": _backend_name(module)},
        )
    try:
        return opener(str(source))
    except Exception as exc:  # pragma: no cover - backend-specific failure text.
        raise PaperInspectError(
            "backend_open_failed",
            "backend failed to open the source PDF",
            details={"backend": _backend_name(module), "reason": str(exc)},
        ) from exc

def _close_document(document: Any) -> None:
    close = getattr(document, "close", None)
    if callable(close):
        close()


def _backend_name(module: Any) -> str:
    return str(getattr(module, "__name__", module.__class__.__name__))


def _page_count(document: Any) -> int:
    count = getattr(document, "page_count", None)
    if count is None:
        try:
            count = len(document)
        except TypeError as exc:
            raise PaperInspectError(
                "invalid_backend",
                "backend document must expose page_count or __len__",
            ) from exc
    try:
        return int(count)
    except (TypeError, ValueError) as exc:
        raise PaperInspectError("invalid_backend", "backend document page_count is not an integer") from exc


def _load_page(document: Any, index: int) -> Any:
    loader = getattr(document, "load_page", None)
    if callable(loader):
        return loader(index)
    return document[index]


def _metadata(document: Any) -> dict[str, str]:
    raw = getattr(document, "metadata", {}) or {}
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): str(value).strip()
        for key, value in sorted(raw.items(), key=lambda item: str(item[0]))
        if value is not None and str(value).strip()
    }


def _paper_identity(
    *,
    sha256: str,
    source_url: str | None,
    filename: str,
    metadata: Mapping[str, str],
) -> dict[str, Any]:
    clues: list[tuple[str, str]] = []
    if source_url:
        clues.append(("source_url", source_url))
    clues.append(("filename", filename))
    for key in sorted(metadata):
        if key.lower() in {"title", "subject", "keywords", "doi", "identifier", "source"}:
            clues.append((f"metadata.{key}", metadata[key]))

    arxiv_clues: list[dict[str, str | None]] = []
    doi_clues: list[dict[str, str]] = []
    for source, text in clues:
        found = _find_arxiv(text, allow_bare=(source == "filename"))
        if found:
            arxiv_clues.append(
                {"id": found[0], "version": found[1], "source": source}
            )
        found_doi = _find_doi(text)
        if found_doi:
            doi_clues.append({"id": found_doi, "source": source})

    arxiv_clues = _dedupe_clues(arxiv_clues)
    doi_clues = _dedupe_clues(doi_clues)
    arxiv_ids = sorted({str(clue["id"]) for clue in arxiv_clues})
    doi_ids = sorted({clue["id"] for clue in doi_clues})
    conflicts: list[dict[str, Any]] = []
    if len(arxiv_ids) > 1:
        conflicts.append({"type": "arxiv_id_conflict", "values": arxiv_ids})
    if len(doi_ids) > 1:
        conflicts.append({"type": "doi_conflict", "values": doi_ids})

    evidence = [{"source": source, "text": text} for source, text in clues if text]
    result: dict[str, Any] = {
        "work_id": sha256,
        "edition_id": sha256,
        "kind": "sha256",
        "sha256": sha256,
        "version": None,
        "arxiv_id": None,
        "doi": doi_ids[0] if len(doi_ids) == 1 else None,
        "ambiguous": bool(conflicts),
        "conflicts": conflicts,
        "identity_clues": {"arxiv": arxiv_clues, "doi": doi_clues},
        "evidence": evidence,
    }
    if not arxiv_ids and len(doi_ids) == 1:
        result.update(
            {
                "work_id": f"doi:{doi_ids[0]}",
                "edition_id": f"doi:{doi_ids[0]}",
                "kind": "doi",
                "identity_source": doi_clues[0]["source"],
            }
        )
    if len(arxiv_ids) == 1:
        arxiv_id = arxiv_ids[0]
        matching = [clue for clue in arxiv_clues if clue["id"] == arxiv_id]
        versions = sorted(
            {str(clue["version"]) for clue in matching if clue["version"]}
        )
        version = versions[0] if len(versions) == 1 else None
        if len(versions) > 1:
            conflict = {
                "type": "arxiv_version_conflict",
                "work_id": f"arxiv:{arxiv_id}",
                "values": versions,
            }
            conflicts.append(conflict)
            result["ambiguous"] = True
            result["conflicts"] = conflicts
        source = next(
            (clue["source"] for clue in matching if clue["version"] == version),
            matching[0]["source"],
        )
        result.update(
            {
                "work_id": f"arxiv:{arxiv_id}",
                "edition_id": (
                    f"arxiv:{arxiv_id}{version or ''}"
                    if len(versions) <= 1
                    else sha256
                ),
                "kind": "arxiv",
                "arxiv_id": arxiv_id,
                "version": version,
                "identity_source": source,
            }
        )
    return result


def _dedupe_clues(clues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[tuple[str, Any], ...]] = set()
    result: list[dict[str, Any]] = []
    for clue in clues:
        key = tuple(sorted(clue.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(clue)
    return result


def _find_arxiv(text: str, *, allow_bare: bool = False) -> tuple[str, str | None] | None:
    for match in ARXIV_RE.finditer(text):
        raw_id = match.group(1).rstrip(".")
        version = match.group(2).lower() if match.group(2) else None
        if not raw_id:
            continue
        if "/" in raw_id:
            raw_id = raw_id.lower()
        return raw_id, version
    if allow_bare:
        for match in ARXIV_BARE_RE.finditer(text):
            raw_id = match.group(1).rstrip(".")
            version = match.group(2).lower() if match.group(2) else None
            if "/" in raw_id:
                raw_id = raw_id.lower()
            return raw_id, version
    return None


def _find_doi(text: str) -> str | None:
    match = DOI_RE.search(text)
    if not match:
        return None
    doi = match.group(1).rstrip(".,;:)].").lower()
    if doi.startswith("10.48550/arxiv."):
        return None
    return doi


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _extract_candidates(
    document: Any,
    *,
    identity: Mapping[str, Any],
    limits: Mapping[str, int],
    image_exports: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    candidates = {name: [] for name in _CANDIDATE_TYPES}
    count = 0
    exported_image_bytes = 0
    for page_index in range(_page_count(document)):
        if count >= limits["max_candidates"]:
            break
        page_number = page_index + 1
        page = _load_page(document, page_index)
        text_dict = _page_text_dict(page)
        lines = _text_lines(text_dict)
        page_text = _page_text(page, lines)
        if len(page_text) > limits["max_text_chars_per_page"]:
            raise PaperInspectError(
                "page_text_limit_exceeded",
                "page text exceeds the configured character limit",
                details={"page": page_number, "chars": len(page_text), "limit": limits["max_text_chars_per_page"]},
            )

        if page_text:
            _check_candidate_limit(count + 1, limits)
            candidates["pages"].append(
                _candidate(
                    "page",
                    page=page_number,
                    locator=f"page:{page_number}",
                    text=_clip_text(page_text, limits["max_candidate_text_chars"]),
                    identity=identity,
                )
            )
            count += 1

        if count < limits["max_candidates"]:
            for image in _image_blocks(
                document,
                page,
                text_dict,
                page_number,
                limits,
                max_items=limits["max_candidates"] - count,
                remaining_export_bytes=(
                    limits["max_total_export_bytes"] - exported_image_bytes
                ),
            ):
                _check_candidate_limit(count + 1, limits)
                candidate = _candidate("image", identity=identity, **image["candidate"])
                candidates["images"].append(candidate)
                if image.get("bytes"):
                    exported_image_bytes += len(image["bytes"])
                    image_exports.append(
                        {
                            "candidate_identity": candidate["identity"],
                            "candidate_hash": candidate["hash"],
                            "ext": image["ext"],
                            "bytes": image["bytes"],
                        }
                    )
                count += 1

        for line in lines:
            text = line["text"].strip()
            if CAPTION_RE.search(text):
                if count >= limits["max_candidates"]:
                    break
                candidates["captions"].append(
                    _candidate(
                        "caption",
                        page=page_number,
                        bbox=line.get("bbox"),
                        locator=line["locator"],
                        text=_clip_text(text, limits["max_candidate_text_chars"]),
                        caption=_clip_text(text, limits["max_candidate_text_chars"]),
                        identity=identity,
                    )
                )
                count += 1
            if TABLE_RE.search(text):
                if count >= limits["max_candidates"]:
                    break
                candidates["tables"].append(
                    _candidate(
                        "table",
                        page=page_number,
                        bbox=line.get("bbox"),
                        locator=line["locator"],
                        text=_clip_text(text, limits["max_candidate_text_chars"]),
                        caption=_clip_text(text, limits["max_candidate_text_chars"]),
                        identity=identity,
                    )
                )
                count += 1
            if _looks_like_formula(text):
                if count >= limits["max_candidates"]:
                    break
                candidates["formulas"].append(
                    _candidate(
                        "formula",
                        page=page_number,
                        bbox=line.get("bbox"),
                        locator=line["locator"],
                        text=_clip_text(text, limits["max_candidate_text_chars"]),
                        identity=identity,
                    )
                )
                count += 1
        if count < limits["max_candidates"]:
            for table in _backend_tables(
                page,
                page_number,
                limits,
                max_items=limits["max_candidates"] - count,
            ):
                _check_candidate_limit(count + 1, limits)
                candidates["tables"].append(
                    _candidate("table", identity=identity, **table)
                )
                count += 1

    for name in _CANDIDATE_TYPES:
        candidates[name].sort(key=_candidate_sort_key)
    return candidates


def _check_candidate_limit(count: int, limits: Mapping[str, int]) -> None:
    if count > limits["max_candidates"]:
        raise PaperInspectError(
            "candidate_limit_exceeded",
            "candidate count exceeds the configured limit",
            details={"candidates": count, "limit": limits["max_candidates"]},
        )


def _page_text_dict(page: Any) -> dict[str, Any]:
    getter = getattr(page, "get_text", None)
    if not callable(getter):
        return {}
    try:
        raw = getter("dict")
    except TypeError:
        raw = None
    except Exception:
        raw = None
    return raw if isinstance(raw, dict) else {}


def _page_text(page: Any, lines: list[dict[str, Any]]) -> str:
    getter = getattr(page, "get_text", None)
    if callable(getter):
        try:
            raw = getter("text")
        except TypeError:
            raw = getter()
        except Exception:
            raw = None
        if isinstance(raw, str) and raw.strip():
            return _normalise_space(raw)
    return _normalise_space("\n".join(str(line["text"]) for line in lines))


def _text_lines(text_dict: Mapping[str, Any]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for block_index, block in enumerate(text_dict.get("blocks", []) or []):
        if not isinstance(block, Mapping) or block.get("type", 0) != 0:
            continue
        for line_index, line in enumerate(block.get("lines", []) or []):
            if not isinstance(line, Mapping):
                continue
            spans = []
            for span in line.get("spans", []) or []:
                if isinstance(span, Mapping) and span.get("text"):
                    spans.append(str(span["text"]))
            text = _normalise_space("".join(spans))
            if not text:
                continue
            lines.append(
                {
                    "text": text,
                    "bbox": _bbox(line.get("bbox") or block.get("bbox")),
                    "locator": f"page:{{page}}:block:{block_index:04d}:line:{line_index:04d}",
                    "block_index": block_index,
                    "line_index": line_index,
                }
            )
    lines.sort(key=lambda item: (_bbox_sort_key(item.get("bbox")), item["block_index"], item["line_index"], item["text"]))
    for line in lines:
        line["locator"] = line["locator"].replace("page:{page}", "page:{page_number}")
    return lines


def _image_blocks(
    document: Any,
    page: Any,
    text_dict: Mapping[str, Any],
    page_number: int,
    limits: Mapping[str, int],
    *,
    max_items: int,
    remaining_export_bytes: int,
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    blocks = text_dict.get("blocks", []) or []
    image_index = 0
    for block_index, block in enumerate(blocks):
        if not isinstance(block, Mapping) or block.get("type") != 1:
            continue
        if len(images) >= max_items:
            _check_candidate_limit(limits["max_candidates"] + 1, limits)
        image_bytes = block.get("image") if isinstance(block.get("image"), bytes) else None
        ext = _safe_ext(str(block.get("ext") or "bin"))
        image_hash = _image_hash(image_bytes, block)
        bounded_bytes = _bounded_image_bytes(image_bytes, page_number, limits)
        remaining_export_bytes = _consume_export_budget(
            bounded_bytes, page_number, remaining_export_bytes, limits
        )
        images.append(
            {
                "candidate": {
                    "page": page_number,
                    "bbox": _bbox(block.get("bbox")),
                    "locator": f"page:{page_number}:image:{image_index:04d}",
                    "text": "",
                    "hash_hint": image_hash,
                    "image": {
                        "width": _optional_int(block.get("width")),
                        "height": _optional_int(block.get("height")),
                        "xref": _optional_int(block.get("xref")),
                    },
                },
                "bytes": bounded_bytes,
                "ext": ext,
                "block_index": block_index,
            }
        )
        image_index += 1

    if images:
        return images

    getter = getattr(page, "get_image_info", None)
    if callable(getter):
        try:
            infos = getter(xrefs=True)
        except TypeError:
            infos = getter()
        except Exception:
            infos = []
        for info in infos or []:
            if not isinstance(info, Mapping):
                continue
            if len(images) >= max_items:
                _check_candidate_limit(limits["max_candidates"] + 1, limits)
            image_bytes, ext = _extract_image_bytes(document, info, page_number, limits)
            remaining_export_bytes = _consume_export_budget(
                image_bytes, page_number, remaining_export_bytes, limits
            )
            images.append(
                {
                    "candidate": {
                        "page": page_number,
                        "bbox": _bbox(info.get("bbox")),
                        "locator": f"page:{page_number}:image:{image_index:04d}",
                        "text": "",
                        "hash_hint": _image_hash(image_bytes, info),
                        "image": {
                            "width": _optional_int(info.get("width")),
                            "height": _optional_int(info.get("height")),
                            "xref": _optional_int(info.get("xref")),
                        },
                    },
                    "bytes": image_bytes,
                    "ext": ext,
                    "block_index": image_index,
                }
            )
            image_index += 1
    images.sort(key=lambda item: (_bbox_sort_key(item["candidate"].get("bbox")), item["candidate"]["locator"]))
    return images


def _extract_image_bytes(
    document: Any,
    info: Mapping[str, Any],
    page_number: int,
    limits: Mapping[str, int],
) -> tuple[bytes | None, str]:
    xref = _optional_int(info.get("xref"))
    extractor = getattr(document, "extract_image", None)
    if not xref or not callable(extractor):
        return None, "bin"
    try:
        extracted = extractor(xref)
    except Exception:
        return None, "bin"
    if not isinstance(extracted, Mapping):
        return None, "bin"
    image = extracted.get("image")
    ext = _safe_ext(str(extracted.get("ext") or "bin"))
    if isinstance(image, bytes):
        return _bounded_image_bytes(image, page_number, limits), ext
    return None, ext


def _bounded_image_bytes(image: bytes | None, page_number: int, limits: Mapping[str, int]) -> bytes | None:
    if image is None:
        return None
    if len(image) > limits["max_export_image_bytes"]:
        raise PaperInspectError(
            "image_limit_exceeded",
            "candidate image exceeds the configured export byte limit",
            details={"page": page_number, "size_bytes": len(image), "limit": limits["max_export_image_bytes"]},
        )
    return image


def _consume_export_budget(
    image: bytes | None,
    page_number: int,
    remaining: int,
    limits: Mapping[str, int],
) -> int:
    if image is None:
        return remaining
    if len(image) > remaining:
        raise PaperInspectError(
            "total_image_limit_exceeded",
            "candidate images exceed the configured total export byte limit",
            details={
                "page": page_number,
                "size_bytes": len(image),
                "remaining_bytes": remaining,
                "limit": limits["max_total_export_bytes"],
            },
        )
    return remaining - len(image)


def _image_hash(image: bytes | None, fallback: Mapping[str, Any]) -> str:
    if image is not None:
        return "sha256:" + hashlib.sha256(image).hexdigest()
    stable = json.dumps(_jsonable(fallback), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _backend_tables(
    page: Any,
    page_number: int,
    limits: Mapping[str, int],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    finder = getattr(page, "find_tables", None)
    if not callable(finder):
        return []
    try:
        raw = finder()
    except Exception:
        return []
    tables = getattr(raw, "tables", raw)
    results: list[dict[str, Any]] = []
    for index, table in enumerate(tables or []):
        if len(results) >= max_items:
            _check_candidate_limit(limits["max_candidates"] + 1, limits)
        bbox = _bbox(getattr(table, "bbox", None) or (table.get("bbox") if isinstance(table, Mapping) else None))
        text = _table_text(table)
        results.append(
            {
                "page": page_number,
                "bbox": bbox,
                "locator": f"page:{page_number}:table:{index:04d}",
                "text": _clip_text(text, limits["max_candidate_text_chars"]),
            }
        )
    results.sort(key=lambda item: (_bbox_sort_key(item.get("bbox")), item["locator"]))
    return results


def _table_text(table: Any) -> str:
    if isinstance(table, Mapping):
        for key in ("text", "caption"):
            if table.get(key):
                return _normalise_space(str(table[key]))
    markdown = getattr(table, "to_markdown", None)
    if callable(markdown):
        try:
            return _normalise_space(str(markdown()))
        except Exception:
            return ""
    extractor = getattr(table, "extract", None)
    if callable(extractor):
        try:
            return _normalise_space(json.dumps(extractor(), sort_keys=True))
        except Exception:
            return ""
    return ""


def _candidate(
    candidate_type: str,
    *,
    page: int,
    locator: str,
    identity: Mapping[str, Any],
    bbox: list[float] | None = None,
    text: str = "",
    caption: str | None = None,
    hash_hint: str | None = None,
    image: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    locator = locator.replace("{page}", str(page)).replace(
        "{page_number}", str(page)
    )
    item: dict[str, Any] = {
        "type": candidate_type,
        "page": page,
        "bbox": bbox,
        "locator": locator,
        "text": text,
    }
    if caption is not None:
        item["caption"] = caption
    if image is not None:
        item["image"] = {key: value for key, value in sorted(image.items()) if value is not None}
    material = dict(item)
    if hash_hint:
        material["hash_hint"] = hash_hint
    candidate_hash = "sha256:" + hashlib.sha256(
        json.dumps(_jsonable(material), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    item["hash"] = hash_hint or candidate_hash
    item["identity"] = (
        f"{identity['edition_id']}#{candidate_type}:{page}:"
        f"{candidate_hash.split(':', 1)[1][:16]}"
    )
    return item


def _candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(candidate.get("page", 0)),
        _TYPE_ORDER.get(str(candidate.get("type")) + "s", 99),
        _bbox_sort_key(candidate.get("bbox")),
        str(candidate.get("locator", "")),
        str(candidate.get("hash", "")),
    )


def _looks_like_formula(text: str) -> bool:
    compact = text.strip()
    if len(compact) < 3:
        return False
    if CAPTION_RE.search(compact):
        return False
    return bool(FORMULA_RE.search(compact))


def _bbox(raw: Any) -> list[float] | None:
    if raw is None:
        return None
    try:
        values = [float(value) for value in raw]
    except (TypeError, ValueError):
        return None
    if len(values) != 4:
        return None
    return [round(value, 3) for value in values]


def _bbox_sort_key(bbox: Any) -> tuple[float, float, float, float]:
    values = _bbox(bbox)
    if values is None:
        return (float("inf"), float("inf"), float("inf"), float("inf"))
    return tuple(values)  # type: ignore[return-value]


def _normalise_space(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def _clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _optional_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _safe_ext(ext: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", ext.lower())[:12]
    return cleaned or "bin"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return "sha256:" + hashlib.sha256(value).hexdigest()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _write_outputs(
    report: Mapping[str, Any],
    output_dir: Path,
    image_exports: list[dict[str, Any]],
    limits: Mapping[str, int],
) -> dict[str, Any]:
    written: list[Path] = []
    try:
        output_dir.mkdir()
        candidate_dir = output_dir / "candidates"
        candidate_dir.mkdir()
        exported_images: list[dict[str, str]] = []
        by_identity = {
            candidate["identity"]: candidate
            for bucket in report["candidates"].values()
            for candidate in bucket
            if isinstance(candidate, Mapping)
        }
        for export in sorted(image_exports, key=lambda item: (item["candidate_identity"], item["candidate_hash"])):
            image_bytes = export["bytes"]
            if len(image_bytes) > limits["max_export_image_bytes"]:
                raise PaperInspectError(
                    "image_limit_exceeded",
                    "candidate image exceeds the configured export byte limit",
                    details={"size_bytes": len(image_bytes), "limit": limits["max_export_image_bytes"]},
                )
            filename = f"{export['candidate_hash'].split(':', 1)[1][:24]}.{export['ext']}"
            target = candidate_dir / filename
            _assert_contained(output_dir, target)
            if target.exists() and target.read_bytes() != image_bytes:
                raise PaperInspectError(
                    "export_collision",
                    "two candidate images resolved to the same output filename",
                    details={"path": str(target)},
                )
            if not target.exists():
                try:
                    with target.open("xb") as handle:
                        handle.write(image_bytes)
                except BaseException:
                    target.unlink(missing_ok=True)
                    raise
                written.append(target)
            relpath = target.relative_to(output_dir).as_posix()
            exported_images.append(
                {
                    "candidate_identity": export["candidate_identity"],
                    "path": relpath,
                    "sha256": "sha256:" + hashlib.sha256(image_bytes).hexdigest(),
                }
            )
            candidate = by_identity.get(export["candidate_identity"])
            if candidate is not None:
                candidate["artifact"] = relpath

        inspect_path = output_dir / "inspect.json"
        _assert_contained(output_dir, inspect_path)
        json_report = dict(report)
        json_report["exports"] = {
            "json": "inspect.json",
            "images": exported_images,
        }
        try:
            with inspect_path.open("x", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        json_report, indent=2, sort_keys=True, ensure_ascii=False
                    )
                    + "\n"
                )
        except BaseException:
            inspect_path.unlink(missing_ok=True)
            raise
        written.append(inspect_path)
        return {"directory": str(output_dir), "json": "inspect.json", "images": exported_images}
    except FileExistsError as exc:
        raise PaperInspectError(
            "output_dir_exists",
            "output directory already exists; refusing to overwrite",
            details={"path": str(output_dir)},
        ) from exc
    except BaseException:
        for path in reversed(written):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for directory in (output_dir / "candidates", output_dir):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise


def _assert_contained(root: Path, path: Path) -> None:
    root_resolved = root.resolve(strict=True)
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise PaperInspectError(
            "path_escape",
            "paper inspection output escaped its output directory",
            details={"root": str(root_resolved), "path": str(candidate)},
        ) from exc
