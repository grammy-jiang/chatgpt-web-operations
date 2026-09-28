#!/usr/bin/env python3
"""Write tests/fixtures/http/read_paths.json (``make read-paths``).

The file lists, per endpoint, every field the skill's read commands and the
preflight/health read functions actually read when they run over payloads
synthesized from the committed shapes (``tests/api_contract.py``). The daily
``api shapes`` check warns only when one of these fields disappears or
changes type. ``tests/test_api_contracts.py`` fails when the file is stale,
so a code change that reads a new field cannot land without it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import api_contract  # noqa: E402
import api_shapes  # noqa: E402  (api_contract put scripts/ on sys.path)


def main() -> int:
    paths = api_contract.read_paths(api_shapes.load(api_shapes.COMMITTED))
    target = api_shapes.READ_PATHS_FILE
    text = json.dumps(paths, indent=1, sort_keys=True) + "\n"
    old = target.read_text(encoding="utf-8") if target.is_file() else None
    if old == text:
        print(f"{target}: unchanged")
    else:
        target.write_text(text, encoding="utf-8")
        total = sum(len(v) for v in paths.values())
        print(f"{target}: written ({total} paths across {len(paths)} endpoints)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
