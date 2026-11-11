"""Generate Caliper's brand SVGs (mark, README hero, social preview) into docs/brand/.

    uv run python docs/brand/make_brand.py          # writes the .svg files
    node docs/brand/render.mjs                      # rasterizes them to .png (Playwright)

The mark is a vernier caliper: a ruled beam, a fixed jaw and a sliding jaw, and
a measured gap between them. Two themes: dark (GitHub dark) and light.
"""

# ruff: noqa: E501  (SVG markup reads better unwrapped)
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).parent

THEMES = {
    "dark": {
        "bg": "#0d1117",
        "bg2": "#161b22",
        "ink": "#e6edf3",
        "ink2": "#9da7b3",
        "line": "#30363d",
        "accent": "#4f8cff",
        "good": "#7ee787",
        "tick": "#0d1117",
    },
    "light": {
        "bg": "#ffffff",
        "bg2": "#f6f8fa",
        "ink": "#1f2328",
        "ink2": "#59636e",
        "line": "#d1d9e0",
        "accent": "#2c55c9",
        "good": "#1a7f37",
        "tick": "#ffffff",
    },
}

TAGLINE = ["Know when a model or prompt change breaks", "your AI feature, before your users do."]
FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO = "'SF Mono', Menlo, Consolas, monospace"


def caliper(t: dict[str, str], *, x: float, y: float, s: float) -> str:
    """The caliper drawing in a 512-unit box, placed at (x, y) and scaled by s."""
    ticks = []
    for i, tx in enumerate(range(164, 290, 14)):
        h = 22 if i % 5 == 0 else 13
        ticks.append(
            f'<rect x="{tx - 2}" y="166" width="4" height="{h}" rx="1" fill="{t["tick"]}"/>'
        )
    return f"""
<g transform="translate({x} {y}) scale({s})">
  <!-- beam -->
  <rect x="72" y="150" width="368" height="56" rx="12" fill="{t["ink"]}"/>
  {"".join(ticks)}
  <!-- fixed jaw -->
  <path d="M72 194 H150 V400 L118 368 L72 300 Z" fill="{t["ink"]}"/>
  <!-- sliding jaw -->
  <rect x="300" y="132" width="88" height="92" rx="14" fill="{t["accent"]}"/>
  <rect x="318" y="166" width="52" height="24" rx="5" fill="{t["tick"]}" opacity="0.9"/>
  <path d="M300 214 H362 V300 L332 368 L300 400 Z" fill="{t["accent"]}"/>
  <!-- the measured gap -->
  <path d="M170 318 H280" stroke="{t["good"]}" stroke-width="12" stroke-linecap="round"/>
  <path d="M186 300 L166 318 L186 336 M264 300 L284 318 L264 336" fill="none"
        stroke="{t["good"]}" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>
</g>"""


def mark(theme: str) -> str:
    t = THEMES[theme]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="112" fill="{t["bg"]}"/>
  <rect x="1.5" y="1.5" width="509" height="509" rx="110.5" fill="none" stroke="{t["line"]}" stroke-width="3"/>
  {caliper(t, x=0, y=-20, s=1)}
</svg>
"""


def scale_rule(t: dict[str, str], *, width: int, y: int) -> str:
    """A vernier-style rule across the bottom: main scale above, vernier below."""
    parts = [f'<rect x="0" y="{y}" width="{width}" height="1.5" fill="{t["line"]}"/>']
    for i, x in enumerate(range(0, width + 1, 16)):
        h = 18 if i % 10 == 0 else (11 if i % 5 == 0 else 6)
        parts.append(f'<rect x="{x}" y="{y - h}" width="1.5" height="{h}" fill="{t["line"]}"/>')
    for i, x in enumerate(range(0, width + 1, 15)):  # vernier: 15 vs 16, they drift apart
        h = 10 if i % 5 == 0 else 5
        parts.append(f'<rect x="{x}" y="{y + 2}" width="1.5" height="{h}" fill="{t["line"]}"/>')
    return "".join(parts)


def banner(theme: str, *, width: int, height: int, tagline_lines: list[str]) -> str:
    t = THEMES[theme]
    mark_size = min(height * 0.62, 300)
    s = mark_size / 512
    word = height * 0.24 if height < 500 else 118
    tag = word * 0.255
    labels = ("noise floor", "cost per row", "CI gate")
    chip_w = [len(label) * tag * 0.44 + tag * 1.3 for label in labels]
    text_w = max(20.7 * tag, sum(chip_w) + tag * 0.5 * (len(labels) - 1))
    gap = width * 0.035
    mx = (width - (mark_size + gap + text_w)) / 2  # centre the mark + text block
    my = (height - mark_size) / 2 - height * 0.03
    tx = mx + mark_size + gap
    ty = height / 2 - (len(tagline_lines) * tag * 1.35) / 2 + word * 0.18
    lines = "".join(
        f'<text x="{tx}" y="{ty + word * 0.1 + (i + 1) * tag * 1.35}" font-family="{FONT}" '
        f'font-size="{tag:.1f}" fill="{t["ink2"]}">{line}</text>'
        for i, line in enumerate(tagline_lines)
    )
    chips_y = ty + word * 0.1 + (len(tagline_lines) + 1) * tag * 1.35 + tag * 0.9
    chips, cx = [], tx
    for label, w in zip(labels, chip_w, strict=True):
        chips.append(
            f'<rect x="{cx}" y="{chips_y - tag * 1.05}" width="{w:.1f}" height="{tag * 1.55:.1f}" '
            f'rx="{tag * 0.78:.1f}" fill="{t["bg2"]}" stroke="{t["line"]}" stroke-width="1.5"/>'
            f'<text x="{cx + tag * 0.65:.1f}" y="{chips_y:.1f}" font-family="{MONO}" '
            f'font-size="{tag * 0.72:.1f}" fill="{t["ink"]}">{label}</text>'
        )
        cx += w + tag * 0.5
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="{width}" height="{height}" fill="{t["bg"]}"/>
  {scale_rule(t, width=width, y=height - 34)}
  {caliper(t, x=mx, y=my, s=s)}
  <text x="{tx}" y="{ty}" font-family="{FONT}" font-weight="700" font-size="{word:.1f}"
        letter-spacing="{-word * 0.03:.1f}" fill="{t["ink"]}">caliper</text>
  {lines}
  {"".join(chips)}
</svg>
"""


def main() -> None:
    files = {}
    for theme in THEMES:
        files[f"caliper-mark-{theme}.svg"] = mark(theme)
        files[f"caliper-hero-{theme}.svg"] = banner(
            theme,
            width=1280,
            height=360,
            tagline_lines=TAGLINE,
        )
    files["caliper-social.svg"] = banner(
        "dark",
        width=1280,
        height=640,
        tagline_lines=TAGLINE,
    )
    for name, svg in files.items():
        (OUT / name).write_text(svg, encoding="utf-8")
        print(f"wrote docs/brand/{name}")


if __name__ == "__main__":
    main()
