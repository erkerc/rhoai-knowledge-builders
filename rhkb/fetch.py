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
from .html2md import make_soup, page_title, to_markdown
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


def list_guides(sess: requests.Session, product: str, version: str) -> List[str]:
    soup: BeautifulSoup = make_soup(get_html(sess, f"{DOC_ROOT}/{product}/{version}", timeout=30))
    slugs: List[str] = []
    for anchor in soup.find_all("a", href=True):
        match = GUIDE_RE.search(anchor["href"])
        if not match or match.group("product").lower() != product.lower():
            continue
        guide = match.group("slug")
        if guide.lower() not in SKIP_SLUGS and guide not in slugs:
            slugs.append(guide)
    return sorted(slugs)


def fetch_redhat_docs(source: Source, out_dir: Path, sess: Optional[requests.Session] = None,
                      limit: int = 0) -> List[Path]:
    sess = sess or session()
    version, display = resolve_version(sess, source.product, source.version)
    LOG.info("[%s] %s %s", source.id, display, version)

    guides = list_guides(sess, source.product, version)
    if source.include:
        wanted = [g for g in guides if g in source.include]
        missing = [g for g in source.include if g not in guides]
        if missing:
            LOG.warning("[%s] not in %s %s: %s", source.id, display, version, ", ".join(missing))
        guides = wanted
    if limit:
        guides = guides[:limit]
    LOG.info("[%s] %d guide(s)", source.id, len(guides))

    written: List[Path] = []
    for guide in guides:
        url = f"{DOC_ROOT}/{source.product}/{version}/html-single/{guide}/index"
        try:
            html = get_html(sess, url)
            soup = make_soup(html)
            title = page_title(soup) or guide.replace("_", " ").title()
            body = to_markdown(html)
        except (FetchError, requests.RequestException) as exc:
            LOG.warning("[%s] %s: %s", source.id, guide, exc)
            continue
        if len(body) < 400:
            LOG.warning("[%s] %s: extracted almost nothing, skipping", source.id, guide)
            continue

        head = frontmatter({
            "source": source.id,
            "kind": "product-docs",
            "product": display,
            "version": version,
            "title": title,
            "url": url,
            "tokens": est_tokens(body),
        })
        dest = out_dir / source.id / version / f"{slug(guide)}.md"
        write_atomic(dest, head + "\n" + body)
        written.append(dest)
        LOG.debug("[%s] wrote %s (%s tokens)", source.id, dest.name, est_tokens(body))
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
