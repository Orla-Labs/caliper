"use client";

import type { DryRunResponse, EvalRowOut, RowResult } from "../lib/api";
import { Histogram } from "./Histogram";

interface ResultsPanelProps {
  data: DryRunResponse | null;
  error: string | null;
  loading: boolean;
}

export function ResultsPanel({ data, error, loading }: ResultsPanelProps) {
  return (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        background: "#161616",
        borderTop: "1px solid #2a2a2a",
        overflow: "auto",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
      }}
    >
      <div
        style={{
          padding: "10px 16px",
          borderBottom: "1px solid #2a2a2a",
          color: "#cfcfcf",
          fontSize: 13,
          fontWeight: 600,
        }}
      >
        Dry-run results{loading ? " — running…" : ""}
      </div>

      {error !== null && (
        <div
          style={{
            margin: 16,
            padding: 12,
            background: "#2a1818",
            border: "1px solid #5a2e2e",
            color: "#ffb4a8",
            fontFamily:
              "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
            fontSize: 12,
            whiteSpace: "pre-wrap",
          }}
        >
          {error}
        </div>
      )}

      {data !== null && error === null && (
        <ResultsBody data={data} />
      )}

      {data === null && error === null && !loading && (
        <div
          style={{
            padding: 16,
            color: "#7a7a7a",
            fontSize: 13,
          }}
        >
          Edit the grader above and click <b>Dry run</b> to score it against the
          eval set.
        </div>
      )}
    </div>
  );
}

function ResultsBody({ data }: { data: DryRunResponse }) {
  const { report, rows } = data;
  const scores = report.results.map((r) => r.score);
  const rowsById: Record<string, EvalRowOut> = Object.fromEntries(
    rows.map((r) => [r.id, r]),
  );

  const sortedByScore = [...report.results].sort((a, b) => a.score - b.score);
  const lowest = sortedByScore.slice(0, 3);
  const highest = sortedByScore.slice(-3).reverse();

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
        gap: 16,
        padding: 16,
        color: "#cfcfcf",
        fontSize: 12,
      }}
    >
      {/* Summary */}
      <Section title="Summary">
        <table style={{ borderCollapse: "collapse", fontSize: 12 }}>
          <tbody>
            <Stat label="grader" value={`${report.grader_name} v${report.grader_version}`} />
            <Stat label="eval set" value={data.eval_set} />
            <Stat label="rows" value={`${report.n_succeeded}/${report.n_rows}`} />
            <Stat label="mean" value={report.mean_score.toFixed(3)} />
            <Stat label="std" value={report.std_score.toFixed(3)} />
            <Stat label="min/max" value={`${report.min_score.toFixed(2)} / ${report.max_score.toFixed(2)}`} />
            <Stat label="failures" value={String(report.n_failed)} />
          </tbody>
        </table>
      </Section>

      {/* Histogram */}
      <Section title="Score distribution (10 bins)">
        <Histogram scores={scores} bins={10} />
      </Section>

      {/* Lowest scoring */}
      <Section title="Lowest scoring rows">
        {lowest.length === 0 ? (
          <Empty />
        ) : (
          lowest.map((r) => (
            <RowCard key={r.row_id} result={r} row={rowsById[r.row_id]} />
          ))
        )}
      </Section>

      {/* Highest scoring */}
      <Section title="Highest scoring rows">
        {highest.length === 0 ? (
          <Empty />
        ) : (
          highest.map((r) => (
            <RowCard key={r.row_id} result={r} row={rowsById[r.row_id]} />
          ))
        )}
      </Section>

      {/* Failures (full width) */}
      {report.failures.length > 0 && (
        <div style={{ gridColumn: "1 / span 2" }}>
          <Section title={`Failures (${report.failures.length})`}>
            <table
              style={{
                width: "100%",
                borderCollapse: "collapse",
                fontFamily:
                  "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                fontSize: 11,
              }}
            >
              <thead>
                <tr style={{ color: "#9a9a9a", textAlign: "left" }}>
                  <th style={{ padding: "4px 8px" }}>row</th>
                  <th style={{ padding: "4px 8px" }}>error</th>
                  <th style={{ padding: "4px 8px" }}>message</th>
                </tr>
              </thead>
              <tbody>
                {report.failures.slice(0, 10).map((f) => (
                  <tr key={f.row_id} style={{ borderTop: "1px solid #2a2a2a" }}>
                    <td style={{ padding: "4px 8px", color: "#cfcfcf" }}>{f.row_id}</td>
                    <td style={{ padding: "4px 8px", color: "#ffb4a8" }}>{f.error_type}</td>
                    <td style={{ padding: "4px 8px", color: "#cfcfcf" }}>{f.error_message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
        </div>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div
      style={{
        background: "#1c1c1c",
        border: "1px solid #2a2a2a",
        borderRadius: 4,
        padding: 12,
      }}
    >
      <div
        style={{
          fontSize: 11,
          letterSpacing: 0.6,
          color: "#9a9a9a",
          textTransform: "uppercase",
          marginBottom: 8,
        }}
      >
        {title}
      </div>
      {children}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <tr>
      <td
        style={{
          color: "#9a9a9a",
          padding: "2px 12px 2px 0",
          fontFamily:
            "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
          textAlign: "right",
          width: 80,
        }}
      >
        {label}
      </td>
      <td
        style={{
          padding: "2px 0",
          fontFamily:
            "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
          color: "#e6e6e6",
        }}
      >
        {value}
      </td>
    </tr>
  );
}

function Empty() {
  return <div style={{ color: "#7a7a7a" }}>(none)</div>;
}

function RowCard({
  result,
  row,
}: {
  result: RowResult;
  row: EvalRowOut | undefined;
}) {
  const scoreColor =
    result.score >= 0.5 ? "#7ed957" : result.score > 0 ? "#e6c34a" : "#ff7b72";
  return (
    <div
      style={{
        marginBottom: 8,
        padding: 8,
        background: "#1a1a1a",
        border: "1px solid #2a2a2a",
        borderRadius: 4,
      }}
    >
      <div
        style={{
          display: "flex",
          gap: 12,
          fontFamily:
            "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
          fontSize: 11,
          color: "#9a9a9a",
        }}
      >
        <span>{result.row_id}</span>
        <span style={{ color: scoreColor, fontWeight: 600 }}>
          {result.score.toFixed(3)}
        </span>
      </div>
      {row && (
        <>
          <div style={{ marginTop: 4, color: "#cfcfcf", fontSize: 12 }}>
            {row.prompt}
          </div>
          <div
            style={{
              marginTop: 4,
              color: "#7a7a7a",
              fontSize: 11,
              fontFamily:
                "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
              maxHeight: 40,
              overflow: "hidden",
              whiteSpace: "nowrap",
              textOverflow: "ellipsis",
            }}
          >
            {row.response}
          </div>
        </>
      )}
      {result.explanation && (
        <div
          style={{
            marginTop: 4,
            color: "#9a9a9a",
            fontSize: 11,
            fontStyle: "italic",
          }}
        >
          {result.explanation}
        </div>
      )}
    </div>
  );
}

export default ResultsPanel;
