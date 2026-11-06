"""`caliper.label.yaml` v1: the project config (CONTRACT - see packages/label/CONTRACT.md).

The UI is the main way to write this file; hand-editing stays supported and the
two directions must round-trip losslessly (owned by dev 2: load/save/migrate).
Credentials never appear here: connections only *reference* env vars, AWS
profiles, or the default credential chain.

Example::

    version: 1
    name: Support ticket triage
    source:
      kind: file
      path: data/tickets.csv          # relative to the project dir
      id_column: ticket_id
    card:
      fields:
        - {column: subject, type: text}
        - {column: body, type: long_text}
        - {column: screenshot_url, type: image}
    labels:
      type: single
      options:
        - {value: correct, label: Correct, hotkey: "1", color: "#2e7d32", approved: true}
        - {value: wrong, label: Wrong, hotkey: "2", color: "#c62828"}
        - {value: unsure, label: Unsure, hotkey: "3", requires_note: true}
    workflow:
      queues: {by: priority}
      double_label: 2
      assignment: round_robin
      instructions: |
        Is the triage label right for this ticket?
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from caliper_eval.types import LabelSource
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

CONFIG_FILENAME = "caliper.label.yaml"
STATE_DIRNAME = ".caliper-label"  # project store, snapshots, caches; gitignored


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Hand-written YAML: `hotkey: 1` is an int and `value: true` a bool. Numbers become strings;
# booleans are refused with a hint (a YAML 1.1 reader would see yes/no/on/off as booleans too).
def _text(v: Any) -> Any:
    if isinstance(v, bool):
        raise ValueError(f'read as a boolean ({v}); quote it in YAML, e.g. "yes"')
    return str(v) if isinstance(v, int | float) else v


# --------------------------------------------------------------------------- source


class SourceKind(StrEnum):
    FILE = "file"  # csv/tsv/jsonl/json/parquet/xlsx, local path or uploaded copy
    FOLDER = "folder"  # folder of images -> one item per image (+ optional sidecar)
    S3 = "s3"  # s3:// incl. S3-compatible endpoints (R2, MinIO)
    GCS = "gcs"
    HTTP = "http"
    POSTGRES = "postgres"
    MYSQL = "mysql"
    SQLITE = "sqlite"
    PLUGIN = "plugin"  # third-party connector registered via entry point


class Credentials(_M):
    """References only. Never a secret value."""

    env: str | None = None  # e.g. "DATABASE_URL" (whole DSN) or "PGPASSWORD"
    env_map: dict[str, str] = Field(default_factory=dict)  # option -> ENV_VAR
    aws_profile: str | None = None  # else default chain (env, SSO, instance role)
    region: str | None = None
    endpoint_url: str | None = None  # S3-compatible endpoints (not a secret)


class SourceConfig(_M):
    kind: SourceKind
    path: str | None = None  # file/folder/sqlite path, or URL / glob for s3/gcs/http
    format: str | None = None  # csv|tsv|jsonl|json|parquet|xlsx; None = by extension
    table: str | None = None  # databases: a table ...
    query: str | None = None  # ... or a SQL query (exactly one of table/query)
    sidecar: str | None = None  # folder sources: CSV joined on file name
    id_column: str | None = None  # None = row number (stable within a snapshot)
    plugin: str | None = None  # kind=plugin: entry-point name
    options: dict[str, Any] = Field(default_factory=dict)  # connector-specific, non-secret
    credentials: Credentials = Field(default_factory=Credentials)


# --------------------------------------------------------------------------- card


class DisplayType(StrEnum):
    TEXT = "text"
    LONG_TEXT = "long_text"
    NUMBER = "number"
    BADGE = "badge"
    LINK = "link"
    IMAGE = "image"  # URL, s3:// key, or local path (served safely / presigned)
    JSON = "json"


class CardField(_M):
    column: str | None = None  # source column; dotted paths into JSON columns allowed
    template: str | None = None  # "cards/{id}.svg" style; exactly one of column/template
    type: DisplayType = DisplayType.TEXT
    label: str | None = None
    decimals: int | None = Field(default=None, ge=0, le=10)
    hidden: bool = False  # kept in snapshot/export but not shown
    mono: bool = False  # text: monospace, keep whitespace (legacy FieldSpec.mono)

    _t = field_validator("column", "template", "label", mode="before")(_text)


class CardConfig(_M):
    fields: list[CardField] = Field(default_factory=list)


# --------------------------------------------------------------------------- labels


class LabelType(StrEnum):
    SINGLE = "single"
    MULTI = "multi"
    YES_NO_UNSURE = "yes_no_unsure"  # preset of SINGLE
    FREE_TEXT = "free_text"
    SCORE = "score"


class LabelOption(_M):
    value: str  # stored value; stable identity (renames change `label`, or migrate)
    label: str | None = None
    hotkey: str | None = None  # auto-assigned 1..9 by the wizard; clash-checked
    color: str | None = None  # "#rrggbb"
    approved: bool = False  # counts as approved/positive (label_approved, human_label=1)
    requires_note: bool = False
    outcome: str | None = None  # GoldenRow.label on export; default: value
    score: float | None = Field(default=None, ge=0.0, le=1.0)  # human_label override
    retired: bool = False  # removed in settings after labels existed: kept, flagged

    _t = field_validator("value", "label", "hotkey", "outcome", mode="before")(_text)


class ScoreRange(_M):
    min: float = 1
    max: float = 5
    step: float = 1


class Keys(_M):
    skip: str = "s"
    undo: str = "backspace"
    note: str = "n"

    _t = field_validator("skip", "undo", "note", mode="before")(_text)


class LabelsConfig(_M):
    type: LabelType = LabelType.SINGLE
    options: list[LabelOption] = Field(default_factory=list)
    score: ScoreRange | None = None  # type=score
    keys: Keys = Field(default_factory=Keys)
    category_column: str | None = None  # inter-annotator kappa by category


# --------------------------------------------------------------------------- workflow


class Assignment(StrEnum):
    OPEN = "open"
    ROUND_ROBIN = "round_robin"


class QueueConfig(_M):
    by: str | None = None  # column (or "stratum" from `caliper sample`)


class GoldenCheck(_M):
    """Hidden items with known answers; per-labeller accuracy on them."""

    answer_column: str  # rows with a non-empty value here are golden items
    rate: float = Field(default=0.05, ge=0.0, le=1.0)  # share of served items


class WorkflowConfig(_M):
    queues: QueueConfig = Field(default_factory=QueueConfig)
    double_label: int = Field(default=1, ge=1, le=10)
    assignment: Assignment = Assignment.OPEN
    instructions: str | None = None  # markdown
    golden: GoldenCheck | None = None
    source: LabelSource = LabelSource.INTERNAL_REVIEWER
    stage: str | None = None


# --------------------------------------------------------------------------- export


class ExportTarget(_M):
    """A saved export destination (optional). Same connector/credential rules as source."""

    name: str
    format: str  # golden|rollouts|records|csv|jsonl|parquet|table
    destination: SourceConfig  # file path / s3 URL / database table


# --------------------------------------------------------------------------- project


class RolloutConfig(_M):
    """`export --format rollouts` for non-EvalRow rows: dotted paths (legacy `rollout:`)."""

    prompt: str | None = None
    response: str | None = None
    gold: str | None = None


class ProjectConfig(_M):
    version: int = 1  # schema version of this file (always 1 for now)
    revision: int = Field(default=1, ge=1)  # config revision: bumped by every settings save
    name: str = "Untitled project"
    description: str | None = None
    source: SourceConfig
    card: CardConfig = Field(default_factory=CardConfig)
    labels: LabelsConfig = Field(default_factory=LabelsConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    exports: list[ExportTarget] = Field(default_factory=list)
    rollout: RolloutConfig | None = None


# =========================================================================== I/O (dev 2)
#
# load_project / save_project round-trip losslessly:
#   * UI -> YAML -> UI: a fresh file is the minimal dump (defaults left out), in schema order,
#     and loading it gives back an equal ProjectConfig.
#   * hand-edited YAML -> UI -> YAML: the existing file is loaded in ruamel round-trip mode and
#     only the nodes whose *validated* value changed are touched. Comments, key order, flow vs
#     block style, quoting and untouched sections stay as written. List items are matched by
#     identity (options by `value`, card fields by `column`/`template`, exports by `name`),
#     so reordering keeps each item's comment with the item.
#
# Always read the file with `load_project` (YAML 1.2), never `yaml.safe_load` (YAML 1.1 reads
# bare yes/no/on/off as booleans). Writes quote such strings so both readers agree.


class ProjectFileError(ValueError):
    """A project file that cannot be read or is invalid. `hint` says how to fix it."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


HEADER = (
    "caliper label project. Written by the setup wizard / Project settings; hand edits are\n"
    "kept (comments and order too). Credentials never go here: reference env vars instead.\n"
    "Reference: docs/label-setup.md"
)

_YAML11_WORDS = {"y", "n", "yes", "no", "on", "off", "true", "false", "null", "~", ""}


def config_path(project: str | Path) -> Path:
    """`dir` -> dir/caliper.label.yaml; a path to a .yaml/.yml file is used as is."""
    p = Path(project).expanduser()
    if p.suffix.lower() in (".yaml", ".yml") and not p.is_dir():
        return p
    return p / CONFIG_FILENAME


def _yaml() -> Any:
    from ruamel.yaml import YAML

    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096  # never re-wrap: ruamel can drop a space when folding flow scalars
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _plain(node: Any) -> Any:
    """ruamel round-trip nodes -> plain dict/list/str/int/float/bool/None."""
    if isinstance(node, dict):
        return {str(k): _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, str):
        return str(node)
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    return node


def _short_errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def _read_doc(path: Path) -> Any:
    from ruamel.yaml import YAMLError

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ProjectFileError(
            f"no project config at {path}",
            f"create one with the setup wizard or `caliper label init {path.parent}`",
        ) from None
    try:
        doc = _yaml().load(text)
    except YAMLError as exc:
        raise ProjectFileError(f"{path}: cannot parse YAML: {exc}", "fix the YAML syntax") from exc
    if not isinstance(doc, dict):
        raise ProjectFileError(f"{path}: expected a mapping at the top level")
    return doc


def parse_project(data: dict[str, Any], where: str = "config") -> ProjectConfig:
    """Validate a plain dict (from YAML or the UI) into a ProjectConfig, with a short error."""
    if "decisions" in data and "labels" not in data:
        raise ProjectFileError(
            f"{where}: this looks like a legacy label.yaml (it has `decisions`)",
            "run it with `caliper label serve DATA --config label.yaml`, or convert it with "
            "`caliper label init --from-legacy label.yaml --data DATA`",
        )
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        raise ProjectFileError(
            f"{where}: {_short_errors(exc)}", "fix the field named above (see docs/label-setup.md)"
        ) from exc


def load_project(project: str | Path) -> ProjectConfig:
    """Read `caliper.label.yaml` from a project dir (or a given .yaml path)."""
    path = config_path(project)
    return parse_project(_plain(_read_doc(path)), str(path))


def dump_project(cfg: ProjectConfig) -> dict[str, Any]:
    """The minimal plain dict for `cfg`: defaults left out, `version` and `name` always kept."""
    out = cfg.model_dump(mode="json", exclude_defaults=True)
    head = {"version": cfg.version, "name": cfg.name}
    return head | {k: v for k, v in out.items() if k not in head}


def render_project(cfg: ProjectConfig, existing: str | None = None) -> str:
    """YAML text for `cfg`. With `existing` (the current file's text), apply only the changes."""
    import io

    y = _yaml()
    if existing is not None and existing.strip():
        doc = y.load(existing)
        if not isinstance(doc, dict):
            raise ProjectFileError("existing config is not a mapping")
        try:
            old = parse_project(_plain(doc))
        except ProjectFileError:
            old = None  # invalid before: rewrite every key we know, keep comments where we can
        old_full = old.model_dump(mode="json") if old else {}
        new_full = cfg.model_dump(mode="json")
        _merge_map(doc, old_full, new_full, dump_project(cfg), (), y)
    else:
        doc = _fresh_doc(cfg)
    buf = io.StringIO()
    y.dump(doc, buf)
    return buf.getvalue()


def save_project(project: str | Path, cfg: ProjectConfig) -> Path:
    """Write `cfg` to the project's config, editing the existing file in place if there is one.

    Also creates `.caliper-label/.gitignore` (`*`) next to it. Atomic (write + rename).
    """
    path = config_path(project)
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    text = render_project(cfg, existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    state = path.parent / STATE_DIRNAME
    state.mkdir(exist_ok=True)
    gi = state / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- YAML building

_FLOW_LISTS = {("card", "fields"), ("labels", "options")}  # one item per line, `{a: b}` style


def _identity(path: tuple[str, ...], item: Any) -> Any:
    if not isinstance(item, dict):
        return None
    if path == ("labels", "options"):
        return item.get("value")
    if path == ("card", "fields"):
        return ("c", item["column"]) if item.get("column") else ("t", item.get("template"))
    if path == ("exports",):
        return item.get("name")
    return None


def _yaml11_not_str(v: str) -> bool:
    """True if a YAML 1.1 reader (pyyaml) would read plain `v` as something other than a str."""
    import yaml

    if v.strip().lower() in _YAML11_WORDS:
        return True
    tag = yaml.resolver.Resolver().resolve(yaml.ScalarNode, v, (True, False))
    return tag != "tag:yaml.org,2002:str"


def _folded(v: str, width: int = 88) -> Any:
    from ruamel.yaml.scalarstring import FoldedScalarString

    f = FoldedScalarString(v)
    pos, last = [], 0
    for i in range(1, len(v) - 1):
        if v[i] == " " and i - last > width - 12 and v[i - 1] != " " and v[i + 1] != " ":
            pos.append(i)
            last = i
    f.fold_pos = pos
    return f


def _scalar(v: Any, flow: bool = False) -> Any:
    """A YAML node for a scalar. Block scalars (| and >) are not allowed inside `{...}`."""
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString

    if isinstance(v, str):
        block_ok = not flow and v == v.strip(" \t") and "\t" not in v
        if "\n" in v and block_ok and " \n" not in v:
            return LiteralScalarString(v)
        if "\n" in v:
            return DoubleQuotedScalarString(v)
        if len(v) > 88 and block_ok and v.isprintable():
            return _folded(v)
        if v != v.strip() or _yaml11_not_str(v):
            return DoubleQuotedScalarString(v)
    return v


def _build(v: Any, path: tuple[str, ...], flow: bool = False, inflow: bool = False) -> Any:
    """YAML nodes for a plain value. `flow`: make this mapping `{...}`; `inflow`: the parent
    is already a flow collection (so no block scalars below)."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString

    if isinstance(v, dict):
        m = CommentedMap()
        for k, x in v.items():
            key = DoubleQuotedScalarString(k) if _yaml11_not_str(k) or k != k.strip() else k
            m[key] = _build(x, (*path, k), inflow=flow or inflow)
        if flow:
            m.fa.set_flow_style()
        return m
    if isinstance(v, list):
        s = CommentedSeq()
        for x in v:
            s.append(_build(x, path, flow=path in _FLOW_LISTS, inflow=inflow))
        return s
    return _scalar(v, flow=inflow)


def _in_flow(node: Any) -> bool:
    return bool(getattr(node, "fa", None) and node.fa.flow_style())


def _fresh_doc(cfg: ProjectConfig) -> Any:
    doc = _build(dump_project(cfg), ())
    doc.yaml_set_start_comment(HEADER)
    return doc


_MISSING = object()


def _same(a: Any, b: Any) -> bool:
    return a == b and isinstance(a, bool) == isinstance(b, bool)


# A comment block that follows a collection ("\n# ---- next section\n") is stored by ruamel on
# the *last* node inside it. When that node moves or goes away, move the block with the position.


def _is_block(v: Any) -> bool:
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    return isinstance(v, CommentedMap | CommentedSeq) and len(v) > 0 and not v.fa.flow_style()


def _last_slot(node: Any) -> tuple[dict[Any, Any], Any, int] | None:
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    if isinstance(node, CommentedMap) and len(node):
        k = list(node.keys())[-1]
        return _last_slot(node[k]) if _is_block(node[k]) else (node.ca.items, k, 2)
    if isinstance(node, CommentedSeq) and len(node):
        i = len(node) - 1
        return _last_slot(node[i]) if _is_block(node[i]) else (node.ca.items, i, 0)
    return None


def _detach_trailing(node: Any) -> str:
    slot = _last_slot(node)
    if slot is None:
        return ""
    items, k, pos = slot
    entry = items.get(k)
    tok = entry[pos] if entry and len(entry) > pos else None
    if tok is None or not isinstance(tok.value, str):
        return ""
    head, _, rest = tok.value.partition("\n")
    if not rest.strip():
        return ""
    if head.strip():
        tok.value = head + "\n"
    else:
        entry[pos] = None
    return rest


def _attach_trailing(node: Any, text: str) -> bool:
    from ruamel.yaml.error import CommentMark
    from ruamel.yaml.tokens import CommentToken

    if not text:
        return True
    slot = _last_slot(node)
    if slot is None:
        return False
    items, k, pos = slot
    entry = items.setdefault(k, [None, None, None, None])
    while len(entry) <= pos:
        entry.append(None)
    tok = entry[pos]
    if tok is not None and isinstance(tok.value, str):
        tok.value = tok.value.rstrip("\n") + "\n" + text
    else:
        entry[pos] = CommentToken("\n" + text, CommentMark(0), None)
    return True


def _key_trailing(doc: Any, key: Any) -> str:
    """Detach the comment block that follows `key` (scalar value or block collection)."""
    if _is_block(doc[key]):
        return _detach_trailing(doc[key])
    entry = doc.ca.items.get(key)
    tok = entry[2] if entry and len(entry) > 2 else None
    if tok is None or not isinstance(tok.value, str):
        return ""
    head, _, rest = tok.value.partition("\n")
    if not rest.strip():
        return ""
    if head.strip():
        tok.value = head + "\n"
    else:
        entry[2] = None
    return rest


def _del_key(doc: Any, key: Any, extra: str = "") -> str:
    """Delete `key`, handing the comment block below it to the key above.

    Returns the block when there is no key above (the caller re-homes it).
    """
    keys = list(doc.keys())
    entry = doc.ca.items.get(key)
    before = entry[1] if entry and len(entry) > 1 and isinstance(entry[1], list) else []
    pre = "".join(t.value for t in before if t is not None and isinstance(t.value, str))
    trailing = pre + extra + _key_trailing(doc, key)
    del doc[key]
    idx = keys.index(key)
    if not trailing:
        return ""
    if idx == 0:
        return trailing
    _attach_after(doc, keys[idx - 1], trailing)
    return ""


def _attach_after(doc: Any, key: Any, text: str) -> None:
    if not text:
        return
    if _is_block(doc[key]):
        _attach_trailing(doc[key], text)
    else:
        _attach_trailing_key(doc, key, text)


def _attach_trailing_key(doc: Any, key: Any, text: str) -> None:
    from ruamel.yaml.error import CommentMark
    from ruamel.yaml.tokens import CommentToken

    entry = doc.ca.items.setdefault(key, [None, None, None, None])
    tok = entry[2]
    if tok is not None and isinstance(tok.value, str):
        tok.value = tok.value.rstrip("\n") + "\n" + text
    else:
        entry[2] = CommentToken("\n" + text, CommentMark(0), None)


def _replace_value(doc: Any, key: Any, value: Any) -> None:
    trailing = _key_trailing(doc, key)
    doc[key] = value
    if trailing:
        if _is_block(value):
            _attach_trailing(value, trailing)
        else:
            _attach_trailing_key(doc, key, trailing)


def _set_scalar(doc: Any, key: Any, value: Any) -> None:
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString, SingleQuotedScalarString

    old = doc.get(key)
    if isinstance(value, str) and "\n" not in value:
        # keep the user's quoting (single or double) for a changed one-line string
        if isinstance(old, SingleQuotedScalarString | DoubleQuotedScalarString):
            doc[key] = type(old)(value)
            return
    doc[key] = _scalar(value, flow=_in_flow(doc))


def _insert_in_order(doc: Any, key: str, value: Any, order: list[str]) -> None:
    """Insert after the nearest preceding key (schema order) that the file already has."""
    idx = order.index(key) if key in order else len(order)
    keys = list(doc.keys())
    pos = len(keys)
    for prev in reversed(order[:idx]):
        if prev in doc:
            pos = keys.index(prev) + 1
            break
    else:
        for nxt in order[idx + 1 :]:
            if nxt in doc:
                pos = keys.index(nxt)
                break
    doc.insert(pos, key, value)


def _merge_map(
    doc: Any, old: dict[str, Any], new: dict[str, Any], new_min: Any, path: tuple[str, ...], y: Any
) -> str:
    """Apply new-vs-old changes onto `doc`. Returns a comment block it could not re-home."""
    order = list(new.keys())
    new_min = new_min if isinstance(new_min, dict) else {}
    left = ""
    for key in list(doc.keys()):
        if key not in new:
            left += _del_key(doc, key)  # removed (free-form dicts) or not a field any more
    for key in order:
        nv = new[key]
        ov = old.get(key, _MISSING)
        if ov is not _MISSING and _same(ov, nv):
            continue  # unchanged: leave the node exactly as written
        sub = (*path, key)
        if key not in doc:
            if key in new_min:
                _insert_in_order(doc, key, _build(new_min[key], sub, inflow=_in_flow(doc)), order)
            continue
        cur = doc[key]
        if nv is None or (nv in ([], {}) and key not in new_min):
            left += _del_key(doc, key)  # cleared back to the default: drop, never write `null`
        elif isinstance(nv, dict | list) and isinstance(cur, type(nv)):
            if isinstance(nv, dict):
                sub_left = _merge_map(
                    cur, ov if isinstance(ov, dict) else {}, nv, new_min.get(key), sub, y
                )
            else:
                sub_left = _merge_list(
                    cur, ov if isinstance(ov, list) else [], nv, new_min.get(key), sub, y
                )
            if not len(cur) and key not in new_min:
                left += _del_key(doc, key, sub_left)  # emptied back to the default
            else:
                _attach_after(doc, key, sub_left)
        elif isinstance(nv, dict | list):
            _replace_value(doc, key, _build(new_min.get(key, nv), sub, inflow=_in_flow(doc)))
        else:
            _set_scalar(doc, key, nv)
    if left and len(doc):
        # rare: the first key was deleted and a comment block followed it. Keep the comment
        # (at the end of this mapping) rather than lose it.
        _attach_after(doc, list(doc.keys())[-1], left)
        left = ""
    return left


def _merge_list(
    seq: Any, old: list[Any], new: list[Any], new_min: Any, path: tuple[str, ...], y: Any
) -> str:
    from ruamel.yaml.comments import CommentedMap

    new_min = new_min if isinstance(new_min, list) and len(new_min) == len(new) else new
    if len(seq) != len(old):
        old = [None] * len(seq)  # file and model disagree (should not happen): positional
    ids_old = [_identity(path, o) for o in old]
    ids_new = [_identity(path, n) for n in new]
    by_id: dict[Any, int] = {}
    if None not in ids_old and len(set(ids_old)) == len(ids_old):
        by_id = {i: n for n, i in enumerate(ids_old)}
    if not by_id or None in ids_new or len(set(ids_new)) != len(ids_new):
        # positional: same length lists of scalars/maps
        by_id = {}
    flow = any(isinstance(x, CommentedMap) and x.fa.flow_style() for x in seq) or (
        not seq and path in _FLOW_LISTS
    )
    trailing = _detach_trailing(seq)
    items: list[Any] = []
    comments: dict[int, Any] = {}
    for n, nv in enumerate(new):
        src = by_id.get(ids_new[n]) if by_id else (n if n < len(seq) else None)
        if src is not None:
            node = seq[src]
            ov = old[src]
            if not _same(ov, nv):
                if isinstance(node, dict) and isinstance(nv, dict):
                    _merge_map(node, ov if isinstance(ov, dict) else {}, nv, new_min[n], path, y)
                else:
                    node = _build(new_min[n], path, flow=flow, inflow=_in_flow(seq))
            if src in seq.ca.items:
                comments[n] = seq.ca.items[src]
        else:
            node = _build(new_min[n], path, flow=flow, inflow=_in_flow(seq))
        items.append(node)
    del seq[:]
    seq.extend(items)
    seq.ca.items.clear()
    seq.ca.items.update(comments)
    return "" if _attach_trailing(seq, trailing) else trailing
