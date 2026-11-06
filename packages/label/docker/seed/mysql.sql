-- Fictional support tickets for the caliper-label connector tests.
CREATE TABLE tickets (
  ticket_id  VARCHAR(16) PRIMARY KEY,
  subject    VARCHAR(200) NOT NULL,
  priority   VARCHAR(16) NOT NULL,
  amount     DECIMAL(10, 2),
  opened_at  DATETIME NOT NULL
);
INSERT INTO tickets
WITH RECURSIVE seq (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM seq WHERE i < 40)
SELECT
  CONCAT('MY-', LPAD(i, 3, '0')),
  CONCAT('Moonwick order ', i, ' issue'),
  ELT(1 + i % 3, 'low', 'medium', 'high'),
  i * 3.25,
  TIMESTAMP('2026-10-01 09:00:00') + INTERVAL i HOUR
FROM seq;
