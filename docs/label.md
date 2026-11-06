# `caliper label`

`caliper label` is a local, keyboard-first labelling UI. It shows one item at a
time as a card, records a decision with a single key, and writes every label
to an append-only log. Exports go straight into `caliper eval` (golden rows
with `label` / `label_approved`) and `caliper_kappa` (labeled rollouts).
Nothing is installed in the browser, no account is needed, and the data never
leaves your machine.

![caliper label on the catalog_cleanup example](label.png)

```bash
uv run caliper label serve examples/catalog_cleanup/golden.jsonl \
    --config examples/catalog_cleanup/label.yaml
```

This opens `http://127.0.0.1:8765/`. Type your name, then press `1`, `2` or `3`.
Labels go to `build/labels/golden.labels.jsonl` unless you pass `--store`.

- [Workflow](#workflow)
- [Keyboard](#keyboard)
- [Input data](#input-data)
- [Config reference](#config-reference)
- [Storage](#storage)
- [Several labellers and double labelling](#several-labellers-and-double-labelling)
- [Export](#export)
- [CLI reference](#cli-reference)

## Workflow

1. **Name.** On first visit the page asks who is labelling. The name is kept in
   the browser (localStorage) and sent with every label. Click the name chip to
   change it. `--labeller NAME` pre-fills the box.
2. **Queue.** The sidebar splits the dataset into queues by `queue_key` (for
   example the sampler's `stratum`). Each queue shows a progress bar (items
   complete) and how many items are left for you. "All items" walks every queue
   in data order. The choice is remembered.
3. **Card.** Text, links, numbers, badges and images, as configured. Click an
   image or press `Z` to see it full size. Decision keys still work while it is
   open, and the zoom stays open on the next item, so an image-heavy queue can
   be labelled entirely at full size.
4. **Decide.** One key per decision. The label is saved before the next card
   appears. A note (`N`) is attached to the next decision.
5. **Skip / undo.** Skip puts the item behind everything else in the queue; it
   comes back, marked "skipped earlier", when nothing else is left. Undo
   retracts your last label or skip and brings that item back on screen.

## Keyboard

| key | action |
| --- | --- |
| decision keys (`1`/`2`/`3`, `t`/`u`/`f`, ...) | label the card and move on |
| `S` | skip (configurable: `keys.skip`) |
| `Backspace` | undo your last label or skip (configurable: `keys.undo`) |
| `N` | add a note to the next decision; `Enter` or `Esc` returns to the keys (configurable: `keys.note`) |
| `Z` | image full size; `Z` again or `Esc` closes |
| `Esc` | close the zoom, help or agreement overlay |
| `?` | shortcut overlay |

Keys are case-insensitive. `Z`, `?`, `Esc`, `Enter`, `Tab` and `Space` are
reserved. A config that binds a key twice, or binds a reserved key, is
rejected at startup with the clashing pair named. Shortcuts are off while the
note box has focus. A bar along the bottom lists them all (hidden on phones,
where the decision buttons are large and sticky instead).

## Input data

`serve` and `export` take one JSONL file. Its kind is detected from the rows:

| every row has | kind | item id | notes |
| --- | --- | --- | --- |
| `request` (object) and valid as `GoldenRow` | Caliper `GoldenRow` | `id` | `export --format golden` available. Rows with a `request` that are not GoldenRows (e.g. production logs) are labelled as plain rows, with a notice |
| `rollout` (object) | `EvalRow` (`caliper_sdk`) | `id` | rollouts export keeps the rollout and adds `human_label` |
| anything else | plain rows | `id_key` (default `id`), else `row-0000`, `row-0001`, ... | |

Ids must be unique. Labels refer to items by id, so you can re-sort or extend
the data file and keep labelling. Labels for ids that are no longer in the file
are kept in the log and reported at startup.

## Config reference

YAML (`label.yaml`) or TOML (`label.toml`), same keys. The catalog example:

```yaml
title: Catalog cleanup · tax-code review
instructions: Is the tax code production assigned right for this listing? ...

fields:
  - {template: "cards/{id}.svg", type: image, label: Product}
  - {key: metadata.production_tax_code, type: badge, label: Production code}
  - {key: metadata.production_score, type: number, decimals: 2, label: Confidence}
  - {key: reference_outcome, type: badge, label: Outcome}
  - {key: request.messages.-1.content, type: text, label: Listing}

decisions:
  - {key: "1", value: correct, label: Code correct, approved: true, outcome: auto_publish}
  - {key: "2", value: wrong, label: Wrong code, outcome: needs_review}
  - {key: "3", value: unsure, label: Unsure, outcome: needs_review}

keys: {skip: s, undo: backspace, note: n}
queue_key: stratum
category_key: metadata.production_tax_code
source: internal_reviewer
stage: tax_code_review
double_label: 1

rollout:
  prompt: request.messages.-1.content
  response: metadata.production_tax_code
```

| key | default | meaning |
| --- | --- | --- |
| `title`, `instructions` | | shown at the top of the page |
| `id_key` | `id` | dotted path to the item id (plain rows) |
| `fields` | required | what the card shows, in order (below) |
| `decisions` | required | one entry per decision (below) |
| `approved` | `[]` | decision values that mean "approved"; same as `approved: true` on the decision |
| `keys.skip` / `keys.undo` / `keys.note` | `s` / `backspace` / `n` | one character, or `backspace`, `delete` |
| `queue_key` | none (one queue, `all`) | dotted path whose value names the queue |
| `category_key` | none | dotted path whose value is the category for kappa by category |
| `source` | `internal_reviewer` | `LabelSource` recorded on every label: `human_end_user`, `internal_reviewer`, `llm_teacher`, `heuristic` |
| `stage` | none | free-form `LabelRecord.stage` / `GoldenRow.label_stage`, e.g. `triage` |
| `double_label` | 1 | labels from distinct people each item needs (1 to 10) |
| `image_root` | data file's folder | where local image paths resolve (relative to the data file) |
| `rollout.prompt` / `.response` / `.gold` | see [Export](#export) | dotted paths used to build a `Rollout` from non-EvalRow input |

**Paths** are dotted: `metadata.supplier`. Integer parts index lists, and
negative ones count from the end: `request.messages.-1.content` is the last
message.

**Fields** take either `key` (a dotted path) or `template` (a string with
`{dotted.path}` placeholders, e.g. `cards/{id}.svg`), plus:

| `type` | renders | extra keys |
| --- | --- | --- |
| `text` (default) | wrapped text, whitespace kept; objects as pretty JSON | `mono: true` for code |
| `badge` | a coloured pill (colour derived from the value) | |
| `number` | a large tabular number | `decimals` |
| `link` | a link that opens in a new tab; only `http(s)` values are clickable | |
| `image` | a thumbnail; click or `Z` for full size | |

`label` names the field on the card (default: the key). Missing values show
as a dash.

**Images** are either `http(s)` URLs, which the browser loads directly, or
paths relative to `image_root`. The browser never sends a path: it asks for
`/api/image/<item>/<field number>`, and the server resolves that field for that
item. The resolved file must be inside `image_root` (checked after resolving
`..` and symlinks) and have an image extension (`png`, `jpg`, `jpeg`, `gif`,
`webp`, `svg`, `avif`). Otherwise the server answers 403. SVGs are served with
a sandboxing `Content-Security-Policy`.

**Decisions**:

| key | meaning |
| --- | --- |
| `key` | the single key, e.g. `"1"` or `t` (quote digits in YAML to be safe) |
| `value` | what is stored in `LabelRecord.decision`; unique |
| `label` | button text (default: `value`) |
| `approved` | the person approved the output: `GoldenRow.label_approved`, and `human_label` 1 |
| `score` | `human_label` in rollouts exports, 0 to 1 (default 1 if approved, else 0) |
| `outcome` | `GoldenRow.label` on golden export (default: `value`) |

YAML 1.1 reads bare `yes`, `no`, `on` and `off` as booleans. A decision value
written that way is rejected with a hint to quote it.

## Storage

The default store is a JSONL file of `LabelRecord`s
(`caliper_eval.types.LabelRecord`), one per line:

```json
{"item_id": "lst-0003", "decision": "wrong", "skipped": false, "labeller": "ana",
 "source": "internal_reviewer", "stage": "tax_code_review",
 "labelled_at": "2026-11-06T14:02:11.482913Z", "queue": "common",
 "category": "CTX-100", "note": "two codes fit", "undoes": null,
 "record_id": "7f3c0d9e5b1a2c4d6e8f0a1b"}
```

- **Append-only.** Records are never rewritten. Each write is flushed and fsynced
  before the next card appears, so a crash loses at most the label being
  written. A torn last line (an interrupted write, never acknowledged) is
  ignored when reading and trimmed when `serve` reopens the store, with a notice.
  `export` only reads, so it is safe to run while `serve` is up.
- **Undo** appends a record with `decision: null` and `undoes: <record_id>`.
- **Skip** appends a record with `skipped: true` and `decision: null`.
- **Effective labels:** a record is active unless a later record undoes it. Per
  item and labeller, the latest active record wins. Skips never count as labels.
- **Default path:** `build/labels/<data file stem>.labels.jsonl`, outside the
  data folder. Restarting `serve` with the same store picks up where you left
  off.
- **SQLite:** pass `--store labels.sqlite` (or `.db`) to keep the same records in
  one SQLite table (WAL, synchronous writes). It behaves the same way.

## Several labellers and double labelling

Several people can label at once against one `serve` process (share the URL on
your network with `--host 0.0.0.0`). There is no login: the name each person
types is recorded on their labels.

While a card is on someone's screen, it holds a 10-minute lease. Another
labeller is not served an item whose remaining labels are all leased, so two
people working one queue do not label the same item twice by accident.

With `double_label: N` (N ≥ 2):

- every item needs labels from N different people before it counts as complete;
- a labeller is never served an item they already labelled;
- items someone else has already labelled come first, so the overlap set fills
  early;
- the **Agreement** view (top right) shows inter-annotator kappa overall and by
  `category_key`, the share of items where everyone agrees, and the items where
  labellers disagree, with their notes.

Kappa comes from `caliper_eval.inter_rater.inter_rater_from_records` (built on
`caliper_kappa.inter_rater_kappa`). It uses Cohen's kappa when exactly two people
labelled the overlap items and Fleiss' kappa for three or more, so rotating pairs
work too. Only human sources (`internal_reviewer`, `human_end_user`) count:
agreement with an `llm_teacher` is teacher agreement, not inter-annotator
agreement. Kappa is shown as "undefined" when it is (for example, when every
label is the same), never as a misleading 0.

## Export

```bash
uv run caliper label export --data examples/catalog_cleanup/golden.jsonl \
    --config examples/catalog_cleanup/label.yaml --out build/golden.labelled.jsonl
```

**Consensus.** Each item resolves from its active labels:

| situation | result |
| --- | --- |
| no labels | left out (`unlabelled`) |
| fewer than `double_label` labels | left out (`incomplete`) |
| all labels agree | resolved to that decision |
| labels disagree, `--conflicts exclude` (default) | left out (`conflict`) |
| labels disagree, `--conflicts majority` | strict majority wins; ties are left out |

The summary on stdout counts each group, and every item left out is listed on
stderr with who said what. Nothing is dropped silently. Resolve conflicts by
discussing them and having one labeller undo, or by adding a third label and
exporting with `--conflicts majority`.

**`--format golden`** (GoldenRow input only). Each resolved row is written back
unchanged except for:

- `label`: the decision's `outcome` (or its `value`);
- `label_approved`: whether the decision is approved;
- `label_source`, `label_stage`: from the records;
- `metadata.labelling`: `human_decision`, `labellers`, `labelled_at`, `notes`.

The file loads with `caliper eval`'s golden loader and feeds its calibration
(`label_approved`). `--include-unlabelled` also writes the rows without a
resolved label, untouched.

**`--format rollouts`** writes `EvalRow` JSONL with
`rollout.metadata.human_label`, the same shape as
[`examples/invoice_totals/labeled_rollouts.jsonl`](../examples/invoice_totals/labeled_rollouts.jsonl).
It works with `caliper_kappa.compute_kappa` and calibration with no conversion:

```python
labels = LabelSet(name="human", rows=[
    LabeledRow(row_id=r.id, label_score=float(r.rollout.metadata["human_label"])) for r in rows])
```

`human_label` is the decision's `score`: 1 when approved, 0 otherwise, unless
the decision sets its own score. Integers are written as `1` and `0`.
`metadata` also carries `human_decision`, `labellers`, `label_source`,
`label_stage`, `labelled_at` and `notes`. A score of 0.5 lands in the "pass"
bucket under `compute_kappa`'s default bins (`[0, 0.5, 1]`). If you give
"unsure" a score of 0.5, pass three-bucket edges such as `[0, 0.25, 0.75, 1]`.

For EvalRow input the rollout is kept as is. For other input the rollout is
built from `rollout.prompt` / `rollout.response` / `rollout.gold` paths. The
default prompt is the request rendered as `[system]` / `[role]` blocks, and the
default response is `reference_outcome`. The row's `metadata` and `stratum` are
copied into the rollout's metadata.

**`--format records`** writes the raw log (every label, skip and undo), for
`caliper_eval.inter_rater.inter_rater_from_records` or your own analysis.

## CLI reference

```
caliper label serve DATA.jsonl --config label.yaml [--port 8765] [--host 127.0.0.1]
                    [--labeller NAME] [--store labels.jsonl|labels.sqlite] [--no-browser]

caliper label export --data DATA.jsonl --config label.yaml --out OUT.jsonl
                     [--store labels.jsonl] [--format golden|rollouts|records]
                     [--conflicts exclude|majority] [--include-unlabelled]
```

`--store` defaults to `build/labels/<data stem>.labels.jsonl` for both commands.
Exit codes: 0 ok, 2 usage or config error (bad config, unreadable data, missing
store, golden export from non-golden input).

`scripts/smoke_label.py` runs the whole loop offline. It has two fictional
labellers label the catalog example through the HTTP API with double labelling
on, then reads the agreement, exports both formats and computes `compute_kappa`
on the rollouts. `packages/label/e2e/label_e2e.mjs` is an optional browser
check. It needs `playwright-core`, set through `PLAYWRIGHT_CORE`, and skips
cleanly without it.
