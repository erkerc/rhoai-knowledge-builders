"""Offline checks - no network. Run: python tests/test_offline.py"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rhkb.catalog import load_catalog, set_enabled
from rhkb.html2md import to_markdown
from rhkb.state import CHANGED, DONE, PENDING, State
from rhkb.units import units_for_document
from rhkb.util import est_tokens, frontmatter, split_frontmatter, write_atomic
from rhkb.wiki import IndexEntry, build_brief, ensure_scaffold, index_slice, pack_session

ROOT = Path(__file__).resolve().parents[1]

PAGE_HTML = """
<html><head><title>Serving models | Red Hat OpenShift AI | 3.5 | Red Hat Documentation</title></head>
<body>
<header><a href="/">Skip to content</a></header>
<nav class="md-nav"><a href="/x">Nav noise</a></nav>
<div class="toc">Table of contents noise</div>
<main><article>
  <h1>Serving models</h1>
  <p>You can deploy a model with <code>KServe</code>.</p>
  <h2>Deploying a model</h2>
  <p>Run the following command:</p>
  <pre class="language-bash">Copy to clipboard
oc apply -f isvc.yaml
oc get inferenceservice -n my-ns</pre>
  <pre class="language-yaml">apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: my-model
spec:
  predictor:
    model:
      runtime: vllm-runtime</pre>
  <h3>Verification</h3>
  <ul><li>Check the pod is <strong>Running</strong></li><li>Check the route</li></ul>
  <table><tr><th>Field</th><th>Meaning</th></tr>
         <tr><td>runtime</td><td>Serving runtime name</td></tr></table>
  <h2>Troubleshooting</h2>
  <p>See <a href="https://example.invalid/x">the guide</a>.</p>
</article></main>
<div class="feedback">Was this helpful?</div>
<footer>© Red Hat</footer>
</body></html>
"""


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}{('  ' + str(detail)) if not condition else ''}")
    return bool(condition)


def main():
    ok = True

    # --- html -> markdown -------------------------------------------------
    md = to_markdown(PAGE_HTML)
    ok &= check("headings survive", "# Serving models" in md and "## Deploying a model" in md)
    ok &= check("shell commands kept verbatim", "oc get inferenceservice -n my-ns" in md, md[:200])
    ok &= check("YAML indentation preserved",
                "    model:\n      runtime: vllm-runtime" in md, repr(md[md.find("apiVersion"):][:160]))
    ok &= check("code fences carry the language", "```yaml" in md and "```bash" in md)
    ok &= check("'Copy to clipboard' stripped", "Copy to clipboard" not in md)
    ok &= check("nav / footer / feedback dropped",
                all(x not in md for x in ("Nav noise", "Was this helpful", "Table of contents noise")),
                md)
    ok &= check("tables become markdown tables", "| Field | Meaning |" in md, md)
    ok &= check("inline code and links kept", "`KServe`" in md and "](https://example.invalid/x)" in md)
    ok &= check("lists kept", "- Check the pod is **Running**" in md, md)

    # --- units ------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw"
        doc = raw / "rhoai" / "3.5" / "serving.md"
        head = frontmatter({"source": "rhoai", "kind": "product-docs", "product": "RHOAI",
                            "version": "3.5", "title": "Serving models",
                            "url": "https://example.invalid/serving"})
        write_atomic(doc, head + "\n" + md)

        fields, body = split_frontmatter(doc.read_text())
        ok &= check("frontmatter round-trips", fields["version"] == "3.5" and body.startswith("# Serving"))

        units = units_for_document(doc, raw, unit_tokens=6000)
        titles = [u.title for u in units]
        # A short page is deliberately kept whole - stub sections are not units.
        ok &= check("a short page stays one unit", len(units) == 1, titles)

        # A realistically sized guide must split at its headings.
        guide = raw / "rhoai" / "3.5" / "guide.md"
        big_body = "# Guide\n\nIntro.\n\n" + "".join(
            f"## {name}\n\n" + (f"Text about {name.lower()} here. " * 250) + "\n\n"
            for name in ("Installing", "Configuring", "Serving", "Troubleshooting"))
        write_atomic(guide, head + "\n" + big_body)
        guide_units = units_for_document(guide, raw, unit_tokens=6000)
        ok &= check("a real guide splits at headings",
                    [u.title for u in guide_units]
                    == ["Installing", "Configuring", "Serving", "Troubleshooting"],
                    [u.title for u in guide_units])
        ok &= check("no sliver units", all(u.tokens > 50 for u in guide_units),
                    [u.tokens for u in guide_units])
        tight = units_for_document(guide, raw, unit_tokens=800)
        ok &= check("a tighter budget produces more, smaller units",
                    len(tight) > len(guide_units)
                    and all(u.tokens <= 800 * 1.12 for u in tight),
                    [u.tokens for u in tight])
        ok &= check("still no slivers at a tight budget", all(u.tokens > 50 for u in tight),
                    [u.tokens for u in tight])

        # A section with no paragraph breaks must still be split.
        wall = raw / "rhoai" / "3.5" / "wall.md"
        write_atomic(wall, head + "\n# Wall\n\n" + ("word " * 12000))
        wall_units = units_for_document(wall, raw, unit_tokens=1500)
        ok &= check("an unbroken wall of text is still chunked",
                    len(wall_units) > 1 and all(u.tokens <= 1500 * 1.12 for u in wall_units),
                    [u.tokens for u in wall_units])
        ok &= check("units carry breadcrumb + source metadata",
                    all(u.source == "rhoai" and u.version == "3.5" for u in units))
        ok &= check("unit ids are stable and unique",
                    len({u.id for u in guide_units}) == len(guide_units)
                    and guide_units == units_for_document(guide, raw, unit_tokens=6000))
        ok &= check("no unit exceeds the budget", all(u.tokens <= 6000 for u in units),
                    [u.tokens for u in units])
        rendered = units[0].render()
        ok &= check("rendered unit names its source document",
                    "rhoai/3.5/serving.md" in rendered and "https://example.invalid/serving" in rendered)

        # A single oversized section must still be split.
        big = raw / "rhoai" / "3.5" / "big2.md"
        write_atomic(big, head + "\n# One section\n\n" + ("paragraph text here.\n\n" * 4000))
        big_units = units_for_document(big, raw, unit_tokens=2000)
        ok &= check("oversized sections are chunked",
                    len(big_units) > 1 and all(u.tokens <= 2600 for u in big_units),
                    [u.tokens for u in big_units])
        ok &= check("chunks are numbered", big_units[0].parts == len(big_units))

        # Code fences must never be split across chunks.
        fence_count = sum(u.body.count("```") for u in units)
        ok &= check("code fences stay balanced", fence_count % 2 == 0, fence_count)

        # --- state --------------------------------------------------------
        state = State(Path(tmp))
        first = units[0]
        ok &= check("unknown unit is pending", state.status_of(first) == PENDING)
        state.mark_done(first)
        state.save()
        reloaded = State(Path(tmp))
        ok &= check("state persists", reloaded.status_of(first) == DONE)

        edited = units_for_document(doc, raw, unit_tokens=6000)[0]
        edited.sha = "different-hash"
        ok &= check("edited content flips done -> changed", reloaded.status_of(edited) == CHANGED)
        ok &= check("outstanding excludes done units",
                    first.id not in {u.id for u in reloaded.outstanding(units)})

        # --- brief / context budget ----------------------------------------
        wiki = Path(tmp) / "wiki"
        ensure_scaffold(wiki)
        ok &= check("scaffold creates index and log",
                    (wiki / "index.md").exists() and (wiki / "log.md").exists())

        entries = [
            IndexEntry("kserve", "- [[kserve]] (component) - KServe serving runtime and InferenceService"),
            IndexEntry("gpu-time-slicing", "- [[gpu-time-slicing]] (procedure) - sharing one GPU"),
            IndexEntry("etcd-backup", "- [[etcd-backup]] (procedure) - backing up etcd"),
        ]
        picked = index_slice(entries, units)
        slugs = [e.slug for e in picked]
        ok &= check("index slice surfaces related pages", "kserve" in slugs, slugs)
        ok &= check("index slice drops unrelated pages", "etcd-backup" not in slugs, slugs)

        batch = pack_session(units, session_tokens=3000)
        ok &= check("session packing respects the budget",
                    sum(u.tokens for u in batch) <= 3000 or len(batch) == 1,
                    sum(u.tokens for u in batch))

        brief = build_brief(batch, entries, wiki, 3000)
        ok &= check("brief includes only the packed units",
                    all(u.id in brief for u in batch)
                    and sum(1 for u in units if u.id in brief) == len(batch))
        ok &= check("brief points at the schema, not the whole wiki", "CLAUDE.md" in brief)
        ok &= check("brief stays near the budget", est_tokens(brief) < 3000 * 2, est_tokens(brief))

    # --- block-page detection -------------------------------------------
    from rhkb.fetch import FetchError, _reject_block_page

    def rejects(html, label):
        try:
            _reject_block_page(html, "https://example.invalid/x")
            return False
        except FetchError:
            return True

    ok &= check("proxy block page is rejected",
                rejects("<html><body><pre>Host not in allowlist: docs.redhat.com. "
                        "Add this host to your network egress settings.</pre></body></html>", "proxy"))
    ok &= check("CDN challenge page is rejected",
                rejects("<html><title>Attention Required</title><body>"
                        + "Checking your browser before accessing. " * 60 + "</body></html>", "cdn"))
    ok &= check("a suspiciously short page is rejected", rejects("<html><body>hi</body></html>", "short"))
    ok &= check("a real page passes", not rejects(
        "<html><body><article><h1>Serving models</h1>" + ("<p>Real documentation text. </p>" * 80)
        + "</article></body></html>", "real"))

    # --- catalog ----------------------------------------------------------
    catalog = load_catalog(ROOT / "sources.yaml")
    ok &= check("catalog parses", len(catalog.sources) > 10, len(catalog.sources))
    ok &= check("every source has a label and tier",
                all(s.label and s.tier in ("core", "platform", "upstream", "extra") for s in catalog.sources))
    ok &= check("repo sources restrict their paths",
                all(s.paths for s in catalog.sources if s.type == "repo"),
                [s.id for s in catalog.sources if s.type == "repo" and not s.paths])
    ok &= check("core sources are on by default",
                all(s.enabled for s in catalog.sources if s.tier == "core" and s.id != "rhelai"))
    ok &= check("ocp is scoped, not the whole product",
                bool(catalog.by_id("ocp").include))
    ok &= check("tier selection works",
                {s.id for s in catalog.select(["core"])} >= {"rhoai", "rhcl"})
    ok &= check("'repo' selects only repos",
                all(s.type == "repo" for s in catalog.select(["repo"])))
    ok &= check("unknown ids are ignored, not fatal", catalog.select(["nope"]) == [])

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "sources.yaml"
        shutil.copy(ROOT / "sources.yaml", copy)
        changed = set_enabled(copy, ["trustyai"], True)
        after = load_catalog(copy)
        ok &= check("enable flips exactly one source",
                    changed == ["trustyai"] and after.by_id("trustyai").enabled
                    and not after.by_id("kuberay").enabled)
        ok &= check("enable preserves comments in the file",
                    "# rhoai-knowledge-builders - source catalog" in copy.read_text())
        set_enabled(copy, ["rhoai"], False)
        ok &= check("disable works too", not load_catalog(copy).by_id("rhoai").enabled)

    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
