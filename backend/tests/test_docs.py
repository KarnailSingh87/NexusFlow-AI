"""Tests that the published documentation cannot drift from the code.

``tools/gen_architecture_diagram.py`` is the single source of truth for the
architecture diagram and rewrites both published copies (README and the in-app
page). These tests assert the two copies still match what the generator would
produce, so editing either by hand fails the suite instead of being silently
clobbered by the next generator run.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GENERATOR_PATH = REPO_ROOT / "tools" / "gen_architecture_diagram.py"
README_PATH = REPO_ROOT / "README.md"
PAGE_PATH = REPO_ROOT / "frontend" / "src" / "app" / "architecture" / "page.tsx"

# Mirrors the patterns in the generator's ``main()``.
README_BLOCK = re.compile(r"```\n  ┌.*?\n```", re.S)
PAGE_LITERAL = re.compile(r"(const DIAGRAM = `)[^`]*(`)")


def _load_generator() -> Any:
    """Import the tool by path; it is a script, not an installed package."""
    if not GENERATOR_PATH.is_file():
        pytest.skip("architecture generator not present in this checkout")
    spec = importlib.util.spec_from_file_location("gen_architecture_diagram", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def diagram() -> str:
    """The canonical diagram text, straight from the generator."""
    return "\n".join(_load_generator().build())


def test_generated_diagram_is_internally_aligned(diagram: str) -> None:
    problems = _load_generator().validate(diagram)
    assert problems == []


def test_readme_diagram_matches_the_generator(diagram: str) -> None:
    match = README_BLOCK.search(README_PATH.read_text(encoding="utf-8"))
    assert match is not None, "architecture block not found in README.md"
    assert match.group(0) == f"```\n{diagram}\n```"


def test_architecture_page_matches_the_generator(diagram: str) -> None:
    match = PAGE_LITERAL.search(PAGE_PATH.read_text(encoding="utf-8"))
    assert match is not None, "DIAGRAM literal not found in page.tsx"
    assert match.group(0) == f"{match.group(1)}\n{diagram}\n{match.group(2)}"
