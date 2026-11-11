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
  money too); ``cost_per_row`` is dollars per (row, replica) execution, so a
  replayed reference is not counted twice, and a vote loop's replies all count;
  ``cost_vs_reference`` divides it by the reference's. A non-error reply that
  used tokens but cost $0 is ``unpriced`` (no price, or a zero price): the ratio is then
  unknown and ``[ci] max_cost_ratio`` fails it;
* ``paired_excess`` is the paired bootstrap CI of ``regression - replay
  noise`` per row (``caliper_eval.transitions.compute_paired_excess``); the
  CI gate reads its upper bound (``[ci].require = "non_inferiority"``) or its
  lower bound (``"no_evidence_of_regression"``);
* every label-based number is reported per label source and named by it:
  "precision" only for human truth, "teacher agreement" for LLM-teacher labels
  (``caliper_eval.provenance``);
* thresholds are picked on ``split == "val"`` and reported on ``"test"`` when
  the golden set is split.

``config`` is duck-typed: any object with ``reference``, ``noise_replays``,
``seed``, ``costs``, ``candidates`` (``Candidate``), ``ci.max_regression``
and ``calibration.thresholds`` works (``caliper_eval.config.EvalConfig``);
``ci.require``, ``operating_point`` and ``slices`` are optional.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ._stats import DEFAULT_N_BOOT, nearest_rank
from .calibration import Calibration, SourceMetrics, compute_calibration
from .config import GateRequirement
from .noise import NoiseFloor, compute_noise_floor, index_results
from .operating_point import MatchedOperatingPoint, compute_matched_operating_point
from .provenance import ProvenanceSummary, summarize
from .slices import SliceDef, SliceResult, describe_slices, evaluate_slices
from .transitions import PairedExcess, Transitions, compute_paired_excess, compute_transitions
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
    cost_per_row: float | None = None  # dollars / results
    cost_vs_reference: float | None = None  # cost_per_row / reference cost_per_row
    unpriced_replies: int = 0  # non-error replies that used tokens but cost $0
    monthly_cost_usd: float | None = None  # with [cost_projection]
    monthly_delta_usd: float | None = None  # vs the reference, with [cost_projection]
    served_models: list[str] = Field(default_factory=list)  # "provider:model", sorted
    served_mismatches: int = 0  # replies with matches_request=False
    served_unknown: int = 0  # non-error replies with no served_by
    stale_results: int = 0  # recorded for a different request or candidate settings than now
    transitions: Transitions | None = None
    paired_excess: PairedExcess | None = None  # regression - replay noise, paired per row
    calibration: Calibration | None = None
    matched_operating_point: MatchedOperatingPoint | None = None
    slices: list[SliceResult] = Field(default_factory=list)


class EvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str
    seed: int
    n_boot: int
    n_golden: int
    noise_replays: int
    max_regression: float
    require: str = GateRequirement.NON_INFERIORITY.value
    max_cost_ratio: float | None = None
    calls_per_day: float | None = None
    days_per_month: float | None = None
    noise_floor: NoiseFloor | None
    provenance: ProvenanceSummary | None = None
    slices: list[SliceDef] = Field(default_factory=list)
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
    mismatches = unknown = provider_errors = n_replies = unpriced = 0
    for res in results:
        for rep in res.replies:
            n_replies += 1
            used = rep.usage.input_tokens + rep.usage.output_tokens
            if rep.error is None and rep.cost_usd == 0 and used > 0:
                unpriced += 1
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
        cost_per_row=dollars / len(results) if results else None,
        unpriced_replies=unpriced,
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
    require = GateRequirement(_get(config, "ci", "require", default="non_inferiority"))
    op = _get(config, "operating_point")
    auto_outcomes = list(_get(op, "auto_outcomes", default=[]))
    slice_cfgs = list(_get(config, "slices", default=[]))
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

    # No-human-truth and no-split flags are rendered from ``provenance`` at
    # the top of the report (has_human_truth / has_splits in report.json).
    prov = summarize(golden)

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
            rep.paired_excess = compute_paired_excess(
                latest, cand.name, reference, costs=costs, seed=seed, n_boot=n_boot
            )
            if auto_outcomes:
                rep.matched_operating_point = compute_matched_operating_point(
                    golden,
                    latest,
                    cand.name,
                    reference,
                    auto_outcomes=auto_outcomes,
                    seed=seed,
                    n_boot=n_boot,
                )
            if slice_cfgs:
                rep.slices = evaluate_slices(
                    slice_cfgs,
                    golden,
                    latest,
                    cand.name,
                    reference,
                    costs=costs,
                    require=require,
                    mop=rep.matched_operating_point,
                    auto_outcomes=auto_outcomes,
                    seed=seed,
                    n_boot=n_boot,
                )
        rep.calibration = compute_calibration(
            golden,
            latest,
            cand.name,
            thresholds=thresholds,
            target_precision=target_precision,
        )
        reports.append(rep)
    projection = _get(config, "cost_projection")
    calls_per_day = _get(projection, "calls_per_day")
    days = _get(projection, "days_per_month", default=30.0)
    _cost_comparison(reports, calls_per_day, days)
    known = {c.name for c in config.candidates}
    for name in sorted(set(by_cand) - known):
        warnings.append(f"results for unknown candidate '{name}' ignored")
    slice_defs = describe_slices(golden, slice_cfgs)
    for sd in slice_defs:
        if sd.n_rows == 0:
            warnings.append(f"slice '{sd.name}' has no rows (check GoldenRow.slices / where)")

    return EvalReport(
        reference=reference,
        seed=seed,
        n_boot=n_boot,
        n_golden=len(golden),
        noise_replays=int(_get(config, "noise_replays", default=0)),
        max_regression=max_regression,
        require=require.value,
        max_cost_ratio=_get(config, "ci", "max_cost_ratio"),
        calls_per_day=None if calls_per_day is None else float(calls_per_day),
        days_per_month=None if calls_per_day is None else float(days),
        noise_floor=noise,
        provenance=prov,
        slices=slice_defs,
        candidates=reports,
        warnings=warnings,
    )


def _cost_comparison(
    reports: list[CandidateReport], calls_per_day: float | None, days: float
) -> None:
    """Fill each candidate's cost ratio and monthly projection against the reference."""
    ref = next((r for r in reports if r.is_reference), None)
    ref_cost = None
    if ref is not None and not ref.unpriced_replies and ref.cost_per_row:
        ref_cost = ref.cost_per_row
    for r in reports:
        priced = r.cost_per_row is not None and not r.unpriced_replies
        if priced and ref_cost:
            assert r.cost_per_row is not None
            r.cost_vs_reference = r.cost_per_row / ref_cost
        if calls_per_day is not None and priced:
            assert r.cost_per_row is not None
            r.monthly_cost_usd = r.cost_per_row * calls_per_day * days
            if ref_cost:
                r.monthly_delta_usd = r.monthly_cost_usd - ref_cost * calls_per_day * days


# --------------------------------------------------------------------------- rendering


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _num(x: float | None, nd: int = 3) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def _ci(ci: Any, nd: int = 3) -> str:
    return f"{ci.estimate:.{nd}f} [{ci.low:.{nd}f}, {ci.high:.{nd}f}]"


def _signed_ci(ci: Any, nd: int = 3) -> str:
    return f"{ci.estimate:+.{nd}f} [{ci.low:+.{nd}f}, {ci.high:+.{nd}f}]"


def _ids(rows: Sequence[str], limit: int = 4) -> str:
    more = f" +{len(rows) - limit} more" if len(rows) > limit else ""
    return ", ".join(rows[:limit]) + more


def _who(c: CandidateReport) -> str:
    models = ",".join(m.split(":", 1)[1] for m in c.served_models) or "-"
    flags = []
    if c.served_mismatches:
        flags.append(f"MISMATCH x{c.served_mismatches}")
    if c.served_unknown:
        flags.append(f"unknown x{c.served_unknown}")
    return models + (f" ({'; '.join(flags)})" if flags else "")


def _per_row(c: CandidateReport) -> str:
    """``$/row (x ref)`` cell."""
    if c.cost_per_row is None:
        return "-"
    if c.unpriced_replies:
        return f"unpriced x{c.unpriced_replies}"
    if c.is_reference:
        return f"{c.cost_per_row:.6f} (ref)"
    ratio = "?" if c.cost_vs_reference is None else f"{c.cost_vs_reference:.2f}x"
    return f"{c.cost_per_row:.6f} ({ratio})"


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _projection_line(report: EvalReport) -> str | None:
    if report.calls_per_day is None:
        return None
    parts = []
    for c in report.candidates:
        if c.monthly_cost_usd is None:
            parts.append(f"{c.name} unpriced")
            continue
        part = f"{c.name} {_usd(c.monthly_cost_usd)}/mo"
        if not c.is_reference and c.monthly_delta_usd is not None:
            sign = "+" if c.monthly_delta_usd >= 0 else "-"
            part += f" ({sign}{_usd(abs(c.monthly_delta_usd))} vs ref)"
        parts.append(part)
    return (
        f"cost projection ({report.calls_per_day:,.0f} calls/day x "
        f"{report.days_per_month:g} days): " + "; ".join(parts)
    )


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


_KIND_NOTE = {
    "human_truth": "human truth",
    "teacher_agreement": "teacher agreement only",
    "heuristic_agreement": "heuristic agreement only",
    "unknown_source": "source not recorded",
}


def _provenance_lines(report: EvalReport) -> list[str]:
    """Label sources and split, or nothing when the golden set has no labels."""
    p = report.provenance
    if p is None or not p.n_labelled:
        return []
    from .provenance import basis_kind

    parts = [f"{k} {n} ({_KIND_NOTE[basis_kind([k])]})" for k, n in p.by_source.items()]
    lines = [f"labels ({p.n_labelled} rows): " + ", ".join(parts)]
    if not p.has_human_truth:
        lines.append(
            "  NO HUMAN-TRUTH LABELS: label numbers below are agreement, not precision. "
            "Label a human-truth slice (label_source internal_reviewer / human_end_user)."
        )
    if p.has_splits:
        split = ", ".join(f"{k} {n}" for k, n in p.by_split.items())
        lines.append(f"splits: {split}; thresholds picked on val, reported on test")
    else:
        lines.append("splits: none; thresholds picked and reported on the same rows (optimistic)")
    return lines


def _gate_status(gate: Any, name: str) -> str:
    if gate is None:
        return ""
    for cg in gate.candidates:
        if cg.name == name:
            return "PASS" if cg.passed else "FAIL"
    return ""


def _excess(c: CandidateReport) -> str:
    return "-" if c.paired_excess is None else _ci(c.paired_excess.delta)


def _source_metric_line(m: SourceMetrics) -> str:
    head = f"{m.source} ({m.n_rows})"
    if m.threshold is None:
        return f"{head}: ECE {_num(m.ece)}"
    return (
        f"{head}: {m.metric} {_pct(m.auto_approved_rate)}, coverage {_pct(m.coverage)}, "
        f"approved->review {_pct(m.approved_to_review)}, kappa {_num(m.kappa)}"
    )


def _cal_head(c: CandidateReport, cal: Calibration) -> str:
    basis = ", ".join(cal.basis_sources)
    head = (
        f"calibration {c.name} (sweep on {cal.selection_split}: {cal.n_rows} rows labelled by "
        f"{basis}; ECE {_num(cal.ece)})"
    )
    if cal.target_precision is None:
        return head
    if cal.recommended_threshold is None:
        return head + f": no threshold reaches {_pct(cal.target_precision)} {cal.metric}"
    return (
        head + f": picked {cal.recommended_threshold:.2f} "
        f"(lowest with {cal.metric} >= {_pct(cal.target_precision)})"
    )


def _cal_report_lines(cal: Calibration, indent: str) -> list[str]:
    show = cal.report_split != cal.selection_split or len(cal.on_report) > 1
    if not show or not cal.on_report:
        return []
    t = cal.recommended_threshold
    lead = f"on {cal.report_split}" + (f" at {t:.2f}" if t is not None else "") + ": "
    out = []
    for i, m in enumerate(cal.on_report):
        out.append(indent + (lead if i == 0 else " " * len(lead)) + _source_metric_line(m))
    return out


def _mop_lines(report: EvalReport, c: CandidateReport) -> list[str]:
    m = c.matched_operating_point
    if m is None:
        return []
    ref = report.reference
    if m.threshold is None:
        match = f"{c.name} has no scores: own outcomes ({_pct(m.candidate_coverage)})"
    else:
        match = f"{c.name} matches at score >= {m.threshold:.3f} ({_pct(m.candidate_coverage)})"
    lines = [
        f"  {c.name}: {ref} auto-handles {_pct(m.reference_coverage)} of {m.n_selection} "
        f"{m.selection_split} rows; {match}"
    ]
    for s in m.by_source:
        if s.kind == "human_truth":
            body = (
                f"{ref} loses {s.reference.lost} (missed {s.reference.missed}, bad auto "
                f"{s.reference.bad_auto}), {c.name} loses {s.candidate.lost} (missed "
                f"{s.candidate.missed}, bad auto {s.candidate.bad_auto})"
            )
        else:
            body = (
                f"{s.metric}: {ref} disagrees on {s.reference.lost}, {c.name} on {s.candidate.lost}"
            )
        only = f"; {c.name} only: {_ids(s.candidate_only)}" if s.candidate_only else ""
        lines.append(
            f"    {s.source} ({s.n_rows} on {m.report_split}): {body}{only}; "
            f"delta {_signed_ci(s.delta)}"
        )
    return lines


def render_text(report: EvalReport, gate: Any = None) -> str:
    """Terminal rendering. Deterministic: no paths, timestamps or run ids."""
    lines = [
        f"caliper eval: {len(report.candidates)} candidates, {report.n_golden} golden rows, "
        f"reference={report.reference}, seed={report.seed}, bootstrap n={report.n_boot}",
        *_provenance_lines(report),
        _noise_line(report),
        "",
    ]
    headers = [
        "candidate",
        "rows",
        "errors",
        "tokens in/out/cache",
        "p50/p95 ms",
        "cost $",
        "$/row (x ref)",
        "who answered",
        "flips",
        "regression [95% CI]",
        "excess vs noise (paired)",
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
            _per_row(c),
            _who(c),
            "-" if t is None else f"{len(t.flipped)}/{t.n_rows}",
            "-" if t is None else _ci(t.regression),
            _excess(c),
        ]
        if gate is not None:
            row.append(_gate_status(gate, c.name) or "-")
        rows.append(row)
    lines += _table(headers, rows)
    projection = _projection_line(report)
    if projection:
        lines += ["", projection]

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

    if report.slices:
        lines += ["", "slices:"]
        width = max(len(c.name) for c in report.candidates)
        for sd in report.slices:
            rule = sd.rule + (f" <= {sd.threshold:g}" if sd.rule == "max_regression" else "")
            rule = rule if sd.rule != "min_precision" else f"min_precision >= {sd.threshold:g}"
            on_test = f", {sd.n_report_rows} on test" if report.provenance.has_splits else ""
            lines.append(f"  {sd.name} ({rule}; {sd.n_rows} rows{on_test})")
            for c in report.candidates:
                for r in c.slices:
                    if r.slice == sd.name:
                        verdict = "PASS" if r.passed else "FAIL"
                        lines.append(f"    {c.name:<{width}}  {verdict}  {r.detail}")

    mops = [c for c in report.candidates if c.matched_operating_point is not None]
    if mops:
        first = mops[0].matched_operating_point
        assert first is not None
        lines += [
            "",
            f"matched operating point (auto = {', '.join(first.auto_outcomes)}; coverage "
            f"matched on {first.selection_split}, losses on {first.report_split}; delta = "
            "candidate - reference lost rows per row, paired 95% CI):",
        ]
        for c in mops:
            lines += _mop_lines(report, c)

    full_sweep_done = False
    for c in report.candidates:
        cal = c.calibration
        if cal is None:
            continue
        lines += ["", _cal_head(c, cal)]
        if not full_sweep_done and cal.sweep:
            full_sweep_done = True
            lines += [
                "  " + ln
                for ln in _table(
                    ["score >=", "auto", "coverage", cal.metric, "approved->review", "kappa"],
                    [
                        [
                            f"{p.threshold:.2f}",
                            str(p.n_auto),
                            _pct(p.coverage),
                            _pct(p.auto_approved_rate),
                            _pct(p.approved_to_review),
                            _num(p.kappa),
                        ]
                        for p in cal.sweep
                    ],
                )
            ]
        elif cal.recommended_threshold is not None and not _cal_report_lines(cal, ""):
            p = next(p for p in cal.sweep if p.threshold == cal.recommended_threshold)
            lines.append(
                f"  at {p.threshold:.2f}: coverage {_pct(p.coverage)}, {cal.metric} "
                f"{_pct(p.auto_approved_rate)}, approved->review "
                f"{_pct(p.approved_to_review)}, kappa {_num(p.kappa)}"
            )
        lines += _cal_report_lines(cal, "  ")

    if gate is not None:
        lines += ["", f"CI gate: {'PASS' if gate.passed else 'FAIL'} ({_gate_rule(report)})"]
        for cg in gate.candidates:
            for reason in cg.reasons:
                lines.append(f"  {cg.name}: {reason}")
    for w in report.warnings:
        lines.append(f"warning: {w}")
    return "\n".join(lines) + "\n"


def _gate_rule(report: EvalReport) -> str:
    if report.require == GateRequirement.NO_EVIDENCE_OF_REGRESSION.value:
        rule = f"fail when paired excess lower bound > {report.max_regression:g}"
    else:
        rule = f"pass when paired excess upper bound <= {report.max_regression:g}"
    if report.max_cost_ratio is not None:
        rule += f" and cost <= {report.max_cost_ratio:g}x the reference"
    return rule


def _cal_summary(cal: Calibration) -> str:
    head = f"{cal.n_rows} rows on {cal.selection_split} ({cal.metric}), ECE {_num(cal.ece)}"
    t = cal.recommended_threshold
    if t is None:
        if cal.target_precision is not None:
            return head + f"; no threshold reaches {_pct(cal.target_precision)} {cal.metric}"
        return head
    best = next(p for p in cal.sweep if p.threshold == t)
    head += f"; picked {t:.2f}"
    if cal.report_split == cal.selection_split and len(cal.on_report) <= 1:
        return head + (
            f": coverage {_pct(best.coverage)}, {cal.metric} {_pct(best.auto_approved_rate)}, "
            f"approved sent to review {_pct(best.approved_to_review)}, kappa {_num(best.kappa)}"
        )
    parts = [
        f"{m.metric} {_pct(m.auto_approved_rate)} ({m.source}, {m.n_rows})" for m in cal.on_report
    ]
    return head + f"; on {cal.report_split}: " + ", ".join(parts)


def render_markdown(report: EvalReport, gate: Any = None) -> str:
    """PR-comment rendering: compact (about 35 lines for 3 candidates and 2 slices)."""
    title = "## Caliper eval"
    if gate is not None:
        title += f": {'PASS' if gate.passed else 'FAIL'}"
    lines = [
        title,
        "",
        f"{report.n_golden} golden rows, reference `{report.reference}`. {_noise_line(report)}.",
    ]
    prov = _provenance_lines(report)
    if prov:
        lines += ["", "; ".join(ln.strip() for ln in prov)]
    lines += [
        "",
        "| candidate | rows | errors | cost $ | $/row (x ref) | p95 ms | who answered "
        "| flips | regression [95% CI] | excess vs noise (paired) |"
        + (" gate |" if gate is not None else ""),
        "|---|---:|---:|---:|---:|---:|---|---:|---|---|" + ("---|" if gate is not None else ""),
    ]
    for c in report.candidates:
        t = c.transitions
        cells = [
            f"`{c.name}`" + (" (ref)" if c.is_reference else ""),
            str(c.rows),
            f"{c.provider_errors}p/{c.decide_errors}d",
            f"{c.dollars:.4f}",
            _per_row(c),
            f"{c.latency_p95_ms:.0f}",
            _who(c),
            "-" if t is None else f"{len(t.flipped)}/{t.n_rows}",
            "-" if t is None else _ci(t.regression),
            _excess(c),
        ]
        if gate is not None:
            cells.append(_gate_status(gate, c.name) or "-")
        lines.append("| " + " | ".join(cells) + " |")
    projection = _projection_line(report)
    if projection:
        lines += ["", projection[0].upper() + projection[1:] + "."]

    trans = [c for c in report.candidates if c.transitions is not None]
    if trans:
        lines += ["", "**Changed outcomes vs reference**", ""]
        for c in trans:
            assert c.transitions is not None
            moved = [m for m in c.transitions.matrix if m.src != m.dst]
            text = ", ".join(f"{m.src} -> {m.dst}: {m.count}" for m in moved) or "none"
            lines.append(f"- `{c.name}`: {text}")
    if report.slices:
        lines += ["", "**Slices**", ""]
        for sd in report.slices:
            verdicts = [
                f"`{c.name}` {'PASS' if r.passed else 'FAIL'}"
                for c in report.candidates
                for r in c.slices
                if r.slice == sd.name
            ]
            lines.append(f"- `{sd.name}` ({sd.rule}, {sd.n_rows} rows): " + ", ".join(verdicts))
    mops = [c for c in report.candidates if c.matched_operating_point is not None]
    if mops:
        lines += ["", "**Matched operating point** (same coverage as the reference)", ""]
        for c in mops:
            m = c.matched_operating_point
            assert m is not None
            parts = []
            for s in m.by_source:
                what = "lost" if s.kind == "human_truth" else "disagreements"
                parts.append(
                    f"{s.source} {what} {s.candidate.lost} vs {s.reference.lost} "
                    f"(delta {_signed_ci(s.delta)})"
                )
            at = "own outcomes" if m.threshold is None else f"score >= {m.threshold:.3f}"
            lines.append(
                f"- `{c.name}` at {at} (reference coverage {_pct(m.reference_coverage)} on "
                f"{m.selection_split}), on {m.report_split}: " + "; ".join(parts)
            )
    cals = [c for c in report.candidates if c.calibration is not None]
    if cals:
        lines += ["", "**Calibration** (auto-accept when score >= threshold)", ""]
        for c in cals:
            assert c.calibration is not None
            lines.append(f"- `{c.name}`: {_cal_summary(c.calibration)}")
    if gate is not None:
        reasons = [(cg.name, r) for cg in gate.candidates for r in cg.reasons]
        if reasons:
            lines += ["", f"**Gate failures** ({_gate_rule(report)})", ""]
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
