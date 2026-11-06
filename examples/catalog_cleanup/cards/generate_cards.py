# ruff: noqa: E501  (SVG markup template)
"""Regenerate the fictional product placeholder cards used by label.yaml.

    uv run python examples/catalog_cleanup/cards/generate_cards.py

One small SVG per golden row (``cards/<id>.svg``): a flat "product photo"
placeholder with the item name from the listing. Deterministic; no real
products, brands or photos.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE.parent / "golden.jsonl"


def _item_name(listing: str) -> str:
    for line in listing.splitlines():
        if line.startswith("Item:"):
            return line[len("Item:") :].split("|")[0].strip()
    return "unnamed item"


def _hue(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:4], 16) % 360


def card_svg(row_id: str, name: str, supplier: str) -> str:
    h = _hue(name.lower().split()[-1] if name else row_id)
    lines = textwrap.wrap(name, 22)[:3]
    text = "".join(
        f'<tspan x="160" dy="{0 if i == 0 else 22}">{escape(t)}</tspan>'
        for i, t in enumerate(lines)
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 320" width="320" height="320">
<rect width="320" height="320" rx="18" fill="hsl({h} 45% 90%)"/>
<g transform="translate(160 118)" fill="none" stroke="hsl({h} 40% 35%)" stroke-width="5" stroke-linejoin="round">
<path d="M-58 -22 L0 -50 L58 -22 L58 42 L0 70 L-58 42 Z" fill="hsl({h} 50% 80%)"/>
<path d="M-58 -22 L0 6 L58 -22 M0 6 L0 70"/>
<path d="M-29 -36 L29 -8" stroke-width="4"/>
</g>
<text x="160" y="226" text-anchor="middle" font-family="system-ui, sans-serif" font-size="18" font-weight="600" fill="hsl({h} 40% 22%)">{text}</text>
<text x="160" y="300" text-anchor="middle" font-family="ui-monospace, monospace" font-size="12" fill="hsl({h} 25% 40%)">{escape(row_id)} · fictional placeholder</text>
</svg>
"""


def main() -> None:
    n = 0
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        listing = row["request"]["messages"][-1]["content"]
        supplier = row.get("metadata", {}).get("supplier", "")
        svg = card_svg(row["id"], _item_name(listing), supplier)
        (HERE / f"{row['id']}.svg").write_text(svg, encoding="utf-8")
        n += 1
    print(f"wrote {n} cards to {HERE}")


if __name__ == "__main__":
    main()
