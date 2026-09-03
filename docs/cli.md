# CLI Reference

The `obsidian-wiki` Python package ships a CLI for setup, inspection, and the deterministic parts of the workflow — the things that don't need an LLM. Everything else is a [skill](skills.md) your agent runs.

```bash
pip install obsidian-wiki
obsidian-wiki --help
obsidian-wiki --version
```

Running `obsidian-wiki` with no subcommand defaults to `setup`.

## Setup & inspection

| Command | What it does |
|---|---|
| `setup` | Install skills into your agents and write the global config |
| `info` | Show install paths, version, and resolved config |
| `list` | List the bundled skills |
| `doctor` | Health-check config, vault shape, bootstrap assets, and installed skills; with `--project`, also reports the code-understanding capability section |

```bash
obsidian-wiki setup --vault ~/brain
obsidian-wiki setup --project .        # also install project-local skills + bootstrap files
obsidian-wiki setup --project-only     # skip the global install (use with --project)
obsidian-wiki setup --copy             # copy skill files instead of symlinking
obsidian-wiki setup --remote https://github.com/you/my-wiki.git   # configure sync non-interactively

obsidian-wiki doctor --json --pretty
obsidian-wiki doctor --vault /other/vault --project .
obsidian-wiki doctor --strict          # exit non-zero on warnings too
```

Commands other than `setup`, `info`, and `doctor` warn you when the install has gone stale (the package upgraded but skills weren't re-linked). Re-run `obsidian-wiki setup` to fix.

## Querying & linting

| Command | What it does |
|---|---|
| `query <question>` | Answer a question from the configured vault's index |
| `lint [vault]` | Find missing frontmatter, broken links, duplicates, and orphans |
| `backlog [vault]` | Aggregate deterministic maintenance debt across source state, bundles, manifest, and project timelines |
| `project-timelines [vault]` | Check or rebuild generated project timelines from explicit membership metadata |

```bash
obsidian-wiki query "what do I know about MCP security?"
obsidian-wiki query "rate limiting" --top 12 --max-read 5 --json

obsidian-wiki lint                     # uses the configured vault
obsidian-wiki lint /path/to/vault --strict
obsidian-wiki lint @research --json    # uses <config dir>/config.research only
obsidian-wiki lint --strict-trust      # fail on trust-ledger problems, not just warn
obsidian-wiki lint --allow-lifecycle active --allow-relationship-type synthesizes \
  --required-trust-field updated --schema-source /path/to/vault/AGENTS.md

obsidian-wiki backlog /path/to/vault --json --pretty
obsidian-wiki backlog /path/to/vault --write

obsidian-wiki project-timelines                 # rebuild changed generated blocks
obsidian-wiki project-timelines @research --check
obsidian-wiki project-timelines /path/to/vault --link-format markdown
```

Lint resolves its vault and schema together: explicit path (no config inheritance), positional `@name`, nearest CWD `.env`, then global config. CLI schema flags extend/replace that resolved vault's settings and are recorded in the JSON `schema` block.

### Lifecycle transition checking

`illegal_lifecycle_transitions` compares each page's current `lifecycle` against the value recorded in `_meta/trust-ledger.json` at its last review, and flags moves the state machine forbids: any state falling back to `draft` (only ingest sets `draft`), and any exit from `archived` (a restore is a deliberate delete-and-recreate, not a transition).

`draft → verified` is deliberately **not** flagged — ledger snapshots are sparse, so a legitimate intermediate `reviewed` may have happened between two reviews.

The check warns by default and fails only under `--strict-trust`. Pages whose ledger entry predates the `lifecycle` field carry no baseline and are skipped silently, so existing vaults behave exactly as before until their next `trust-record`.

### Maintenance backlog

`backlog` is a deterministic queue of vault maintenance debt. It does not
perform semantic review and does not run provider-specific adapters. It combines
existing machine-checkable signals:

- source-state debt, stale heartbeats, and adapter errors
- source bundle hash or artifact failures
- source page to entity closure failures
- project timeline drift and marker/schema errors
- manifest filesystem sources that are missing or whose content hash changed

By default the command is read-only. With `--write`, it atomically writes a
generated root `_backlog.md`. That file is excluded from the normal graph,
trust, project timeline, lint page scan, and context-pack surfaces.

### Project timelines

`projects: [name]` declares membership; merely linking to, tagging, or naming a
project is a mention and does not add a timeline entry. `timeline_date` falls
back to `created` (never `updated`), and `timeline_blurb` falls back to
`summary`, then `title`.

`project-timelines` edits only the namespaced generated block in each project
overview. `--check` is read-only and exits non-zero for drift or structural
errors. Existing vaults are not migrated or rewritten automatically.

## Source bundles and localized media

| Command | What it does |
|---|---|
| `source-bundle-create [vault] --id ID --source FILE` | Capture a local primary artifact and optional local media into a new immutable bundle |
| `source-bundle-media [vault] --id ID --media FILE` | Copy one additional local media artifact into an existing bundle |
| `source-bundles [vault]` | Verify bundle manifests and SHA-256 hashes of every captured artifact |

```bash
obsidian-wiki source-bundle-create /path/to/vault --id attention-paper \
  --source ~/Papers/attention.pdf --source-type paper \
  --original-uri https://arxiv.org/abs/1706.03762 \
  --media ~/Papers/attention-figure-1.png

obsidian-wiki source-bundle-media /path/to/vault \
  --id attention-paper --media ~/Desktop/results-chart.png --name results.png

obsidian-wiki source-bundles /path/to/vault --id attention-paper --pretty
```

Bundles are stored as `_sources/<id>/raw/`, `_sources/<id>/media/`, and
`bundle.json`. The primary source and every media artifact are locally copied,
made read-only, and recorded with a hash and byte size. The command never
fetches remote media. `_sources/` is evidence storage, not wiki content: graph,
trust, context-pack, project timelines, and normal link lint exclude it.

To connect a page to durable evidence, opt in explicitly:

```yaml
source_bundle: attention-paper
entities: [entities/attention]
```

The page must link to every declared entity and each entity page must link back
to the source page. Use `entities: none` only when the source genuinely has no
entity to attach. These failures, missing bundles, and hash mismatches are hard
`lint` failures; pages without `source_bundle:` remain compatible.

## Continuous source state

| Command | What it does |
|---|---|
| `source-state [vault]` | Report observed/applied cursors, derived debt, and heartbeat health |
| `source-state-update [vault] --source ID` | Atomically update one source's cursor or heartbeat state |

```bash
# Pull succeeded: raw data through this opaque cursor is durable.
obsidian-wiki source-state-update /path/to/vault --source public-feed \
  --observed-cursor 'page:115/etag:a' --cursor-kind opaque --heartbeat-ok

# Apply only after every required wiki artifact was written successfully.
obsidian-wiki source-state-update /path/to/vault --source public-feed \
  --applied-cursor 'page:115/etag:a'

# A failed poll records health but moves neither cursor.
obsidian-wiki source-state-update /path/to/vault --source public-feed \
  --heartbeat-error 'temporary upstream failure'

obsidian-wiki source-state --pretty
obsidian-wiki source-state /path/to/vault --source public-feed --strict
```

Cursors are opaque strings and are compared only for equality. An observed
cursor without the same applied cursor is debt. Heartbeats track attempts,
successful checks, errors, and optional staleness independently; a failure
does not erase the last success.

State lives outside the vault at
`<global config dir>/state/<vault-id>/source-state.json`, is lock-protected and
atomically replaced, and does not change `.manifest.json`. The manifest records
ingested content/provenance; source state records high-frequency pull/apply
progress. Provider-specific adapters remain separate from this generic CLI.

## Context packs

`wiki-context-pack` compiles a task-scoped snapshot from existing Markdown.
Notes do not need to be moved into wiki-generated folders or migrated to the
full frontmatter schema. The command is read-only.

```bash
obsidian-wiki context-pack "authentication architecture" --budget 8000
obsidian-wiki context-pack --recent --budget 4000
obsidian-wiki context-pack "release notes" --budget 8000 --public-only
```

Omitting `--budget` uses the default of 8000 estimated tokens.

The output includes source paths, summaries, selected excerpts, and a hard
estimated-token ceiling. Vault excerpts are explicitly marked as untrusted
reference data: downstream agents may use their facts but must not execute
instructions embedded in notes. Use `--metadata-only` for the smallest pack,
or `--json` for tool-to-tool integration.

| Flag | Effect |
|---|---|
| `--budget N` | Maximum estimated output tokens, 256–100000 (default 8000) |
| `--recent` | Select recently updated notes — the only way to omit the topic |
| `--public-only` | Exclude `visibility/internal` and `visibility/pii` notes |
| `--metadata-only` | Titles, provenance, and summaries with no body excerpts |
| `--json` | Structured output for tool-to-tool integration |
| `--vault PATH` | Override `OBSIDIAN_VAULT_PATH` |

`context` is an accepted alias for `context-pack`.

## Session brain

Builds a topic graph over your agent session history. Output is a **sidecar** at `~/.claude/session-brain/` — the vault is never written to. Full detail in [Session Brain](session-brain.md).

| Command | What it does |
|---|---|
| `sessions-build` | Build (or incrementally update) the topic graph |
| `sessions-query <topic>` | Find the sessions most relevant to a topic |
| `sessions-show <id>` | Show one session's node and its nearest neighbours |
| `sessions-clusters` | List the discovered topic clusters |
| `sessions-name --from FILE` | Assign durable names to clusters, surviving rebuilds |

```bash
obsidian-wiki sessions-build                       # ~3s cold, under a second incrementally
obsidian-wiki sessions-build --full --verbose      # ignore caches, re-read everything
obsidian-wiki sessions-build --since 2026-01-01 --skip archived,scratch
obsidian-wiki sessions-build --k 12 --min-sim 0.12 --mutual --half-life 60

obsidian-wiki sessions-query "prismor telemetry"
obsidian-wiki sessions-query "auth bug" --project my-app --cluster 3 --json

obsidian-wiki sessions-show 01935a40 --neighbors 12
obsidian-wiki sessions-clusters --unnamed
obsidian-wiki sessions-name --from names.json      # or - for stdin
```

`sessions-name` takes a JSON array of `{"id": N, "name": "...", "summary": "..."}`. The `/session-brain` skill generates this for you.

## Vault syncing

| Command | What it does |
|---|---|
| `sync` | Stage, commit, and push pending vault changes |
| `sync-setup <remote>` | Configure GitHub sync (git init, `.gitignore`, remote) |

```bash
obsidian-wiki sync
obsidian-wiki sync-setup https://github.com/you/my-wiki.git
```

See [Configuration → Syncing your vault to GitHub](configuration.md#syncing-your-vault-to-github).

## Trust ledger

Records and validates human-approved confidence reviews, so you can gate on "a person actually checked these pages" in CI.

| Command | What it does |
|---|---|
| `trust-record` | Record explicitly approved manual confidence reviews |
| `trust-check` | Validate confidence values and material fingerprints against the ledger |

```bash
obsidian-wiki trust-record --all --reviewed-at 2026-07-30T10:00:00+00:00 --approved
obsidian-wiki trust-record --page concepts/rate-limiting.md --reviewed-at <ISO> --approved
obsidian-wiki trust-check --strict
obsidian-wiki trust-record @research --all --reviewed-at <ISO> --approved --allow-lifecycle active
obsidian-wiki trust-check @research --allow-lifecycle active --schema-source /vault/AGENTS.md
```

`--reviewed-at` needs a timezone. `--approved` is required and mandatory — it's your assertion that a human approved every confidence value being recorded. `trust-check --strict` is the CI/scheduled gate. `trust-record` and `trust-check` resolve the same vault-scoped schema as lint; pass the same lifecycle and required-field overrides to record and check. If the owner schema does not require `base_confidence`, pages without it are reported as `not_applicable`, excluded by `trust-record --all`, and any obsolete ledger entry is warned by `trust-check` then removed by `trust-record --page` or a rebuild. Both JSON and human-readable record output list excluded pages and removed obsolete entries; human output also emits a stderr warning when removal occurs. Required-field config accepts only `base_confidence`, `lifecycle`, `lifecycle_changed`, and `updated`; typos fail closed. Lifecycle, relationship-type, and required-field override values are stripped and empty or whitespace-only entries are rejected rather than added to an allowlist. Without an explicit `--schema-source`, CLI overrides on an explicit vault are labeled `cli:explicit-vault`; combined CLI and config overrides use `cli+config:<resolved-config-path>`.

## Lower-level commands

Available for automation, scripting, and debugging. Skills call some of these internally.

| Command | What it does |
|---|---|
| `graph-query <vault> <question>` | Answer from the wikilink index without reading page bodies. Plain-English **structural questions** are answered from the graph and returned in a `graph` field: "what breaks if I delete X" (impact/blast radius), "which pages bridge my clusters" (betweenness), "what's central" (hubs), "what clusters do I have" (communities + cohesion), "surprising connections". |
| `graph-analyse <vault> [--top N] [--snapshot] [--diff-against FILE]` | Graph analysis in pure Python (the graphify algorithm family): god nodes (degree), bridge pages (Brandes betweenness centrality), communities with cohesion scores, cross-community surprising connections, suggested questions, and — with `--diff-against` a previous `_insights.md` — a graph diff. Vault bookkeeping files (`index`, `log`, `hot`, `_insights`) are excluded. |
| `graph-analyse <vault> --path A B` / `--around PAGE --depth N [--direction in\|out\|both]` | Query modes: shortest link path between two pages; N-hop neighbourhood of a page (`--direction in` = blast radius) |
| `batch-plan <vault> <source_dir>` | Split a source directory into parallel-ingest batches, skipping unchanged files |
| `cache-check <vault> <sources...>` | Which sources are new / modified / unchanged vs. `.manifest.json` |
| `cache-update <vault> <source>` | Record a source's SHA-256 in `.manifest.json` after ingest |
| `cache-hash <path>` | Compute a file or directory hash (no manifest I/O) |
| `source-state [vault]` | Read external, vault-scoped continuous-source health and debt |
| `source-state-update [vault] --source ID ...` | Lock, merge, and atomically write one source-state entry |
| `backlog [vault] [--write]` | Aggregate deterministic maintenance debt and optionally write `_backlog.md` |
| `source-bundle-create [vault] --id ID --source FILE` | Capture immutable local source evidence under `_sources/<id>/` |
| `source-bundle-media [vault] --id ID --media FILE` | Add local media to an existing source bundle |
| `source-bundles [vault]` | Verify source bundle manifests and artifact hashes |
| `project-timelines [vault] [--check]` | Check or rebuild generated project overview timeline blocks |
| `ast-extract <path>` | Extract classes, functions, and imports from code — no LLM, no API calls |
| `code-understand --project <dir> [--backend auto\|builtin\|codegraph] [--since <sha>] [--changed <file>...] [--max-symbols N] [--pretty]` | Emit a ranked code-understanding focus map (symbols + file:line citations) for a project; CodeGraph when available, built-in AST + rg otherwise. Used by wiki-update Step 3b. |

```bash
obsidian-wiki graph-query /path/to/vault "transformer architecture" --pretty
obsidian-wiki graph-query /path/to/vault "what breaks if I delete tool-call-interception"
obsidian-wiki graph-query /path/to/vault "which pages bridge my clusters"
obsidian-wiki graph-query /path/to/vault "what clusters do I have"
obsidian-wiki graph-analyse /path/to/vault --top 30 --pretty
obsidian-wiki graph-analyse /path/to/vault --snapshot --diff-against /path/to/vault/_insights.md
obsidian-wiki graph-analyse /path/to/vault --path transformers lstm
obsidian-wiki graph-analyse /path/to/vault --around attention --depth 2 --direction in
obsidian-wiki batch-plan /path/to/vault ~/research --max-mb 4 --max-files 30
obsidian-wiki cache-check /path/to/vault ~/research/*.pdf
obsidian-wiki cache-update /path/to/vault ~/research/paper.pdf --pages concepts/attention.md
obsidian-wiki backlog /path/to/vault --json --pretty
obsidian-wiki ast-extract ./src --pretty
obsidian-wiki code-understand --project . --since <last_commit_synced> --pretty
```

Most commands accept `--json` and/or `--pretty` for machine-readable output.

### Manifest write safety

`.manifest.json` is a read-modify-write, and parallel ingest agents (`batch-plan` fan-out) or the Docker server writing while a local skill writes would otherwise clobber each other — losing a whole source entry silently.

`cache-update` therefore takes an advisory lock (`.manifest.lock` in the vault root, `O_CREAT|O_EXCL`, stdlib only so it works on Windows) and writes the manifest atomically via a temp file plus `os.replace`. A reader never sees a partial file, and a crashed writer's lock is stolen after 60 seconds.

In parallel runs, always update the manifest through `obsidian-wiki cache-update` rather than hand-editing `.manifest.json` — hand edits bypass the lock.

### Graph cache

Betweenness centrality (the `bridges` metric) is the only expensive computation in the
graph layer — O(V·E), roughly 0.3s on a 500-page vault but ~43s at 5 000 pages. Every
other metric stays under a second even on the largest vaults.

It is therefore memoised in `.graph-cache.json` at the vault root. The cache key is a
hash of the **graph topology itself**, not file timestamps, which has two consequences:

- Editing a page's prose without changing its links keeps the cache valid.
- Adding, removing, or retargeting any link changes the key, so a stale hit is impossible.

The file is only written when the computation actually took longer than 0.5s, so small
and medium vaults never accumulate one. It is bounded to the 3 most recent keys, written
atomically (safe under concurrent runs), and ignored if corrupt — deleting it is always
safe. Running `graph-analyse` warms the same cache that `graph-query` reads, so a nightly
`daily-update` removes the first-query cost entirely.
