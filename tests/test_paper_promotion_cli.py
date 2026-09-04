"""Subprocess-level contracts for paper inspection and candidate promotion."""

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
    extra_pythonpath: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    home.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment.pop("XDG_CONFIG_HOME", None)
    paths = [str(REPO_ROOT)]
    if extra_pythonpath is not None:
        paths.insert(0, str(extra_pythonpath))
    if environment.get("PYTHONPATH"):
        paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    return subprocess.run(
        [sys.executable, "-m", "obsidian_wiki.cli", *args],
        cwd=home,
        env=environment,
        capture_output=True,
        text=True,
    )


def _fake_pymupdf(path: Path) -> Path:
    path.mkdir()
    (path / "pymupdf.py").write_text(
        """
class Page:
    def get_text(self, kind='text'):
        if kind == 'dict':
            return {'blocks': [{'type': 0, 'lines': [
                {'bbox': (0, 10, 100, 20), 'spans': [{'text': 'Figure 1: Architecture'}]},
                {'bbox': (0, 30, 100, 40), 'spans': [{'text': 'y = x + 1'}]},
            ]}]}
        return 'Figure 1: Architecture\\ny = x + 1'

    def find_tables(self):
        return []

    def get_image_info(self, xrefs=True):
        return []


class Document:
    page_count = 1
    metadata = {}

    def load_page(self, index):
        return Page()

    def close(self):
        pass


def open(path):
    return Document()
""".lstrip(),
        encoding="utf-8",
    )
    return path


def test_new_commands_have_stable_help_contracts(tmp_path: Path) -> None:
    for command in (
        "paper-inspect",
        "promotion-candidates",
        "promotion-observe",
        "promotion-resolve",
    ):
        proc = _run(tmp_path / "home", command, "--help")
        assert proc.returncode == 0
        assert "usage:" in proc.stdout
        assert "Traceback" not in proc.stderr


def test_paper_inspect_cli_emits_identity_and_create_only_output(tmp_path: Path) -> None:
    home = tmp_path / "home"
    backend = _fake_pymupdf(tmp_path / "fake-backend")
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n")
    original = source.read_bytes()
    output = tmp_path / "inspect-output"

    proc = _run(
        home,
        "paper-inspect",
        str(source),
        "--source-url",
        "https://arxiv.org/abs/2401.01234v2",
        "--output",
        str(output),
        "--pretty",
        extra_pythonpath=backend,
    )

    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["identity"]["work_id"] == "arxiv:2401.01234"
    assert report["identity"]["edition_id"] == "arxiv:2401.01234v2"
    assert report["candidate_counts"]["captions"] == 1
    assert report["candidate_counts"]["formulas"] == 1
    assert (output / "inspect.json").is_file()
    assert source.read_bytes() == original

    second = _run(
        home,
        "paper-inspect",
        str(source),
        "--output",
        str(output),
        extra_pythonpath=backend,
    )
    assert second.returncode == 1
    assert json.loads(second.stdout)["error"]["code"] == "output_dir_exists"
    assert source.read_bytes() == original


def test_promotion_cli_observe_list_and_resolve_round_trip(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    vault.mkdir()

    first = _run(
        home,
        "promotion-observe",
        str(vault),
        "--kind",
        "concept",
        "--title",
        "Sparse Attention",
        "--source-lineage",
        "arxiv:2401.00001",
        "--evidence-path",
        "references/paper-one.md",
        "--confidence",
        "0.75",
    )
    second = _run(
        home,
        "promotion-observe",
        str(vault),
        "--kind",
        "concept",
        "--title",
        "Sparse Attention",
        "--source-lineage",
        "doi:10.5555/paper-two",
        "--evidence-path",
        "references/paper-two.md",
        "--confidence",
        "0.72",
    )

    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["status"] == "candidate"
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["status"] == "eligible"

    listed = _run(
        home,
        "promotion-candidates",
        str(vault),
        "--state",
        "eligible",
        "--pretty",
    )
    listed_report = json.loads(listed.stdout)
    assert listed.returncode == 0
    assert [item["candidate_id"] for item in listed_report["candidates"]] == [
        "concept:sparse-attention"
    ]
    assert listed_report["candidates"][0]["promotion_plan"]["target_path"] == (
        "concepts/sparse-attention.md"
    )

    canonical = vault / "concepts" / "sparse-attention.md"
    canonical.parent.mkdir()
    canonical.write_text("# Sparse Attention\n", encoding="utf-8")
    resolved = _run(
        home,
        "promotion-resolve",
        str(vault),
        "--kind",
        "concept",
        "--slug",
        "sparse-attention",
        "--resolution",
        "promoted",
        "--canonical-path",
        "concepts/sparse-attention.md",
        "--resolved-by",
        "wiki-ingest",
    )

    assert resolved.returncode == 0, resolved.stderr
    resolved_report = json.loads(resolved.stdout)
    assert resolved_report["status"] == "promoted"
    assert resolved_report["promotion_plan"] is None
    assert canonical.read_text(encoding="utf-8") == "# Sparse Attention\n"


def test_promotion_cli_fails_cleanly_for_invalid_contract(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    proc = _run(
        tmp_path / "home",
        "promotion-candidates",
        str(vault),
        "--slug",
        "missing-kind",
    )

    assert proc.returncode == 1
    assert "--kind is required" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_rejected_resolution_rejects_canonical_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    vault.mkdir()
    observed = _run(
        home,
        "promotion-observe",
        str(vault),
        "--kind",
        "concept",
        "--title",
        "Rejected Path",
        "--source-lineage",
        "paper:a",
        "--evidence-path",
        "references/a.md",
        "--confidence",
        "0.5",
    )
    assert observed.returncode == 0

    rejected = _run(
        home,
        "promotion-resolve",
        str(vault),
        "--kind",
        "concept",
        "--slug",
        "rejected-path",
        "--resolution",
        "rejected",
        "--canonical-path",
        "concepts/rejected-path.md",
    )

    assert rejected.returncode == 1
    assert "only valid for a promoted resolution" in rejected.stderr
    assert "Traceback" not in rejected.stderr
