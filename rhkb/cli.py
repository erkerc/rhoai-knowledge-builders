"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__
from .catalog import Catalog, Source, load_catalog, set_enabled
from .fetch import FetchError, fetch, session
from .state import CHANGED, DONE, PENDING, SKIPPED, State
from .units import Unit, iter_units
from .util import LOG, est_tokens, human_tokens, setup_logging
from .wiki import append_log, build_brief, ensure_scaffold, pack_session, read_index

EPILOG = """\
typical flow:
  rhkb sources                     # what is available, what is on
  rhkb enable vllm kserve          # pick what you want
  rhkb fetch                       # pull enabled sources into raw/
  rhkb plan                        # how much material, how many sessions
  rhkb next > brief.md             # one context-sized ingest brief
  rhkb done <unit-ids>             # mark them off after the agent ingests
  rhkb status
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rhkb", description="Build a maintained wiki from Red Hat AI docs and upstream repos.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"rhkb {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", "--root", type=Path, default=Path("."),
                        help="project root (default: current directory)")
    common.add_argument("--catalog", type=Path, default=None, help="path to sources.yaml")
    common.add_argument("-s", "--source", action="append", metavar="ID",
                        help="source id, tier (core/platform/upstream/extra), 'docs', 'repo', "
                             "'all' or 'enabled' (repeatable, comma-separated)")
    common.add_argument("-v", "--verbose", action="count", default=0)
    common.add_argument("-q", "--quiet", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("sources", parents=[common], help="list configured sources")
    listing.add_argument("--json", action="store_true")

    for name, help_text in (("enable", "turn sources on"), ("disable", "turn sources off")):
        toggler = sub.add_parser(name, parents=[common], help=help_text)
        toggler.add_argument("ids", nargs="+", metavar="ID")

    getter = sub.add_parser("fetch", parents=[common], help="download enabled sources into raw/")
    getter.add_argument("--limit", type=int, default=0, help="cap items per source (for a trial run)")
    getter.add_argument("--dry-run", action="store_true")

    planner = sub.add_parser("plan", parents=[common], help="show ingest volume and session estimate")
    planner.add_argument("--json", action="store_true")

    nxt = sub.add_parser("next", parents=[common], help="print one ingest brief")
    nxt.add_argument("-n", "--units", type=int, default=0, help="force a unit count")
    nxt.add_argument("--budget", type=int, default=0, help="session token budget")
    nxt.add_argument("--out", type=Path, help="write the brief to a file instead of stdout")

    doner = sub.add_parser("done", parents=[common], help="mark unit ids as ingested")
    doner.add_argument("ids", nargs="+", metavar="UNIT_ID")
    doner.add_argument("--pages", action="append", default=[], help="wiki pages the units touched")

    skipper = sub.add_parser("skip", parents=[common], help="mark unit ids as not worth ingesting")
    skipper.add_argument("ids", nargs="+", metavar="UNIT_ID")
    skipper.add_argument("--note", default="")

    sub.add_parser("status", parents=[common], help="ingest progress")

    resetter = sub.add_parser("reset", parents=[common], help="forget ingest state for a prefix")
    resetter.add_argument("prefix", nargs="?", default="", help="e.g. 'vllm:' or a full unit id")
    resetter.add_argument("--yes", action="store_true")

    sub.add_parser("init", parents=[common], help="create raw/, wiki/ and the wiki scaffold")
    return parser


# --------------------------------------------------------------- helpers ---
def catalog_path(args) -> Path:
    if args.catalog:
        return args.catalog
    return args.root / "sources.yaml"


def load(args) -> Catalog:
    path = catalog_path(args)
    if not path.exists():
        raise SystemExit(f"no catalog at {path} - run rhkb from the project root, or pass --catalog")
    return load_catalog(path)


def raw_dir(args) -> Path:
    return args.root / "raw"


def wiki_dir(args) -> Path:
    return args.root / "wiki"


def collect_units(args, catalog: Catalog, sources: Optional[List[Source]] = None) -> List[Unit]:
    chosen = sources if sources is not None else catalog.select(args.source)
    ids = [s.id for s in chosen] if chosen else None
    unit_tokens = catalog.int_default("unit_tokens", 6000)
    return list(iter_units(raw_dir(args), sources=ids, unit_tokens=unit_tokens))


# -------------------------------------------------------------- commands ---
def cmd_sources(args) -> int:
    catalog = load(args)
    if args.json:
        print(json.dumps([s.__dict__ for s in catalog.sources], indent=2))
        return 0
    print(f"{'id':22} {'kind':5} {'tier':9} {'on':3} label")
    print("-" * 78)
    for tier in ("core", "platform", "upstream", "extra"):
        for source in [s for s in catalog.sources if s.tier == tier]:
            mark = "yes" if source.enabled else ""
            print(f"{source.id:22} {source.kind_label:5} {source.tier:9} {mark:3} {source.label}")
    on = len(catalog.enabled())
    print(f"\n{on} of {len(catalog.sources)} enabled. "
          f"rhkb enable <id> ... / rhkb disable <id> ...")
    return 0


def cmd_toggle(args, value: bool) -> int:
    path = catalog_path(args)
    if not path.exists():
        raise SystemExit(f"no catalog at {path}")
    changed = set_enabled(path, args.ids, value)
    if changed:
        print(f"{'enabled' if value else 'disabled'}: {', '.join(changed)}")
    else:
        print("nothing changed")
    return 0 if changed else 1


def cmd_init(args) -> int:
    raw_dir(args).mkdir(parents=True, exist_ok=True)
    ensure_scaffold(wiki_dir(args))
    print(f"ready: {raw_dir(args)}/ and {wiki_dir(args)}/")
    return 0


def cmd_fetch(args) -> int:
    catalog = load(args)
    sources = catalog.select(args.source)
    if not sources:
        raise SystemExit("no sources selected - enable some (rhkb sources) or pass --source")

    if args.dry_run:
        for source in sources:
            target = (f"{source.product} {source.version}" if source.type == "redhat-docs"
                      else f"{source.repo}@{source.ref} paths={','.join(source.paths) or 'all'}")
            print(f"{source.id:22} {target}")
        return 0

    raw = raw_dir(args)
    raw.mkdir(parents=True, exist_ok=True)
    sess = session()
    total, failures = 0, []
    for source in sources:
        try:
            written = fetch(source, raw, sess=sess, limit=args.limit)
            total += len(written)
        except FetchError as exc:
            LOG.error("[%s] %s", source.id, exc)
            failures.append(source.id)
        except Exception as exc:  # one bad source must not stop the rest
            LOG.error("[%s] unexpected: %s", source.id, exc)
            LOG.debug("", exc_info=True)
            failures.append(source.id)

    print(f"\n{total} file(s) under {raw}")
    if failures:
        print(f"failed: {', '.join(failures)}")
    return 2 if failures else 0


def cmd_plan(args) -> int:
    catalog = load(args)
    units = collect_units(args, catalog)
    if not units:
        raise SystemExit("no units - run `rhkb fetch` first")
    state = State(args.root)
    budget = catalog.int_default("session_tokens", 25000)

    per_source = {}
    for unit in units:
        entry = per_source.setdefault(unit.source, {"units": 0, "tokens": 0, "todo": 0})
        entry["units"] += 1
        entry["tokens"] += unit.tokens
        if state.status_of(unit) in (PENDING, CHANGED):
            entry["todo"] += 1

    if args.json:
        print(json.dumps(per_source, indent=2))
        return 0

    print(f"{'source':22} {'units':>6} {'todo':>6} {'tokens':>9}")
    print("-" * 47)
    for source_id, entry in sorted(per_source.items()):
        print(f"{source_id:22} {entry['units']:>6} {entry['todo']:>6} "
              f"{human_tokens(entry['tokens']):>9}")
    outstanding = state.outstanding(units)
    todo_tokens = sum(u.tokens for u in outstanding)
    sessions = (todo_tokens // budget) + 1 if todo_tokens else 0
    print("-" * 47)
    print(f"{'total':22} {len(units):>6} {len(outstanding):>6} "
          f"{human_tokens(sum(u.tokens for u in units)):>9}")
    print(f"\n{len(outstanding)} unit(s) outstanding, {human_tokens(todo_tokens)} tokens, "
          f"~{sessions} session(s) at {human_tokens(budget)} each")
    biggest = max(units, key=lambda u: u.tokens)
    print(f"largest unit: {human_tokens(biggest.tokens)} tokens - {biggest.label[:60]}")
    return 0


def cmd_next(args) -> int:
    catalog = load(args)
    units = collect_units(args, catalog)
    if not units:
        raise SystemExit("no units - run `rhkb fetch` first")
    state = State(args.root)
    outstanding = state.outstanding(units)
    if not outstanding:
        print("nothing outstanding - everything is ingested", file=sys.stderr)
        return 1

    budget = args.budget or catalog.int_default("session_tokens", 25000)
    batch = (outstanding[:args.units] if args.units
             else pack_session(outstanding, budget))

    ensure_scaffold(wiki_dir(args))
    brief = build_brief(batch, read_index(wiki_dir(args)), wiki_dir(args), budget)

    if args.out:
        args.out.write_text(brief, encoding="utf-8")
        print(f"{args.out}: {len(batch)} unit(s), ~{human_tokens(est_tokens(brief))} tokens",
              file=sys.stderr)
    else:
        print(brief)
        print(f"[{len(batch)} unit(s), ~{human_tokens(est_tokens(brief))} tokens; "
              f"{len(outstanding) - len(batch)} still outstanding]", file=sys.stderr)
    return 0


def _resolve_ids(state: State, units: List[Unit], ids: Sequence[str]) -> List[Unit]:
    by_id = {u.id: u for u in units}
    resolved, missing = [], []
    for wanted in ids:
        if wanted in by_id:
            resolved.append(by_id[wanted])
        else:
            matches = [u for u in units if u.id.endswith(wanted) or wanted in u.id]
            if len(matches) == 1:
                resolved.append(matches[0])
            elif len(matches) > 1:
                LOG.warning("%r matches %d units - be more specific", wanted, len(matches))
            else:
                missing.append(wanted)
    for wanted in missing:
        LOG.warning("no unit %r", wanted)
    return resolved


def cmd_done(args) -> int:
    catalog = load(args)
    units = collect_units(args, catalog, sources=catalog.sources)
    state = State(args.root)
    resolved = _resolve_ids(state, units, args.ids)
    for unit in resolved:
        state.mark_done(unit, pages=args.pages)
    state.save()
    if resolved:
        append_log(wiki_dir(args), "ingest",
                   f"{len(resolved)} unit(s)",
                   "\n".join(f"- {u.label}" for u in resolved[:12]))
    print(f"marked {len(resolved)} unit(s) done")
    return 0 if resolved else 1


def cmd_skip(args) -> int:
    catalog = load(args)
    units = collect_units(args, catalog, sources=catalog.sources)
    state = State(args.root)
    resolved = _resolve_ids(state, units, args.ids)
    for unit in resolved:
        state.mark_skipped(unit, note=args.note)
    state.save()
    print(f"skipped {len(resolved)} unit(s)")
    return 0 if resolved else 1


def cmd_status(args) -> int:
    catalog = load(args)
    units = collect_units(args, catalog, sources=catalog.sources)
    state = State(args.root)
    tally = {PENDING: 0, DONE: 0, CHANGED: 0, SKIPPED: 0}
    for unit in units:
        tally[state.status_of(unit)] = tally.get(state.status_of(unit), 0) + 1
    total = len(units) or 1
    print(f"units      {len(units)}")
    for key in (DONE, CHANGED, PENDING, SKIPPED):
        print(f"  {key:9} {tally.get(key, 0):>5}  {tally.get(key, 0) * 100 // total:>3}%")
    pages = len(list((wiki_dir(args) / "pages").glob("*.md"))) if (wiki_dir(args) / "pages").exists() else 0
    print(f"wiki pages {pages}")
    if tally.get(CHANGED):
        print(f"\n{tally[CHANGED]} unit(s) changed upstream since ingest - `rhkb next` picks them up")
    return 0


def cmd_reset(args) -> int:
    state = State(args.root)
    target = args.prefix or "everything"
    if not args.yes:
        print(f"this forgets ingest state for {target}; re-run with --yes")
        return 1
    count = state.reset(args.prefix)
    state.save()
    print(f"reset {count} record(s)")
    return 0


COMMANDS = {
    "sources": cmd_sources,
    "enable": lambda a: cmd_toggle(a, True),
    "disable": lambda a: cmd_toggle(a, False),
    "init": cmd_init,
    "fetch": cmd_fetch,
    "plan": cmd_plan,
    "next": cmd_next,
    "done": cmd_done,
    "skip": cmd_skip,
    "status": cmd_status,
    "reset": cmd_reset,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.quiet)
    try:
        return COMMANDS[args.command](args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `rhkb next | head` and friends - exit quietly rather than dumping a traceback.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
