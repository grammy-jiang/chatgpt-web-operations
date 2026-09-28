"""Contract tests: every read command over payloads synthesized from the
recorded API shapes (TESTING.md section 6, P5).

``tests/api_contract.py`` runs each scenario: the command's real ``main()``
and a real ``ChatGPTSession``, over a fake transport that answers every
registered endpoint with ``api_shapes.synthesize`` of its recorded shape.
When ChatGPT changes a response, the daily check records the new shape and
warns; ``make contract SHAPES=<the run's http/>`` then runs these same tests
against the new shapes and shows which command breaks, offline, before a
real run does.

Also enforced here: every registered endpoint has a committed shape and is
exercised by at least one contract, and ``tests/fixtures/http/read_paths.json``
(the fields the daily check guards) is what the contracts actually read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import api_contract
import api_shapes
import pytest


@pytest.fixture(scope="module")
def shapes(request: pytest.FixtureRequest) -> dict[str, Any]:
    """The committed shapes, overlaid by ``--api-shapes DIR`` when given."""
    committed = api_shapes.load(api_shapes.COMMITTED)
    overlay = request.config.getoption("--api-shapes")
    if overlay:
        fresh = api_shapes.load(Path(overlay))
        assert fresh, f"--api-shapes {overlay}: no *.shape.json there"
        committed.update(fresh)
    return committed


@pytest.mark.parametrize("scenario", api_contract.SCENARIOS, ids=lambda s: s.id)
def test_a_read_command_runs_on_the_recorded_shapes(
    scenario: api_contract.Scenario, shapes: dict[str, Any]
) -> None:
    outcome = api_contract.run_scenario(scenario, shapes, None)
    assert outcome.unrecorded == (), outcome.unrecorded
    assert outcome.code in scenario.exits, outcome.output[-800:]
    assert outcome.calls, "the command read nothing"


@pytest.mark.parametrize("name", api_contract.FUNCTION_NAMES)
def test_a_preflight_or_health_read_runs_on_the_recorded_shapes(
    name: str, shapes: dict[str, Any]
) -> None:
    outcome = api_contract.run_function(name, shapes, None)
    assert outcome.unrecorded == (), outcome.unrecorded
    result = outcome.result
    rows = result if isinstance(result, list) else [result]
    if isinstance(result, tuple):  # fetch_skills_inventory: (check, document)
        rows = [result[0]]
    for row in rows:
        assert row["state"] in {"ok", "warn", "block"}, row


def test_every_registered_endpoint_has_a_committed_shape() -> None:
    committed = api_shapes.load(api_shapes.COMMITTED)
    missing = [e.name for e in api_shapes.ENDPOINTS if e.name not in committed]
    assert missing == [], "record them: health.py --record-shapes, then promote"


def test_every_committed_shape_is_a_registered_endpoint() -> None:
    stale = sorted(set(api_shapes.load(api_shapes.COMMITTED)) - set(api_shapes.BY_NAME))
    assert stale == [], stale


def test_every_registered_endpoint_is_exercised_by_a_contract(shapes) -> None:
    """A recorded shape that no command reads in a contract is guarded by
    nothing: either a scenario is missing or the endpoint is not needed."""
    hit: set[str] = set()
    for scenario in api_contract.SCENARIOS:
        for method, path in api_contract.run_scenario(scenario, shapes, None).calls:
            endpoint = api_shapes.endpoint_for(method, path)
            if endpoint:
                hit.add(endpoint.name)
    for name in api_contract.FUNCTION_NAMES:
        for method, path in api_contract.run_function(name, shapes, None).calls:
            endpoint = api_shapes.endpoint_for(method, path)
            if endpoint:
                hit.add(endpoint.name)
    unexercised = sorted(set(api_shapes.BY_NAME) - hit)
    assert unexercised == [], unexercised


def test_read_paths_json_is_what_the_contracts_read() -> None:
    """The daily check warns only for these fields; a code change that reads
    a new one must regenerate the file (``make read-paths``) or the change
    to that field would go unwarned."""
    expected = api_contract.read_paths(api_shapes.load(api_shapes.COMMITTED))
    on_disk = json.loads(api_shapes.READ_PATHS_FILE.read_text(encoding="utf-8"))
    assert on_disk == expected, "stale: run `make read-paths` and commit"


def test_the_read_paths_include_the_fields_a_reply_is_read_from() -> None:
    """A floor, so a scenario change cannot quietly drop the most important
    contract: where a reply's text and its effort are read from."""
    on_disk = json.loads(api_shapes.READ_PATHS_FILE.read_text(encoding="utf-8"))
    conversation = set(on_disk["conversation"])
    for path in (
        "current_node",
        "mapping{}.message.author.role",
        "mapping{}.message.content.parts",
        "mapping{}.message.metadata.thinking_effort",
        "mapping{}.parent",
    ):
        assert path in conversation, path
    assert "items[].id" in on_disk["conversations"]


def test_the_overlay_option_replaces_committed_shapes(tmp_path: Path) -> None:
    """``make contract SHAPES=<dir>``: the same scenarios, the fresh shapes."""
    committed = api_shapes.load(api_shapes.COMMITTED)
    changed = json.loads(json.dumps(committed["conversations"]))
    del changed["fields"]["items"]["items"]["fields"]["title"]
    (tmp_path / f"conversations{api_shapes.SHAPE_SUFFIX}").write_text(
        json.dumps(changed), encoding="utf-8"
    )
    overlaid = {**committed, **api_shapes.load(tmp_path)}
    body = api_shapes.synthesize(overlaid["conversations"])
    assert "title" not in body["items"][0]
    scenario = api_contract.SCENARIOS[0]
    assert api_contract.run_scenario(scenario, overlaid, None).code == 0
