#!/usr/bin/env python3
"""Where a research round actually stands. Read-only by default.

Prints what the round admitted, what it has read, and whether each in-flight
worker reply is finished and waiting to be collected. Run it after any
interrupt, and before trusting a round's report.

    python3 scripts/round_status.py <workdir>   # switches to the skill's .venv

Exit 0 when the round has read everything it admitted, 1 when it has not.

``--collect`` looks past the current round: a ledger entry stuck at status
"sent" from an *earlier* round can never be resumed by the orchestrator
(``--resume`` only picks up a "sent" entry whose round matches the round the
run is currently on and whose prompt fingerprint still matches), so its
reply, if it still exists, can only be archived as evidence. Every "sent"
entry is classified into exactly one of four verdicts -- "resumable",
"superseded", "collectable", "lost" -- and reported with the action to take.
Dry run unless ``--apply`` is also given, which archives a "collectable"
reply and reclassifies a "superseded" or "lost" entry in the ledger. Exit 0
when nothing needed collecting or everything succeeded, 1 when a fetch or a
write failed, 2 when refused (an orchestrator is running, or the ledger
changed on disk since it was read).
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import chatgpt_client as cc
import round_state as cr
from _common import ensure_venv, open_session
from read_chat import turn_facts

TRANSCRIPTS_DIRNAME = "transcripts"


# ---------------------------------------------------------------------------
# argument parsing -- parse_args stays "just the workdir" (tests/test_cli.py
# relies on that exact contract); parse_collect_args is the fuller parse
# --collect actually needs, over the same options.
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workdir", help="a research topic's working directory")
    ap.add_argument(
        "--collect",
        action="store_true",
        help=(
            "classify every ledger entry stuck at status 'sent' -- "
            "resumable, superseded, collectable or lost -- and report the "
            "action to take; dry run unless --apply"
        ),
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help=(
            "with --collect: archive a collectable reply, and reclassify a "
            "superseded or lost entry in the ledger; never touches a "
            "resumable entry; refused while an orchestrator is running or "
            "if the ledger changed on disk since it was read"
        ),
    )
    return ap


def parse_args(argv: list[str] | None = None) -> Path:
    """The one positional argument every invocation needs: the workdir."""
    args = _build_parser().parse_args(argv)
    return Path(args.workdir).expanduser().resolve()


def parse_collect_args(argv: list[str] | None = None) -> argparse.Namespace:
    """``workdir`` (resolved), ``collect`` and ``apply``.

    ``parse_args`` stays the single-purpose function other code already
    depends on for just the workdir; this parses the same options and keeps
    the two this command's ``--collect`` path needs as well.
    """
    args = _build_parser().parse_args(argv)
    args.workdir = Path(args.workdir).expanduser().resolve()
    return args


# ---------------------------------------------------------------------------
# --collect: pure classification
# ---------------------------------------------------------------------------


def successful_twin(
    entry: dict[str, Any], entries: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The entry, if any, that supersedes ``entry``: same job and round,
    already "done".

    That is free evidence the "sent" entry's own reply never needs
    collecting -- across eight real topics, one of the two conversations
    stuck at "sent" was exactly this, the corpse of a retry that then
    succeeded 28 minutes later.
    """
    for other in entries:
        if (
            isinstance(other, dict)
            and other is not entry
            and other.get("job") == entry.get("job")
            and other.get("round") == entry.get("round")
            and other.get("status") == "done"
        ):
            return other
    return None


def classify_sent_entry(
    entry: dict[str, Any],
    current_round: int,
    entries: list[dict[str, Any]],
    *,
    readable: bool,
) -> str:
    """One of "resumable", "superseded", "collectable" or "lost".

    Pure: the only live fact this needs is whether the conversation still
    reads, and that is supplied by the caller as ``readable`` rather than
    fetched here -- the caller is round_status.py's own --collect handling,
    the one place allowed to touch the network, and it only needs to take a
    live read at all for an entry neither of the first two branches below
    already settles. ``readable`` is not inspected in those branches, so a
    caller that has not taken a read yet may pass anything for it.
    """
    if entry.get("round") == current_round:
        # The orchestrator's own --resume still picks this one up: it
        # resumes a "sent" entry only when its round matches the round the
        # run is currently on and the prompt fingerprint still matches.
        return "resumable"
    if successful_twin(entry, entries) is not None:
        return "superseded"
    return "collectable" if readable else "lost"


def longest_reply(conv: dict[str, Any]) -> str:
    """The longest visible assistant reply in ``conv``, or "" when there is
    none.

    "Longest" rather than "last": an interrupted turn can leave more than
    one assistant text message, and ChatGPT also emits interim narration as
    assistant text (SKILL.md), so the reply worth archiving is the finished
    answer, not a short narration line that happens to be the tail.
    """
    msgs = cc.assistant_text_messages(conv)
    if not msgs:
        return ""
    return max((cc.message_text(m) for m in msgs), key=len)


def orphan_transcript(
    entry: dict[str, Any], conv: dict[str, Any], *, archived_at: str
) -> dict[str, Any]:
    """The archive document for one collected orphan reply.

    Same field set the orchestrator's own transcripts carry: job, round,
    conversation_id, model, title, status, problems, turns_sent,
    archived_at, turns. ``model`` and ``turns`` come from the conversation
    itself -- the only place they were ever recorded for a send the
    orchestrator never got to archive; ``problems`` and ``turns_sent`` come
    from the ledger entry, since they describe what this send attempted,
    not what the conversation holds now. ``status`` is always "collected":
    this document only exists because the normal archive never happened.
    """
    facts = turn_facts(conv)
    model = facts[-1]["resolved_model_slug"] if facts else ""
    turns = [
        {
            # The four keys the orchestrator's own transcripts carry, in its
            # order: a reader must not be able to tell an orphan archive from
            # a normal one by the shape of a turn (checked against a real
            # transcript, 2026-09-20).
            "role": (m.get("author") or {}).get("role", ""),
            "text": cc.message_text(m),
            "model": (m.get("metadata") or {}).get("model_slug") or "",
            "create_time": m.get("create_time"),
        }
        for m in cc.chain(conv)
        if cc.message_text(m).strip()
    ]
    return {
        "job": entry.get("job"),
        "round": entry.get("round"),
        "conversation_id": entry.get("chat"),
        "model": model,
        "title": conv.get("title") or "",
        "status": "collected",
        "problems": entry.get("problems") or [],
        "turns_sent": entry.get("turns"),
        "archived_at": archived_at,
        "turns": turns,
    }


def orphan_paths(workdir: Path, round_no: Any, job: Any) -> tuple[Path, Path]:
    """Where a collected orphan's transcript and reply file go."""
    directory = workdir / "chatgpt" / TRANSCRIPTS_DIRNAME
    stem = f"r{round_no}-{job}.orphan"
    return directory / f"{stem}.json", directory / f"{stem}.reply.md"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# --collect: the I/O this needs (network reads, and with --apply, writes)
# ---------------------------------------------------------------------------


def orchestrator_running() -> bool:
    """True when chatgpt_research.py has a process in flight.

    The bracket in the pattern keeps this call's own ``pgrep`` invocation
    from matching itself (SKILL.md, House facts).
    """
    result = subprocess.run(
        ["pgrep", "-f", "chatgpt_researc[h].py"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def write_ledger(
    path: Path, entries: list[dict[str, Any]], original_bytes: bytes
) -> bool:
    """Atomically rewrite the ledger. False (and nothing written) when the
    file changed on disk since ``original_bytes`` was read.

    The orchestrator loads the whole ledger into memory at start and writes
    it back wholesale, so a write here while it runs would lose whatever it
    wrote -- the reason for the ``orchestrator_running`` refusal before any
    of this starts. This is the narrower guard for the gap between this
    process's own read and its own write: writes to a temporary file in the
    same directory and ``os.replace``s it into place, which is atomic on
    the same filesystem.
    """
    if path.read_bytes() != original_bytes:
        return False
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True


def run_collect(
    workdir: Path,
    conv_file: Path,
    ledger_bytes: bytes,
    entries: list[dict[str, Any]],
    current_round: int,
    *,
    apply: bool,
) -> int:
    """The --collect report and, with --apply, its actions.

    Exit 0 when nothing needed collecting or everything succeeded, 1 when a
    fetch or a write failed, 2 when refused.
    """
    if apply and orchestrator_running():
        print(
            "\nrefusing to write: chatgpt_research.py is running. Do not "
            "touch the ledger while a dispatch is in flight (SKILL.md, "
            "'Never interrupt a dispatch')."
        )
        return 2

    sent = [e for e in entries if isinstance(e, dict) and e.get("status") == "sent"]
    print(f"\n--collect: {len(sent)} entrie(s) stuck at status 'sent'")
    if not sent:
        print("nothing to collect")
        return 0

    session = open_session()
    verdicts: list[tuple[dict[str, Any], str, dict[str, Any] | None, str | None]] = []
    for entry in sent:
        twin = successful_twin(entry, entries)
        readable = False
        error: str | None = None
        if entry.get("round") != current_round and twin is None:
            try:
                session.get_conversation(entry["chat"])
                readable = True
            except cc.TransportError as exc:
                error = str(exc)
        verdict = classify_sent_entry(entry, current_round, entries, readable=readable)
        verdicts.append((entry, verdict, twin, error))

    failed = False
    for entry, verdict, twin, error in verdicts:
        job, round_no, chat = entry.get("job"), entry.get("round"), entry.get("chat")
        print(f"\n[{verdict}] job {job}  round {round_no}  chat {chat}")

        if verdict == "resumable":
            print("  Re-run the orchestrator with --resume. Do not touch this entry.")
            continue

        if verdict == "superseded":
            transcript = (twin or {}).get("transcript")
            if transcript:
                print(
                    "  Nothing to do. The successful entry's transcript is "
                    f"{transcript}."
                )
            else:
                print(
                    "  Nothing to do. The successful entry has no recorded transcript."
                )
            if apply:
                entry["status"] = "superseded"
                entry["classified_at"] = now_iso()
            continue

        if verdict == "lost":
            # Only reachable through the fetch attempt below, so `error` is
            # always set here -- "lost" never happens without a failed read.
            print(f"  Nothing to recover. The conversation does not read: {error}")
            if apply:
                entry["status"] = "lost"
                entry["classified_at"] = now_iso()
            continue

        # collectable
        transcript_path, reply_path = orphan_paths(workdir, round_no, job)
        print(
            f"  Its reply was archived to {transcript_path}. Producing an "
            "artifact needs the whole round re-opened."
        )
        if not apply:
            continue
        try:
            conv = session.get_conversation(entry["chat"])
        except cc.TransportError as exc:
            print(f"  FAILED to fetch the conversation for archiving: {exc}")
            failed = True
            continue
        archived_at = now_iso()
        doc = orphan_transcript(entry, conv, archived_at=archived_at)
        try:
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text(
                json.dumps(doc, indent=2) + "\n", encoding="utf-8"
            )
            reply_path.write_text(longest_reply(conv), encoding="utf-8")
        except OSError as exc:
            print(f"  FAILED to write the archive: {exc}")
            failed = True
            continue
        entry["status"] = "collected"
        entry["collected_at"] = archived_at
        entry["orphan_transcript"] = str(transcript_path)
        print(f"  Collected: wrote {transcript_path}")

    if not apply:
        print("\ndry run: nothing changed. Re-run with --apply to act.")
        return 0

    try:
        written = write_ledger(conv_file, entries, ledger_bytes)
    except OSError as exc:
        print(f"\nFAILED to write the ledger: {exc}")
        return 1
    if not written:
        print(
            "\nrefusing to write: chatgpt/conversations.json changed on "
            "disk since it was read. Re-run --collect --apply."
        )
        return 2
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# the original report: admitted, read, written off, pending
# ---------------------------------------------------------------------------


def main(workdir: Path, *, collect: bool = False, apply: bool = False) -> int:
    state = json.loads((workdir / "workflow_state.json").read_text(encoding="utf-8"))
    run = workdir / "runs" / state["run_id"]
    admitted, analysed = cr.admitted_ids(run), cr.analysed_ids(run)
    skipped = cr.skipped_papers(run)
    # Read or written off both settle a paper; only the first is evidence.
    unread = sorted(admitted - cr.accounted_ids(run))

    print(
        f"round {state['round']}/{state.get('max_rounds', '?')}  run {state['run_id']}"
    )
    print(
        f"papers: {len(admitted)} admitted, {len(analysed)} read, "
        f"{len(skipped)} written off, {len(unread)} unread"
    )
    for pid in unread[:10]:
        print(f"  unread: {pid}")
    for pid, why in sorted(skipped.items())[:10]:
        print(f"  written off {pid}: {' '.join(why.split())[:90]}")

    conv_file = workdir / "chatgpt" / "conversations.json"
    ledger_bytes = conv_file.read_bytes()
    entries = json.loads(ledger_bytes)
    pending = [
        c
        for c in entries
        if isinstance(c, dict)
        and c.get("round") == state["round"]
        and c.get("status") == "sent"
    ]
    if pending:
        print(f"\n{len(pending)} conversation(s) sent but not collected:")
        session = open_session()
        for entry in pending:
            try:
                conv = session.get_conversation(entry["chat"])
            except cc.TransportError as exc:
                print(f"  {entry['job']:16s} unreadable: {str(exc)[:60]}")
                continue
            _, length, _, final = cc.tail_signature(conv)
            replies = len(cc.assistant_text_messages(conv))
            ready = "REPLY READY to collect" if final and replies else "still running"
            print(f"  {entry['job']:16s} {length:4d} msg(s)  {ready}")

    if unread:
        print("\nThis round has NOT read everything it admitted. Do not trust a")
        print("synthesis or report produced from it.")
        result = 1
    elif not admitted:
        # Saying "everything is read" of an empty corpus reads as a pass,
        # and a round still searching has not passed anything yet.
        print("\nThis round has admitted nothing yet; it is still before the")
        print("admission gate. Nothing to judge.")
        result = 0
    else:
        print(f"\nEvery one of the {len(admitted)} admitted papers is accounted for.")
        result = 0

    if not collect:
        return result
    return run_collect(
        workdir, conv_file, ledger_bytes, entries, state["round"], apply=apply
    )


if __name__ == "__main__":
    ensure_venv()
    cli_args = parse_collect_args(sys.argv[1:])
    raise SystemExit(
        main(cli_args.workdir, collect=cli_args.collect, apply=cli_args.apply)
    )
