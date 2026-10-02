"""Generate the NexusFlow AI architecture diagram with exact column alignment.

Kept in the repo so the README and the in-app /architecture page can never
drift apart: run `python3 tools/gen_architecture_diagram.py` and it rewrites
both copies.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# --- geometry -------------------------------------------------------------
WIDTH = 78          # total characters per line, including the 2-space indent
LEFT = 2            # column of the outer box's left edge
RIGHT = WIDTH - 1   # column of the outer box's right edge
SPLIT = 35          # column of the arrow tee on the bottom borders
INNER_INDENT = 15   # columns between the outer left edge and the nested box
INNER_W = 56        # nested box width, including its corners

# Columns available for text inside each border.
OUTER_INNER = RIGHT - LEFT - 1          # 74
INNER_INNER = INNER_W - 2               # 54
CONNECTOR_INNER = WIDTH - SPLIT - 1     # 42
NESTED_RIGHT = LEFT + 1 + INNER_INDENT + INNER_W - 1   # column 73
NESTED_GAP = RIGHT - NESTED_RIGHT - 1                   # 3


def _outer(content: str = "") -> str:
    """Render one line of the outer box: `│ <content> │` padded to WIDTH."""
    return " " * LEFT + "│" + content[:OUTER_INNER].ljust(OUTER_INNER) + "│"


def _box_top() -> str:
    return " " * LEFT + "┌" + "─" * OUTER_INNER + "┐"


def _box_bottom(tee: bool = True) -> str:
    """Bottom border. The tee is only drawn where a connector leaves the box."""
    if not tee:
        return " " * LEFT + "└" + "─" * OUTER_INNER + "┘"
    left = SPLIT - LEFT - 1
    right = RIGHT - SPLIT - 1
    return " " * LEFT + "└" + "─" * left + "┬" + "─" * right + "┘"


def _nested(text: str = "") -> str:
    """Render a line of a box nested inside the outer box."""
    return (
        " " * LEFT
        + "│"
        + " " * INNER_INDENT
        + "│"
        + text[:INNER_INNER].ljust(INNER_INNER)
        + "│"
        + " " * NESTED_GAP
        + "│"
    )


def _nested_border(left: str, right: str) -> str:
    return (
        " " * LEFT
        + "│"
        + " " * INNER_INDENT
        + left
        + "─" * INNER_INNER
        + right
        + " " * NESTED_GAP
        + "│"
    )


def _plain(text: str) -> str:
    return text[: WIDTH - LEFT].ljust(WIDTH - LEFT)


def _pad(text: str, width: int, gap: int = 0) -> str:
    """Left-align text in a fixed column, followed by `gap` spaces."""
    return text.ljust(width) + " " * gap


def _connector(text: str) -> str:
    """Arrow shaft line: the vertical bar sits exactly under the tee."""
    return " " * SPLIT + "│" + text[: CONNECTOR_INNER]


def _arrow() -> str:
    return " " * SPLIT + "▼"


def _outer_block(rows: list[str]) -> list[str]:
    _assert_fits("outer-box", rows, OUTER_INNER)
    return [_outer(row) for row in rows]


def _connector_block(rows: list[str]) -> list[str]:
    _assert_fits("connector", rows, CONNECTOR_INNER)
    return [_connector(row) for row in rows]


# Model table: name / parameters / context window / intended tier.
# Rendered as fixed-width columns so the box cannot drift.
MODEL_TABLE = [
    ("Nemotron 3.5 Lightning", "30B MoE", "1M ctx", "fast"),
    ("Nemotron 3 Super", "120B MoE", "256K ctx", "balanced"),
    ("Nemotron 3 Ultra", "550B MoE", "1M ctx", "frontier"),
    ("Nemotron Nano V2", "12B VL", "128K ctx", "vision"),
]

NEMOTRON_ROWS = [
    "  NVIDIA  NEMOTRON  —  open-weight checkpoints",
    "",
    *["  " + _pad(name, 23, 1) + _pad(params, 8, 2) + _pad(ctx, 8, 2) + tier
       for name, params, ctx, tier in MODEL_TABLE],
    "",
    "  H100 / H200 / B200 · vLLM · FP8 / NVFP4",
]

PG_ROWS = [
    "  PostgreSQL 17   SQLAlchemy 2.0 async · asyncpg",
    "  workflows · runs · ledger   Alembic migrations",
]

API_ROWS = [
    "app/main.py",
    "  ├── request-id middleware + redacting logger",
    "  ├── CORS, uniform errors → { error, request_id }",
    "  └── lifespan: shared httpx pool, migrations on boot",
    "",
    "GET  /health                      liveness (no I/O)",
    "GET  /health/ready                DB + provider probe",
    "GET  /api/v1/models               live ∪ curated registry",
    "",
    "POST /api/v1/chat/completions     buffered completion",
    "POST /api/v1/chat/completions/stream   SSE passthrough",
    "POST /api/v1/chat/embeddings      vector embeddings",
    "",
    "app/services/nebius/client.py",
    "  Bearer auth · jittered retry · 429/5xx backoff",
    "  key never logged",
]


def _assert_fits(label: str, rows: list[str], budget: int) -> None:
    """Fail loudly instead of silently truncating a word mid-syllable."""
    too_wide = [
        (len(row), row) for row in rows if len(row) > budget
    ]
    if too_wide:
        detail = "\n".join(f"      {width:3} chars: {row!r}" for width, row in too_wide)
        raise SystemExit(
            f"ERROR: {label} rows exceed the {budget}-column budget; "
            f"shorten them:\n{detail}"
        )


def build() -> list[str]:
    _assert_fits("nested-box", NEMOTRON_ROWS, INNER_INNER)
    _assert_fits("nested-box", PG_ROWS, INNER_INNER)
    _assert_fits("outer-box", API_ROWS, OUTER_INNER)

    lines: list[str] = [
        _box_top(),
        *_outer_block([
            "  BROWSER (user)",
            "",
            "  NexusFlow AI UI — Next.js 16 App Router · React 19",
            "  Playground · model picker · SSE stream · token usage",
        ]),
        _box_bottom(),
        *_connector_block([
            "  HTTPS · fetch() · EventSource",
            "  Origin: http://localhost:3000",
            "  CORS preflight (CORS_ORIGINS allowlist)",
        ]),
        _arrow(),
        _box_top(),
        *_outer_block([
            "  FastAPI CONTROL PLANE  ·  :8000",
            "",
            *API_ROWS,
            "",
        ]),
        _nested_border("┌", "┐"),
        *[_nested(row) for row in PG_ROWS],
        _nested_border("└", "┘"),
        _box_bottom(),
        *_connector_block([
            "  HTTPS · Bearer $NEBIUS_API_KEY",
            "  GET  {NEBIUS_BASE_URL}/models",
            "  POST {NEBIUS_BASE_URL}/chat/completions",
            "  POST {NEBIUS_BASE_URL}/embeddings",
        ]),
        _arrow(),
        _box_top(),
        *_outer_block([
            "  NEBIUS  TOKEN  FACTORY  ·  serverless inference",
            "  https://api.tokenfactory.nebius.com/v1",
            "",
            "  OpenAI-compatible: /models · /chat/completions",
            "  /embeddings · SSE streaming · quotas · rate limits",
            "",
        ]),
        _nested_border("┌", "┐"),
        *[_nested(row) for row in NEMOTRON_ROWS],
        _nested_border("└", "┘"),
        _box_bottom(tee=False),
    ]
    return lines


def validate(diagram: str) -> list[str]:
    """Return a list of alignment problems; empty means the diagram is sound.

    Box rows must be exactly WIDTH columns with the right border in the last
    column. Connector rows and arrow heads are deliberately ragged: they have no
    right border, but their shaft must still sit under the tee at SPLIT.
    """
    problems: list[str] = []
    for i, line in enumerate(diagram.split("\n")):
        if line.endswith("│"):
            if len(line) != WIDTH:
                problems.append(
                    f"line {i}: box row is {len(line)} columns, expected {WIDTH}: {line!r}"
                )
            elif line[RIGHT] != "│":
                problems.append(f"line {i}: right border in the wrong column: {line!r}")
        elif line.lstrip()[:1] in {"│", "▼"}:
            bar = SPLIT if line.lstrip().startswith("│") else SPLIT
            if line[bar] not in {"│", "▼"}:
                problems.append(
                    f"line {i}: connector shaft off the tee column {SPLIT}: {line!r}"
                )
    return problems


def main() -> int:
    diagram = "\n".join(build())

    problems = validate(diagram)
    if problems:
        print(
            f"ERROR: {len(problems)} alignment problem(s):\n  "
            + "\n  ".join(problems),
            file=sys.stderr,
        )
        return 1

    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    pattern = re.compile(r"(```\n)  ┌.*?\n(```)", re.S)
    if not pattern.search(text):
        print("ERROR: architecture block not found in README.md", file=sys.stderr)
        return 1
    readme.write_text(pattern.sub(lambda m: m.group(1) + diagram + "\n" + m.group(2), text), encoding="utf-8")

    page = ROOT / "frontend" / "src" / "app" / "architecture" / "page.tsx"
    src = page.read_text(encoding="utf-8")
    # Replace the body of `const DIAGRAM = `...`` in place, keeping the
    # backticks on their own lines so the declaration stays readable. The
    # diagram contains no backticks, so a lazy match to the next one is safe.
    literal = re.compile(r"(const DIAGRAM = `)[^`]*(`)")
    if not literal.search(src):
        print(f"ERROR: DIAGRAM literal not found in {page.relative_to(ROOT)}", file=sys.stderr)
        return 1
    page.write_text(
        literal.sub(lambda m: m.group(1) + "\n" + diagram + "\n" + m.group(2), src),
        encoding="utf-8",
    )

    print(f"OK: {len(diagram.splitlines())} lines, box rows all {WIDTH} columns wide")
    print(f"  updated {readme.relative_to(ROOT)}")
    print(f"  updated {page.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
