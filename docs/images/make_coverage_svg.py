"""Draw `llmdv-coverage.svg`, the README's "What llmdv tests" graphic.

It shows every test suite under `src/llmdv/suites/` as a chip in one of four groups, each chip
naming the pytest marker that runs the suite (`-m <marker>`), with a footer saying every case
runs per layer: gateway, router, engine.

Regenerate it from the repo root with:

    python docs/images/make_coverage_svg.py

A new suite must be added to a group in `GROUPS` below, under its package name as the marker.
`tests/test_coverage_graphic.py` fails when the table and the suite packages disagree, or when
the committed SVG is not what this script renders.
"""

from __future__ import annotations

from pathlib import Path

# (group name, accent colour, [(chip label, suite marker), ...])
GROUPS: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "API surfaces",
        "#8f9be0",
        [
            ("OpenAI Chat Completions", "openai_chat"),
            ("OpenAI Completions", "openai_completions"),
            ("OpenAI Responses", "openai_responses"),
            ("Anthropic Messages", "anthropic_messages"),
            ("Model discovery", "discovery"),
        ],
    ),
    (
        "Model behaviour",
        "#a98fe0",
        [
            ("Tool calling", "tools"),
            ("Streaming (SSE)", "streaming"),
            ("Structured output & options", "field_options"),
            ("Image input", "images"),
            ("Long context", "long_context"),
        ],
    ),
    (
        "Failure & resilience",
        "#c393dd",
        [
            ("Error paths & envelopes", "unhappy"),
            ("Transport & TLS", "transport"),
            ("Client scenarios", "scenarios"),
            ("Real clients (Claude Code)", "real_clients"),
        ],
    ),
    (
        "Contract & change",
        "#d39bcf",
        [
            ("Declared surfaces & headers", "contract"),
            ("Render & token parity", "render"),
            ("Prefix-cache reuse", "cache"),
            ("Change sentinels", "evolution"),
            ("Fleet config, metrics & logs", "fleet"),
        ],
    ),
]

LAYERS = ("Gateway", "Router", "Engine")

OUTPUT = Path(__file__).resolve().parent / "llmdv-coverage.svg"

_NUMBER_WORDS = (
    "Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen "
    "Fifteen Sixteen Seventeen Eighteen Nineteen Twenty Twenty-one Twenty-two Twenty-three "
    "Twenty-four Twenty-five Twenty-six Twenty-seven Twenty-eight Twenty-nine Thirty"
).split()

W = 1440
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"
ROW_H, CHIP_H = 62, 50
COL_W, GAP, TOP, X0 = 312, 24, 150, 60


def markers() -> list[str]:
    """Every suite marker the graphic shows, in table order."""
    return [marker for _, _, items in GROUPS for _, marker in items]


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render() -> str:
    """The SVG text, exactly as committed."""
    count = len(markers())
    word = _NUMBER_WORDS[count] if count < len(_NUMBER_WORDS) else str(count)
    ph = 66 + max(len(items) for _, _, items in GROUPS) * ROW_H + 12
    fy = TOP + ph + 62
    h = fy + 46
    o: list[str] = []
    a = o.append
    a(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h}" width="{W}" '
        f'height="{h}" role="img" aria-labelledby="t d">'
    )
    a('<title id="t">What llmdv tests</title>')
    a(
        f'<desc id="d">{word} test suites and growing, in four groups, each with the pytest '
        "marker that runs it: API surfaces, model behaviour, failure and resilience, contract "
        "and change. Every case runs per layer: gateway, router, engine.</desc>"
    )
    a(f'<rect width="{W}" height="{h}" rx="18" fill="#181430"/>')
    a(
        f'<text x="60" y="78" font-family="{FONT}" font-size="34" font-weight="700" '
        f'fill="#f3f0ff">What <tspan font-family="{MONO}">llmdv</tspan> tests</text>'
    )
    a(
        f'<text x="60" y="114" font-family="{FONT}" font-size="19" fill="#a9a1d5">{count} '
        "suites and growing, run against every layer you can reach. Run any one with "
        f'<tspan font-family="{MONO}" fill="#d6cff5">-m &lt;marker&gt;</tspan>.</text>'
    )
    for gi, (name, accent, items) in enumerate(GROUPS):
        x = X0 + gi * (COL_W + GAP)
        a(
            f'<rect x="{x}" y="{TOP}" width="{COL_W}" height="{ph}" rx="14" fill="#221c40" '
            'stroke="#3b3266"/>'
        )
        a(
            f'<rect x="{x}" y="{TOP}" width="{COL_W}" height="5" rx="2.5" fill="{accent}" '
            'fill-opacity="0.85"/>'
        )
        a(
            f'<text x="{x + 22}" y="{TOP + 44}" font-family="{FONT}" font-size="21" '
            f'font-weight="700" fill="{accent}">{_esc(name)}</text>'
        )
        for ii, (label, marker) in enumerate(items):
            y = TOP + 64 + ii * ROW_H
            a(
                f'<rect x="{x + 18}" y="{y}" width="{COL_W - 36}" height="{CHIP_H}" rx="10" '
                'fill="#2a2350" stroke="#433a78"/>'
            )
            cy = y + CHIP_H / 2
            a(f'<circle cx="{x + 42}" cy="{cy}" r="9" fill="{accent}" fill-opacity="0.75"/>')
            a(
                f'<path d="M{x + 37.5} {cy + 0.5} l3 3 l6 -6.5" fill="none" stroke="#181430" '
                'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>'
            )
            a(
                f'<text x="{x + 62}" y="{y + 22}" font-family="{FONT}" font-size="16.5" '
                f'fill="#ece8ff">{_esc(label)}</text>'
            )
            a(
                f'<text x="{x + 62}" y="{y + 40}" font-family="{MONO}" font-size="12.5" '
                f'fill="#8f86bd">-m {marker}</text>'
            )
    a(
        f'<text x="60" y="{fy}" font-family="{FONT}" font-size="18" fill="#a9a1d5">'
        "Every case runs per layer</text>"
    )
    lx = 300
    for label in LAYERS:
        w = 18 * len(label) * 0.6 + 40
        a(
            f'<rect x="{lx}" y="{fy - 24}" width="{w}" height="34" rx="17" fill="#2a2350" '
            'stroke="#6e64a8"/>'
        )
        a(
            f'<text x="{lx + w / 2}" y="{fy - 1}" text-anchor="middle" font-family="{FONT}" '
            f'font-size="17" font-weight="600" fill="#ece8ff">{label}</text>'
        )
        lx += w + 14
    a(
        f'<text x="{lx + 8}" y="{fy}" font-family="{FONT}" font-size="18" fill="#a9a1d5">'
        "· each judged on its own, with the evidence to reproduce it</text>"
    )
    a("</svg>")
    return "\n".join(o) + "\n"


def main() -> None:
    OUTPUT.write_text(render(), encoding="utf-8")


if __name__ == "__main__":
    main()
