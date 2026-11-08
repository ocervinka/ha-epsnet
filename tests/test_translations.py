"""Translation files stay in step with strings.json (pure, no HA)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "epsnet"
STRINGS = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _flatten(node: object, prefix: str = "") -> dict[str, str]:
    if isinstance(node, dict):
        out: dict[str, str] = {}
        for key, value in node.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else key))
        return out
    assert isinstance(node, str), prefix
    return {prefix: node}


def test_english_translation_is_strings_json() -> None:
    # Custom integrations only load translations/<lang>.json; strings.json is
    # the source those are generated from in core, so keep them identical.
    en = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    assert en == STRINGS


@pytest.mark.parametrize(
    "path", sorted((COMPONENT / "translations").glob("*.json")), ids=lambda p: p.stem
)
def test_translation_matches_keys_and_placeholders(path: Path) -> None:
    source = _flatten(STRINGS)
    translated = _flatten(json.loads(path.read_text(encoding="utf-8")))
    assert set(translated) == set(source)
    for key, text in source.items():
        assert set(PLACEHOLDER.findall(translated[key])) == set(
            PLACEHOLDER.findall(text)
        ), key


def test_every_shown_step_has_a_translation() -> None:
    flow = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    assert "description_placeholders={\"error\"" not in flow
    config_steps = set(STRINGS["config"]["step"])
    options_steps = set(STRINGS["options"]["step"])
    for error in ("plc_busy", "cannot_connect"):
        assert f"source_{error}" in config_steps
        assert f"init_{error}" in options_steps
