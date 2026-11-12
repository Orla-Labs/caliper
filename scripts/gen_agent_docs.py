"""Generate the agent reference for caliper.orlalabs.com from the code.

    uv run python scripts/gen_agent_docs.py           # write the files
    uv run python scripts/gen_agent_docs.py --check   # exit 1 when a committed file is stale

Writes:

* ``site/src/content/docs/agents/cli.md``: every command and flag, walked from the
  argparse parsers, with each command's exit codes and what it touches;
* ``site/src/content/docs/agents/files.md``: every file Caliper reads or writes,
  field by field, from the Pydantic models;
* ``site/public/schemas/*.schema.json``: the same models as JSON Schema.

Field descriptions come from the models' source comments (the ``# ...`` after a
field, or the comment lines right above it), so the code stays the one source.
``packages/eval/tests/test_agent_docs.py`` runs ``--check`` in CI.
"""

from __future__ import annotations

import argparse
import inspect
import io
import json
import sys
import textwrap
import tokenize
import typing
from pathlib import Path
from typing import Any

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "site/src/content/docs/agents"
SCHEMAS = ROOT / "site/public/schemas"
SITE = "https://caliper.orlalabs.com"
GENERATED = "Generated from the code by `scripts/gen_agent_docs.py`; edit the code, then re-run it."

# --------------------------------------------------------------------------- CLI

# Exit codes per module, keyed by the module's EXIT_* constant names. Generation fails
# when a module gains or loses a constant, so a new code cannot ship undocumented.
EVAL_EXITS = {
    "EXIT_OK": "Success. Without `--ci` this includes runs where a candidate regressed: "
    "read `report.json`, or pass `--ci` to turn the gate into the exit code.",
    "EXIT_GATE": "`--ci` only: at least one candidate (or the reference) failed the gate. "
    "The reasons are printed per candidate and are in `--summary-md`.",
    "EXIT_USAGE": "Bad flags, config, golden set or `decide` function. Nothing ran.",
    "EXIT_PREFLIGHT": "Preflight rejected a candidate, or a provider SDK extra is missing. "
    "Nothing was spent on the golden set.",
    "EXIT_BUDGET": "`budget_usd` / `--budget-usd` reached. Results so far are kept; re-run "
    "the same command (optionally with a higher budget) to resume.",
}
OK_USAGE_EXITS = {
    "EXIT_OK": "Success.",
    "EXIT_USAGE": "Bad flags or input (the message on stderr says which).",
}

# What each command touches. Generation fails when a command is missing here.
TOUCHES = {
    "caliper eval": "**Spends money** with a non-`fake` provider: one preflight request per "
    "candidate, then every golden row x `replies_per_row` (the reference "
    "`noise_replays` times). Reads the AWS price list over HTTPS (cached for a day; "
    "`CALIPER_PRICING_OFFLINE=1` stops it) and, for Bedrock models, the model's display name "
    "from Bedrock `GetFoundationModel` (free). With only `fake` candidates: offline and free.",
    "caliper sample": "Local files only.",
    "caliper review": "Local files only, and free: reads the eval's `results.jsonl`, writes "
    "`items.jsonl` and, with the `label` extra, a `caliper.label.yaml` project. No model calls.",
    "caliper example": "Local files only (copies a bundled project).",
    "caliper kappa": "Local files only. `--grader` imports and runs your Python file.",
    "caliper label": "Starts a local web server and opens a browser. Reads the project's data "
    "source, which may be remote (S3, GCS, HTTP, Postgres, MySQL).",
    "caliper label serve": "Starts a local web server and opens a browser.",
    "caliper label export": "Local files, or writes to S3 / the source database when "
    "`--out s3://...` or `--table` is given.",
    "caliper label init": "Writes `caliper.label.yaml`. Opens the data source to detect "
    "columns unless `--no-detect`.",
    "caliper-compile": "Local files only. Imports and runs your grader file.",
    "caliper-server": "Starts a local HTTP server.",
}


def _cell(text: Any) -> str:
    """One table cell: a single line, pipes escaped, ``<`` escaped outside code spans."""
    parts = " ".join(str(text).split()).replace("|", "\\|").split("`")
    return "`".join(p if i % 2 else p.replace("<", "&lt;") for i, p in enumerate(parts))


def _flag_rows(parser: argparse.ArgumentParser) -> list[str]:
    rows = ["| Flag | Required | Default | Description |", "| --- | --- | --- | --- |"]
    for a in parser._actions:
        if isinstance(a, argparse._HelpAction | argparse._SubParsersAction):
            continue
        if a.option_strings:
            name = ", ".join(f"`{o}`" for o in a.option_strings)
            if a.nargs != 0:
                longest = max(a.option_strings, key=len).lstrip("-")
                name += f" `{a.metavar or longest.upper().replace('-', '_')}`"
        else:
            name = f"`{a.metavar or a.dest.upper()}`" + (" (optional)" if a.nargs == "?" else "")
        required = "yes" if a.required else ""
        default = ""
        if (
            a.default is not None
            and a.default is not False
            and a.nargs != 0
            and a.default != argparse.SUPPRESS
        ):
            default = f"`{a.default}`"
        desc = a.help or ""
        if a.choices:
            if desc and not desc.endswith((".", ")")):
                desc += "."
            elif desc.endswith(")"):
                desc += "."
            desc = (
                (desc + " " if desc else "")
                + "One of: "
                + ", ".join(f"`{c}`" for c in a.choices)
                + "."
            )
        rows.append(f"| {name} | {required} | {default} | {_cell(desc)} |")
    for group in parser._mutually_exclusive_groups:
        if group.required:
            names = " / ".join(f"`{a.option_strings[0]}`" for a in group._group_actions)
            rows.append("")
            rows.append(f"Exactly one of {names} is required.")
    return rows


def _exit_rows(module: Any, meanings: dict[str, str]) -> list[str]:
    found = {k: getattr(module, k) for k in dir(module) if k.startswith("EXIT_")}
    if set(found) != set(meanings):
        raise SystemExit(
            f"{module.__name__}: EXIT_* constants {sorted(found)} do not match the documented "
            f"{sorted(meanings)}; update scripts/gen_agent_docs.py"
        )
    rows = ["| Exit | Meaning |", "| --- | --- |"]
    for name, code in sorted(found.items(), key=lambda kv: kv[1]):
        rows.append(f"| `{code}` | {_cell(meanings[name])} |")
    return rows


def _command(title: str, about: str, parser: argparse.ArgumentParser, exits: list[str]) -> str:
    if title not in TOUCHES:
        raise SystemExit(f"{title}: add it to TOUCHES in scripts/gen_agent_docs.py")
    out = [f"## `{title}`", "", _cell(about), "", *_flag_rows(parser), ""]
    out += ["**Exit codes**", "", *exits, ""]
    out += [f"**Touches:** {TOUCHES[title]}", ""]
    return "\n".join(out)


def cli_page() -> str:
    from caliper_eval import cli, cli_kappa, review
    from caliper_label import cli as label_cli
    from caliper_label import cli_init
    from caliper_rft import cli as rft_cli
    from caliper_server import cli as server_cli

    top = cli.build_parser()
    sub = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction))
    helps = {a.dest: a.help for a in sub._choices_actions}
    _label_parser, label_sub = label_cli.build_parser()
    label_helps = {a.dest: a.help for a in label_sub._choices_actions}

    eval_exits = _exit_rows(cli, EVAL_EXITS)
    ok_usage = ["| Exit | Meaning |", "| --- | --- |"]
    ok_usage += [
        f"| `{c}` | {m} |" for c, m in ((0, "Success."), (2, OK_USAGE_EXITS["EXIT_USAGE"]))
    ]
    _exit_rows(cli_kappa, OK_USAGE_EXITS)
    _exit_rows(review, OK_USAGE_EXITS)
    _exit_rows(label_cli, OK_USAGE_EXITS)
    _exit_rows(cli_init, OK_USAGE_EXITS)
    server_exits = [
        "| Exit | Meaning |",
        "| --- | --- |",
        "| `0` | Stopped cleanly. |",
        "| `2` | Bad flags. |",
    ]

    sections = [
        _command("caliper eval", helps["eval"], sub.choices["eval"], eval_exits),
        _command("caliper sample", helps["sample"], sub.choices["sample"], ok_usage),
        _command("caliper review", helps["review"], sub.choices["review"], ok_usage),
        _command("caliper example", helps["example"], sub.choices["example"], ok_usage),
        _command("caliper kappa", helps["kappa"], sub.choices["kappa"], ok_usage),
        _command(
            "caliper label",
            "Open the labelling UI: the start screen, or `PROJECT_DIR`. Needs the `label` extra "
            "(`pip install 'orlalabs-caliper[label]'`).",
            label_cli.open_parser(),
            ok_usage,
        ),
    ]
    for name in ("serve", "export", "init"):
        sections.append(
            _command(f"caliper label {name}", label_helps[name], label_sub.choices[name], ok_usage)
        )
    sections.append(
        _command(
            "caliper-compile", rft_cli.build_parser().description, rft_cli.build_parser(), ok_usage
        )
    )
    sections.append(
        _command(
            "caliper-server",
            "Caliper dry-run backend (HTTP). Needs the `server` extra.",
            server_cli.build_parser(),
            server_exits,
        )
    )
    head = [
        "---",
        'title: "CLI reference"',
        'description: "Every Caliper command and flag, with exit codes and what each command '
        'touches (files, network, money)."',
        "editUrl: false",
        "---",
        "",
        f":::note\n{GENERATED}\n:::",
        "",
        "Conventions for every command:",
        "",
        "- stdout is deterministic (no absolute paths, timestamps or run ids); progress and "
        "notices go to stderr.",
        "- argparse itself exits `2` on an unknown flag or a missing required one.",
        "- Paths inside a config file resolve against the config file's folder, not the "
        "working directory.",
        "- Machine-readable output: `caliper eval` writes `OUT/report.json` "
        "([schema](/schemas/eval-report.schema.json)); `caliper kappa --json` prints JSON.",
        "",
    ]
    return "\n".join(head) + "\n" + "\n".join(sections).rstrip() + "\n"


# --------------------------------------------------------------------------- schemas


def _models_in(root: type[BaseModel]) -> dict[str, type[BaseModel]]:
    """Every BaseModel reachable from ``root``'s fields, by class name."""
    found: dict[str, type[BaseModel]] = {}

    def walk(tp: Any) -> None:
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            if tp.__name__ in found:
                return
            found[tp.__name__] = tp
            for field in tp.model_fields.values():
                walk(field.annotation)
            return
        for arg in typing.get_args(tp):
            walk(arg)

    walk(root)
    return found


def _field_comments(cls: type) -> dict[str, str]:
    """Field name -> its source comment (trailing, or the comment lines just above)."""
    try:
        src = textwrap.dedent(inspect.getsource(cls))
    except (OSError, TypeError):
        return {}
    tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    comments: dict[int, str] = {}
    fields: dict[str, int] = {}
    for i, tok in enumerate(tokens):
        if tok.type == tokenize.COMMENT:
            text = tok.string.lstrip("#").strip()
            if not text.startswith(("noqa", "type:", "pragma")):
                comments[tok.start[0]] = (tok.start[1], text)
        elif (
            tok.type == tokenize.NAME
            and tok.start[1] == 4
            and i + 1 < len(tokens)
            and tokens[i + 1].string == ":"
            and tok.string not in fields
        ):
            fields[tok.string] = tok.start[0]
    out: dict[str, str] = {}
    for name, line in fields.items():
        if line in comments and comments[line][0] > 4:
            out[name] = comments[line][1]
            continue
        above: list[str] = []
        n = line - 1
        while n in comments and comments[n][0] == 4:
            above.insert(0, comments[n][1])
            n -= 1
        if above:
            out[name] = " ".join(above)
    return out


def _schema(root: type[BaseModel], name: str, mode: str, about: str) -> dict[str, Any]:
    schema = root.model_json_schema(mode=mode)  # type: ignore[arg-type]
    models = _models_in(root)
    targets = [(root.__name__, schema), *schema.get("$defs", {}).items()]
    for title, node in targets:
        cls = models.get(title)
        if cls is None or "properties" not in node:
            continue
        comments = _field_comments(cls)
        for field, prop in node["properties"].items():
            if "description" not in prop and field in comments:
                prop["description"] = comments[field]
    schema["properties"].pop("config_dir", None)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{SITE}/schemas/{name}.schema.json",
        "$comment": about,
        **schema,
    }


def schemas() -> list[tuple[str, str, str, dict[str, Any]]]:
    """(name, file, intro, schema) for every documented file."""
    from caliper_eval.approvals import Approval
    from caliper_eval.config import EvalConfig
    from caliper_eval.report import EvalReport
    from caliper_eval.review import ReviewItem
    from caliper_eval.types import GoldenRow, LabelRecord, RowResult
    from caliper_label.project import ProjectConfig
    from caliper_sdk.types import EvalRow

    specs = [
        (
            "eval-config",
            "caliper.toml",
            EvalConfig,
            "validation",
            "The eval config, TOML, passed to `caliper eval --config` (any file name). "
            "Relative paths (`golden`, `decide`, `fake_script`) resolve against the config "
            "file's folder.",
        ),
        (
            "golden-row",
            "golden.jsonl",
            GoldenRow,
            "validation",
            "The golden set, one JSON object per line. Written by `caliper sample` and "
            "`caliper label export --format golden`; read by `caliper eval`. Only `id` and "
            "`request` are required; `request` is passed to every candidate as-is.",
        ),
        (
            "results-row",
            "OUT/results.jsonl",
            RowResult,
            "serialization",
            "One line per (candidate, golden row, replica), appended while `caliper eval` runs. "
            "Re-running the same command resumes from it; `--no-resume` starts it fresh.",
        ),
        (
            "eval-report",
            "OUT/report.json",
            EvalReport,
            "serialization",
            "Everything `caliper eval` prints, and more, as JSON. Parse this instead of stdout. "
            "The gate verdict itself is not in it: use the exit code of `--ci`, or "
            "`--summary-md`.",
        ),
        (
            "approval",
            "approvals JSONL",
            Approval,
            "validation",
            "Human verdicts on outcomes, one per line, listed in `[quality] approvals`. "
            "`caliper label export --format jsonl` of a `caliper review` project writes them "
            "in its own shape (`item.row_id`, `item.outcome`, `item.request_sha256`, "
            "`approved`, `source`, `labeller`), which `caliper eval` reads as well.",
        ),
        (
            "review-item",
            "REVIEW/items.jsonl",
            ReviewItem,
            "serialization",
            "The outcomes `caliper review` asks a person to judge, shuffled. The card shows "
            "`outcome` and `request` only.",
        ),
        (
            "label-project",
            "caliper.label.yaml",
            ProjectConfig,
            "validation",
            "A label project, YAML 1.2. Written by `caliper label init` or the setup wizard. "
            "Credentials are never stored here: `credentials.env` names an environment "
            "variable.",
        ),
        (
            "label-record",
            "label records JSONL",
            LabelRecord,
            "validation",
            "One label event per line: `caliper label export --format records` writes it and "
            "`caliper kappa --labels` reads it.",
        ),
        (
            "eval-row",
            "rollouts JSONL",
            EvalRow,
            "validation",
            "Graded rollouts for `caliper-compile --dataset` and `caliper kappa --rollouts` "
            "(`rollout.metadata.human_label` holds the human label for kappa).",
        ),
    ]
    return [
        (name, file, intro, _schema(model, name, mode, f"{file}: {intro}"))
        for name, file, model, mode, intro in specs
    ]


def _type(node: dict[str, Any], defs: dict[str, Any]) -> str:
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        target = defs[name]
        if "enum" in target:
            return " or ".join(f"`{json.dumps(v)}`" for v in target["enum"])
        return f"[{name}](#{name.lower()})"
    if "anyOf" in node:
        parts = [p for p in node["anyOf"] if p.get("type") != "null"]
        text = " or ".join(_type(p, defs) for p in parts)
        return text + (" or null" if len(parts) < len(node["anyOf"]) else "")
    if "enum" in node:
        return " or ".join(f"`{json.dumps(v)}`" for v in node["enum"])
    if "const" in node:
        return f"`{json.dumps(node['const'])}`"
    kind = node.get("type")
    if kind == "array":
        return "list of " + _type(node.get("items", {}), defs)
    if kind == "object":
        extra = node.get("additionalProperties")
        if isinstance(extra, dict) and extra:
            return "map of string to " + _type(extra, defs)
        return "object"
    return {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}.get(
        kind or "", "any"
    )


def _limits(node: dict[str, Any]) -> str:
    bits = []
    for key, sym in (
        ("minimum", ">="),
        ("exclusiveMinimum", ">"),
        ("maximum", "<="),
        ("exclusiveMaximum", "<"),
        ("minItems", "items >="),
        ("minLength", "len >="),
    ):
        if key in node:
            bits.append(f"{sym} {node[key]}")
    for sub in node.get("anyOf", []):
        if sub.get("type") != "null":
            limit = _limits(sub)
            if limit:
                bits.append(limit)
    return ", ".join(bits)


def _table(node: dict[str, Any], defs: dict[str, Any]) -> list[str]:
    required = set(node.get("required", []))
    rows = [
        "| Field | Type | Required | Default | Description |",
        "| --- | --- | --- | --- | --- |",
    ]
    for field, prop in node.get("properties", {}).items():
        default = f"`{json.dumps(prop['default'])}`" if "default" in prop else ""
        desc = prop.get("description", "")
        limit = _limits(prop)
        if limit:
            desc = f"{desc} ({limit})" if desc else f"({limit})"
        rows.append(
            f"| `{field}` | {_cell(_type(prop, defs))} | {'yes' if field in required else ''} "
            f"| {_cell(default)} | {_cell(desc)} |"
        )
    return rows


def _refs(node: Any) -> list[str]:
    """Names of $defs referenced from ``node``, in document order."""
    out: list[str] = []
    if isinstance(node, dict):
        if "$ref" in node:
            out.append(node["$ref"].rsplit("/", 1)[-1])
        for key, value in node.items():
            if key != "$defs":
                out += _refs(value)
    elif isinstance(node, list):
        for value in node:
            out += _refs(value)
    return out


def files_page(specs: list[tuple[str, str, str, dict[str, Any]]]) -> str:
    out = [
        "---",
        'title: "Files and schemas"',
        'description: "Every file Caliper reads or writes, field by field, with a JSON Schema '
        'for each."',
        "editUrl: false",
        "---",
        "",
        f":::note\n{GENERATED}\n:::",
        "",
        "| File | Schema | Read by | Written by |",
        "| --- | --- | --- | --- |",
        "| `caliper.toml` | [eval-config](/schemas/eval-config.schema.json) | `caliper eval` | "
        "you |",
        "| `golden.jsonl` | [golden-row](/schemas/golden-row.schema.json) | `caliper eval`, "
        "`caliper label serve` | `caliper sample`, `caliper label export --format golden` |",
        "| `OUT/results.jsonl` | [results-row](/schemas/results-row.schema.json) | `caliper eval` "
        "(resume) | `caliper eval` |",
        "| `OUT/report.json` | [eval-report](/schemas/eval-report.schema.json) | you | "
        "`caliper eval` |",
        "| approvals JSONL | [approval](/schemas/approval.schema.json) | `caliper eval`, "
        "`caliper review` (via `[quality] approvals`) | you, or `caliper label export --format "
        "jsonl` of a review project |",
        "| `REVIEW/items.jsonl` | [review-item](/schemas/review-item.schema.json) | "
        "`caliper label` | `caliper review` |",
        "| `caliper.label.yaml` | [label-project](/schemas/label-project.schema.json) | "
        "`caliper label` | `caliper label init`, the setup wizard |",
        "| label records JSONL | [label-record](/schemas/label-record.schema.json) | "
        "`caliper kappa --labels` | `caliper label export --format records` |",
        "| rollouts JSONL | [eval-row](/schemas/eval-row.schema.json) | `caliper-compile`, "
        "`caliper kappa --rollouts` | you |",
        "",
        "Validate a file before running it, for example with `check-jsonschema`:",
        "",
        "```bash",
        "pip install check-jsonschema",
        f"check-jsonschema --schemafile {SITE}/schemas/golden-row.schema.json golden.jsonl  # JSON",
        "```",
        "",
        "`check-jsonschema` reads JSON, YAML and TOML documents; for JSONL, validate each line "
        "(`caliper eval` does this anyway and names the bad line).",
        "",
    ]
    written: dict[str, dict[str, Any]] = {}
    for name, file, intro, schema in specs:
        defs = schema.get("$defs", {})
        out += [
            f"## `{file}`",
            "",
            intro,
            "",
            f"Schema: [{SITE}/schemas/{name}.schema.json](/schemas/{name}.schema.json)",
            "",
        ]
        out += [*_table(schema, defs), ""]
        queue = _refs({k: v for k, v in schema.items() if k != "$defs"})
        seen: set[str] = set()
        while queue:
            ref = queue.pop(0)
            if ref in seen:
                continue
            seen.add(ref)
            node = defs[ref]
            queue += _refs(node)
            if "properties" not in node:
                continue
            if ref in written:
                if written[ref] != node:
                    raise SystemExit(f"two different models named {ref}; rename one")
                continue
            written[ref] = node
            out += [f"### {ref}", ""]
            if node.get("description"):
                out += [" ".join(node["description"].split()), ""]
            out += [*_table(node, defs), ""]
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- main


def outputs() -> dict[Path, str]:
    specs = schemas()
    files = {DOCS / "cli.md": cli_page(), DOCS / "files.md": files_page(specs)}
    for name, _file, _intro, schema in specs:
        files[SCHEMAS / f"{name}.schema.json"] = json.dumps(schema, indent=2) + "\n"
    return files


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--check", action="store_true", help="exit 1 when a file is stale")
    args = p.parse_args(argv)
    stale = []
    for path, text in outputs().items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        if args.check:
            stale.append(path.relative_to(ROOT))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        print("stale (run: uv run python scripts/gen_agent_docs.py):", file=sys.stderr)
        for path in stale:
            print(f"  {path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
