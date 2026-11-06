// Value display helpers for the labeller (fix-dev B owns this file; anyone may import it).
//
// badgeTone(value, options): a badge's colour is a pure function of its value, so the same
// value always looks the same, on every card and in the wizard preview:
//   1. the value names a label option (value or label, any case) with a colour -> that colour;
//   2. a word with an obvious meaning -> a semantic tone (positive green, negative red,
//      mixed/unsure amber, neutral grey; low < medium < high < urgent as a scale);
//   3. anything else (product names, codes, suppliers) -> neutral. A hash colour carried no
//      meaning and made every item look different (usability #10).
//
// formatDateTime(raw): ISO-8601 text -> the viewer's local date/time (usability #15).

const TONES = {
  good: ["positive", "pos", "good", "yes", "y", "true", "correct", "right", "approved", "accept",
    "accepted", "pass", "passed", "ok", "resolved", "success", "valid", "safe", "done", "complete",
    "completed", "relevant", "helpful"],
  bad: ["negative", "neg", "bad", "no", "n", "false", "wrong", "incorrect", "rejected", "reject",
    "fail", "failed", "error", "invalid", "unsafe", "blocked", "toxic", "spam", "irrelevant",
    "unhelpful"],
  warn: ["mixed", "unsure", "uncertain", "maybe", "partial", "partially", "pending", "review",
    "needs review", "needs_review", "warning", "unclear"],
  neutral: ["neutral", "n/a", "na", "none", "other", "unknown"],
  "lvl-1": ["low", "minor", "trivial", "p3", "p4"],
  "lvl-2": ["medium", "normal", "moderate", "p2"],
  "lvl-3": ["high", "major", "p1"],
  "lvl-4": ["urgent", "critical", "blocker", "severe", "p0"],
};
const WORD_TONE = new Map();
for (const [tone, words] of Object.entries(TONES)) for (const w of words) WORD_TONE.set(w, tone);

const HEX = /^#[0-9a-f]{6}$/i;
const norm = (v) => String(v).trim().toLowerCase();

// {cls, color}: a CSS class ("tone-good", ...) and, for option colours, the hex to use.
export function badgeTone(value, options = []) {
  const v = norm(value);
  for (const o of options || []) {
    if (!o || !o.color || !HEX.test(o.color)) continue;
    if (norm(o.value) === v || (o.label && norm(o.label) === v)) return { cls: "tone-option", color: o.color };
  }
  const tone = WORD_TONE.get(v);
  return { cls: `tone-${tone || "neutral"}`, color: null };
}

// Intl formatters are costly to build; one each, reused for every card.
let dateFmt = null;
let dateTimeFmt = null;
const ISO = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(Z|[+-]\d{2}(?::?\d{2})?)?)?$/i;

// null when the text is not an ISO date/time (the caller then shows it as it is).
export function formatDateTime(raw) {
  if (raw === null || raw === undefined) return null;
  const m = ISO.exec(String(raw).trim());
  if (!m) return null;
  const [, day, time, zone] = m;
  if (!time) {
    // a date only: no timezone shift (2026-11-06 stays 6 Nov everywhere)
    const d = new Date(`${day}T00:00:00`);
    if (Number.isNaN(d.getTime())) return null;
    dateFmt = dateFmt || new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
    return dateFmt.format(d);
  }
  let z = zone || "";
  if (/^[+-]\d{2}$/.test(z)) z += ":00"; // Postgres-style "+00"
  else if (/^[+-]\d{4}$/.test(z)) z = `${z.slice(0, 3)}:${z.slice(3)}`;
  const d = new Date(`${day}T${time}${z}`);
  if (Number.isNaN(d.getTime())) return null;
  dateTimeFmt = dateTimeFmt || new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  return dateTimeFmt.format(d);
}
