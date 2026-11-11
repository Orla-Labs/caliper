"""Regenerate logs.jsonl and golden.jsonl for catalog_cleanup.

Deterministic: choices come from sha256 digests, no clock, no network. Run
from the repo root:

    uv run python examples/catalog_cleanup/generate.py

Everything is synthetic and fictional: suppliers, brands, products, the CTX
tax-category scheme, and every number. Nothing here describes a real shop.

* ``logs.jsonl`` is a fake production log: one row per supplier listing the
  feature processed, with the exact request it sent, what it decided
  (``outcome``), the tax code it filled, and the reviewer's verdict where a
  person looked at the record. Rare product families are a few percent of
  traffic, as they would be in a real catalog.
* ``golden.jsonl`` is the labeled golden set built from those logs: every
  rare-family, mixed-family and lookalike listing, plus the first common ones.
  ``caliper sample`` (see README) shows the stratified pick; the golden file
  then adds a human label to every row.

Golden rows also carry (all fictional, all deterministic):

* ``split``: within each stratum, rows alternate between ``val`` and ``test``
  in digest order, so every hard stratum appears on both sides. Thresholds
  are picked on val and reported on test.
* ``label_source`` / ``label_stage``: most rows were checked by an internal
  reviewer (human truth). Some lookalike, mixed and common rows were labelled
  by an LLM teacher instead. The teacher is fooled by the same misleading
  keyword as the production model, so on lookalikes it "approves" a wrong
  code: its agreement is not accuracy. Two common rows have no recorded
  source (``unknown``).
* ``slices``: ``rare_code`` (must never be lost) and ``lookalike`` (must not
  regress), used by the ``[[slices]]`` gates in ``caliper.toml``.

Production outcomes are computed by replaying ``fake-large`` through
``decide.py`` on a replica index the eval never uses, so the golden set's
``reference_outcome`` is consistent with, but not identical to, a fresh
baseline run. That gap is the noise floor.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRODUCTION_REPLICA = 99
PRODUCTION_MODEL = "fake-large"
REPLIES_PER_ROW = 3

SYSTEM_PROMPT = """\
You fill product-catalog records for an online marketplace from raw supplier listings.

Return only a JSON object with these keys:
  "description": one plain sentence describing the product, no marketing claims
  "specs": an object with the listed attributes, lowercase keys
  "tax_code": exactly one code from the list below
  "confidence": your confidence that tax_code is right, a number from 0 to 1

Tax-category codes (a fictional scheme used only in this example):
  CTX-100 general merchandise
  CTX-210 packaged food
  CTX-310 printed books and maps
  CTX-420 children's clothing
  CTX-430 adult clothing and footwear
  CTX-510 small electronics
  CTX-520 loose batteries and power cells
  CTX-610 home health devices
  CTX-900 gift cards and vouchers

If a listing could fit two codes, pick the better one and lower your confidence."""

SUPPLIERS = [
    ("Puddlejump Wholesale", "PJ"),
    ("Northbeam Trading Co.", "NB"),
    ("Kestrel & Finch Supply", "KF"),
    ("Marrowby Goods", "MG"),
    ("Tinwhistle Imports", "TW"),
    ("Saltgrass Distribution", "SG"),
]
BRANDS = ["Glimmerkin", "Woolbarrow", "Moonwick", "Fennick", "Puddlecat", "Brambleton"]
COLOURS = ["slate", "moss", "oat", "ink blue", "rust", "chalk"]
NOTES = [
    "ships in 2 days",
    "min order 6",
    "carton dims on request",
    "restock expected next month",
    "photos in shared folder",
    "price excl. delivery",
]

# (product name, true tax code, attribute key, attribute values)
COMMON = [
    ("over-ear headphones", "CTX-510", "colour", COLOURS),
    ("4-port usb hub", "CTX-510", "cable", ["0.5 m", "1 m"]),
    ("bluetooth speaker", "CTX-510", "colour", COLOURS),
    ("smart plug", "CTX-510", "socket", ["type A", "type C"]),
    ("ceramic mug 350ml", "CTX-100", "colour", COLOURS),
    ("throw pillow", "CTX-100", "size", ["40x40 cm", "50x50 cm"]),
    ("phone case", "CTX-100", "fits", ["model S", "model M", "model L"]),
    ("terracotta plant pot", "CTX-100", "diameter", ["12 cm", "18 cm"]),
    ("paperback novel 'The Lantern Cartographer'", "CTX-310", "pages", ["312"]),
    ("pocket road atlas", "CTX-310", "edition", ["2nd", "3rd"]),
    ("hazelnut granola 500g", "CTX-210", "best before", ["9 months"]),
    ("cold-pressed olive oil 1L", "CTX-210", "bottle", ["glass", "tin"]),
    ("herbal tea, chamomile, 20 bags", "CTX-210", "caffeine", ["none"]),
    ("toddler fleece pyjamas", "CTX-420", "size", ["2-3Y", "3-4Y"]),
    ("adult rain jacket", "CTX-430", "size", ["S", "M", "L", "XL"]),
    ("canvas sneakers", "CTX-430", "size", ["38", "41", "44"]),
    ("merino wool socks, 3-pack", "CTX-430", "size", ["S/M", "L/XL"]),
]
RARE = [
    ("lithium coin cell 3V, 4-pack", "CTX-520", "voltage", ["3 V"]),
    ("rechargeable AA cells, 4-pack", "CTX-520", "capacity", ["2000 mAh"]),
    ("battery pack 10000mAh", "CTX-520", "output", ["USB-C"]),
    ("digital ear thermometer", "CTX-610", "probe covers", ["20"]),
    ("upper-arm pressure cuff monitor", "CTX-610", "cuff", ["22-36 cm"]),
    ("fingertip pulse meter", "CTX-610", "display", ["OLED"]),
    ("gift card, 25 store credits", "CTX-900", "delivery", ["printed", "email"]),
    ("spa voucher, one visit", "CTX-900", "valid", ["12 months"]),
]
MIXED = [  # two product families in one listing; the first one mentioned is not always right
    ("kids rain jacket", "CTX-420", "size", ["6-7Y", "8-9Y"]),
    ("cookbook with spice sampler", "CTX-310", "pages", ["180"]),
    ("herbal tea gift set with ceramic mug", "CTX-210", "contents", ["tea + mug"]),
    ("kids headphones, volume-limited", "CTX-510", "colour", COLOURS),
    ("toddler sneakers", "CTX-420", "size", ["22", "24"]),
    ("olive oil and ceramic mug gift box", "CTX-210", "contents", ["oil + mug"]),
    ("speaker with phone case bundle", "CTX-510", "colour", COLOURS),
    ("wool socks and paperback reading kit", "CTX-430", "contents", ["socks + book"]),
]
LOOKALIKE = [  # a keyword points confidently at the wrong family
    ("gift card holder wallet (card not included)", "CTX-100", "material", ["felt"]),
    ("thermometer-style wall clock", "CTX-100", "diameter", ["30 cm"]),
    ("sneakers-shaped keyring", "CTX-100", "material", ["enamel"]),
    ("atlas-print tote bag", "CTX-100", "material", ["cotton"]),
    ("speaker stand, solid oak (no electronics)", "CTX-100", "height", ["60 cm"]),
]

UNLISTED = [  # nothing the model recognises: low confidence, a person decides
    ("hand-carved walnut chess set", "CTX-100", "board", ["35 cm"]),
    ("cork yoga block", "CTX-100", "density", ["medium"]),
    ("cedar bird feeder", "CTX-100", "capacity", ["1 L"]),
    ("brass door wedge", "CTX-100", "weight", ["400 g"]),
]

COUNTS = {"common": 250, "rare_code": 14, "mixed": 20, "lookalike": 8, "unlisted": 8}  # 300
GOLDEN_COMMON = 30  # golden = all non-common rows + this many common ones


def _digest(*parts: object) -> int:
    return int.from_bytes(
        hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()[:8], "big"
    )


def _pick(seq, *parts):
    return seq[_digest(*parts) % len(seq)]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"catalog_{name}", HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_listing(kind: str, i: int) -> tuple[str, str, str]:
    """(listing text, true tax code, supplier)."""
    table = {
        "common": COMMON,
        "rare_code": RARE,
        "mixed": MIXED,
        "lookalike": LOOKALIKE,
        "unlisted": UNLISTED,
    }[kind]
    name, code, attr, values = table[i % len(table)]
    supplier, prefix = _pick(SUPPLIERS, kind, i, "supplier")
    brand = _pick(BRANDS, kind, i, "brand")
    value = _pick(values, kind, i, "value")
    pack = _pick(["1", "2", "6", "12"], kind, i, "pack")
    note = _pick(NOTES, kind, i, "note")
    ref = f"{prefix}-{1000 + _digest(kind, i, 'ref') % 9000}"
    item = f"{brand} {name}"
    if _digest(kind, i, "shout") % 4 == 0:  # some suppliers shout
        item = item.upper()
    text = (
        f"Supplier: {supplier} (fictional)\n"
        f"Ref: {ref}\n"
        f"Item: {item} | {attr}: {value} | pack: {pack}\n"
        f"Notes: {note}"
    )
    return text, code, supplier


def make_request(listing: str) -> dict:
    return {
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": listing}],
        "max_tokens": 400,
    }


def label_provenance(row_id: str, kind: str) -> tuple[str | None, str | None]:
    """(label_source, label_stage) for a golden row. Rare rows always get a reviewer."""
    if kind == "lookalike" and _digest(row_id, "source") % 2 == 0:
        return "llm_teacher", "teacher_pass"
    if kind in ("mixed", "common") and _digest(row_id, "source") % 6 == 0:
        return "llm_teacher", "teacher_pass"
    if kind == "common" and _digest(row_id, "source") % 6 == 1:
        return None, None  # source never recorded
    return "internal_reviewer", "final_review"


def teacher_approves(row_id: str, kind: str, approved: bool) -> bool:
    """The LLM teacher's verdict: fooled by lookalikes, occasionally wrong elsewhere."""
    if kind == "lookalike":
        return True  # the misleading keyword convinces the teacher too
    if _digest(row_id, "teacher") % 8 == 0:
        return not approved
    return approved


def assign_splits(golden: list[dict]) -> None:
    """Alternate val/test within each stratum, in digest order."""
    by_kind: dict[str, list[dict]] = {}
    for g in golden:
        by_kind.setdefault(g["stratum"], []).append(g)
    for rows in by_kind.values():
        rows.sort(key=lambda g: _digest(g["id"], "split"))
        for i, g in enumerate(rows):
            g["split"] = "val" if i % 2 == 0 else "test"


def main() -> None:
    fake = _load("fake_models")
    decide = _load("decide")
    from caliper_eval.types import Reply

    kinds = [k for k, n in COUNTS.items() for _ in range(n)]
    per_kind: dict[str, int] = {}
    tagged = []
    for k in kinds:
        idx = per_kind.get(k, 0)
        per_kind[k] = idx + 1
        tagged.append((k, idx))
    tagged.sort(key=lambda t: _digest("order", t[0], t[1]))  # interleave, deterministically

    logs, golden = [], []
    common_in_golden = 0
    for n, (kind, idx) in enumerate(tagged):
        row_id = f"lst-{n:04d}"
        listing, true_code, supplier = make_listing(kind, idx)
        request = make_request(listing)
        replies = [
            Reply(
                text=fake.respond(
                    request,
                    model=PRODUCTION_MODEL,
                    row_id=row_id,
                    replica=PRODUCTION_REPLICA,
                    sample=s,
                )["text"]
            )
            for s in range(REPLIES_PER_ROW)
        ]
        d = decide.decide(replies)
        code = d.detail.get("tax_code")
        approved = code == true_code
        reviewed = d.outcome != "auto_publish" or _digest(row_id, "audit") % 20 == 0
        logs.append(
            {
                "id": row_id,
                "request": request,
                "outcome": d.outcome,
                "tax_code": code,
                "score": d.score,
                "review": ("approved" if approved else "corrected") if reviewed else None,
            }
        )
        take = kind != "common" or common_in_golden < GOLDEN_COMMON
        if take:
            common_in_golden += kind == "common"
            source, stage = label_provenance(row_id, kind)
            verdict = approved
            if source == "llm_teacher":
                verdict = teacher_approves(row_id, kind, approved)
            golden.append(
                {
                    "id": row_id,
                    "request": request,
                    "reference_outcome": d.outcome,
                    "label": "auto_publish" if verdict else "needs_review",
                    "label_approved": verdict,
                    "label_source": source,
                    "label_stage": stage,
                    "stratum": kind,
                    "slices": [kind] if kind in ("rare_code", "lookalike") else [],
                    "metadata": {
                        "true_tax_code": true_code,
                        "production_tax_code": code,
                        "production_score": d.score,
                        "supplier": supplier,
                        "synthetic": True,
                    },
                }
            )

    assign_splits(golden)
    for path, rows in ((HERE / "logs.jsonl", logs), (HERE / "golden.jsonl", golden)):
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"wrote {len(logs)} log rows and {len(golden)} golden rows")


if __name__ == "__main__":
    main()
