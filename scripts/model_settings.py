#!/usr/bin/env python3
"""Which model and effort a send will use. Read-only, plain HTTP.

    model_settings.py [--browser chrome]

The composer's "Power" slider is not five effort levels: its positions are the
``intelligence_presets`` of the selected version in ``/backend-api/models``,
and one of them changes the model rather than the effort. This prints those
presets, the API's ``thinking_efforts`` per model, and the account's own
record of the last web send: ``GET /backend-api/settings/user`` ->
``settings.last_used_model_config``, resolved to a preset. That record --
never the composer's ``oai-last-model-config`` cookie, which measured
2026-09-20 does not steer either the send or the composer's own label
(SKILL.md, "Reasoning effort") -- is what a send without an explicit
``--effort``/``--model`` inherits; ``send_prompt.py --effort``/``--model``
pin a send by rewriting its POST body in flight, never by touching this
record or that cookie.

Exit 0 when the server record resolves to a known preset, 1 when it does not.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _common import ensure_venv, load_client, open_session, table

MODELS = (
    "/backend-api/models?iim=false&is_gizmo=false"
    "&supports_model_picker_upgrade_presets=true"
)
SETTINGS = "/backend-api/settings/user"
# The send path drives the web composer, so that is the surface whose
# server-side record matters. settings/user also keeps ios_app and
# windows_app.
SURFACE = "web"


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


def server_config(settings: dict[str, Any]) -> dict[str, str]:
    """The (model, effort) pair ``settings/user`` remembers for the web surface.

    ``last_used_model_config.slugs[SURFACE]`` is the model the composer last
    sent with; ``last_used_model_config.juices[SURFACE][<that slug>]`` is the
    effort remembered for it (shape: ``tests/fixtures/settings_user.json``).
    Empty strings when the record, or the surface inside it, is absent --
    never an exception, so a changed field name degrades to "no record"
    rather than a crash.
    """
    prefs = settings.get("settings") or {}
    last = prefs.get("last_used_model_config") or {}
    slug = str((last.get("slugs") or {}).get(SURFACE) or "")
    juices = (last.get("juices") or {}).get(SURFACE) or {}
    return {"model": slug, "effort": str(juices.get(slug) or "")}


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

    _status, settings = session.session.call(SETTINGS)
    settings = settings if isinstance(settings, dict) else {}
    config = server_config(settings)
    if not config["model"]:
        print(
            f"\nserver record ({SETTINGS}): no last_used_model_config for {SURFACE!r}"
        )
        return 1
    preset = resolve_preset(presets, config["model"], config["effort"])
    print(
        f"\nserver record (last_used_model_config[{SURFACE!r}]): "
        f"model={config['model'] or '-'} effort={config['effort'] or '-'}"
    )
    if preset is None:
        print("  -> matches no preset")
        return 1
    print(
        f"  -> preset {preset['title']!r}, "
        f"position {preset['position']} of {len(presets)}"
    )
    print(
        "  A send without --effort inherits this; send_prompt.py "
        "--effort/--model rewrite it in flight."
    )
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
