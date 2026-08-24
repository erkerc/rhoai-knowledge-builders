"""Splitting raw documents into ingest units.

The whole point of this module: never hand an agent a whole guide. An RHOAI
serving guide is 40k+ tokens; OpenShift's networking guide is worse. A unit is
a heading subtree capped at `unit_tokens`, carrying enough breadcrumb context
to be understood alone, so one ingest session reads a few units instead of one
enormous document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .util import est_tokens, sha256_text, slug, split_frontmatter

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass
class Unit:
    id: str
    source: str
    doc: str                 # raw/ path this came from
    title: str               # heading text
    breadcrumb: List[str]    # ancestor headings, outermost first
    body: str
    tokens: int
    url: str = ""
    product: str = ""
    version: str = ""
    kind: str = ""
    part: int = 1
    parts: int = 1
    sha: str = ""
    meta: Dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        trail = " > ".join(self.breadcrumb + [self.title]) if self.breadcrumb else self.title
        suffix = f" ({self.part}/{self.parts})" if self.parts > 1 else ""
        return f"{trail}{suffix}"

    def render(self) -> str:
        """What the agent actually reads."""
        lines = [
            f"### Ingest unit `{self.id}`",
            "- source: " + " ".join(x for x in [self.source and f"{self.source}",
                                                 self.product and f"({self.product}"
                                                 + (f" {self.version})" if self.version else ")")] if x),
            f"- document: {self.doc}",
            f"- section: {self.label}",
        ]
        if self.url:
            lines.append(f"- url: {self.url}")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(self.body.strip())
        return "\n".join(lines) + "\n"


def _split_blocks(text: str) -> List[Tuple[Optional[int], str]]:
    """Return (heading_level or None, line) with fenced code protected."""
    out: List[Tuple[Optional[int], str]] = []
    in_fence = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            out.append((None, line))
            continue
        if in_fence:
            out.append((None, line))
            continue
        match = HEADING_RE.match(line)
        out.append((len(match.group(1)) if match else None, line))
    return out


def _sections(text: str, max_level: int = 3) -> List[Tuple[List[str], str, str]]:
    """Cut a document into (breadcrumb, title, body) at headings <= max_level."""
    lines = _split_blocks(text)
    sections: List[Tuple[List[str], str, str]] = []
    stack: List[Tuple[int, str]] = []
    current_title = ""
    current_crumb: List[str] = []
    buffer: List[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append((list(current_crumb), current_title, body))

    for level, line in lines:
        if level is not None and level <= max_level:
            flush()
            buffer = []
            while stack and stack[-1][0] >= level:
                stack.pop()
            title = HEADING_RE.match(line).group(2).strip()  # type: ignore[union-attr]
            current_crumb = [t for _, t in stack]
            current_title = title
            stack.append((level, title))
        else:
            buffer.append(line)
    flush()
    return sections


def _hard_split(text: str, budget_chars: int) -> List[str]:
    """Split text that has no paragraph breaks: on lines, then on characters.

    Without this a single enormous paragraph (a long table, a wall of prose with
    no blank lines) silently blows the token budget it was supposed to respect.
    """
    if len(text) <= budget_chars:
        return [text]
    pieces: List[str] = []
    current: List[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        while len(line) > budget_chars:          # one line longer than the budget
            if current:
                pieces.append("".join(current))
                current, size = [], 0
            pieces.append(line[:budget_chars])
            line = line[budget_chars:]
        if size and size + len(line) > budget_chars:
            pieces.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        pieces.append("".join(current))
    return [p for p in pieces if p.strip()]


def _chunk(body: str, budget_chars: int) -> List[str]:
    """Split an oversized section on paragraph breaks, never inside a code fence."""
    if len(body) <= budget_chars:
        return [body]
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    in_fence = False
    for para in body.split("\n\n"):
        fences = len(FENCE_RE.findall(para))
        if size and size + len(para) > budget_chars and not in_fence:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(para)
        size += len(para) + 2
        if fences % 2:
            in_fence = not in_fence
    if current:
        chunks.append("\n\n".join(current))

    # A paragraph can still be bigger than the budget on its own.
    out: List[str] = []
    for chunk in chunks:
        out.extend(_hard_split(chunk, budget_chars) if len(chunk) > budget_chars else [chunk])

    # Fold slivers into a neighbour rather than emitting a 5-token "unit".
    sliver = max(200, budget_chars // 10)
    folded: List[str] = []
    for piece in out:
        # Folding may overshoot the budget by up to one sliver (~10%); a unit
        # slightly over budget beats a unit containing four words.
        if folded and (len(piece) < sliver or len(folded[-1]) < sliver) \
                and len(folded[-1]) + len(piece) <= budget_chars + sliver:
            folded[-1] = folded[-1].rstrip() + "\n\n" + piece.lstrip()
        else:
            folded.append(piece)
    return [p for p in folded if p.strip()]


def units_for_document(path: Path, raw_root: Path, unit_tokens: int = 6000) -> List[Unit]:
    text = path.read_text(encoding="utf-8")
    meta, body = split_frontmatter(text)
    rel = path.relative_to(raw_root).as_posix()
    source = meta.get("source", path.parts[len(raw_root.parts)] if len(path.parts) > len(raw_root.parts) else "")
    budget_chars = int(unit_tokens * 4)

    sections = _sections(body)
    doc_title = meta.get("title") or path.stem
    sections = [(c, t or doc_title, b) for c, t, b in sections]
    if not sections:
        sections = [([], meta.get("title") or path.stem, body)]

    # Merge tiny adjacent sections so we don't emit 200-token fragments.
    stub = max(120, unit_tokens // 20)
    merged: List[Tuple[List[str], str, str]] = []
    carried: Optional[Tuple[List[str], str, str]] = None
    for crumb, title, chunk in sections:
        if carried is not None:                       # a stub was waiting for a home
            crumb, title, chunk = crumb, title, f"{carried[2]}\n\n{chunk}"
            carried = None
        size = est_tokens(chunk)
        if merged and est_tokens(merged[-1][2]) + size < unit_tokens // 3:
            prev_crumb, prev_title, prev_body = merged[-1]
            merged[-1] = (prev_crumb, prev_title, f"{prev_body}\n\n## {title}\n\n{chunk}")
        elif not merged and size < stub:
            carried = (crumb, title, f"## {title}\n\n{chunk}" if title else chunk)
        else:
            merged.append((crumb, title, chunk))
    if carried is not None:                            # nothing followed it
        merged.append(carried)

    units: List[Unit] = []
    for crumb, title, chunk in merged:
        pieces = _chunk(chunk, budget_chars)
        for index, piece in enumerate(pieces, start=1):
            title_slug = slug(title) if title else "body"
            base = f"{rel}:{title_slug}"
            unit_id = base if len(pieces) == 1 else f"{base}#{index}"
            units.append(Unit(
                id=unit_id,
                source=source,
                doc=rel,
                title=title or meta.get("title", path.stem),
                breadcrumb=crumb,
                body=piece,
                tokens=est_tokens(piece),
                url=meta.get("url", ""),
                product=meta.get("product", ""),
                version=meta.get("version", ""),
                kind=meta.get("kind", ""),
                part=index,
                parts=len(pieces),
                sha=sha256_text(piece),
                meta=meta,
            ))
    return units


def iter_units(raw_root: Path, sources: Optional[List[str]] = None,
               unit_tokens: int = 6000) -> Iterator[Unit]:
    if not raw_root.exists():
        return
    for path in sorted(raw_root.rglob("*.md")):
        rel_parts = path.relative_to(raw_root).parts
        if sources and (not rel_parts or rel_parts[0] not in sources):
            continue
        for unit in units_for_document(path, raw_root, unit_tokens=unit_tokens):
            yield unit
