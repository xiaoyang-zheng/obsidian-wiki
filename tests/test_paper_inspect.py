"""Focused tests for deterministic paper inspection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from obsidian_wiki import paper_inspect
from obsidian_wiki.paper_inspect import PaperInspectError, inspect_paper


class FakeTable:
    def __init__(self, bbox: tuple[float, float, float, float], text: str) -> None:
        self.bbox = bbox
        self._text = text

    def to_markdown(self) -> str:
        return self._text


class FakeTableResult:
    def __init__(self, tables: list[FakeTable]) -> None:
        self.tables = tables


class FakePage:
    def __init__(
        self,
        blocks: list[dict[str, Any]],
        *,
        text: str | None = None,
        tables: list[FakeTable] | None = None,
        image_info: list[dict[str, Any]] | None = None,
    ) -> None:
        self.blocks = blocks
        self.text = text
        self.tables = tables or []
        self.image_info = image_info or []

    def get_text(self, kind: str = "text") -> Any:
        if kind == "dict":
            return {"blocks": self.blocks}
        if self.text is not None:
            return self.text
        lines: list[str] = []
        for block in self.blocks:
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                lines.append("".join(span["text"] for span in line.get("spans", [])))
        return "\n".join(lines)

    def find_tables(self) -> FakeTableResult:
        return FakeTableResult(self.tables)

    def get_image_info(self, xrefs: bool = False) -> list[dict[str, Any]]:
        return self.image_info


class FakeDocument:
    def __init__(
        self,
        pages: list[FakePage],
        *,
        metadata: dict[str, str] | None = None,
        images: dict[int, tuple[bytes, str]] | None = None,
    ) -> None:
        self.pages = pages
        self.page_count = len(pages)
        self.metadata = metadata or {}
        self.images = images or {}
        self.closed = False

    def load_page(self, index: int) -> FakePage:
        return self.pages[index]

    def extract_image(self, xref: int) -> dict[str, Any]:
        image, ext = self.images[xref]
        return {"image": image, "ext": ext}

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    __name__ = "fake_pymupdf"

    def __init__(
        self,
        pages: list[FakePage],
        *,
        metadata: dict[str, str] | None = None,
        images: dict[int, tuple[bytes, str]] | None = None,
    ) -> None:
        self.pages = pages
        self.metadata = metadata or {}
        self.images = images or {}
        self.opened_paths: list[str] = []
        self.documents: list[FakeDocument] = []

    def open(self, path: str) -> FakeDocument:
        self.opened_paths.append(path)
        document = FakeDocument(self.pages, metadata=self.metadata, images=self.images)
        self.documents.append(document)
        return document


def _pdf(tmp_path: Path, name: str = "paper.pdf", body: bytes = b"%PDF-1.4\nbody\n") -> Path:
    path = tmp_path / name
    path.write_bytes(body)
    return path


def _line(bbox: tuple[float, float, float, float], text: str) -> dict[str, Any]:
    return {"bbox": bbox, "spans": [{"text": text}]}


def _text_block(*lines: dict[str, Any]) -> dict[str, Any]:
    return {"type": 0, "lines": list(lines)}


def _image_block(
    bbox: tuple[float, float, float, float],
    image: bytes,
    *,
    ext: str = "png",
    xref: int = 7,
) -> dict[str, Any]:
    return {
        "type": 1,
        "bbox": bbox,
        "image": image,
        "ext": ext,
        "width": 12,
        "height": 8,
        "xref": xref,
    }


def _sample_pages() -> list[FakePage]:
    return [
        FakePage(
            [
                _text_block(
                    _line((0, 120, 100, 130), "y = x + 1"),
                    _line((0, 60, 200, 70), "Table 1: Results"),
                    _line((0, 10, 200, 20), "Figure 1: Model overview"),
                ),
                _image_block((220, 30, 300, 90), b"image-one"),
            ],
            tables=[FakeTable((0, 180, 200, 220), "| metric | value |")],
        ),
        FakePage(
            [
                _text_block(
                    _line((0, 40, 200, 50), "Figure 2: Later caption"),
                    _line((0, 10, 200, 20), "plain paragraph"),
                )
            ]
        ),
    ]


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_arxiv_identity_versions_share_work_and_split_editions(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path)
    backend = FakeBackend([FakePage([])])

    first = inspect_paper(
        pdf,
        source_url="https://arxiv.org/pdf/2301.12345v1",
        backend=backend,
    )
    second = inspect_paper(
        pdf,
        source_url="https://arxiv.org/abs/2301.12345v2",
        backend=backend,
    )

    assert first["identity"]["kind"] == "arxiv"
    assert first["identity"]["work_id"] == "arxiv:2301.12345"
    assert second["identity"]["work_id"] == first["identity"]["work_id"]
    assert first["identity"]["edition_id"] == "arxiv:2301.12345v1"
    assert second["identity"]["edition_id"] == "arxiv:2301.12345v2"
    assert first["identity"]["version"] == "v1"
    assert second["identity"]["version"] == "v2"


def test_arxiv_url_wins_over_doi_metadata(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path)
    backend = FakeBackend(
        [FakePage([])],
        metadata={"doi": "10.1145/9999999.0000001", "title": "A paper"},
    )

    result = inspect_paper(
        pdf,
        source_url="https://doi.org/10.48550/arXiv.2401.01234v3",
        backend=backend,
    )

    assert result["identity"]["kind"] == "arxiv"
    assert result["identity"]["work_id"] == "arxiv:2401.01234"
    assert result["identity"]["edition_id"] == "arxiv:2401.01234v3"
    assert result["identity"]["doi"] == "10.1145/9999999.0000001"
    assert result["identity"]["ambiguous"] is False


def test_pymupdf_module_name_is_preferred_over_legacy_fitz(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = _pdf(tmp_path)
    preferred = FakeBackend([FakePage([])])
    imported: list[str] = []

    def fake_import(name: str) -> Any:
        imported.append(name)
        if name == "pymupdf":
            return preferred
        raise AssertionError("legacy fitz should not be imported after success")

    monkeypatch.setattr(paper_inspect.importlib, "import_module", fake_import)

    result = inspect_paper(pdf)

    assert result["backend"] == "fake_pymupdf"
    assert imported == ["pymupdf"]


def test_identity_enriches_unversioned_arxiv_and_flags_version_conflicts(
    tmp_path: Path,
) -> None:
    unversioned = _pdf(tmp_path, "2401.01234v2.pdf")
    conflict = _pdf(tmp_path, "2401.01234v2-copy.pdf")
    backend = FakeBackend([FakePage([])])

    enriched = inspect_paper(
        unversioned,
        source_url="https://arxiv.org/abs/2401.01234",
        backend=backend,
    )
    conflicting = inspect_paper(
        conflict,
        source_url="https://arxiv.org/abs/2401.01234v1",
        backend=backend,
    )

    assert enriched["identity"]["edition_id"] == "arxiv:2401.01234v2"
    assert enriched["identity"]["ambiguous"] is False
    assert conflicting["identity"]["work_id"] == "arxiv:2401.01234"
    assert conflicting["identity"]["edition_id"] == _sha256(conflict)
    assert conflicting["identity"]["version"] is None
    assert conflicting["identity"]["ambiguous"] is True
    assert conflicting["identity"]["conflicts"] == [
        {
            "type": "arxiv_version_conflict",
            "work_id": "arxiv:2401.01234",
            "values": ["v1", "v2"],
        }
    ]


def test_conflicting_work_identity_falls_back_to_content_hash(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path, "2401.01234v1.pdf")
    backend = FakeBackend(
        [FakePage([])], metadata={"identifier": "arXiv:2502.54321v3"}
    )

    result = inspect_paper(
        pdf,
        source_url="https://arxiv.org/abs/2301.11111v2",
        backend=backend,
    )

    identity = result["identity"]
    assert identity["kind"] == "sha256"
    assert identity["work_id"] == _sha256(pdf)
    assert identity["edition_id"] == _sha256(pdf)
    assert identity["ambiguous"] is True
    assert identity["conflicts"][0]["type"] == "arxiv_id_conflict"


def test_doi_identity_is_not_misclassified_as_arxiv_suffix(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path)
    backend = FakeBackend([FakePage([])])

    result = inspect_paper(
        pdf,
        source_url="https://doi.org/10.5555/2401.12345",
        backend=backend,
    )

    assert result["identity"]["kind"] == "doi"
    assert result["identity"]["work_id"] == "doi:10.5555/2401.12345"
    assert result["identity"]["edition_id"] == "doi:10.5555/2401.12345"
    assert result["identity"]["arxiv_id"] is None


def test_filename_arxiv_clue_and_fallback_sha256(tmp_path: Path) -> None:
    arxiv_pdf = _pdf(tmp_path, "2302.00001v4.pdf")
    fallback_pdf = _pdf(tmp_path, "local-paper.pdf", b"%PDF-1.4\nfallback\n")
    backend = FakeBackend([FakePage([])])

    arxiv = inspect_paper(arxiv_pdf, backend=backend)
    fallback = inspect_paper(
        fallback_pdf,
        backend=backend,
        source_url="https://example.test/papers/local-paper",
    )

    assert arxiv["identity"]["kind"] == "arxiv"
    assert arxiv["identity"]["work_id"] == "arxiv:2302.00001"
    assert arxiv["identity"]["edition_id"] == "arxiv:2302.00001v4"
    assert fallback["identity"]["kind"] == "sha256"
    assert fallback["identity"]["work_id"] == _sha256(fallback_pdf)
    assert fallback["identity"]["edition_id"] == _sha256(fallback_pdf)


def test_candidates_have_stable_structure_and_deterministic_sorting(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path)
    backend = FakeBackend(_sample_pages())

    first = inspect_paper(
        pdf,
        source_url="https://arxiv.org/abs/2301.12345v2",
        backend=backend,
    )
    second = inspect_paper(
        pdf,
        source_url="https://arxiv.org/abs/2301.12345v2",
        backend=backend,
    )

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["candidate_counts"] == {
        "pages": 2,
        "images": 1,
        "captions": 3,
        "tables": 2,
        "formulas": 1,
    }
    assert first["candidate_budget_exhausted"] is False
    assert [item["text"] for item in first["candidates"]["captions"]] == [
        "Figure 1: Model overview",
        "Table 1: Results",
        "Figure 2: Later caption",
    ]
    assert [item["text"] for item in first["candidates"]["formulas"]] == ["y = x + 1"]
    assert [item["text"] for item in first["candidates"]["tables"]] == [
        "Table 1: Results",
        "| metric | value |",
    ]

    image = first["candidates"]["images"][0]
    assert image["page"] == 1
    assert image["bbox"] == [220.0, 30.0, 300.0, 90.0]
    assert image["hash"] == "sha256:" + hashlib.sha256(b"image-one").hexdigest()
    assert image["image"] == {"height": 8, "width": 12, "xref": 7}
    assert [item["locator"] for item in first["candidates"]["captions"]] == [
        "page:1:block:0000:line:0002",
        "page:1:block:0000:line:0001",
        "page:2:block:0000:line:0000",
    ]
    assert all("{" not in item["locator"] for bucket in first["candidates"].values() for item in bucket)
    for bucket in first["candidates"].values():
        for candidate in bucket:
            assert set(candidate) >= {"type", "page", "bbox", "locator", "text", "hash", "identity"}
            assert candidate["hash"].startswith("sha256:")
            assert candidate["identity"].startswith("arxiv:2301.12345v2#")


def test_repeated_image_bytes_keep_unique_candidate_identities(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path)
    repeated = b"same-image"
    backend = FakeBackend(
        [
            FakePage(
                [
                    _image_block((0, 10, 100, 50), repeated, xref=7),
                    _image_block((0, 60, 100, 100), repeated, xref=8),
                ]
            )
        ]
    )

    result = inspect_paper(pdf, backend=backend)
    images = result["candidates"]["images"]

    assert images[0]["hash"] == images[1]["hash"]
    assert images[0]["identity"] != images[1]["identity"]


def test_candidate_limit_truncates_before_image_discovery(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path)

    class CountingDocument(FakeDocument):
        def __init__(self) -> None:
            super().__init__(
                [FakePage([], image_info=[{"xref": 1}, {"xref": 2}])],
                images={1: (b"one", "png"), 2: (b"two", "png")},
            )
            self.extract_calls = 0

        def extract_image(self, xref: int) -> dict[str, Any]:
            self.extract_calls += 1
            return super().extract_image(xref)

    document = CountingDocument()
    document.pages[0].get_image_info_calls = 0
    original_get_image_info = document.pages[0].get_image_info

    def counted_get_image_info(xrefs: bool = False) -> list[dict[str, Any]]:
        document.pages[0].get_image_info_calls += 1
        return original_get_image_info(xrefs=xrefs)

    document.pages[0].get_image_info = counted_get_image_info  # type: ignore[method-assign]

    class CountingBackend:
        __name__ = "counting"

        def open(self, path: str) -> CountingDocument:
            return document

    with pytest.raises(PaperInspectError) as raised:
        inspect_paper(
            pdf,
            backend=CountingBackend(),
            limits={"max_candidates": 1},
        )

    assert raised.value.code == "candidate_limit_exceeded"
    assert document.extract_calls == 1
    assert document.pages[0].get_image_info_calls == 1

    text_page = FakePage([], text="full page", image_info=[{"xref": 1}])
    text_page.get_image_info_calls = 0
    original_info = text_page.get_image_info

    def counted_info(xrefs: bool = False) -> list[dict[str, Any]]:
        text_page.get_image_info_calls += 1
        return original_info(xrefs=xrefs)

    text_page.get_image_info = counted_info  # type: ignore[method-assign]
    full_budget = inspect_paper(
        pdf,
        backend=FakeBackend([text_page], images={1: (b"one", "png")}),
        limits={"max_candidates": 1},
    )
    assert full_budget["candidate_counts"]["pages"] == 1
    assert full_budget["candidate_counts"]["images"] == 0
    assert full_budget["candidate_budget_exhausted"] is True
    assert text_page.get_image_info_calls == 0


def test_page_candidates_stop_at_budget_and_return_bounded_report(
    tmp_path: Path,
) -> None:
    pdf = _pdf(tmp_path)
    backend = FakeBackend(
        [FakePage([], text="page one"), FakePage([], text="page two")]
    )

    result = inspect_paper(
        pdf,
        backend=backend,
        limits={"max_candidates": 1},
    )

    assert result["candidate_counts"] == {
        "pages": 1,
        "images": 0,
        "captions": 0,
        "tables": 0,
        "formulas": 0,
    }
    assert result["candidates"]["pages"][0]["text"] == "page one"
    assert result["candidate_budget_exhausted"] is True


def test_total_export_byte_limit_is_enforced_across_images(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path)
    backend = FakeBackend(
        [
            FakePage(
                [
                    _image_block((0, 10, 100, 50), b"123456", xref=7),
                    _image_block((0, 60, 100, 100), b"abcdef", xref=8),
                ]
            )
        ]
    )

    with pytest.raises(PaperInspectError) as raised:
        inspect_paper(
            pdf,
            backend=backend,
            limits={"max_total_export_bytes": 10},
        )

    assert raised.value.code == "total_image_limit_exceeded"


def test_optional_backend_missing_reports_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = _pdf(tmp_path)

    def missing_fitz(name: str) -> Any:
        assert name in {"pymupdf", "fitz"}
        raise ImportError("no fitz here")

    monkeypatch.setattr(paper_inspect.importlib, "import_module", missing_fitz)

    with pytest.raises(PaperInspectError) as raised:
        inspect_paper(pdf)

    assert raised.value.code == "py_mupdf_unavailable"
    details = raised.value.as_dict()["error"]["details"]
    assert details["modules"] == ["pymupdf", "fitz"]
    assert details["extra"] == "paper"


@pytest.mark.parametrize("invalid", [True, 1.5, 0, -1])
def test_limits_require_positive_integers(tmp_path: Path, invalid: object) -> None:
    pdf = _pdf(tmp_path)

    with pytest.raises(PaperInspectError) as raised:
        inspect_paper(
            pdf,
            backend=FakeBackend([FakePage([])]),
            limits={"max_pages": invalid},  # type: ignore[dict-item]
        )

    assert raised.value.code == "invalid_limits"


def test_output_export_is_create_only_and_source_is_not_modified(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path, body=b"%PDF-1.4\nsource\n")
    original_bytes = pdf.read_bytes()
    original_children = {path.name for path in tmp_path.iterdir()}
    backend = FakeBackend(_sample_pages())

    no_output = inspect_paper(pdf, backend=backend)
    assert pdf.read_bytes() == original_bytes
    assert {path.name for path in tmp_path.iterdir()} == original_children
    assert "exports" not in no_output

    output_dir = tmp_path / "paper-inspect-output"
    result = inspect_paper(pdf, backend=backend, output_dir=output_dir)

    assert pdf.read_bytes() == original_bytes
    assert result["exports"]["directory"] == str(output_dir.resolve())
    assert (output_dir / "inspect.json").is_file()
    exported = result["exports"]["images"][0]
    assert (output_dir / exported["path"]).read_bytes() == b"image-one"
    assert json.loads((output_dir / "inspect.json").read_text(encoding="utf-8"))["source"]["sha256"] == _sha256(pdf)

    existing = tmp_path / "existing-output"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(PaperInspectError) as raised:
        inspect_paper(pdf, backend=backend, output_dir=existing)
    assert raised.value.code == "output_dir_exists"
    assert marker.read_text(encoding="utf-8") == "keep"


def test_path_containment_and_resource_limits_fail_closed(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path, body=b"%PDF-1.4\nresource limits\n")

    with pytest.raises(PaperInspectError) as file_limit:
        inspect_paper(
            pdf,
            backend=FakeBackend([FakePage([])]),
            limits={"max_file_bytes": len(pdf.read_bytes()) - 1},
        )
    assert file_limit.value.code == "file_limit_exceeded"

    page_backend = FakeBackend([FakePage([]), FakePage([])])
    with pytest.raises(PaperInspectError) as page_limit:
        inspect_paper(pdf, backend=page_backend, limits={"max_pages": 1})
    assert page_limit.value.code == "page_limit_exceeded"
    assert page_backend.documents[-1].closed is True

    with pytest.raises(PaperInspectError) as unsafe:
        inspect_paper(
            pdf,
            backend=FakeBackend([FakePage([])]),
            output_dir=tmp_path / "safe" / ".." / "escape",
        )
    assert unsafe.value.code == "unsafe_output_dir"
