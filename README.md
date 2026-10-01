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
pip install -r requirements.txt
python -m playwright install chromium    # one-time; see below
```

Playwright is a hard requirement because `docs.redhat.com` rejects plain HTTP clients with a
403 (details under [When fetching is refused](#when-fetching-is-refused-403)). `pip` installs
the Playwright *package* but not the Chromium build it drives, which is why the second command
exists. It cannot go in `requirements.txt`, since `pip` only reads package names from that file.
If you skip it, `rhkb fetch` fails with a message that says to run it.

Use `python -m playwright ...` rather than a bare `playwright ...` so the browser is installed
for the same interpreter you run rhkb with.

Then set up your private workspace (next section) before you fetch anything.

## Your data lives outside this repository

This repository is the **tool**. Everything it produces or consumes is private to you and
belongs in a separate **workspace** directory that is not a git repository (or is a *private* one):

| in the workspace, `~/rhkb-workspace` | why it must stay out of this repo |
|---|---|
| `raw/` | fetched copies of Red Hat documentation and upstream repos |
| `wiki/` | compiled from your own work; it accumulates customer names, cluster details and troubleshooting notes |
| `state.json`, `brief.md`, `briefs/` | ingest state and generated briefs |
| `sources.yaml` | *your* selection of sources, including any private notes in it |
| `CLAUDE.md` | your copy of the wiki schema; agents append to it |

```bash
export RHKB_HOME="$HOME/rhkb-workspace"
python -m rhkb init            # creates raw/, wiki/, and copies sources.yaml + CLAUDE.md in
```

With `RHKB_HOME` set, every command reads and writes there regardless of your current
directory, and `enable` / `disable` edit *your* `sources.yaml` rather than the template in this
repo. Run your agent from the workspace, not from the code repo:

```bash
cd ~/rhkb-workspace && claude
```

rhkb warns if you point it at its own source checkout.

**What stops private files reaching GitHub.** The `.gitignore` here is an *allowlist*: it ignores
everything and re-includes only the tool's own code (`rhkb/**/*.py`, `tests/**/*.py`, the CI
workflow, and a handful of project files). A new folder of notes, a chat export, a `.env` or a
PDF is therefore ignored automatically, with no need to anticipate its name. Two further guards:

```bash
git config core.hooksPath .githooks          # once per clone: blocks `git add -f` of anything off the allowlist
python tests/test_repo_hygiene.py            # CI runs this; fails if a non-allowlisted file is tracked
python tests/test_repo_hygiene.py --history  # audit every path ever committed, on any branch
```

An ignore rule cannot remove a file that was already committed, and it does nothing about a
push that already happened. If something private did get committed, `--history` lists it and
`python tests/test_repo_hygiene.py --filter-repo-args` prints the `git filter-repo` command that
rewrites history down to the allowlist.

If you want version control for the workspace, make that a **private** repository.

## Running rhkb

Every command example below is written as `python -m rhkb <command>`, run from the repo root
(or with `PYTHONPATH` set to it). That needs no installation beyond the steps above.

Typing that every time gets old. Two ways to shorten it to `rhkb <command>`:

**A shell function** (works without installing the package, from any directory). Add to
`~/.zshrc` (the macOS default) or `~/.bashrc`, adjusting the paths:

```bash
export RHKB_HOME="$HOME/rhkb-workspace"
rhkb() { PYTHONPATH="$HOME/working/rhoai-knowledge-builders" python -m rhkb "$@"; }
```

```bash
source ~/.zshrc
type rhkb          # should say: rhkb is a shell function
rhkb sources
```

It is a function rather than an `alias` because it forwards arguments with `"$@"`, which an
`alias` can't do cleanly. A stale `alias rhkb=...` in your rc file will shadow the function, so
delete any old one and run `unalias rhkb` in open terminals.

A shell redirect such as `rhkb next > brief.md` writes into the directory you are standing in.
Use `--out` to put it in the workspace:

```bash
rhkb next --out "$RHKB_HOME/brief.md"
```

**Or install the package**, which gives a real `rhkb` executable:

```bash
pip install -e .
rhkb sources
```

That is simpler, but the command only exists in the Python environment you installed into, so
activate the same virtualenv first. Set `RHKB_HOME` either way.

Hints printed by rhkb itself (for example "run `rhkb fetch` first") use the short form.

## Workflow

```bash
python -m rhkb sources                 # what is available, what is on
python -m rhkb enable trustyai kuberay # pick what you want
python -m rhkb disable ocp             # skip what you don't
python -m rhkb fetch                   # pull enabled sources into raw/
python -m rhkb plan                    # how much material, how many sessions
python -m rhkb next > brief.md         # one context-sized ingest brief
# ... hand brief.md to your agent, it updates wiki/ ...
python -m rhkb done <unit-ids>         # mark them off
python -m rhkb status
```

Guide slugs change between releases, so check before pinning an `include:` list:

```bash
python -m rhkb guides --source rhoai          # what this version actually publishes
python -m rhkb guides --source rhoai --yaml   # ready to paste into sources.yaml
```

Fetch a subset without touching the catalog:

```bash
python -m rhkb fetch --source kserve,vllm
python -m rhkb fetch --source core          # tier: core | platform | upstream | extra
python -m rhkb fetch --source repo          # every repo source
python -m rhkb fetch --limit 5 --dry-run    # trial run
```

## What's in the catalog

**core** — RHOAI Self-Managed, AI Inference, Connectivity Link. RHEL AI and AI Inference
Server are off by default (the latter's slug currently lists no guides of its own — check
with `rhkb guides --source rhaiis`).

**platform** — the OpenShift guides RHOAI actually depends on: accelerators, specialized
hardware, nodes, machine management, storage, scalability, networking, ingress, operators,
extensions, monitoring, authn/authz, post-install config, AI workloads. Plus the
`openshift-docs` AsciiDoc source (off by default), which is diffable across releases.

**upstream** — KServe, vLLM, llm-d, the Open Data Hub operator (the DataScienceCluster CRD
behind an RHOAI install), the Kuadrant operator, the NVIDIA GPU Operator, ODH
Models-as-a-Service, and the NFD operator. These carry the CRD fields, flags and examples
the product docs only summarise — the details you need when a customer's InferenceService
will not come up.

**extra** — TrustyAI, Data Science Pipelines, KubeRay, Llama Stack operator, ODH Dashboard. Off by default.

**automation-orchestrator** — Red Hat Ansible Automation Platform automation orchestrator, pinned to 2026.8. It is the one `extra` source that is on. It publishes about 70 task-based topic pages (`/2026.8/install-install_with_aapctl`) rather than `/html/<guide>/` books, so rhkb saves one markdown file per page and records its section (Install, Develop, Configure, …) in the frontmatter. Narrow it with `python -m rhkb guides --source automation-orchestrator --yaml`.

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

## When fetching is refused (403)

`docs.redhat.com` sits behind a CDN that sometimes rejects scripted clients. rhkb sends a
full browser header set, and on a 403 it opens the site once in headless Chrome, copies the
clearance cookies into the HTTP session and retries — after which normal requests work.

```bash
python -m playwright install chromium    # if you have not already; then re-run `python -m rhkb fetch`
```

`--browser-fallback off` disables the escalation; `--browser-engine selenium` picks the
other backend. If the interception is your own network rather than the CDN — a corporate
proxy or TLS-inspecting gateway — rhkb says so explicitly instead of writing empty
documents, and `export HTTPS_PROXY=...` is the fix. Repo sources go over git and are
unaffected either way.

## Layout

Two directories, deliberately separate.

```
rhoai-knowledge-builders/     the code repo - public-safe, allowlisted by .gitignore
├── rhkb/                     the tool
├── tests/                    including test_repo_hygiene.py
├── sources.yaml              catalog TEMPLATE (copied into the workspace by `init`)
├── CLAUDE.md                 wiki schema TEMPLATE (likewise)
└── .githooks/pre-commit

~/rhkb-workspace/             RHKB_HOME - private, never pushed to a public repo
├── sources.yaml              your catalog: what to fetch
├── CLAUDE.md                 the wiki schema your agent follows; co-evolve it
├── raw/                      fetched markdown. Immutable - the agent reads, never writes.
├── wiki/                     the agent owns this: pages/, index.md, log.md
└── state.json                which units are ingested, by content hash
```

## Ingest state and version bumps

`state.json` records the content hash of every ingested unit. Re-fetch after a product
release and only the sections that actually changed flip back to `changed`; everything else
stays `done`. `rhkb next` picks up the changes. A 3.5 → 3.6 bump costs you the diff, not
the corpus.

```bash
python -m rhkb fetch --source rhoai    # after a release
python -m rhkb status                  # "12 unit(s) changed upstream since ingest"
python -m rhkb next
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
