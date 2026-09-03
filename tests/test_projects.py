from __future__ import annotations

from pathlib import Path

import pytest

from obsidian_wiki import projects


def _write(vault: Path, relative: str, text: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _page(
    *,
    title: str,
    fields: str = "",
    body: str = "",
) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        f"{fields}"
        "---\n"
        f"# {title}\n\n"
        f"{body}"
    )


def _project(vault: Path, project_id: str, *, layout: str = "flat", body: str = "") -> Path:
    relative = (
        f"projects/{project_id}.md"
        if layout == "flat"
        else f"projects/{project_id}/{project_id}.md"
    )
    return _write(vault, relative, _page(title=project_id, body=body))


def test_parse_projects_accepts_inline_and_block_lists() -> None:
    assert projects.parse_projects("projects: [alpha, 'Beta Project', alpha]") == (
        "alpha",
        "Beta Project",
    )
    assert projects.parse_projects("projects:\n  - alpha\n  - \"Beta Project\"\n") == (
        "alpha",
        "Beta Project",
    )
    assert projects.parse_projects("title: no membership") is None


def test_parse_projects_rejects_scalar_and_invalid_identifiers() -> None:
    with pytest.raises(projects.ProjectTimelineError) as error:
        projects.parse_projects("projects: alpha")
    assert error.value.errors[0]["code"] == "invalid_projects"

    with pytest.raises(projects.ProjectTimelineError) as error:
        projects.parse_projects("projects: [../alpha]")
    assert error.value.errors[0]["code"] == "invalid_project_id"


def test_effective_projects_precedence_and_path_fallback() -> None:
    registry = {"alpha": object(), "beta": object()}

    assert projects.effective_projects(
        "projects/alpha/concepts/note.md",
        {"projects": ["beta"], "project": "alpha"},
        registry,
    ) == ("beta",)
    assert projects.effective_projects(
        "projects/alpha/concepts/note.md",
        {"project": "beta"},
        registry,
    ) == ("beta",)
    assert projects.effective_projects(
        "projects/alpha/concepts/note.md",
        {},
        registry,
    ) == ("alpha",)


def test_explicit_empty_projects_suppresses_legacy_and_path_fallback() -> None:
    assert projects.effective_projects(
        "projects/alpha/concepts/note.md",
        {"projects": [], "project": "alpha"},
        {"alpha": object()},
    ) == ()


def test_mentions_relationships_and_tags_do_not_create_membership() -> None:
    frontmatter = """
title: Mention only
tags: [alpha]
relationships:
  - type: related_to
    target: "[[projects/alpha/alpha]]"
"""
    assert projects.effective_projects(
        "concepts/note.md",
        frontmatter,
        {"alpha": object()},
    ) == ()


def test_discover_projects_supports_flat_and_folder_overviews(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _project(vault, "flat")
    _project(vault, "folder", layout="folder")

    found = projects.discover_projects(vault)

    assert found["flat"].relative_path == "projects/flat.md"
    assert found["flat"].layout == "flat"
    assert found["folder"].relative_path == "projects/folder/folder.md"
    assert found["folder"].layout == "folder"


def test_discover_projects_rejects_ambiguous_overviews(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _project(vault, "alpha")
    _project(vault, "alpha", layout="folder")

    with pytest.raises(projects.ProjectTimelineError) as error:
        projects.discover_projects(vault)

    assert error.value.errors == (
        {
            "code": "ambiguous_project_overview",
            "message": (
                "project 'alpha' has multiple overview pages: "
                "projects/alpha.md, projects/alpha/alpha.md"
            ),
            "project": "alpha",
            "paths": ["projects/alpha.md", "projects/alpha/alpha.md"],
        },
    )


def test_collect_uses_timeline_date_then_created_and_never_updated(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    overview = _project(vault, "alpha")
    _write(
        vault,
        "references/explicit.md",
        _page(
            title="Explicit",
            fields=(
                "projects: [alpha]\n"
                "timeline_date: 2026-08-15\n"
                "created: 2020-01-01\n"
                "updated: 2026-09-01\n"
                "timeline_blurb: Explicit date wins.\n"
            ),
        ),
    )
    _write(
        vault,
        "references/created.md",
        _page(
            title="Created",
            fields=(
                "projects: [alpha]\n"
                "created: 2026-07-02\n"
                "updated: 2026-09-01\n"
                "summary: Created date fallback.\n"
            ),
        ),
    )
    _write(
        vault,
        "references/updated-only.md",
        _page(
            title="Updated Only",
            fields="projects: [alpha]\nupdated: 2026-09-01\n",
        ),
    )

    plan = projects.plan_project_timelines(vault)

    assert plan.projects_scanned == 1
    assert plan.entries == 2
    assert [error["code"] for error in plan.errors] == ["missing_timeline_date"]
    assert plan.errors[0]["path"] == "references/updated-only.md"
    assert overview.read_text(encoding="utf-8") == _page(title="alpha")


def test_created_accepts_standard_iso_timestamp_and_uses_its_date(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _project(vault, "alpha")
    _write(
        vault,
        "references/timestamped.md",
        _page(
            title="Timestamped",
            fields=(
                "projects: [alpha]\n"
                "created: 2026-09-01T23:45:00+08:00\n"
                "summary: Timestamp date fallback.\n"
            ),
        ),
    )

    grouped = projects.collect_timeline_entries(vault)

    assert grouped["alpha"][0].date == "2026-09-01"


def test_collect_sanitises_blurb_sorts_and_deduplicates_membership(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _project(vault, "alpha")
    _project(vault, "beta", layout="folder")
    _write(
        vault,
        "references/zeta.md",
        _page(
            title="Zeta",
            fields=(
                "projects: [alpha, beta, alpha]\n"
                "created: 2026-08-05\n"
                "timeline_blurb: >-\n"
                "  Read [the paper](https://example.test) and [[concepts/cache|Cache]].\n"
            ),
        ),
    )
    _write(
        vault,
        "references/alpha.md",
        _page(
            title="Alpha",
            fields=(
                "project: alpha\n"
                "created: 2026-08-05\n"
                "summary: Summary fallback.\n"
            ),
        ),
    )
    _write(
        vault,
        "projects/alpha/concepts/path-note.md",
        _page(
            title="Path note",
            fields="created: 2026-04-01\n",
        ),
    )

    grouped = projects.collect_timeline_entries(vault)

    assert [entry.path for entry in grouped["alpha"]] == [
        "references/alpha.md",
        "references/zeta.md",
        "projects/alpha/concepts/path-note.md",
    ]
    assert grouped["beta"] == [
        projects.TimelineEntry(
            "2026-08-05",
            "references/zeta.md",
            "Zeta",
            "Read the paper and Cache.",
        )
    ]


def test_render_timeline_supports_wikilink_and_markdown_folder_depth(
    tmp_path: Path,
) -> None:
    overview = projects.ProjectOverview(
        "alpha",
        tmp_path / "projects/alpha/alpha.md",
        "projects/alpha/alpha.md",
        "folder",
    )
    entry = projects.TimelineEntry(
        "2026-09-01",
        "references/a note.md",
        "A [note]",
        "A concise update.",
    )

    wikilink = projects.render_timeline(overview, [entry], "wikilink")
    markdown = projects.render_timeline(overview, [entry], "markdown")

    assert "### 2026 Q3" in wikilink
    assert "[[references/a note|A （note）]]" in wikilink
    assert "[A \\[note\\]](../../references/a%20note.md)" in markdown


def test_strip_generated_timeline_is_fail_closed_for_malformed_markers() -> None:
    valid = f"before\n{projects.TIMELINE_BEGIN}\n[[generated]]\n{projects.TIMELINE_END}\nafter\n"
    assert projects.strip_generated_project_timeline(valid) == "before\n\nafter\n"

    malformed = f"before\n{projects.TIMELINE_BEGIN}\n[[generated]]\n"
    assert projects.strip_generated_project_timeline(malformed) == malformed
    with pytest.raises(projects.ProjectTimelineError):
        projects.strip_generated_project_timeline(malformed, strict=True)


@pytest.mark.parametrize(
    "body",
    [
        f"{projects.TIMELINE_BEGIN}\nold\n",
        f"{projects.TIMELINE_END}\n",
        (
            f"{projects.TIMELINE_BEGIN}\n"
            f"{projects.TIMELINE_BEGIN}\nold\n"
            f"{projects.TIMELINE_END}\n"
            f"{projects.TIMELINE_END}\n"
        ),
        (
            f"{projects.TIMELINE_END}\nold\n"
            f"{projects.TIMELINE_BEGIN}\n"
        ),
    ],
)
def test_malformed_markers_make_plan_fail_without_changes(
    tmp_path: Path,
    body: str,
) -> None:
    vault = tmp_path / "vault"
    overview = _project(vault, "alpha", body=body)
    before = overview.read_bytes()

    plan = projects.plan_project_timelines(vault)
    report = projects.write_project_timelines(vault)

    assert plan.changes == ()
    assert plan.errors[0]["code"] == "malformed_project_timeline_markers"
    assert report["status"] == "error"
    assert overview.read_bytes() == before


def test_write_is_idempotent_and_check_reports_drift_without_writing(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    overview = _project(vault, "alpha", body="Manual prose.\n")
    _write(
        vault,
        "references/one.md",
        _page(
            title="One",
            fields=(
                "projects: [alpha]\n"
                "created: 2026-09-01\n"
                "timeline_blurb: First event.\n"
            ),
        ),
    )
    before = overview.read_bytes()

    checked = projects.check_project_timelines(vault)

    assert checked["status"] == "drift"
    assert checked["check"] is True
    assert overview.read_bytes() == before

    written = projects.write_project_timelines(vault)
    first = overview.read_bytes()
    clean = projects.check_project_timelines(vault)
    second = projects.write_project_timelines(vault)

    assert written["status"] == "updated"
    assert written["entries"] == 1
    assert clean["status"] == "clean"
    assert second["status"] == "clean"
    assert overview.read_bytes() == first


def test_write_validates_every_overview_before_any_write(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    first = _project(vault, "alpha")
    second = _project(
        vault,
        "beta",
        body=f"{projects.TIMELINE_BEGIN}\nbroken\n",
    )
    _write(
        vault,
        "references/one.md",
        _page(
            title="One",
            fields="projects: [alpha]\ncreated: 2026-09-01\n",
        ),
    )
    before = {first: first.read_bytes(), second: second.read_bytes()}

    report = projects.write_project_timelines(vault)

    assert report["status"] == "error"
    assert first.read_bytes() == before[first]
    assert second.read_bytes() == before[second]


def test_atomic_write_rolls_back_prior_replacements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    first = _project(vault, "alpha")
    second = _project(vault, "beta")
    before = {first: first.read_bytes(), second: second.read_bytes()}
    real_replace = projects.os.replace
    replacements = 0

    def fail_second_timeline_replace(source: Path, target: Path) -> None:
        nonlocal replacements
        if target in {first, second} and source.name.endswith(".tmp"):
            replacements += 1
            if replacements == 2:
                raise OSError("simulated write failure")
        real_replace(source, target)

    monkeypatch.setattr(projects.os, "replace", fail_second_timeline_replace)

    report = projects.write_project_timelines(vault)

    assert report["status"] == "error"
    assert report["errors"][0]["code"] == "timeline_write_failed"
    assert first.read_bytes() == before[first]
    assert second.read_bytes() == before[second]
    assert list((vault / "projects").glob(".*.tmp")) == []
