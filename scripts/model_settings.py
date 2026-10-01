#!/usr/bin/env python3
"""Which model and effort a send will use, and the rule for the default.
Plain HTTP; read-only unless ``--default ... --apply``.

    model_settings.py [--browser chrome]
    model_settings.py --default below-pro [--apply]
    model_settings.py --default "Extra High" [--apply]
    model_settings.py --default gpt-5-6-thinking:max [--apply]

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
record or that cookie. The slider itself writes the record with ``PATCH
/backend-api/settings/user_last_used_model_config?model_slug=…&thinking_effort=…``
(measured 2026-10-01), and so does ``--default ... --apply`` here.

The owner's rule for the default (2026-10-01): the Pro lane has a limited
quota, so the account's default is the preset one position below Pro -- the
highest thinking preset, "Extra High" (``gpt-5-6-thinking`` / ``max``) in
the catalog of that day. Names and versions change, the rule does not, so
``below_pro_preset`` derives the preset from the slider's
``intelligence_presets`` every time. Without ``--default`` the report ends
with whether the record matches the rule (``preflight.py`` warns when it does
not); ``--default below-pro`` prints what the rule picks and the PATCH that
would set it, and ``--apply`` sends it and reads the record back. A preset
title or ``model:effort`` names another slider position the same way.

Exit 0 when the server record resolves to a known preset, or the default was
applied (or already held) and read back; 1 when the record resolves to none,
the PATCH fails or the read-back disagrees; 2 when ``--default`` names no
slider position.
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
from typing import Any

from _common import ensure_venv, load_client, open_session, table

MODELS = (
    "/backend-api/models?iim=false&is_gizmo=false"
    "&supports_model_picker_upgrade_presets=true"
)
SETTINGS = "/backend-api/settings/user"
# What the Power slider calls when it moves (2026-10-01): the model and the
# effort go in the query string, the body is empty, and the server writes
# last_used_model_config for the calling surface (``web`` and ``default`` when
# called from here; ``windows_app`` and ``ios_app`` keep their own).
SET_DEFAULT = "/backend-api/settings/user_last_used_model_config"
# The send path drives the web composer, so that is the surface whose
# server-side record matters. settings/user also keeps ios_app and
# windows_app.
SURFACE = "web"
# The owner's rule for the default: one preset below the Pro lane.
DEFAULT_RULE = "below-pro"
PRO_LANE = "pro"


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
                    "lane": str(p.get("lane") or ""),
                }
                for i, p in enumerate(entry.get("intelligence_presets") or [])
            ]
    return []


def below_pro_preset(presets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The preset the owner's rule picks: one position below Pro.

    Pro is the first preset in the ``pro`` lane (or titled "Pro"); the pick
    is the last preset before it that carries a ``thinking_effort``, so a
    Pro moved around the slider or an effort-less preset slipped in between
    cannot shift the choice onto a model that takes no effort. Without a
    Pro preset the last effort-carrying preset wins; None when none does.
    """
    pro = next(
        (
            i
            for i, p in enumerate(presets)
            if p.get("lane") == PRO_LANE or p["title"].strip().lower() == "pro"
        ),
        len(presets),
    )
    for p in reversed(presets[:pro]):
        if p["effort"]:
            return p
    return None


def choose_default(spec: str, presets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The slider position ``--default SPEC`` names: ``DEFAULT_RULE``, a
    preset title (case-insensitive) or ``model:effort``; None otherwise.
    Only positions the slider offers, so a pair the server would store but
    the picker could never show is refused rather than sent."""
    if spec.strip().lower() == DEFAULT_RULE:
        return below_pro_preset(presets)
    wanted = spec.strip().lower()
    for p in presets:
        if p["title"].strip().lower() == wanted:
            return p
    if ":" in spec:
        model, _, effort = spec.partition(":")
        return resolve_preset(presets, model.strip(), effort.strip())
    return None


def matches(config: dict[str, str], preset: dict[str, Any]) -> bool:
    """Whether the server record is exactly ``preset``'s model and effort."""
    return config.get("model", "") == preset["model"] and (
        config.get("effort") or ""
    ) == (preset["effort"] or "")


def set_default_path(preset: dict[str, Any]) -> str:
    """The PATCH the slider makes for ``preset``: query parameters, no body."""
    query = {"model_slug": preset["model"]}
    if preset["effort"]:
        query["thinking_effort"] = preset["effort"]
    return f"{SET_DEFAULT}?{urllib.parse.urlencode(query)}"


def rule_line(rule: dict[str, Any] | None, config: dict[str, str]) -> str:
    """One line: what the rule picks and whether the record matches it."""
    if rule is None:
        return f"rule {DEFAULT_RULE!r}: no preset below Pro carries an effort"
    verdict = (
        "the record matches it"
        if matches(config, rule)
        else (
            "the record differs; model_settings.py --default below-pro --apply sets it"
        )
    )
    return (
        f"rule {DEFAULT_RULE!r} (one preset below Pro): preset {rule['title']!r} "
        f"(model={rule['model']} effort={rule['effort']}); {verdict}"
    )


def apply_default(
    spec: str,
    apply: bool,
    session: Any,
    presets: list[dict[str, Any]],
    config: dict[str, str],
) -> int:
    """``--default SPEC [--apply]``: name the position, dry-run or PATCH it,
    read the record back. Exit codes as in the module docstring."""
    target = choose_default(spec, presets)
    if target is None:
        print(f"--default {spec!r} names no position of the Power slider")
        return 2
    print(
        f"\ndefault {spec!r} -> preset {target['title']!r} "
        f"(model={target['model']} effort={target['effort'] or '-'})"
    )
    if matches(config, target):
        print("  the server record already holds it; nothing to send")
        return 0
    path = set_default_path(target)
    if not apply:
        print(f"  dry run; --apply would send PATCH {path}")
        return 0
    status, _body = session.session.call(path, method="PATCH")
    if status != 200:
        print(f"  PATCH {path}: HTTP {status}")
        return 1
    _status, settings = session.session.call(SETTINGS)
    got = server_config(settings if isinstance(settings, dict) else {})
    if matches(got, target):
        print(
            "  applied and read back: "
            f"model={got['model']} effort={got['effort'] or '-'}"
        )
        return 0
    print(
        "  PATCH answered 200 but the record reads "
        f"model={got['model'] or '-'} effort={got['effort'] or '-'}"
    )
    return 1


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
    ap.add_argument(
        "--default",
        metavar="RULE|PRESET",
        help=(
            f"set the account's web default: {DEFAULT_RULE!r} (one preset below "
            "Pro), a preset title, or model:effort; a dry run without --apply"
        ),
    )
    ap.add_argument(
        "--apply", action="store_true", help="send the PATCH and read it back"
    )
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
    if args.default:
        return apply_default(args.default, args.apply, session, presets, config)
    rule = below_pro_preset(presets)
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
    print(f"  {rule_line(rule, config)}")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
