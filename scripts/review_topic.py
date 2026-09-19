#!/usr/bin/env python3
"""Was a finished research topic done properly? Read-only, offline.

The companion to ``round_status.py``, which answers "is the round running
right now safe to trust?" over a live session. This one answers "was the
whole topic done properly?" from the folder alone: no network, no session,
and it still works after the run logs are gone -- which they were twice on
2026-09-17, wiped by a reboot.

    python3 scripts/review_topic.py \
        <programme>/research/06-item-state-and-temporal-reasoning

The artifact meanings were probed against a finished topic rather than
assumed, because a first version reported two false alarms on a clean one:

    shortlist.json / screened.pre_admission.jsonl   this round's candidates
    screen/admission.json decisions[].decision      ADMIT or REJECT, this round
    screen/admission_rejected.jsonl                 rejected this round
    screen/screened.jsonl                           the CUMULATIVE corpus
    analysis/<paper_id>_analysis.json               one per corpus paper

Six invariants, in the order they matter:

 1. Every candidate was scored.
 2. Everything shortlisted was judged.
 3. This round's ADMIT count equals the corpus growth.
 4. Every admitted paper is in the corpus.
 5. Every paper in the corpus has an analysis, or a recorded write-off. This
    is the one msgloom Topic 04 round 2 broke silently: it admitted 29
    papers, read 13, and then synthesised, reviewed and reported as though
    nothing were missing.
 6. No study appears twice under two identifiers. Identifier checks are blind
    to this, which is why it needs its own pass: Topic 08 was rejected for
    citing one paper as two corroborating sources, and Topic 04 was already
    closed and reported with 47 papers when it has 45.

A check that cries wolf gets ignored, which is how that round stayed
invisible, so rounds that have not reached the screen stage are skipped and
a conversation left at ``sent`` is reported as uncollected, not as failed.

Exit 0 when nothing is wrong, 1 when any invariant is broken.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import sys
from datetime import datetime

OK = "  ok"
BAD = "  !!"


def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def count_lines(path: pathlib.Path) -> int:
    if not path.is_file():
        return 0
    return len([x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()])


def rounds_of(topic: pathlib.Path) -> list[tuple[int, pathlib.Path]]:
    """Run directories in round order. The names do not sort that way."""
    found = []
    runs = topic / "runs"
    if runs.is_dir():
        for run in runs.iterdir():
            if not run.is_dir():
                continue
            ctx = read_json(run / "round_context.json") or {}
            found.append((int(ctx.get("round", 0)), run))
    return sorted(found)


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            with contextlib.suppress(json.JSONDecodeError):
                rows.append(json.loads(line))
    return rows


def ident(row: dict) -> str:
    for key in ("id", "paper_id", "arxiv_id", "doi"):
        if row.get(key):
            return str(row[key])
    return str(row.get("title", ""))[:60]


def duplicate_titles(rows: list[dict]) -> list[list[str]]:
    """Groups of corpus rows that are the same study under different ids.

    Case and whitespace only, the same normalisation the search gate uses.
    The pairs seen so far all look alike: a bare `manual-` row written by the
    model's own search, and the same work admitted again in a later round
    once a lookup supplied its DOI.
    """
    seen: dict[str, list[str]] = {}
    for row in rows:
        title = " ".join(str(row.get("title") or "").lower().split())
        if title:
            seen.setdefault(title, []).append(ident(row))
    return [ids for ids in seen.values() if len(ids) > 1]


def written_off(run: pathlib.Path) -> dict[str, str]:
    """Papers a worker declared unreadable, mapped to the reason it gave."""
    data = read_json(run / "analysis" / "skipped.json")
    if not isinstance(data, list):
        return {}
    return {
        str(item["paper_id"]): str(item.get("reason") or "no reason given")
        for item in data
        if isinstance(item, dict) and item.get("paper_id")
    }


def integrity(run: pathlib.Path, prior: set[str]) -> tuple[list[str], set[str]]:
    """Check this round against the corpus the previous round left.

    Compares identifiers, not counts. Counting says Topic 08 round 2 lost a
    paper -- it admitted 15 and the corpus grew by 14 -- when in fact one
    admitted paper was already in the corpus from round 1. That is ordinary
    cross-round dedup, and a check that reports it as a loss is a check
    nobody reads.
    """
    lines: list[str] = []
    screen = run / "screen"
    candidates = count_lines(run / "search" / "candidates.jsonl")
    scored = count_lines(screen / "cheap_scores.jsonl")
    shortlist = len(read_json(screen / "shortlist.json") or [])
    decisions = (read_json(screen / "admission.json") or {}).get("decisions") or []
    admitted = {
        str(d.get("paper_id"))
        for d in decisions
        if str(d.get("decision", "")).upper() == "ADMIT"
    }
    corpus = {ident(r) for r in read_jsonl(screen / "screened.jsonl")}
    analyses = {
        p.name.replace("_analysis.json", "")
        for p in (run / "analysis").glob("*_analysis.json")
    }
    fresh = admitted - prior
    already = admitted & prior

    lines.append(
        f"    {candidates:3d} candidates -> {scored:3d} scored -> {shortlist:3d} "
        f"shortlisted -> {len(admitted):2d} admitted"
        + (f" ({len(already)} already in the corpus)" if already else "")
    )
    lines.append(
        f"    corpus {len(prior):3d} -> {len(corpus):3d}   analyses {len(analyses):3d}"
    )
    lines.append(
        (OK if scored == candidates else BAD)
        + f" every candidate scored ({scored}/{candidates})"
    )
    lines.append(
        (OK if len(decisions) == shortlist else BAD)
        + f" everything shortlisted judged ({len(decisions)}/{shortlist})"
    )
    lost = admitted - corpus
    lines.append(
        (OK if not lost else BAD)
        + f" every admitted paper is in the corpus ({len(admitted) - len(lost)}"
        f"/{len(admitted)})" + (f"  missing {sorted(lost)[:4]}" if lost else "")
    )
    grew = corpus - prior
    lines.append(
        (OK if grew == fresh else BAD)
        + f" the corpus grew by exactly the new admissions (+{len(grew)} vs "
        f"{len(fresh)})"
    )
    # A paper the system declared unreadable is settled, not missing:
    # analysis/skipped.json is how make_shards.py stops re-sharding it. It is
    # still reported, because a written-off paper is a paper not read, and a
    # topic that claims "none written off" should have to prove it.
    skipped = written_off(run)
    # Two records of one study inflate the corpus and let a finding cite
    # itself as corroboration. Identifier checks are blind to it: the ids
    # genuinely differ. Topic 08 was rejected for exactly this, and a scan
    # then found two more pairs in Topic 04, already closed and reported.
    pairs = duplicate_titles(read_jsonl(screen / "screened.jsonl"))
    lines.append(
        (OK if not pairs else BAD)
        + f" no study appears twice ({len(corpus) - sum(len(g) - 1 for g in pairs)}"
        f" distinct of {len(corpus)})"
    )
    for group in pairs:
        lines.append(f"     same study: {group}")

    unread = corpus - analyses - set(skipped)
    lines.append(
        (OK if not unread else BAD) + f" every corpus paper analysed or written off "
        f"({len(analyses & corpus)}/{len(corpus)})"
        + (f"  unread {sorted(unread)[:4]}" if unread else "")
    )
    for paper, reason in sorted(skipped.items()):
        if paper in corpus:
            lines.append(f"     written off: {paper} -- {reason[:70]}")
    return lines, corpus


def reviews(run: pathlib.Path) -> list[str]:
    lines = []
    for path in sorted((run / "review").glob("synthesis_review.a*.json")):
        data = read_json(path) or {}
        s = data.get("scores") or {}
        attempt = path.name.split(".")[1]
        lines.append(
            f"    {attempt}  {data.get('status', '?')!s:22} "
            f"faith {s.get('faithfulness', '-')}  coh {s.get('coherence', '-')}  "
            f"gaps {s.get('gap_completeness', '-')}  "
            f"{len(data.get('issues') or [])} issue(s)"
        )
    return lines or ["    (no verdict on disk)"]


def gaps(topic: pathlib.Path) -> list[str]:
    lines = []
    for path in sorted((topic / "chatgpt").glob("r*-gaps.json")):
        data = read_json(path) or {}
        items = data.get("gaps") or []
        kinds: dict[str, int] = {}
        for gap in items:
            key = str(gap.get("classification", "?"))
            kinds[key] = kinds.get(key, 0) + 1
        conv = data.get("convergence") or {}
        lines.append(
            f"    {path.stem:10} {len(items):2d} gap(s)  {dict(sorted(kinds.items()))}"
            f"  continue={conv.get('should_continue')} ({conv.get('reason')})"
        )
    return lines or ["    (no gap classification on disk)"]


def cost(topic: pathlib.Path) -> list[str]:
    ledger = read_json(topic / "chatgpt" / "conversations.json") or []
    by_round: dict[int, list[dict]] = {}
    for record in ledger:
        by_round.setdefault(int(record.get("round", 0)), []).append(record)
    lines = []
    for rnd in sorted(by_round):
        records = by_round[rnd]
        stamps = sorted(
            datetime.fromisoformat(r["sent_at"]).timestamp()
            for r in records
            if r.get("sent_at")
        )
        span = (stamps[-1] - stamps[0]) / 60 if len(stamps) > 1 else 0.0
        # "sent" means the prompt went out and the reply was not collected
        # by the orchestrator: in flight while the round runs, and worth a
        # look once it is over. "failed" is the one that always matters.
        failed = [r for r in records if r.get("status") == "failed"]
        sent = [r for r in records if r.get("status") == "sent"]
        note = ""
        if failed:
            note += f"   {BAD.strip()} {len(failed)} failed"
        if sent:
            note += f"   ({len(sent)} sent, reply not collected here)"
        lines.append(
            f"    round {rnd}: {len(records):2d} conversation(s), "
            f"{span:5.1f} min first->last" + note
        )
    return lines or ["    (no conversation ledger)"]


def main(folders: list[str]) -> int:
    problems = 0
    for folder in folders:
        topic = pathlib.Path(folder).expanduser().resolve()
        print(f"\n{'=' * 74}\n{topic.name}\n{'=' * 74}")
        corpus: set[str] = set()
        for rnd, run in rounds_of(topic):
            if not (run / "screen" / "admission.json").is_file():
                # A round still in its search steps has nothing to account
                # for yet. Reporting it as "corpus shrank by 14" is how a
                # check teaches its reader to ignore it.
                print(f"\n  round {rnd}  ({run.name})  -- still running, skipped")
                continue
            print(f"\n  round {rnd}  ({run.name})")
            lines, corpus = integrity(run, corpus)
            for line in lines:
                problems += line.startswith(BAD)
                print(line)
            print("    review:")
            for line in reviews(run):
                print(line)
        print("\n  gaps per round")
        for line in gaps(topic):
            print(line)
        print("\n  conversations and duration")
        for line in cost(topic):
            problems += BAD.strip() in line
            print(line)
    print(f"\n{'=' * 74}")
    if problems:
        print(f"{problems} PROBLEM(S) -- read the !! lines")
        return 1
    print("no integrity problem found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["."]))
