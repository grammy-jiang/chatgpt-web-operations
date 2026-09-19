#!/usr/bin/env python3
"""Which model and effort a send will use. Read-only, plain HTTP.

    model_settings.py [--browser chrome]

The composer's "Power" slider is not five effort levels: its positions are the
``intelligence_presets`` of the selected version in ``/backend-api/models``,
and one of them changes the model rather than the effort. This prints those
presets, the API's ``thinking_efforts`` per model, and the profile's
``oai-last-model-config`` cookie resolved to a preset, which is what a send
without an explicit ``effort`` will inherit.

Exit 0 when the cookie resolves to a known preset, 1 when it does not.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from _common import ensure_venv, load_client, open_session, table
from probe_cookies import read_jar

MODELS = (
    "/backend-api/models?iim=false&is_gizmo=false"
    "&supports_model_picker_upgrade_presets=true"
)
CONFIG_COOKIE = "oai-last-model-config"


def presets_of(models: dict[str, Any], version: str = "latest") -> list[dict[str, Any]]:
    """The slider's positions for one version, in slider order."""
    for entry in models.get("versions") or []:
        if entry.get("id") == version:
            return [
                {
                    "position": i + 1,
                    "title": str(p.get("title") or ""),
                    "model": str(p.get("model_slug") or ""),
                    "effort": str(p.get("thinking_effort") or ""),
                }
                for i, p in enumerate(entry.get("intelligence_presets") or [])
            ]
    return []


def effort_levels_of(models: dict[str, Any]) -> dict[str, list[str]]:
    """``thinking_effort`` values per model that lets you choose one."""
    return {
        str(m.get("slug")): [
            str(e.get("thinking_effort")) for e in m.get("thinking_efforts") or []
        ]
        for m in models.get("models") or []
        if m.get("configurable_thinking_effort")
    }


def resolve_preset(
    presets: list[dict[str, Any]], model: str, effort: str
) -> dict[str, Any] | None:
    """The preset a (model, effort) pair lands on, or None."""
    for p in presets:
        if p["model"] == model and p["effort"] == (effort or ""):
            return p
    return None


def parse_config(raw: str) -> dict[str, str]:
    """``{"model": ..., "effort": ...}`` out of the cookie's value."""
    try:
        data = json.loads(unquote(raw))
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "model": str(data.get("model") or ""),
        "effort": str(data.get("effort") or ""),
    }


def profile_config(cc: Any, browser: str) -> dict[str, str]:
    """The profile's current cookie, decrypted; empty when unreadable."""
    cs = cc._helpers()
    db, app = cs.BROWSERS[browser]
    if not Path(db).exists():
        return {}
    decrypt = cc._tolerant_decryptor(cs, app)
    for name, _host, enc in read_jar(Path(db)):
        if name == CONFIG_COOKIE:
            try:
                return parse_config(decrypt(enc))
            except Exception:
                return {}
    return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--browser", default="chrome")
    args = ap.parse_args(argv)

    cc = load_client()
    session = open_session(args.browser)
    _status, models = session.session.call(MODELS)
    models = models if isinstance(models, dict) else {}

    presets = presets_of(models)
    print("Power slider, version 'latest':")
    print(
        table(
            [
                (str(p["position"]), p["title"], p["model"], p["effort"] or "-")
                for p in presets
            ],
            ("position", "preset", "model", "thinking_effort"),
        )
    )

    print("\nAPI levels per configurable model:")
    print(
        table(
            [
                (slug, ", ".join(levels))
                for slug, levels in effort_levels_of(models).items()
            ],
            ("model", "thinking_efforts"),
        )
    )
    print(f"\nthe client accepts --effort: {', '.join(cc.EFFORTS)}")

    config = profile_config(cc, args.browser)
    if not config:
        print(f"\nprofile cookie {CONFIG_COOKIE}: unreadable or absent")
        return 1
    preset = resolve_preset(presets, config["model"], config["effort"])
    print(
        f"\nprofile cookie: model={config['model'] or '-'} "
        f"effort={config['effort'] or '-'}"
    )
    if preset is None:
        print("  -> matches no preset; a send without --effort uses this anyway")
        return 1
    print(
        f"  -> preset {preset['title']!r}, "
        f"position {preset['position']} of {len(presets)}"
    )
    print("  A send without --effort inherits exactly this.")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
