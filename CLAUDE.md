# Red Hat AI / OpenShift knowledge wiki — schema

This file tells the agent how to build and maintain the wiki. It is the contract.
Read it fully before any ingest, query, or lint operation.

## Layers

- `raw/` — immutable. Markdown extracted from docs.redhat.com and community sites by
  `rhoai-docsync`, one file per guide, with YAML frontmatter recording product,
  version, source URL and fetch date. Never edit. Never delete on a version bump —
  new versions land beside old ones.
- `wiki/` — the agent owns this entirely. Markdown pages, `[[wiki-link]]` cross-refs.
- `index.md`, `log.md` — catalog and chronological record. See below.

## Page types

Every page has YAML frontmatter:

```yaml
type: component | procedure | error | version | concept | recipe
products: [rhoai, ocp, rhcl]          # which products this touches
versions: [rhoai-3.5, ocp-4.22]        # version-qualified where behaviour differs
sources: [raw/rhoai/3.5/serving_models.md#L120-160]
verified: 2026-08-24                   # last time a human or a cluster confirmed it
confidence: documented | tested | inferred
```

**component** — one thing that exists in a cluster: KServe, the GPU Operator, vLLM,
Kuadrant's RateLimitPolicy, an InferenceService. Body covers: what it is, what it
depends on (`[[links]]`), what depends on it, the CRs involved, the namespace it
lives in, and the knobs that matter in practice.

**procedure** — an ordered, runnable sequence: install X, expose Y through an AI
Gateway, enable GPU time-slicing. Body: preconditions, numbered steps with the actual
`oc`/`kubectl` commands and YAML, verification command with expected output, rollback.
Every step cites the raw page it came from.

**error** — a symptom, keyed by what you actually see: a log line, an event message, a
CR status condition, a pod state. Body: verbatim symptom, which components can produce
it (`[[links]]`), diagnostic commands in order, known causes with fixes, and how to
tell the causes apart. This page type is the one that pays for the whole wiki.

**version** — what changed between two versions of one product, and what it breaks.

**concept** — the explanation a customer needs before the procedure makes sense.

**recipe** — a demo or enablement asset you have actually run: goal, audience, cluster
prerequisites, the procedure pages it composes, the manifests, timing, what tends to go
wrong on stage.

## Rules

1. **Cite or delete.** Every factual claim carries a source: a `raw/` path, or an
   observed cluster result marked `confidence: tested`. A claim with no source does not
   go in the wiki. If the docs do not say it, say that they do not.
2. **Never quote a moving value.** Image tags, operator channels, resource sizings and
   default replica counts belong to a version-qualified page or a frontmatter field —
   never inline in prose on a version-agnostic page.
3. **Version-qualify aggressively.** "RHOAI does X" is almost always wrong. Write "as
   of 3.5" and link the `version` page. When a new version contradicts an old claim,
   do not overwrite: keep both, mark which version each applies to, and note the change
   on the version page.
4. **Separate documented from tested.** Something the docs claim and something you saw
   work on a customer cluster are different epistemic objects. `confidence:` says which.
   Tested beats documented when they disagree — record the disagreement, do not silently
   pick a side.
5. **One page per thing.** Before creating a page, search the index for an existing one
   under a different name. Prefer updating.
6. **Links are the product.** A page with no outbound links is almost certainly wrong or
   too shallow. Components link to their dependencies; procedures link to components and
   to the errors they commonly produce; errors link back to components.

## Operations

**Ingest.** Given a new or updated `raw/` file: read it; identify the components,
procedures and errors it describes; check the index for existing pages; update those and
create only what is genuinely missing; add cross-references in both directions; append to
`log.md`. Report what changed and what you chose not to change. Ingest one guide at a
time — parallel ingests create duplicate pages under near-identical names.

**Query.** Read `index.md` first, then the relevant pages, then `raw/` only if the wiki
is insufficient (and if it was insufficient, that is a lint finding — record it).
Answers that took real work — a comparison, a procedure assembled from four guides — get
filed back as pages. Chat history is not a knowledge base.

**Lint.** On request: contradictions between pages; claims from a superseded version
presented as current; `verified` dates older than the current product version's release;
orphan pages; components mentioned repeatedly but lacking a page; procedures with no
verification step; errors with no diagnostic commands.

**Version bump.** When a product's docs are re-fetched at a new version, diff the raw
markdown first, then only touch wiki pages the diff actually affects. Write the version
page from the diff, not from the release notes alone.

## index.md and log.md

`index.md` is a catalog grouped by page type, each entry a link, a one-line summary, and
the products/versions it covers. Updated on every ingest.

`log.md` is append-only, one entry per operation, prefixed
`## [YYYY-MM-DD] ingest|query|lint|bump | <subject>` so it greps cleanly.
