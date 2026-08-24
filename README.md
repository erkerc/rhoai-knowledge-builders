# rhoai-knowledge-builders

[![tests](https://github.com/erkerc/rhoai-knowledge-builders/actions/workflows/tests.yml/badge.svg)](https://github.com/erkerc/rhoai-knowledge-builders/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Compile Red Hat AI documentation, the OpenShift layer it depends on, and the upstream
source repos into a **maintained wiki** an LLM agent keeps current — following
[Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

Built for the work that actually consumes these docs: writing setup and configuration
tutorials, building demos, and troubleshooting customer clusters.

Companion to [rhoai-docsync](https://github.com/erkerc/rhoai-docsync), which fetches the
same documentation as PDFs for reading and handoff. This repo fetches it as **markdown for
an agent to compile**.

## The context problem, and what this does about it

RHOAI, OpenShift and the upstream repos together are millions of tokens. You cannot put
that in a context window, and you should not try. Five mechanisms keep each session small:

| mechanism | effect |
|---|---|
| **Selective sources** | `sources.yaml` is a catalog with `enabled:` per entry. Nothing is fetched unless you turn it on. |
| **Selective paths** | Repo sources use blobless sparse checkout — `docs/` and `config/crd/` only, never the whole repo. Kuadrant costs 1.3 MB instead of a full clone. |
| **Selective guides** | OpenShift is scoped to the ~16 guides an RHOAI deployment depends on, not all ~100. |
| **Bounded units** | Documents are split at headings into units capped at `unit_tokens` (default 6k). A guide is never handed over whole. |
| **Index slicing** | An ingest brief carries only the index lines related to the units in hand — not the whole wiki index. |

The result: `rhkb next` emits one brief sized to a session budget (default 25k tokens),
containing a handful of units and the handful of existing pages they relate to. You paste
it at Claude Code, it updates the wiki, you mark the units done, you repeat.

## Install

```bash
git clone https://github.com/erkerc/rhoai-knowledge-builders.git
cd rhoai-knowledge-builders
pip install -r requirements.txt      # or: pip install -e .
```

## Workflow

```bash
rhkb sources                 # what is available, what is on
rhkb enable trustyai kuberay # pick what you want
rhkb disable ocp             # skip what you don't
rhkb fetch                   # pull enabled sources into raw/
rhkb plan                    # how much material, how many sessions
rhkb next > brief.md         # one context-sized ingest brief
# ... hand brief.md to your agent, it updates wiki/ ...
rhkb done <unit-ids>         # mark them off
rhkb status
```

Fetch a subset without touching the catalog:

```bash
rhkb fetch --source kserve,vllm
rhkb fetch --source core          # tier: core | platform | upstream | extra
rhkb fetch --source repo          # every repo source
rhkb fetch --limit 5 --dry-run    # trial run
```

## What's in the catalog

**core** — RHOAI Self-Managed, AI Inference Server, AI Inference, Connectivity Link
(RHEL AI available, off by default).

**platform** — the OpenShift guides RHOAI actually depends on: accelerators, specialized
hardware, nodes, machine management, storage, scalability, networking, ingress, operators,
extensions, monitoring, authn/authz, post-install config, AI workloads. Plus the
`openshift-docs` AsciiDoc source (off by default), which is diffable across releases.

**upstream** — KServe, vLLM, llm-d, the Open Data Hub operator (the DataScienceCluster CRD
behind an RHOAI install), the Kuadrant operator, the NVIDIA GPU Operator, ODH
Models-as-a-Service, and the NFD operator. These carry the CRD fields, flags and examples
the product docs only summarise — the details you need when a customer's InferenceService
will not come up.

**extra** — TrustyAI, Data Science Pipelines, KubeRay, Llama Stack operator, ODH Dashboard.
Off by default.

Editing the catalog is the intended way to work. Add any `docs.redhat.com` product or any
git repo in a few lines; `rhkb enable` / `disable` flip entries in place without disturbing
your comments.

## Why markdown, not PDF

PDF is a print format. Extracting from it mangles YAML indentation and `oc` command
whitespace — fatal when the whole point is producing runnable tutorials. This repo goes
after `html-single` pages and repo markdown/AsciiDoc instead, keeps code fences byte-exact,
and strips navigation, feedback widgets and "Copy to clipboard" artifacts. Every raw file
carries frontmatter recording product, version, source URL and — for repos — the commit.

Keep using `rhoai-docsync` for PDFs. Different artifact, different job.

## Layout

```
sources.yaml     the catalog - what to fetch. Under version control on purpose.
CLAUDE.md        the wiki schema. The agent's contract; co-evolve it with your agent.
raw/             fetched markdown. Immutable - the agent reads, never writes.
wiki/            the agent owns this: pages/, index.md, log.md
state.json       which units are ingested, by content hash
```

## Ingest state and version bumps

`state.json` records the content hash of every ingested unit. Re-fetch after a product
release and only the sections that actually changed flip back to `changed`; everything else
stays `done`. `rhkb next` picks up the changes. A 3.5 → 3.6 bump costs you the diff, not
the corpus.

```bash
rhkb fetch --source rhoai    # after a release
rhkb status                  # "12 unit(s) changed upstream since ingest"
rhkb next
```

`rhkb skip <id>` retires a unit that isn't worth compiling; `rhkb reset <prefix> --yes`
forgets state for one source so it is re-ingested from scratch.

## The wiki schema

`CLAUDE.md` defines six page types — `component`, `procedure`, `error`, `version`,
`concept`, `recipe` — plus the rules that matter for this domain: cite or delete, never
quote a moving value, version-qualify aggressively, and keep `confidence: documented`
separate from `confidence: tested`. The `error` page type, keyed by the symptom you
actually see rather than by component, is the one that pays for the whole exercise.

Treat the schema as yours. It is a starting point shaped by one person's guesses about your
workflow; change it as you learn what your agent gets wrong.

## Tests

```bash
python tests/test_offline.py
```

No network required. Covers HTML→markdown fidelity (YAML indentation, code fences, chrome
removal), unit splitting and budget compliance, state and change detection, index slicing,
and catalog selection and editing.

## Notes

Not affiliated with, endorsed by, or supported by Red Hat. It fetches public documentation
and public repositories at a polite rate. The compiled wiki contains derived summaries of
copyrighted documentation — keep it for your own use rather than republishing it.

## License

[MIT](LICENSE)
