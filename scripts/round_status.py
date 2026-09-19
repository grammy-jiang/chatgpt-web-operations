#!/usr/bin/env python3
"""Where a research round actually stands. Read-only.

Prints what the round admitted, what it has read, and whether each in-flight
worker reply is finished and waiting to be collected. Run it after any
interrupt, and before trusting a round's report.

    python3 scripts/round_status.py <workdir>   # switches to the skill's .venv

Exit 0 when the round has read everything it admitted, 1 when it has not.
"""

import json
import sys
from pathlib import Path

import chatgpt_client as cc
import round_state as cr
from _common import ensure_venv


def main(workdir: Path) -> int:
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
    pending = [
        c
        for c in json.loads(conv_file.read_text(encoding="utf-8"))
        if c.get("round") == state["round"] and c.get("status") == "sent"
    ]
    if pending:
        print(f"\n{len(pending)} conversation(s) sent but not collected:")
        session = cc.ChatGPTSession("chrome")
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
        return 1
    if not admitted:
        # Saying "everything is read" of an empty corpus reads as a pass, and
        # a round still searching has not passed anything yet.
        print("\nThis round has admitted nothing yet; it is still before the")
        print("admission gate. Nothing to judge.")
        return 0
    print(f"\nEvery one of the {len(admitted)} admitted papers is accounted for.")
    return 0


if __name__ == "__main__":
    ensure_venv()
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(Path(sys.argv[1]).expanduser().resolve()))
