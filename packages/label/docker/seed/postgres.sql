-- Fictional support tickets for the caliper-label connector tests.
CREATE SCHEMA support;
CREATE TABLE support.tickets (
  ticket_id   TEXT PRIMARY KEY,
  subject     TEXT NOT NULL,
  body        TEXT NOT NULL,
  priority    TEXT NOT NULL,
  amount      NUMERIC(10, 2),
  opened_at   TIMESTAMPTZ NOT NULL,
  meta        JSONB,
  screenshot  TEXT
);
INSERT INTO support.tickets
SELECT
  'PG-' || lpad(i::text, 3, '0'),
  'Moonwick order ' || i || ' issue',
  'Fictional customer note ' || i || E'.\nSecond line.',
  (ARRAY['low', 'medium', 'high'])[1 + i % 3],
  (i * 3.25)::numeric(10, 2),
  timestamptz '2026-10-01 09:00:00+00' + i * interval '1 hour',
  jsonb_build_object('channel', CASE WHEN i % 2 = 0 THEN 'email' ELSE 'chat' END, 'n', i),
  's3://caliper-test/img/t' || (i % 2) || '.png'
FROM generate_series(1, 40) AS i;
CREATE TABLE support.review_labels (item_id TEXT, decision TEXT);
