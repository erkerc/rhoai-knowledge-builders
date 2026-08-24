"""The wiki side: scaffolding, the index slice, and session briefs.

The context-saving trick lives in `build_brief`. An ingest session gets:
  - the schema (CLAUDE.md), which the agent reads once from disk anyway
  - a *slice* of the index - only pages related to the units in hand
  - a handful of units, chosen to fit a token budget

Not the whole wiki, not a whole guide, not every page in the index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set

from .units import Unit
from .util import LOG, est_tokens, human_tokens, split_frontmatter, write_atomic

INDEX_NAME = "index.md"
LOG_NAME = "log.md"

INDEX_HEADER = """# Wiki index

One line per page: `- [[slug]] (type) - summary [products/versions]`.
The agent updates this on every ingest. Read this before opening any page.

"""

LOG_HEADER = """# Log

Append-only. One entry per operation, newest at the bottom.
Prefix format: `## [YYYY-MM-DD] <op> | <subject>` so it greps cleanly.

"""

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "you", "are", "not",
    "can", "how", "use", "using", "about", "when", "where", "which", "what", "into",
    "must", "have", "has", "was", "were", "will", "should", "may", "its", "it's",
    "red", "hat", "openshift", "cluster", "clusters", "configure", "configuring",
    "create", "creating", "following", "example", "see", "also", "more", "than",
    "page", "guide", "chapter", "section", "documentation", "docs", "procedure",
}
WORD_RE = re.compile(r"[a-zA-Z][\w.-]{2,}")


def ensure_scaffold(wiki_dir: Path) -> None:
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "pages").mkdir(exist_ok=True)
    index = wiki_dir / INDEX_NAME
    if not index.exists():
        write_atomic(index, INDEX_HEADER)
    log = wiki_dir / LOG_NAME
    if not log.exists():
        write_atomic(log, LOG_HEADER)


def append_log(wiki_dir: Path, op: str, subject: str, detail: str = "") -> None:
    entry = f"\n## [{date.today().isoformat()}] {op} | {subject}\n"
    if detail:
        entry += f"\n{detail.strip()}\n"
    log = wiki_dir / LOG_NAME
    if not log.exists():
        ensure_scaffold(wiki_dir)
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(entry)


# --------------------------------------------------------------- index ----
@dataclass
class IndexEntry:
    slug: str
    line: str

    def keywords(self) -> Set[str]:
        return _keywords(self.line)


def read_index(wiki_dir: Path) -> List[IndexEntry]:
    path = wiki_dir / INDEX_NAME
    if not path.exists():
        return []
    entries: List[IndexEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.search(r"\[\[([^\]]+)\]\]", line)
        if match and line.strip().startswith("-"):
            entries.append(IndexEntry(slug=match.group(1).strip(), line=line.strip()))
    return entries


def _keywords(text: str) -> Set[str]:
    return {w.lower() for w in WORD_RE.findall(text) if w.lower() not in STOPWORDS}


def index_slice(entries: Sequence[IndexEntry], units: Sequence[Unit], limit: int = 25) -> List[IndexEntry]:
    """The index lines plausibly related to these units, best first."""
    if not entries:
        return []
    wanted: Set[str] = set()
    for unit in units:
        wanted |= _keywords(unit.label)
        wanted |= _keywords(unit.body[:1500])
    scored = []
    for entry in entries:
        overlap = len(entry.keywords() & wanted)
        if overlap:
            scored.append((overlap, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].slug))
    return [entry for _, entry in scored[:limit]]


# --------------------------------------------------------------- brief ----
BRIEF_INTRO = """# Ingest session

You are maintaining the wiki described in `CLAUDE.md`. Read that file first if
you have not already in this session.

Ingest the units below **in order**. For each one: identify the components,
procedures and errors it describes; check the related index entries for pages
that already cover them; update those pages and create only what is genuinely
missing; add cross-references in both directions; update `{index}`; append one
entry to `{log}`.

Do not read other files under `raw/` unless a unit is unusable on its own - if
that happens, say so rather than guessing. When you are done, list the unit ids
you completed so they can be marked off:

    rhkb done {ids}
"""


def build_brief(units: Sequence[Unit], entries: Sequence[IndexEntry],
                wiki_dir: Path, session_tokens: int) -> str:
    slice_ = index_slice(entries, units)
    ids = " ".join(u.id for u in units)
    parts = [BRIEF_INTRO.format(index=INDEX_NAME, log=LOG_NAME,
                                ids=ids if len(ids) < 200 else "<unit-ids>")]

    if slice_:
        parts.append("## Related index entries\n\n"
                     + "\n".join(e.line for e in slice_)
                     + f"\n\n({len(slice_)} of {len(entries)} pages shown - "
                       "open only the ones you need.)\n")
    elif entries:
        parts.append("## Related index entries\n\nNone matched. "
                     "Check the index yourself before creating pages.\n")
    else:
        parts.append("## Related index entries\n\nThe wiki is empty - this is the first ingest.\n")

    parts.append("## Units\n")
    for unit in units:
        parts.append(unit.render())

    brief = "\n".join(parts)
    total = est_tokens(brief)
    if total > session_tokens * 1.2:
        LOG.warning("brief is %s tokens, above the %s budget",
                    human_tokens(total), human_tokens(session_tokens))
    return brief


def pack_session(units: Sequence[Unit], session_tokens: int, max_units: int = 8) -> List[Unit]:
    """Greedily fill one session, keeping units from the same document together."""
    chosen: List[Unit] = []
    budget = session_tokens
    for unit in units:
        if chosen and (unit.tokens > budget or len(chosen) >= max_units):
            break
        chosen.append(unit)
        budget -= unit.tokens
    return chosen or list(units[:1])
