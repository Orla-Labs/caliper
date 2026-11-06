"""Eval report: one structured object, two deterministic renderings.

``build_report(config, results, golden)`` aggregates per candidate:
rows, errors (provider replies vs ``decide``), tokens, latency, dollars,
**who answered** (distinct served models, ``matches_request=False`` count),
transitions vs the reference, regression above the noise floor and, when
labels exist, calibration.

Conventions (documented because the CI gate and the demo diff depend on them):

* latency is **per reply** (a vote loop's 3 replies are 3 samples), p50/p95 by
  nearest rank;
* dollars and tokens include every replica (the reference's replays cost
  money too);
* ``excess_regression = candidate cost-weighted regression (point estimate)
  - noise floor cost-weighted flip rate (upper CI)``.

``config`` is duck-typed: any object with ``reference``, ``noise_replays``,
``seed``, ``costs``, ``candidates`` (``Candidate``), ``ci.max_regression``
and ``calibration.thresholds`` works (``caliper_eval.config.EvalConfig``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ._stats import DEFAULT_N_BOOT, nearest_rank
from .calibration import Calibration, compute_calibration
from .noise import NoiseFloor, compute_noise_floor, index_results
from .transitions import Transitions, compute_transitions
from .types import Candidate, GoldenRow, RowResult, candidate_sha256, request_sha256


class Tokens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


class CandidateReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    provider: str
    model: str
    thinking: str
    output_mode: str
    is_reference: bool
    rows: int  # distinct golden rows with a result
    results: int  # (row, replica) executions
    replies: int
    provider_errors: int  # replies with an error
    decide_errors: int  # results with no decision
    error_rate: float  # decide_errors / results
    tokens: Tokens
    latency_p50_ms: float
    latency_p95_ms: float
    dollars: float
    served_models: list[str] = Field(default_factory=list)  # "provider:model", sorted
    served_mismatches: int = 0  # replies with matches_request=False
    served_unknown: int = 0  # non-error replies with no served_by
    stale_results: int = 0  # recorded for a different request or candidate settings than now
    transitions: Transitions | None = None
    excess_regression: float | None = None
    calibration: Calibration | None = None


class EvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str
    seed: int
    n_boot: int
    n_golden: int
    noise_replays: int
    max_regression: float
    noise_floor: NoiseFloor | None
    candidates: list[CandidateReport]
    warnings: list[str] = Field(default_factory=list)

    def candidate(self, name: str) -> CandidateReport:
        for c in self.candidates:
            if c.name == name:
                return c
        raise KeyError(name)

    @property
    def noise_upper(self) -> float:
        nf = self.noise_floor
        return nf.cost_weighted_flip_rate.high if nf is not None else 0.0


def _get(obj: Any, *path: str, default: Any = None) -> Any:
    for p in path:
        if obj is None:
            return default
        obj = obj.get(p) if isinstance(obj, dict) else getattr(obj, p, None)
    return default if obj is None else obj


def _candidate_report(
    cand: Candidate,
    results: Sequence[RowResult],
    *,
    is_reference: bool,
) -> CandidateReport:
    tokens = Tokens()
    latencies: list[float] = []
    dollars = 0.0
    served: set[str] = set()
    mismatches = unknown = provider_errors = n_replies = 0
    for res in results:
        for rep in res.replies:
            n_replies += 1
            tokens.input += rep.usage.input_tokens
            tokens.output += rep.usage.output_tokens
            tokens.cache_read += rep.usage.cache_read_tokens
            tokens.cache_write += rep.usage.cache_write_tokens
            latencies.append(rep.latency_ms)
            dollars += rep.cost_usd
            if rep.error is not None:
                provider_errors += 1
            if rep.served_by is None:
                if rep.error is None:
                    unknown += 1
                continue
            served.add(f"{rep.served_by.provider.value}:{rep.served_by.model}")
            if not rep.served_by.matches_request:
                mismatches += 1
    decide_errors = sum(1 for r in results if r.decision is None)
    return CandidateReport(
        name=cand.name,
        provider=cand.provider.value,
        model=cand.model,
        thinking=cand.thinking.value,
        output_mode=cand.output_mode.value,
        is_reference=is_reference,
        rows=len({r.row_id for r in results}),
        results=len(results),
        replies=n_replies,
        provider_errors=provider_errors,
        decide_errors=decide_errors,
        error_rate=decide_errors / len(results) if results else 0.0,
        tokens=tokens,
        latency_p50_ms=nearest_rank(latencies, 50),
        latency_p95_ms=nearest_rank(latencies, 95),
        dollars=dollars,
        served_models=sorted(served),
        served_mismatches=mismatches,
        served_unknown=unknown,
    )


def build_report(
    config: Any,
    results: Iterable[RowResult],
    golden: Iterable[GoldenRow],
    *,
    n_boot: int = DEFAULT_N_BOOT,
    target_precision: float | None = None,
) -> EvalReport:
    """Aggregate ``results`` into an :class:`EvalReport` (pure, deterministic)."""
    golden = list(golden)
    latest = list(index_results(results).values())  # resume duplicates: last wins
    reference: str = config.reference
    seed = int(_get(config, "seed", default=0))
    costs = dict(_get(config, "costs", default={}))
    thresholds = _get(config, "calibration", "thresholds")
    max_regression = float(_get(config, "ci", "max_regression", default=0.0))
    warnings: list[str] = []

    noise = compute_noise_floor(latest, reference, costs=costs, seed=seed, n_boot=n_boot)
    if noise is None:
        warnings.append(
            f"no noise floor: reference '{reference}' was not replayed >= 2x; "
            "regressions are measured against 0"
        )
    elif noise.n_errored_rows or noise.n_incomplete_rows:
        warnings.append(
            f"noise floor excludes {noise.n_errored_rows} errored and "
            f"{noise.n_incomplete_rows} incomplete reference rows"
        )
    noise_upper = noise.cost_weighted_flip_rate.high if noise is not None else 0.0

    want_sha = {g.id: request_sha256(g.request) for g in golden}
    want_cand = {c.name: candidate_sha256(c) for c in config.candidates}
    stale: dict[str, int] = {}
    for r in latest:
        old_request = r.row_id in want_sha and r.request_sha256 != want_sha[r.row_id]
        old_candidate = (
            r.candidate_sha256 is not None
            and r.candidate in want_cand
            and r.candidate_sha256 != want_cand[r.candidate]
        )
        if old_request or old_candidate:
            stale[r.candidate] = stale.get(r.candidate, 0) + 1

    by_cand: dict[str, list[RowResult]] = {}
    for r in sorted(latest, key=lambda r: r.key):
        by_cand.setdefault(r.candidate, []).append(r)

    reports: list[CandidateReport] = []
    for cand in config.candidates:
        is_ref = cand.name == reference
        cres = by_cand.get(cand.name, [])
        rep = _candidate_report(cand, cres, is_reference=is_ref)
        rep.stale_results = stale.get(cand.name, 0)
        if rep.stale_results:
            warnings.append(
                f"{cand.name}: {rep.stale_results} results were recorded for a different "
                "request or candidate settings than the config now holds; "
                "re-run with --no-resume"
            )
        if not is_ref:
            rep.transitions = compute_transitions(
                latest, cand.name, reference, costs=costs, seed=seed, n_boot=n_boot
            )
            rep.excess_regression = rep.transitions.regression.estimate - noise_upper
        rep.calibration = compute_calibration(
            golden,
            latest,
            cand.name,
            thresholds=thresholds,
            target_precision=target_precision,
        )
        reports.append(rep)
    known = {c.name for c in config.candidates}
    for name in sorted(set(by_cand) - known):
        warnings.append(f"results for unknown candidate '{name}' ignored")

    return EvalReport(
        reference=reference,
        seed=seed,
        n_boot=n_boot,
        n_golden=len(golden),
        noise_replays=int(_get(config, "noise_replays", default=0)),
        max_regression=max_regression,
        noise_floor=noise,
        candidates=reports,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- rendering


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _num(x: float | None, nd: int = 3) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def _ci(ci: Any, nd: int = 3) -> str:
    return f"{ci.estimate:.{nd}f} [{ci.low:.{nd}f}, {ci.high:.{nd}f}]"


def _who(c: CandidateReport) -> str:
    models = ",".join(m.split(":", 1)[1] for m in c.served_models) or "-"
    flags = []
    if c.served_mismatches:
        flags.append(f"MISMATCH x{c.served_mismatches}")
    if c.served_unknown:
        flags.append(f"unknown x{c.served_unknown}")
    return models + (f" ({'; '.join(flags)})" if flags else "")


def _tok(t: Tokens) -> str:
    return f"{t.input}/{t.output}/{t.cache_read + t.cache_write}"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    out = [fmt.format(*headers).rstrip(), fmt.format(*("-" * w for w in widths)).rstrip()]
    out += [fmt.format(*r).rstrip() for r in rows]
    return out


def _noise_line(report: EvalReport) -> str:
    nf = report.noise_floor
    if nf is None:
        return f"noise floor: none (reference '{report.reference}' not replayed)"
    return (
        f"noise floor ({nf.reference} x{nf.n_replicas}, {nf.n_rows} rows): "
        f"flip {_pct(nf.flip_rate.estimate)}, cost-weighted {_ci(nf.cost_weighted_flip_rate)}"
    )


def _gate_status(gate: Any, name: str) -> str:
    if gate is None:
        return ""
    for cg in gate.candidates:
        if cg.name == name:
            return "PASS" if cg.passed else "FAIL"
    return ""


def render_text(report: EvalReport, gate: Any = None) -> str:
    """Terminal rendering. Deterministic: no paths, timestamps or run ids."""
    lines = [
        f"caliper eval: {len(report.candidates)} candidates, {report.n_golden} golden rows, "
        f"reference={report.reference}, seed={report.seed}, bootstrap n={report.n_boot}",
        _noise_line(report),
        "",
    ]
    headers = [
        "candidate", "rows", "errors", "tokens in/out/cache", "p50/p95 ms", "cost $",
        "who answered", "flips", "regression [95% CI]", "excess",
    ]
    if gate is not None:
        headers.append("gate")
    rows = []
    for c in report.candidates:
        t = c.transitions
        row = [
            c.name + (" (ref)" if c.is_reference else ""),
            str(c.rows),
            f"{c.provider_errors}p/{c.decide_errors}d",
            _tok(c.tokens),
            f"{c.latency_p50_ms:.0f}/{c.latency_p95_ms:.0f}",
            f"{c.dollars:.4f}",
            _who(c),
            "-" if t is None else f"{len(t.flipped)}/{t.n_rows}",
            "-" if t is None else _ci(t.regression),
            _num(c.excess_regression),
        ]
        if gate is not None:
            row.append(_gate_status(gate, c.name) or "-")
        rows.append(row)
    lines += _table(headers, rows)

    for c in report.candidates:
        t = c.transitions
        if t is None:
            continue
        lines += ["", f"transitions {report.reference} -> {c.name}:"]
        for m in t.matrix:
            mark = "" if m.src == m.dst else f"  (cost {m.unit_cost:g} each)"
            lines.append(f"  {m.src:>12} -> {m.dst:<12} {m.count:>5}{mark}")
        if t.n_errored or t.n_missing:
            lines.append(f"  errored rows: {t.n_errored}, missing rows: {t.n_missing}")
        if t.flipped:
            shown = ", ".join(f"{f.row_id} ({f.src}->{f.dst})" for f in t.flipped[:8])
            more = f" +{len(t.flipped) - 8} more" if len(t.flipped) > 8 else ""
            lines.append(f"  flipped: {shown}{more}")

    for c in report.candidates:
        cal = c.calibration
        if cal is None:
            continue
        lines += [
            "",
            f"calibration {c.name}: {cal.n_rows} labeled rows, {cal.n_approved} approved, "
            f"ECE {_num(cal.ece)}"
            + (
                f", recommended threshold {_num(cal.recommended_threshold, 2)} "
                f"(target precision {_pct(cal.target_precision)})"
                if cal.target_precision is not None
                else ""
            ),
        ]
        lines += [
            "  " + ln
            for ln in _table(
                ["score >=", "auto", "coverage", "precision", "approved->review", "kappa"],
                [
                    [
                        f"{p.threshold:.2f}",
                        str(p.n_auto),
                        _pct(p.coverage),
                        _pct(p.precision),
                        _pct(p.approved_to_review),
                        _num(p.kappa),
                    ]
                    for p in cal.sweep
                ],
            )
        ]

    if gate is not None:
        lines += ["", f"CI gate: {'PASS' if gate.passed else 'FAIL'}"]
        for cg in gate.candidates:
            for reason in cg.reasons:
                lines.append(f"  {cg.name}: {reason}")
    for w in report.warnings:
        lines.append(f"warning: {w}")
    return "\n".join(lines) + "\n"


def _cal_summary(cal: Calibration) -> str:
    best = None
    if cal.recommended_threshold is not None:
        best = next(p for p in cal.sweep if p.threshold == cal.recommended_threshold)
    head = f"{cal.n_rows} labeled, ECE {_num(cal.ece)}"
    if best is None:
        if cal.target_precision is not None:
            return head + f"; no threshold reaches {_pct(cal.target_precision)} precision"
        return head
    return (
        head + f"; score >= {best.threshold:.2f}: coverage {_pct(best.coverage)}, "
        f"precision {_pct(best.precision)}, approved sent to review "
        f"{_pct(best.approved_to_review)}, kappa {_num(best.kappa)}"
    )


def render_markdown(report: EvalReport, gate: Any = None) -> str:
    """PR-comment rendering: compact (about 25 lines for 3 candidates)."""
    title = "## Caliper eval"
    if gate is not None:
        title += f": {'PASS' if gate.passed else 'FAIL'}"
    lines = [
        title,
        "",
        f"{report.n_golden} golden rows, reference `{report.reference}`. {_noise_line(report)}.",
        "",
        "| candidate | rows | errors | cost $ | p95 ms | who answered "
        "| flips | regression [95% CI] | excess |" + (" gate |" if gate is not None else ""),
        "|---|---:|---:|---:|---:|---|---:|---|---:|" + ("---|" if gate is not None else ""),
    ]
    for c in report.candidates:
        t = c.transitions
        cells = [
            f"`{c.name}`" + (" (ref)" if c.is_reference else ""),
            str(c.rows),
            f"{c.provider_errors}p/{c.decide_errors}d",
            f"{c.dollars:.4f}",
            f"{c.latency_p95_ms:.0f}",
            _who(c),
            "-" if t is None else f"{len(t.flipped)}/{t.n_rows}",
            "-" if t is None else _ci(t.regression),
            _num(c.excess_regression),
        ]
        if gate is not None:
            cells.append(_gate_status(gate, c.name) or "-")
        lines.append("| " + " | ".join(cells) + " |")

    trans = [c for c in report.candidates if c.transitions is not None]
    if trans:
        lines += ["", "**Changed outcomes vs reference**", ""]
        for c in trans:
            assert c.transitions is not None
            moved = [m for m in c.transitions.matrix if m.src != m.dst]
            text = ", ".join(f"{m.src} -> {m.dst}: {m.count}" for m in moved) or "none"
            lines.append(f"- `{c.name}`: {text}")
    cals = [c for c in report.candidates if c.calibration is not None]
    if cals:
        lines += ["", "**Calibration** (auto-accept when score >= threshold)", ""]
        for c in cals:
            assert c.calibration is not None
            lines.append(f"- `{c.name}`: {_cal_summary(c.calibration)}")
    if gate is not None:
        reasons = [(cg.name, r) for cg in gate.candidates for r in cg.reasons]
        if reasons:
            lines += ["", "**Gate failures**", ""]
            lines += [f"- `{n}`: {r}" for n, r in reasons]
    if report.warnings:
        lines.append("")
        lines += [f"> warning: {w}" for w in report.warnings]
    return "\n".join(lines) + "\n"


__all__ = [
    "CandidateReport",
    "EvalReport",
    "Tokens",
    "build_report",
    "render_markdown",
    "render_text",
]
