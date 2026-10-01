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

    # --- decomposed-node regression --------------------------------------
    # Nested chrome (a div.toc wrapping a span, an <svg> with children) used to
    # raise "'NoneType' object has no attribute 'get'" and abort a whole product.
    nested = ("<html><body>"
              "<div class='toc'><span id='inner'>nav child</span><ul><li>x</li></ul></div>"
              "<svg><path d='M0'/><title>icon</title></svg>"
              "<nav class='md-nav'><a href='/a'>n</a></nav>"
              "<article><h1>Serving models</h1>"
              + "<p>Real documentation text. </p>" * 40
              + "<pre><code>oc get pods</code></pre></article></body></html>")
    try:
        nested_md = to_markdown(nested)
        crashed = ""
    except Exception as exc:
        nested_md, crashed = "", f"{type(exc).__name__}: {exc}"
    ok &= check("nested chrome does not crash the converter", not crashed, crashed)
    ok &= check("nested chrome is still removed",
                all(x not in nested_md for x in ("nav child", "icon", "md-nav")), nested_md[:200])
    ok &= check("content survives chrome removal",
                "# Serving models" in nested_md and "oc get pods" in nested_md, nested_md[:200])

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

    # --- topic-page products (Automation Orchestrator) ----------------------
    import rhkb.fetch as fetch_mod
    from rhkb.catalog import Source
    from rhkb.fetch import guide_url, list_guides, section_of, fetch_redhat_docs
    from rhkb.html2md import nav_leak_count, page_title, trim_to_title, make_soup

    PRODUCT = "automation_orchestrator"
    BASE = f"https://docs.redhat.com/en/documentation/{PRODUCT}/2026.8"
    TOPICS = {
        "get_started-create_your_first_workflow": "Create your first workflow",
        "plan-choose_a_deployment_model": "Choose a deployment model",
        "install-install_with_aapctl": "Install with aapctl",
        "install-understand_aapctl": "Understand aapctl",
        "develop-validate_workflows": "Validate workflows",
        "configure-manage_groups": "Manage groups",
        "secure-control_access_with_policies_and_roles": "Control access with policies and roles",
        "observe-monitor_audit_events": "Monitor audit events",
        "troubleshoot-troubleshoot_workflow_runs_ref": "Troubleshoot workflow runs",
        "whats_new-automation_orchestrator_release_notes": "Release notes",
    }
    PRODUCT_NAME = "Red Hat Ansible Automation Platform \u2014 automation orchestrator"
    nav_items = "".join(f'<li><a href="{BASE}/{slug_}">{name}</a></li>' for slug_, name in TOPICS.items())

    index_html = (
        f"<html><head><title>{PRODUCT_NAME} | 2026.8 | Red Hat Documentation</title></head><body>"
        f"<ul>{nav_items}</ul>"
        f'<a href="{BASE}/install-install_with_aapctl#install-with-aapctl">dup with anchor</a>'
        f'<a href="{BASE}/download_pdf-automation_orchestrator_2026_8_pdf_reference">PDF page</a>'
        '<a href="https://developers.redhat.com/api-catalog/api/automation-orchestrator">REST API</a>'
        '<a href="https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7">AAP 2.7</a>'
        f'<a href="https://docs.redhat.com/en/documentation/{PRODUCT}/2026.7/install-old_version">old</a>'
        "</body></html>")

    def topic_html(slug_, title):
        other = next(k for k in TOPICS if k != slug_)
        return (
            f"<html><head><title>{PRODUCT_NAME} | 2026.8 | {title} | Red Hat Documentation</title></head><body>"
            f"<article>"
            f"<div><h1>{PRODUCT_NAME}</h1><p>Install</p><ul>{nav_items}</ul></div>"
            f"<div><h2>{PRODUCT_NAME}</h2><p>Develop</p><ul>{nav_items}</ul></div>"
            f"<h1>{title}</h1>"
            f"<p>{title} explains how this part of the product works. " + "Detailed explanatory text. " * 40 + "</p>"
            "<p><strong>Procedure</strong></p>"
            "<pre><code>oc get automationorchestrator -n aao</code></pre>"
            f'<p>Additional resources</p><ul><li><a href="{BASE}/{other}">Related topic</a></li></ul>'
            "</article></body></html>")

    def fake_get_html(sess, url, timeout=60):
        from rhkb.fetch import FetchError
        if url.rstrip("/") in (BASE, f"https://docs.redhat.com/en/documentation/{PRODUCT}"):
            return index_html
        for slug_, name in TOPICS.items():
            if url == f"{BASE}/{slug_}":
                return topic_html(slug_, name)
        raise FetchError(f"HTTP 404 for {url}")

    original_get_html = fetch_mod.get_html
    fetch_mod.get_html = fake_get_html
    try:
        found = list_guides(None, PRODUCT, "2026.8")
        ok &= check("topic pages are discovered", set(found) == set(TOPICS), sorted(set(found) ^ set(TOPICS)))
        ok &= check("they are classified as topic pages", set(found.values()) == {"topic"})
        ok &= check("the PDF-download page, other products, other versions and external links are ignored",
                    not any(k.startswith("download_pdf") or "old_version" in k for k in found))
        ok &= check("topic URLs are built directly under the version",
                    guide_url(PRODUCT, "2026.8", "install-install_with_aapctl", "topic")
                    == f"{BASE}/install-install_with_aapctl")
        ok &= check("classic URLs are unchanged",
                    guide_url("p", "1", "g", "html-single")
                    == "https://docs.redhat.com/en/documentation/p/1/html-single/g/index")
        ok &= check("section comes from the slug prefix",
                    section_of("install-install_with_aapctl", "topic") == "Install"
                    and section_of("get_started-x", "topic") == "Get started"
                    and section_of("whats_new-x", "topic") == "What's new"
                    and section_of("release_notes", "html-single") == "")

        source = Source(id="ao", type="redhat-docs", product=PRODUCT, version="2026.8", include=[])
        with tempfile.TemporaryDirectory() as tmp:
            written = fetch_redhat_docs(source, Path(tmp) / "raw", sess=None)
            ok &= check("one file is written per topic page", len(written) == len(TOPICS), len(written))
            titles = []
            for path in written:
                fields, body = split_frontmatter(path.read_text())
                titles.append(fields.get("title"))
            ok &= check("every page keeps its own title, not the product name",
                        len(set(titles)) == len(TOPICS) and PRODUCT_NAME not in titles, titles)

            sample = next(w for w in written if w.name == "install-install-with-aapctl.md")
            fields, body = split_frontmatter(sample.read_text())
            ok &= check("frontmatter records the section and pinned version",
                        fields.get("section") == "Install" and fields.get("version") == "2026.8", fields)
            ok &= check("navigation is trimmed away",
                        "Understand aapctl" not in body.replace("Related topic", "")
                        and f"# {PRODUCT_NAME}" not in body
                        and nav_leak_count(body, f"/{PRODUCT}/2026.8/") <= 1, body[:300])
            ok &= check("content, procedure labels and code survive",
                        "Detailed explanatory text" in body and "**Procedure**" in body
                        and "oc get automationorchestrator -n aao" in body)
            ok &= check("a genuine cross-reference is kept", "Related topic" in body)

        # The leak detector must fire on an untrimmed page - that is its whole job.
        untrimmed = to_markdown(topic_html("install-install_with_aapctl", "Install with aapctl"))
        ok &= check("an untrimmed page trips the nav-leak detector",
                    nav_leak_count(untrimmed, f"/{PRODUCT}/2026.8/") >= 12,
                    nav_leak_count(untrimmed, f"/{PRODUCT}/2026.8/"))

        # Classic products must not be affected by the new link pattern.
        classic_index = (
            "<html><head><title>Product | 3.5 | Red Hat Documentation</title></head><body>"
            '<a href="https://docs.redhat.com/en/documentation/prod/3.5/html/release_notes/index">RN</a>'
            '<a href="https://docs.redhat.com/en/documentation/prod/3.5/some_stray_page">stray</a>'
            "</body></html>")
        fetch_mod.get_html = lambda sess, url, timeout=60: classic_index
        classic = list_guides(None, "prod", "3.5")
        ok &= check("classic products keep html-single and ignore topic-looking links",
                    classic == {"release_notes": "html-single"}, classic)
    finally:
        fetch_mod.get_html = original_get_html

    # --- title selection ----------------------------------------------------
    soup = make_soup(topic_html("install-install_with_aapctl", "Install with aapctl"))
    ok &= check("page_title skips the product-name h1 in the nav block",
                page_title(soup) == "Install with aapctl", page_title(soup))
    classic_soup = make_soup("<html><head><title>Release notes | Red Hat OpenShift AI | 3.5 | "
                             "Red Hat Documentation</title></head><body><h1>Release notes</h1></body></html>")
    ok &= check("page_title is unchanged for classic guides", page_title(classic_soup) == "Release notes")

    md_text = "intro nav\n\n# Product\n\nmore nav\n\n# Real title\n\n" + "body text here. " * 30 + "\n"
    ok &= check("trim_to_title cuts everything before the last matching heading",
                trim_to_title(md_text, "Real title").startswith("# Real title"))
    ok &= check("trim_to_title leaves a page alone when the heading is missing",
                trim_to_title(md_text, "No such title") == md_text)
    ok &= check("trim_to_title will not reduce a page to almost nothing",
                trim_to_title("nav\n\n# Real title\n\nhi\n", "Real title") == "nav\n\n# Real title\n\nhi\n")
    fenced = "```\n# Real title\n```\n\n# Real title\n\n" + "body. " * 60
    ok &= check("a heading-looking line inside a code fence is ignored",
                trim_to_title("nav\n\n" + fenced, "Real title").startswith("# Real title\n\nbody"))

    # --- workspace outside the repository ------------------------------------
    import contextlib
    import hashlib
    import io
    import os
    from rhkb import cli as cli_mod

    def quiet_main(argv):
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            return cli_mod.main(argv), sink.getvalue()

    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    saved_home = os.environ.pop("RHKB_HOME", None)
    template_hash = digest(ROOT / "sources.yaml")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"

            os.environ["RHKB_HOME"] = str(ws)
            ok &= check("RHKB_HOME becomes the default workspace",
                        Path(cli_mod.build_parser().parse_args(["status"]).root) == ws)
            os.environ.pop("RHKB_HOME")
            ok &= check("without RHKB_HOME the default is the current directory",
                        Path(cli_mod.build_parser().parse_args(["status"]).root) == Path("."))

            rc, out = quiet_main(["init", "-C", str(ws)])
            ok &= check("init builds a complete workspace",
                        rc == 0 and (ws / "raw").is_dir() and (ws / "wiki" / "index.md").exists()
                        and (ws / "sources.yaml").exists() and (ws / "CLAUDE.md").exists()
                        and (ws / ".gitignore").exists(), sorted(p.name for p in ws.iterdir()))
            ok &= check("the workspace .gitignore keeps fetched docs out of git",
                        "raw/" in (ws / ".gitignore").read_text())

            (ws / "sources.yaml").write_text("sources: []\n")
            quiet_main(["init", "-C", str(ws)])
            ok &= check("re-running init never overwrites your catalog",
                        (ws / "sources.yaml").read_text() == "sources: []\n")

            fresh = Path(tmp) / "fresh"
            rc, out = quiet_main(["enable", "trustyai", "-C", str(fresh)])
            ok &= check("enable on a new workspace copies the template and edits the copy",
                        rc == 0 and load_catalog(fresh / "sources.yaml").by_id("trustyai").enabled, out)
            ok &= check("enable never modifies the tracked template",
                        digest(ROOT / "sources.yaml") == template_hash)

            bare = Path(tmp) / "bare"
            bare.mkdir()
            rc, out = quiet_main(["sources", "-C", str(bare)])
            ok &= check("read-only commands fall back to the bundled catalog", rc == 0 and "rhoai" in out)

            ok &= check("a temp directory is not mistaken for the code repository",
                        cli_mod.in_code_repo(Path(tmp)) is False)
            ok &= check("the source checkout is recognized as the code repository",
                        cli_mod.in_code_repo(ROOT) == (ROOT / ".git").exists())
    finally:
        if saved_home is not None:
            os.environ["RHKB_HOME"] = saved_home

    # --- catalog ----------------------------------------------------------
    catalog = load_catalog(ROOT / "sources.yaml")
    ok &= check("catalog parses", len(catalog.sources) > 10, len(catalog.sources))
    ok &= check("every source has a label and tier",
                all(s.label and s.tier in ("core", "platform", "upstream", "extra") for s in catalog.sources))
    ok &= check("repo sources restrict their paths",
                all(s.paths for s in catalog.sources if s.type == "repo"),
                [s.id for s in catalog.sources if s.type == "repo" and not s.paths])
    # rhelai and rhaiis are deliberately off (rhaiis publishes no guides of its own).
    ok &= check("the main core sources are on by default",
                all(catalog.by_id(i).enabled for i in ("rhoai", "rhai-inference", "rhcl")))
    ok &= check("at least one docs source and one repo source are on",
                any(s.enabled and s.type == "redhat-docs" for s in catalog.sources)
                and any(s.enabled and s.type == "repo" for s in catalog.sources))
    ok &= check("ocp is scoped, not the whole product",
                bool(catalog.by_id("ocp").include))
    ok &= check("no source ships a guessed include list for a fast-moving product",
                catalog.by_id("rhoai").include == [],
                catalog.by_id("rhoai").include)
    ao = catalog.by_id("automation-orchestrator")
    ok &= check("automation-orchestrator is pinned to the requested version and enabled",
                ao is not None and ao.product == "automation_orchestrator"
                and ao.version == "2026.8" and ao.enabled, ao)
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
