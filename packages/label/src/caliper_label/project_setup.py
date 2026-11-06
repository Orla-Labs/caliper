"""Project setup helpers (dev 2): templates, validation, label-option migrations, legacy adapter.

Everything here is pure (no I/O beyond reading a legacy config), so the wizard, Project
settings, `caliper label init` and the legacy `serve` path share one implementation.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from .project import (
    CardConfig,
    CardField,
    DisplayType,
    GoldenCheck,
    Keys,
    LabelOption,
    LabelsConfig,
    LabelType,
    ProjectConfig,
    QueueConfig,
    RolloutConfig,
    ScoreRange,
    SourceConfig,
    SourceKind,
    WorkflowConfig,
)
from .typedetect import ISO_DATETIME

# Keys the labelling screen binds itself (same set as the legacy config) plus Backspace etc.
RESERVED_KEYS = {"z", "?", "escape", "enter", "tab", " "}
SPECIAL_KEYS = {"backspace", "delete", "escape", "enter", "tab", "space"}

# Option colours: all give >= 4.5:1 contrast with white text (WCAG AA), light and dark themes.
PALETTE = [
    "#2e7d32",  # green
    "#c62828",  # red
    "#a15c00",  # amber
    "#1565c0",  # blue
    "#6a1b9a",  # purple
    "#00695c",  # teal
    "#ad1457",  # pink
    "#5d4037",  # brown
    "#455a64",  # slate
]


def norm_key(key: str) -> str:
    """Compare keys the way the labeller does: `KeyboardEvent.key`, lowercased."""
    if len(key) == 1:
        return key.lower()
    low = key.strip().lower()
    if low == "esc":
        low = "escape"
    return " " if low == "space" else low


# --------------------------------------------------------------------------- templates


class Template(BaseModel):
    id: str
    name: str
    description: str
    needs_categories: bool = False


TEMPLATES = [
    Template(
        id="correct_wrong_unsure",
        name="Correct / Wrong / Unsure",
        description="Check one output. Correct is a positive answer; Unsure asks for a note.",
    ),
    Template(
        id="true_uncertain_false",
        name="True / Uncertain / False",
        description="Fact or claim checking. True counts as a positive answer.",
    ),
    Template(
        id="categories",
        name="Category list",
        description="Pick one category. Paste one category per line.",
        needs_categories=True,
    ),
    Template(
        id="rating_1_5",
        name="1-5 rating",
        description="A five-point quality scale. 4 and 5 count as positive answers.",
    ),
    Template(
        id="yes_no_unsure",
        name="Yes / No / Unsure",
        description="The yes/no/unsure preset. Yes counts as a positive answer.",
    ),
]


def slug(text: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", text.strip().lower()).strip("_")
    return s or "option"


def auto_hotkeys(options: list[LabelOption], taken: set[str] | None = None) -> list[LabelOption]:
    """Give options without a hotkey the next free key of 1..9 (then 0). Retired ones get none."""
    used = {norm_key(o.hotkey) for o in options if o.hotkey and not o.retired} | (taken or set())
    free = [k for k in "1234567890" if k not in used]
    out = []
    for o in options:
        if not o.hotkey and not o.retired and free:
            o = o.model_copy(update={"hotkey": free.pop(0)})
        out.append(o)
    return out


# Colours that carry meaning. Plain categories get CATEGORY_PALETTE, which avoids green/red/
# amber so two unrelated categories never read as "good" and "bad". All are AA on white text.
GOOD, BAD, UNSURE, NEUTRAL = "#2e7d32", "#c62828", "#a15c00", "#455a64"
CATEGORY_PALETTE = ["#1565c0", "#6a1b9a", "#00695c", "#ad1457", "#5d4037", "#283593", "#4e342e"]
# Ordered scale, least to most severe: slate, amber, deep orange, red, dark red.
SEVERITY_RAMP = ["#455a64", "#a15c00", "#bf360c", "#c62828", "#8e0000"]
_RAMP_PICK = {1: [0], 2: [0, 3], 3: [0, 1, 3], 4: [0, 1, 2, 3], 5: [0, 1, 2, 3, 4]}

_SEMANTIC = {
    GOOD: "correct right yes true positive pass passed good valid accept accepted approve "
    "approved ok safe relevant helpful",
    BAD: "wrong incorrect no false negative fail failed bad invalid reject rejected spam toxic "
    "unsafe irrelevant harmful abusive",
    UNSURE: "unsure uncertain maybe mixed unknown unclear partial partially",
    NEUTRAL: "neutral other none n_a na",
}
_SEMANTIC_OF = {w: c for c, words in _SEMANTIC.items() for w in words.split()}
_SEVERITY_RANK = {
    "lowest": 0, "trivial": 0, "none": 0,
    "low": 1, "minor": 1,
    "medium": 2, "med": 2, "normal": 2, "moderate": 2,
    "high": 3, "major": 3, "severe": 3,
    "urgent": 4, "critical": 4, "highest": 4,
    "blocker": 5, "emergency": 5,
}  # fmt: skip
_P_LEVEL = re.compile(r"^(p|sev|s)(\d)$")


def category_colors(names: list[str]) -> list[str]:
    """Colours for a list of option names, so colour carries meaning.

    - An ordered scale (low/medium/high/urgent, minor/major/critical, P0-P4) gets a ramp from
      slate to red, in severity order (P0 is the most severe).
    - Sentiment / verdict words get semantic colours (correct/positive green, wrong/negative red,
      unsure amber, neutral/other slate); the rest share CATEGORY_PALETTE.
    - Plain categories cycle CATEGORY_PALETTE.
    """
    keys = [slug(n) for n in names]
    if len(keys) >= 2:
        ranks: list[float] | None = None
        if all(k in _SEVERITY_RANK for k in keys):
            ranks = [_SEVERITY_RANK[k] for k in keys]
        elif all(_P_LEVEL.match(k) for k in keys):
            ranks = [-int(_P_LEVEL.match(k).group(2)) for k in keys]  # type: ignore[union-attr]
        if ranks is not None and len(set(ranks)) == len(ranks) and len(ranks) <= 5:
            order = sorted(range(len(ranks)), key=lambda i: ranks[i])
            pick = _RAMP_PICK[len(ranks)]
            out = [""] * len(ranks)
            for pos, i in enumerate(order):
                out[i] = SEVERITY_RAMP[pick[pos]]
            return out
    out, n = [], 0
    for k in keys:
        c = _SEMANTIC_OF.get(k)
        if c is None:
            c = CATEGORY_PALETTE[n % len(CATEGORY_PALETTE)]
            n += 1
        out.append(c)
    return out


def auto_colors(options: list[LabelOption]) -> list[LabelOption]:
    """Fill in missing option colours with `category_colors` (meaningful, never random)."""
    colors = category_colors([o.label or o.value for o in options])
    return [
        o if o.color else o.model_copy(update={"color": c})
        for o, c in zip(options, colors, strict=True)
    ]


def severity_order(names: list[str]) -> list[str] | None:
    """`names` in their natural order if they form a known scale, else None.

    Severity words and P-levels go least to most severe; verdict / sentiment words go
    positive, unsure, neutral, negative.
    """
    keys = [slug(n) for n in names]
    if all(k in _SEVERITY_RANK for k in keys):
        return [
            n for _, n in sorted(zip(keys, names, strict=True), key=lambda t: _SEVERITY_RANK[t[0]])
        ]
    if all(k in _SEMANTIC_OF for k in keys):
        rank = {GOOD: 0, UNSURE: 1, NEUTRAL: 2, BAD: 3}
        return [
            n
            for _, n in sorted(
                zip(keys, names, strict=True), key=lambda t: rank[_SEMANTIC_OF[t[0]]]
            )
        ]
    if all(_P_LEVEL.match(k) for k in keys):  # P3 (least) .. P0 (most)
        return sorted(names, key=lambda n: -int(_P_LEVEL.match(slug(n)).group(2)))  # type: ignore[union-attr]
    return None


def template_labels(
    template: str, categories: str | list[str] | None = None, *, natural_order: bool = False
) -> LabelsConfig:
    """LabelsConfig for a template id (see TEMPLATES).

    ``natural_order``: categories that form a known scale (low/medium/high, P0-P3) are put in
    severity order, so hotkeys 1..n go from least to most severe. Used for values suggested
    from the data, which arrive in frequency order.
    """
    green, red, amber = PALETTE[0], PALETTE[1], PALETTE[2]
    if template == "correct_wrong_unsure":
        opts = [
            LabelOption(value="correct", label="Correct", color=green, approved=True),
            LabelOption(value="wrong", label="Wrong", color=red),
            LabelOption(value="unsure", label="Unsure", color=amber, requires_note=True),
        ]
        kind = LabelType.SINGLE
    elif template == "true_uncertain_false":
        opts = [
            LabelOption(value="true", label="True", color=green, approved=True),
            LabelOption(value="uncertain", label="Uncertain", color=amber, requires_note=True),
            LabelOption(value="false", label="False", color=red),
        ]
        kind = LabelType.SINGLE
    elif template == "yes_no_unsure":
        opts = [
            LabelOption(value="yes", label="Yes", color=green, approved=True),
            LabelOption(value="no", label="No", color=red),
            LabelOption(value="unsure", label="Unsure", color=amber, requires_note=True),
        ]
        kind = LabelType.YES_NO_UNSURE
    elif template == "rating_1_5":
        names = ["Very poor", "Poor", "OK", "Good", "Very good"]
        colors = [PALETTE[1], "#bf360c", PALETTE[2], "#558b2f", PALETTE[0]]
        opts = [
            LabelOption(
                value=str(n + 1),
                label=f"{n + 1} · {name}",
                color=colors[n],
                approved=n >= 3,
                score=n / 4,
            )
            for n, name in enumerate(names)
        ]
        kind = LabelType.SINGLE
    elif template == "categories":
        lines = categories.splitlines() if isinstance(categories, str) else list(categories or [])
        names = [ln.strip() for ln in lines if ln.strip()]
        if natural_order:
            names = severity_order(names) or names
        seen: set[str] = set()
        opts = []
        for name in names:
            v = slug(name)
            base, i = v, 2
            while v in seen:
                v, i = f"{base}_{i}", i + 1
            seen.add(v)
            opts.append(LabelOption(value=v, label=name))
        opts = auto_colors(opts)
        kind = LabelType.SINGLE
    else:
        raise ValueError(f"unknown template '{template}'")
    return LabelsConfig(type=kind, options=auto_hotkeys(opts))


# --------------------------------------------------------------------------- validation

Severity = Literal["error", "warning"]


class Issue(BaseModel):
    path: str  # dotted, e.g. "labels.options.2.hotkey"
    message: str
    hint: str | None = None
    severity: Severity = "error"


_DB = {SourceKind.POSTGRES, SourceKind.MYSQL}
_NEEDS_PATH = {
    SourceKind.FILE,
    SourceKind.FOLDER,
    SourceKind.SQLITE,
    SourceKind.S3,
    SourceKind.GCS,
    SourceKind.HTTP,
}
_SECRET_OPTION = re.compile(r"(pass(word)?|secret|token|api[_-]?key|private[_-]?key)", re.I)
_DSN_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@:\s]+:[^/@\s]+@", re.I)


def hotkey_issues(labels: LabelsConfig) -> list[Issue]:
    """Clashes between option hotkeys, skip/undo/note, and keys the labeller reserves."""
    out: list[Issue] = []
    used: dict[str, str] = {}
    bindings: list[tuple[str, str, str]] = []
    for n, o in enumerate(labels.options):
        if o.retired or not o.hotkey:
            continue
        bindings.append((o.hotkey, f"labels.options.{n}.hotkey", f"option '{o.label or o.value}'"))
    for name in ("skip", "undo", "note"):
        bindings.append((getattr(labels.keys, name), f"labels.keys.{name}", name))
    for key, path, what in bindings:
        k = norm_key(key)
        if len(k) != 1 and k not in SPECIAL_KEYS:
            out.append(
                Issue(
                    path=path,
                    message=f"'{key}' is not a single key",
                    hint="use one character, or backspace / delete",
                )
            )
            continue
        if k in RESERVED_KEYS:
            shown = {" ": "space"}.get(k, k)
            out.append(
                Issue(
                    path=path,
                    message=f"'{shown}' for {what} is reserved",
                    hint="z, ?, Esc, Enter, Tab and Space are used by the labelling screen",
                )
            )
            continue
        if k in used:
            out.append(
                Issue(
                    path=path,
                    message=f"'{key}' is bound twice: {used[k]} and {what}",
                    hint="give one of them another key",
                )
            )
            continue
        used[k] = what
    return out


def validate_project(cfg: ProjectConfig) -> list[Issue]:
    """Problems the UI shows before saving. Errors block a save; warnings do not."""
    out: list[Issue] = []
    src = cfg.source
    if src.kind in _NEEDS_PATH and not (src.path or "").strip():
        out.append(Issue(path="source.path", message="choose a file, folder or URL"))
    if src.kind in _DB and bool(src.table) == bool(src.query):
        out.append(Issue(path="source.table", message="give exactly one of a table or a SQL query"))
    if src.kind is SourceKind.SQLITE and src.table and src.query:
        out.append(Issue(path="source.table", message="give a table or a SQL query, not both"))
    if src.kind is SourceKind.PLUGIN and not src.plugin:
        out.append(Issue(path="source.plugin", message="name the connector plugin"))
    for field in ("path", "query"):
        val = getattr(src, field) or ""
        if _DSN_PASSWORD.search(val.strip()):
            out.append(
                Issue(
                    path=f"source.{field}",
                    message="this URL contains a password",
                    hint="remove it and set credentials.env (e.g. DATABASE_URL) instead; "
                    "project files are committed",
                )
            )
    for k, v in src.options.items():
        if _SECRET_OPTION.search(k) and v not in (None, ""):
            out.append(
                Issue(
                    path=f"source.options.{k}",
                    message=f"'{k}' looks like a secret",
                    hint="reference an env var with credentials.env_map instead",
                )
            )

    if not cfg.card.fields:
        out.append(
            Issue(
                path="card.fields",
                message="the card shows nothing yet",
                hint="click a column in the preview to add it",
            )
        )
    for n, f in enumerate(cfg.card.fields):
        if bool(f.column) == bool(f.template):
            out.append(
                Issue(path=f"card.fields.{n}", message="a field needs a column or a template")
            )

    lab = cfg.labels
    active = [o for o in lab.options if not o.retired]
    if lab.type in (LabelType.SINGLE, LabelType.MULTI, LabelType.YES_NO_UNSURE) and not active:
        out.append(
            Issue(
                path="labels.options", message="add at least one option", hint="or pick a template"
            )
        )
    values = [o.value for o in lab.options]
    for n, o in enumerate(lab.options):
        if not o.value.strip():
            out.append(Issue(path=f"labels.options.{n}.value", message="option needs a value"))
        elif values.index(o.value) != n:
            out.append(
                Issue(path=f"labels.options.{n}.value", message=f"'{o.value}' is used twice")
            )
        if o.color and not re.fullmatch(r"#[0-9a-fA-F]{6}", o.color):
            out.append(
                Issue(path=f"labels.options.{n}.color", message="colour must look like #2e7d32")
            )
    if lab.type is LabelType.SCORE:
        sc = lab.score or ScoreRange()
        if sc.max <= sc.min:
            out.append(Issue(path="labels.score.max", message="max must be above min"))
        if sc.step <= 0:
            out.append(Issue(path="labels.score.step", message="step must be above 0"))
    if lab.type is not LabelType.FREE_TEXT:
        out.extend(hotkey_issues(lab))
    # "Positive" (approved) only matters to eval exports: warn only when the project uses them.
    eval_exports = cfg.rollout is not None or any(
        e.format in ("golden", "rollouts") for e in cfg.exports
    )
    if eval_exports and active and lab.type in (LabelType.SINGLE, LabelType.YES_NO_UNSURE):
        if not any(o.approved for o in active):
            out.append(
                Issue(
                    path="labels.options",
                    message="no option counts as a positive answer",
                    hint="golden and rollouts exports score positive answers as 1; "
                    "tick 'Positive' on the accepted answer(s)",
                    severity="warning",
                )
            )
    wf = cfg.workflow
    if wf.golden and not wf.golden.answer_column.strip():
        out.append(Issue(path="workflow.golden.answer_column", message="choose the answer column"))
    return out


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]


# --------------------------------------------------------------------------- migrations


class OptionChange(BaseModel):
    value: str
    kind: Literal["added", "renamed", "changed", "retired", "deleted", "restored"]
    labels: int = 0  # labels that use this value
    detail: str | None = None


class MigrationPlan(BaseModel):
    changes: list[OptionChange]
    needs_confirmation: bool  # some option with labels would be removed (it gets retired)
    labels: LabelsConfig  # the options to save (removed-with-labels -> retired)

    @property
    def summary(self) -> str:
        parts = [f"{c.kind} {c.value}" for c in self.changes]
        return ", ".join(parts) or "no option changes"


def plan_label_migration(
    old: LabelsConfig, new: LabelsConfig, label_counts: dict[str, int]
) -> MigrationPlan:
    """What saving `new` does to labelled options.

    `value` is an option's identity: labels store it, so a rename changes `label` (history is
    kept as is). An option that has labels and is missing from `new` is *retired*: kept in the
    config with `retired: true` and no hotkey, so its labels still export and show as flagged.
    Options without labels are simply deleted.
    """
    new_by = {o.value: o for o in new.options}
    old_by = {o.value: o for o in old.options}
    changes: list[OptionChange] = []
    options = list(new.options)
    confirm = False
    for o in old.options:
        n = label_counts.get(o.value, 0)
        if o.value in new_by:
            nw = new_by[o.value]
            if (o.label or o.value) != (nw.label or nw.value):
                changes.append(
                    OptionChange(
                        value=o.value,
                        kind="renamed",
                        labels=n,
                        detail=f"{o.label or o.value} -> {nw.label or nw.value}",
                    )
                )
            elif o.retired and not nw.retired:
                changes.append(OptionChange(value=o.value, kind="restored", labels=n))
            elif not o.retired and nw.retired:
                changes.append(OptionChange(value=o.value, kind="retired", labels=n))
                confirm = confirm or n > 0
            elif o != nw:
                changes.append(OptionChange(value=o.value, kind="changed", labels=n))
            continue
        if n > 0:
            confirm = confirm or not o.retired
            changes.append(OptionChange(value=o.value, kind="retired", labels=n))
            options.append(o.model_copy(update={"retired": True, "hotkey": None}))
        else:
            changes.append(OptionChange(value=o.value, kind="deleted"))
    for o in new.options:
        if o.value not in old_by:
            changes.append(OptionChange(value=o.value, kind="added"))
    options = [
        o.model_copy(update={"hotkey": None}) if o.retired and o.hotkey else o for o in options
    ]
    # labels using values the config never had (hand edits): reported, never touched
    for v, n in label_counts.items():
        if n and v not in old_by and v not in new_by:
            changes.append(
                OptionChange(value=v, kind="retired", labels=n, detail="not in the config")
            )
    return MigrationPlan(
        changes=changes,
        needs_confirmation=confirm,
        labels=new.model_copy(update={"options": options}),
    )


# --------------------------------------------------------------------------- legacy adapter


def from_legacy(
    legacy: Any,
    data_path: str | Path,
    *,
    project_dir: str | Path | None = None,
) -> ProjectConfig:
    """Old `label.yaml` (`config.LabelConfig`) + its JSONL data -> an equivalent ProjectConfig.

    `data_path` is stored relative to `project_dir` when given (and inside it), else as is.
    `image_root` (relative to the data file) becomes `source.options.media_roots`; the data
    file's folder is a media root anyway, as before.
    """
    data = Path(data_path)

    def rel(p: Path) -> str:
        if project_dir is None:
            return str(p)
        try:
            return str(p.resolve().relative_to(Path(project_dir).resolve()))
        except ValueError:
            return str(p.resolve())

    path = rel(data)
    options: dict[str, Any] = {}
    if legacy.image_root:  # relative to the data file, as before -> dev 1's media_roots
        options["media_roots"] = [rel(data.parent / legacy.image_root)]
    source = SourceConfig(
        kind=SourceKind.FILE,
        path=path,
        format="jsonl",
        id_column=legacy.id_key,
        options=options,
    )
    fields = []
    for f in legacy.fields:
        fields.append(
            CardField(
                column=f.key,
                template=f.template,
                type=DisplayType(f.type.value),
                label=f.label,
                decimals=f.decimals,
                mono=f.mono,
            )
        )
    approved = set(legacy.approved)
    opts = [
        LabelOption(
            value=d.value,
            label=d.label,
            hotkey=d.key,
            approved=d.approved or d.value in approved,
            outcome=d.outcome,
            score=d.score,
        )
        for d in legacy.decisions
    ]
    labels = LabelsConfig(
        type=LabelType.SINGLE,
        options=opts,
        keys=Keys(skip=legacy.keys.skip, undo=legacy.keys.undo, note=legacy.keys.note),
        category_column=legacy.category_key,
    )
    workflow = WorkflowConfig(
        queues=QueueConfig(by=legacy.queue_key),
        double_label=legacy.double_label,
        instructions=legacy.instructions,
        source=legacy.source,
        stage=legacy.stage,
    )
    r = legacy.rollout
    rollout = (
        RolloutConfig(prompt=r.prompt, response=r.response, gold=r.gold)
        if (r.prompt or r.response or r.gold)
        else None
    )
    return ProjectConfig(
        name=legacy.title,
        source=source,
        card=CardConfig(fields=fields),
        labels=labels,
        workflow=workflow,
        rollout=rollout,
    )


def load_legacy(config_path: str | Path, data_path: str | Path, **kw: Any) -> ProjectConfig:
    from .config import load_label_config

    return from_legacy(load_label_config(config_path), data_path, **kw)


# --------------------------------------------------------------------------- drafting


def default_display(dtype: str, sample: list[Any]) -> DisplayType:
    """Fallback display-type guess when the connector did not provide one."""
    d = (dtype or "").lower()
    vals = [v for v in sample if v not in (None, "")]
    if any(t in d for t in ("struct", "map", "json", "list", "[]")) or any(
        isinstance(v, dict | list) for v in vals
    ):
        return DisplayType.JSON
    if d.startswith(("timestamp", "datetime", "date")):
        return DisplayType.DATETIME
    if any(t in d for t in ("int", "float", "double", "decimal", "numeric", "real")):
        return DisplayType.NUMBER
    strs = [str(v) for v in vals]
    if strs and all(isinstance(v, date) or ISO_DATETIME.match(str(v).strip()) for v in vals):
        return DisplayType.DATETIME
    img = re.compile(r"\.(png|jpe?g|gif|webp|svg|avif)(\?.*)?$", re.I)
    if strs and all(img.search(s) for s in strs):
        return DisplayType.IMAGE
    if strs and all(re.match(r"^https?://", s) for s in strs):
        return DisplayType.LINK
    if strs and max(len(s) for s in strs) > 120:
        return DisplayType.LONG_TEXT
    if strs and len(set(strs)) <= max(2, len(strs) // 4) and max(len(s) for s in strs) <= 32:
        return DisplayType.BADGE
    return DisplayType.TEXT


def golden_from(column: str | None, rate: float = 0.05) -> GoldenCheck | None:
    return GoldenCheck(answer_column=column, rate=rate) if column else None
