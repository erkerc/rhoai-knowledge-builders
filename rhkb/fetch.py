"""Getting raw material onto disk.

Two fetchers, one output shape: markdown files under `raw/<source-id>/` with
YAML frontmatter recording where each one came from. Everything downstream
reads only that.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from .catalog import Source
from .html2md import make_soup, nav_leak_count, page_title, to_markdown, trim_to_title
from .util import LOG, est_tokens, frontmatter, sha256_text, slug, write_atomic

DOC_ROOT = "https://docs.redhat.com/en/documentation"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")
#: Statuses that mean "you look like a bot", not "this does not exist".
BLOCKED = (401, 403, 406, 429)
GUIDE_RE = re.compile(
    r"/documentation/(?P<product>[a-z0-9_.\-]+)/(?P<version>[^/#?]+)/html(?:-single)?/(?P<slug>[^/#?]+)",
    re.IGNORECASE,
)
# Newer products (Automation Orchestrator) publish one page per topic directly
# under the version instead of under /html/<guide>/:
#     /documentation/<product>/<version>/<section>-<topic>
TOPIC_RE = re.compile(
    r"/documentation/(?P<product>[a-z0-9_.\-]+)/(?P<version>[^/#?]+)/"
    r"(?P<slug>[a-z0-9][a-z0-9_.\-]*)/?(?:[#?]|$)",
    re.IGNORECASE,
)
#: Path segments that are page formats, not topics.
NON_TOPIC_SEGMENTS = {"html", "html-single", "pdf", "epub", "index", "topics", "all"}
#: Pages that describe how to download the PDF rather than being documentation.
NON_TOPIC_PREFIXES = ("download_pdf-",)
#: A converted page with this many bare self-links is probably carrying the nav tree.
NAV_LEAK_WARN = 12
SECTION_LABELS = {"whats_new": "What's new"}

SKIP_SLUGS = {"index", "legal-notice", "making-open-source-more-inclusive"}
TEXT_SUFFIXES = {".md", ".adoc", ".markdown", ".mdx", ".rst", ".txt", ".yaml", ".yml"}


class FetchError(Exception):
    pass


def session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not:A-Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "Connection": "keep-alive",
    })
    sess.rhkb_browser = None          # type: ignore[attr-defined]
    sess.rhkb_fallback = "auto"       # type: ignore[attr-defined]
    sess.rhkb_engine = "auto"         # type: ignore[attr-defined]
    return sess


def get_html(sess: requests.Session, url: str, timeout: int = 60) -> str:
    """GET a page, escalating to a headless browser if the CDN refuses us.

    The escalation happens once per session: the browser's cookies and UA are
    copied into the session, so subsequent requests go back over plain HTTP.
    """
    resp = sess.get(url, timeout=timeout)
    if resp.status_code < 400:
        return resp.text

    if resp.status_code not in BLOCKED or getattr(sess, "rhkb_fallback", "auto") == "off":
        raise FetchError(f"HTTP {resp.status_code} for {url}")

    browser = getattr(sess, "rhkb_browser", None)
    if browser is None:
        from .browser import warm_up
        browser = warm_up(sess, url, engine=getattr(sess, "rhkb_engine", "auto"))
        sess.rhkb_browser = browser or False   # type: ignore[attr-defined]
    if not browser:
        raise FetchError(
            f"HTTP {resp.status_code} for {url} - blocked, and no headless browser to fall back on"
        )

    retry = sess.get(url, timeout=timeout)
    if retry.status_code < 400:
        return retry.text

    try:                      # still refused: read it out of the browser itself
        html = browser.html(url)
    except Exception as exc:
        raise FetchError(f"HTTP {retry.status_code} for {url} (browser also failed: {exc})")
    _reject_block_page(html, url)
    return html


BLOCK_MARKERS = (
    "not in allowlist",           # egress proxy
    "access denied",
    "request blocked",
    "checking your browser",      # CDN interstitial
    "enable javascript and cookies",
    "attention required",
)


def _reject_block_page(html: str, url: str) -> None:
    """A block page is not content.

    The browser fallback happily returns whatever it was served - a corporate
    proxy notice, a CDN challenge - and without this check that HTML flows
    downstream and shows up as an empty guide rather than an error.
    """
    stripped = (html or "").strip()
    lowered = stripped.lower()
    hit = next((m for m in BLOCK_MARKERS if m in lowered), None)
    if hit:
        text = re.sub(r"<[^>]+>", " ", stripped)
        text = re.sub(r"\s+", " ", text).strip()
        raise FetchError(f"{url} was intercepted: {text[:160]}")
    if len(stripped) < 1000:
        raise FetchError(
            f"{url} returned only {len(stripped)} bytes via the browser - "
            "almost certainly a block or challenge page, not the document"
        )


# --------------------------------------------------------------- redhat ----
def resolve_version(sess: requests.Session, product: str, version: str) -> Tuple[str, str]:
    """Return (version, display name). `latest` resolves via the product root."""
    url = f"{DOC_ROOT}/{product}/{version}" if version and version != "latest" else f"{DOC_ROOT}/{product}"
    soup = make_soup(get_html(sess, url, timeout=30))
    title = (soup.title.string if soup.title else "") or ""
    parts = [p.strip() for p in title.split("|") if p.strip()
             and "red hat documentation" not in p.lower()]
    display = parts[0] if parts else product.replace("_", " ").title()
    resolved = version
    if version in ("", "latest", None):
        if len(parts) >= 2 and re.fullmatch(r"\d+(\.\d+)*", parts[1]):
            resolved = parts[1]
        else:
            link = soup.find("link", rel="canonical")
            href = (link.get("href") if link else "") or ""
            match = re.search(rf"/{re.escape(product)}/([^/?#]+)", href)
            resolved = match.group(1) if match else "latest"
    return resolved, display


def list_guides(sess: requests.Session, product: str, version: str) -> Dict[str, str]:
    """slug -> page style, for everything this product version publishes.

    Style is "html-single" for the classic /html/<guide>/ layout, or "topic" for
    products that publish individual pages under the version. A product uses one
    or the other; topic-style links are only considered when no classic guide
    links exist, so existing products behave exactly as before.
    """
    soup: BeautifulSoup = make_soup(get_html(sess, f"{DOC_ROOT}/{product}/{version}", timeout=30))
    classic: List[str] = []
    topics: List[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        match = GUIDE_RE.search(href)
        if match:
            guide = match.group("slug")
            if (match.group("product").lower() == product.lower()
                    and guide.lower() not in SKIP_SLUGS and guide not in classic):
                classic.append(guide)
            continue
        topic = TOPIC_RE.search(href)
        if not topic or topic.group("product").lower() != product.lower():
            continue
        if topic.group("version") != version:
            continue
        slug_ = topic.group("slug")
        if (slug_.lower() in NON_TOPIC_SEGMENTS or slug_.lower() in SKIP_SLUGS
                or slug_.lower().startswith(NON_TOPIC_PREFIXES) or slug_ in topics):
            continue
        topics.append(slug_)

    if classic:
        return {g: "html-single" for g in sorted(classic)}
    return {g: "topic" for g in sorted(topics)}


def guide_url(product: str, version: str, guide: str, style: str) -> str:
    if style == "topic":
        return f"{DOC_ROOT}/{product}/{version}/{guide}"
    return f"{DOC_ROOT}/{product}/{version}/html-single/{guide}/index"


def section_of(guide: str, style: str) -> str:
    """'install-install_with_aapctl' -> 'Install'. Topic pages only."""
    if style != "topic" or "-" not in guide:
        return ""
    prefix = guide.split("-", 1)[0]
    return SECTION_LABELS.get(prefix, prefix.replace("_", " ").capitalize())


def fetch_redhat_docs(source: Source, out_dir: Path, sess: Optional[requests.Session] = None,
                      limit: int = 0) -> List[Path]:
    sess = sess or session()
    version, display = resolve_version(sess, source.product, source.version)
    LOG.info("[%s] %s %s", source.id, display, version)

    found = list_guides(sess, source.product, version)
    guides = list(found)
    if source.include:
        wanted = [g for g in guides if g in source.include]
        missing = [g for g in source.include if g not in guides]
        if missing:
            LOG.warning("[%s] not in %s %s: %s", source.id, display, version, ", ".join(missing))
        guides = wanted
    if limit:
        guides = guides[:limit]
    topic_pages = any(found[g] == "topic" for g in guides)
    LOG.info("[%s] %d guide(s)%s", source.id, len(guides), " (topic pages)" if topic_pages else "")

    written: List[Path] = []
    for guide in guides:
        style = found[guide]
        url = guide_url(source.product, version, guide, style)
        try:
            html = get_html(sess, url)
            soup = make_soup(html)
            title = page_title(soup) or guide.replace("_", " ").title()
            body = to_markdown(html)
            if style == "topic":
                body = trim_to_title(body, title)
        except (FetchError, requests.RequestException) as exc:
            LOG.warning("[%s] %s: %s", source.id, guide, exc)
            continue
        except Exception as exc:   # a parser bug on one page is not a reason to lose the rest
            LOG.warning("[%s] %s: could not convert (%s: %s)",
                        source.id, guide, type(exc).__name__, exc)
            LOG.debug("", exc_info=True)
            continue
        if len(body) < 400:
            LOG.warning("[%s] %s: extracted almost nothing, skipping", source.id, guide)
            continue

        leaked = nav_leak_count(body, f"/{source.product}/{version}/")
        if leaked >= NAV_LEAK_WARN:
            LOG.warning("[%s] %s: %d bare links into the same product - navigation may be "
                        "leaking into the page; check the file before ingesting",
                        source.id, guide, leaked)

        head = frontmatter({
            "source": source.id,
            "kind": "product-docs",
            "product": display,
            "version": version,
            "section": section_of(guide, style),
            "title": title,
            "url": url,
            "tokens": est_tokens(body),
        })
        dest = out_dir / source.id / version / f"{slug(guide)}.md"
        write_atomic(dest, head + "\n" + body)
        written.append(dest)
        LOG.debug("[%s] wrote %s (%s tokens)", source.id, dest.name, est_tokens(body))

    if guides and not written:
        LOG.warning("[%s] %d guide(s) discovered but none could be saved", source.id, len(guides))
    elif len(written) < len(guides):
        LOG.info("[%s] saved %d of %d guide(s)", source.id, len(written), len(guides))
    return written


# ----------------------------------------------------------------- repo ----
def _git(args: List[str], cwd: Optional[Path] = None, timeout: int = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise FetchError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:300]}")
    return proc


def fetch_repo(source: Source, out_dir: Path, limit: int = 0) -> List[Path]:
    """Blobless, sparse, depth-1 checkout of only the configured paths."""
    if not shutil.which("git"):
        raise FetchError("git is not installed")

    work = Path(tempfile.mkdtemp(prefix="rhkb-clone-"))
    clone = work / "repo"
    written: List[Path] = []
    try:
        LOG.info("[%s] cloning %s (%s) - paths: %s", source.id, source.repo, source.ref,
                 ", ".join(source.paths) or "(all)")
        _git(["clone", "--filter=blob:none", "--no-checkout", "--depth", "1",
              "--branch", source.ref, source.repo, str(clone)])
        if source.paths:
            _git(["sparse-checkout", "init", "--cone"], cwd=clone)
            _git(["sparse-checkout", "set", *source.paths], cwd=clone)
        _git(["checkout"], cwd=clone)

        commit = _git(["rev-parse", "--short", "HEAD"], cwd=clone).stdout.strip()

        files = [p for p in sorted(clone.rglob("*"))
                 if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES
                 and ".git" not in p.parts]
        # Honour the path list even when sparse-checkout was skipped or is
        # non-cone (a file pattern rather than a directory).
        if source.paths:
            files = [p for p in files
                     if any(str(p.relative_to(clone)).startswith(prefix.rstrip("/"))
                            for prefix in source.paths)]
        files = [p for p in files if source.matches(str(p.relative_to(clone)))]
        if limit:
            files = files[:limit]

        if not files:
            LOG.warning("[%s] no text files matched %s - check the `paths:` list",
                        source.id, ", ".join(source.paths))

        for path in files:
            rel = path.relative_to(clone)
            try:
                body = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if len(body.strip()) < 120:
                continue
            head = frontmatter({
                "source": source.id,
                "kind": "repo",
                "product": source.label or source.id,
                "repo": source.repo,
                "ref": source.ref,
                "commit": commit,
                "title": rel.name,
                "url": f"{source.repo.rstrip('/')}/blob/{source.ref}/{rel.as_posix()}",
                "path": rel.as_posix(),
                "tokens": est_tokens(body),
            })
            dest = out_dir / source.id / rel.parent / (rel.stem + rel.suffix + ".md"
                                                       if rel.suffix != ".md" else rel.name)
            write_atomic(dest, head + "\n" + _wrap_non_markdown(rel, body))
            written.append(dest)
        LOG.info("[%s] %d file(s) at %s", source.id, len(written), commit)
        return written
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _wrap_non_markdown(rel: Path, body: str) -> str:
    """YAML and AsciiDoc are kept verbatim; YAML gets fenced so it survives."""
    if rel.suffix.lower() in (".yaml", ".yml"):
        return f"```yaml\n{body.rstrip()}\n```\n"
    return body


def fetch(source: Source, raw_dir: Path, sess: Optional[requests.Session] = None,
          limit: int = 0) -> List[Path]:
    if source.type == "redhat-docs":
        return fetch_redhat_docs(source, raw_dir, sess=sess, limit=limit)
    if source.type == "repo":
        return fetch_repo(source, raw_dir, limit=limit)
    raise FetchError(f"unknown source type {source.type!r}")


def digest(path: Path) -> str:
    return sha256_text(path.read_text(encoding="utf-8"))
