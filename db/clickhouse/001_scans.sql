CREATE DATABASE IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.scans (
  event_id        UUID,
  ts              DateTime64(3, 'UTC'),
  workspace_id    UUID,
  qr_id           UUID,
  host            LowCardinality(String),
  slug            String,
  destination_url String,
  visitor_hash    String,                      -- sha256(salt + workspace + ip + ua): sem IP bruto (LGPD)
  country         LowCardinality(String),
  region          LowCardinality(String),
  city            String,
  device_type     LowCardinality(String),      -- mobile | tablet | desktop | bot
  os              LowCardinality(String),
  os_version      String,
  browser         LowCardinality(String),
  referer         String,
  language        LowCardinality(String),
  is_bot          UInt8
) ENGINE = ReplacingMergeTree                  -- dedupe por event_id (entrega at-least-once)
PARTITION BY toYYYYMM(ts)
ORDER BY (workspace_id, qr_id, ts, event_id)
TTL toDateTime(ts) + INTERVAL 25 MONTH;

CREATE TABLE IF NOT EXISTS analytics.scans_daily (
  workspace_id UUID,
  qr_id        UUID,
  day          Date,
  country      LowCardinality(String),
  device_type  LowCardinality(String),
  os           LowCardinality(String),
  scans        AggregateFunction(count),
  unique_scans AggregateFunction(uniq, String)
) ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(day)
ORDER BY (workspace_id, qr_id, day, country, device_type, os);

CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.scans_daily_mv TO analytics.scans_daily AS
SELECT
  workspace_id, qr_id, toDate(ts) AS day, country, device_type, os,
  countState()            AS scans,
  uniqState(visitor_hash) AS unique_scans
FROM analytics.scans
WHERE is_bot = 0
GROUP BY workspace_id, qr_id, day, country, device_type, os;
