"use client";

import { useCallback, useEffect, useState } from "react";
import { dryRun, type DryRunError, type DryRunResponse } from "../lib/api";
import { GraderEditor } from "./GraderEditor";
import { ResultsPanel } from "./ResultsPanel";

const STORAGE_KEY = "caliper.editor.draft";
const DEFAULT_EVAL_SET = "legal_v3";

interface GraderWorkspaceProps {
  graderName: string;
  graderVersion: string;
  initialSource: string;
}

export function GraderWorkspace({
  graderName,
  graderVersion,
  initialSource,
}: GraderWorkspaceProps) {
  const [source, setSource] = useState<string>(initialSource);
  const [hydrated, setHydrated] = useState<boolean>(false);
  const [results, setResults] = useState<DryRunResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [evalSet, setEvalSet] = useState<string>(DEFAULT_EVAL_SET);

  // Hydrate from localStorage (and the optional ?evalSet= query param) on mount.
  useEffect(() => {
    const fromQuery = new URLSearchParams(window.location.search).get("evalSet");
    if (fromQuery) setEvalSet(fromQuery);
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      if (stored !== null && stored.length > 0) {
        setSource(stored);
      }
    } catch {
      // localStorage may be unavailable; fall back to initialSource.
    }
    setHydrated(true);
  }, []);

  const handleSourceChange = useCallback((next: string) => {
    setSource(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // ignore quota / unavailable storage
    }
  }, []);

  const handleDryRun = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await dryRun(source, evalSet);
      setResults(data);
    } catch (e) {
      const err = e as DryRunError;
      setError(err.detail ?? String(e));
      setResults(null);
    } finally {
      setLoading(false);
    }
  }, [source, evalSet]);

  return (
    <main
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100vh",
        width: "100vw",
        background: "#1e1e1e",
        color: "#e6e6e6",
      }}
    >
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "12px 20px",
          borderBottom: "1px solid #2a2a2a",
          background: "#181818",
        }}
      >
        <div>
          <div
            style={{ fontSize: 14, fontWeight: 600, letterSpacing: 0.2 }}
          >
            Caliper · Grader Editor
          </div>
          <div
            style={{
              marginTop: 4,
              fontSize: 12,
              color: "#9a9a9a",
              fontFamily:
                "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
            }}
          >
            {results?.grader_name ?? graderName}{" "}
            <span style={{ opacity: 0.6 }}>·</span> v
            {results?.grader_version ?? graderVersion}
            <span style={{ opacity: 0.6 }}> · </span>eval set:{" "}
            <span style={{ color: "#cfcfcf" }}>{evalSet}</span>
          </div>
        </div>
        <button
          onClick={handleDryRun}
          disabled={loading || !hydrated}
          style={{
            padding: "8px 16px",
            background: loading ? "#2a4a6a" : "#3578c4",
            color: "#fff",
            border: "1px solid #4a90d9",
            borderRadius: 4,
            cursor: loading ? "wait" : "pointer",
            fontSize: 13,
            fontWeight: 500,
            opacity: hydrated ? 1 : 0.5,
          }}
        >
          {loading ? "Running…" : "Dry run"}
        </button>
      </header>

      <div
        style={{
          flex: 1,
          minHeight: 0,
          display: "grid",
          gridTemplateRows: "minmax(200px, 60%) minmax(200px, 40%)",
        }}
      >
        {hydrated ? (
          <GraderEditor value={source} onChange={handleSourceChange} />
        ) : (
          <div style={{ background: "#1e1e1e" }} />
        )}
        <ResultsPanel data={results} error={error} loading={loading} />
      </div>
    </main>
  );
}

export default GraderWorkspace;
