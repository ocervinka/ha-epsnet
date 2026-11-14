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
    steps = set(STRINGS["config"]["step"]) | set(STRINGS["options"]["step"])
    # Progress steps included: they take their dialog title from the step.
    shown = set(re.findall(r'(?<!next_)step_id="(\w+)"', flow)) | {
        "reimport_plc", "reimport_file_run"
    }
    assert shown <= steps, shown - steps
    # Variants that carry the warning of a failed reload.
    for error in ("plc_busy", "cannot_connect", "no_map"):
        assert f"init_{error}" in STRINGS["options"]["step"]
    for action in re.findall(r'return "(\w+)", ', flow):
        assert action in STRINGS["config"]["progress"] or action in STRINGS["options"]["progress"]


def test_every_menu_option_has_a_label() -> None:
    flow = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    for step_id, options in re.findall(
        r'step_id="(\w+)",\s*menu_options=\[([^\]]+)\]', flow
    ):
        section = "config" if step_id in STRINGS["config"]["step"] else "options"
        labels = STRINGS[section]["step"][step_id]["menu_options"]
        assert set(re.findall(r'"(\w+)"', options)) == set(labels), step_id
    for step in STRINGS["options"]["step"].values():
        if "menu_options" in step:
            assert step["menu_options"] == STRINGS["options"]["step"]["init"]["menu_options"]
