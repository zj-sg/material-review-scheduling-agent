import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_example(scenario: str, filename: str = "input.json") -> dict:
    return json.loads((ROOT / "examples" / scenario / filename).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def isolate_settings_environment(monkeypatch):
    for name in os.environ:
        if name.upper().startswith("MATERIAL_AGENT_"):
            monkeypatch.delenv(name)


@pytest.fixture
def request_data():
    return read_example("01_normal")["steps"][0]["request"]


@pytest.fixture
def result_data():
    return read_example("01_normal", "expected.json")["results"][0]


@pytest.fixture
def rule_data():
    return read_example("01_normal")["rules"]
