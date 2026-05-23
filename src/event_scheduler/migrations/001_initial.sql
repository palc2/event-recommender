CREATE DATABASE IF NOT EXISTS event_scheduler;

-- 1. URL dedup index
CREATE TABLE IF NOT EXISTS event_scheduler.discovered_urls (
    url_hash UInt64,
    url String,
    source LowCardinality(String),
    discovered_at DateTime64(3),
    last_scraped_at Nullable(DateTime64(3)),
    scrape_status LowCardinality(String) DEFAULT 'pending'
) ENGINE = ReplacingMergeTree(discovered_at)
ORDER BY (source, url_hash);

-- 2. Raw scraped content (TTL 30 days)
CREATE TABLE IF NOT EXISTS event_scheduler.raw_pages (
    url_hash UInt64,
    source LowCardinality(String),
    content String,
    scraped_at DateTime64(3),
    nimble_task_id Nullable(String),
    parse_status LowCardinality(String) DEFAULT 'pending'
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(scraped_at)
ORDER BY (source, scraped_at)
TTL scraped_at + INTERVAL 30 DAY;

-- 3. Structured events
CREATE TABLE IF NOT EXISTS event_scheduler.events (
    event_id UUID,
    url_hash UInt64,
    content_hash UInt64,
    title String,
    description String,
    start_time DateTime64(3),
    end_time Nullable(DateTime64(3)),
    location_name String,
    location_address String,
    lat Nullable(Float64),
    lon Nullable(Float64),
    category LowCardinality(String),
    tags Array(String),
    price_cents UInt32 DEFAULT 0,
    source LowCardinality(String),
    source_url String,
    image_url Nullable(String),
    parsed_at DateTime64(3)
) ENGINE = ReplacingMergeTree(parsed_at)
ORDER BY (content_hash, event_id);

-- 4. User preference weights
CREATE TABLE IF NOT EXISTS event_scheduler.user_preferences (
    user_id String,
    preference_type LowCardinality(String),
    key String,
    weight Float32,
    updated_at DateTime64(3)
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (user_id, preference_type, key);

-- 5. Recommendations with feedback
CREATE TABLE IF NOT EXISTS event_scheduler.recommendations (
    rec_id UUID,
    user_id String,
    event_id UUID,
    score Float32,
    reasoning String,
    status LowCardinality(String) DEFAULT 'pending',
    calendar_event_id Nullable(String),
    created_at DateTime64(3),
    delivered_at Nullable(DateTime64(3)),
    responded_at Nullable(DateTime64(3))
) ENGINE = MergeTree()
ORDER BY (user_id, created_at);

-- 6. Agent observability
CREATE TABLE IF NOT EXISTS event_scheduler.agent_runs (
    run_id UUID,
    agent_name LowCardinality(String),
    started_at DateTime64(3),
    completed_at Nullable(DateTime64(3)),
    items_processed UInt32 DEFAULT 0,
    items_failed UInt32 DEFAULT 0,
    error_message Nullable(String)
) ENGINE = MergeTree()
ORDER BY (agent_name, started_at);
