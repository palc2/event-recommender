# Event Scheduler

Agentic NYC event scheduler that scrapes event sites, learns your preferences, and creates Google Calendar invites for events you'll actually want to attend.

## Architecture

Four tick-based agents form a pipeline. Each agent loads its work queue from ClickHouse, does one batch of work, writes results back, and exits. No long-running processes — every agent invocation is a short, stateless tick.

```mermaid
graph TD
    subgraph Sources
        S1[secretnyc.co]
        S2[lu.ma/nyc]
    end

    subgraph "Nimble API"
        N["/extract + /batch"]
    end

    subgraph "Agent Pipeline"
        A1["Ingest Agent<br/><i>hourly</i>"]
        A2["Parser Agent<br/><i>every 15 min</i>"]
        A3["Recommender Agent<br/><i>every 15 min</i>"]
        A4["Delivery Agent<br/><i>every 15 min</i>"]
    end

    subgraph "ClickHouse"
        T1[(discovered_urls)]
        T2[(raw_pages)]
        T3[(events)]
        T4[(user_preferences)]
        T5[(recommendations)]
        T6[(agent_runs)]
    end

    subgraph "External"
        GC[Google Calendar]
        FB[FastAPI Feedback<br/>Endpoint]
    end

    S1 --> N
    S2 --> N
    N --> A1
    A1 --> T1
    A1 --> T2
    T2 --> A2
    A2 -->|"Claude Haiku"| T3
    T3 --> A3
    T4 --> A3
    A3 -->|"Claude Sonnet"| T5
    T5 --> A4
    A4 --> GC
    GC -->|"RSVP poll"| A4
    FB -->|"accept/reject"| T4
    A4 -->|"feedback"| T4
    A1 --> T6
    A2 --> T6
    A3 --> T6
    A4 --> T6
```

## Agent Tick Pattern

Each agent follows the same stateless pattern. No in-memory state survives between ticks — ClickHouse is the single source of truth.

```mermaid
sequenceDiagram
    participant S as Scheduler
    participant A as Agent
    participant CH as ClickHouse
    participant EXT as External API

    S->>A: tick()
    A->>CH: SELECT pending work
    CH-->>A: work items
    A->>EXT: API calls (Nimble / Claude / Calendar)
    EXT-->>A: results
    A->>CH: INSERT results
    A->>CH: UPDATE status → done
    A->>CH: INSERT agent_run log
    A-->>S: done
```

## Recommendation Flow

Two-stage scoring keeps LLM costs low by pre-filtering with cheap Python math before sending only the top candidates to Claude Sonnet for reranking.

```mermaid
graph LR
    subgraph "Stage 1: Fast Pre-filter"
        E[200 upcoming<br/>events] --> QS["quick_score()<br/><i>Python weighted sum</i>"]
        QS --> F["Top 20<br/>candidates"]
    end

    subgraph "Stage 2: LLM Rerank"
        F --> LLM["Claude Sonnet<br/><i>single API call</i>"]
        LLM --> R["Scored + reasoned<br/>recommendations"]
    end

    subgraph "Delivery"
        R --> CAL["Google Calendar<br/>invite created"]
    end
```

## Feedback Loop

User accept/reject signals update preference weights asymmetrically: rejections have a stronger effect (-0.15) than accepts (+0.10) because false positives are more annoying than missed events.

```mermaid
stateDiagram-v2
    [*] --> Pending: Recommender creates
    Pending --> Delivered: Delivery Agent sends calendar invite
    Delivered --> Accepted: User clicks accept / keeps event
    Delivered --> Rejected: User clicks reject / deletes event
    Accepted --> [*]: category +0.10, time +0.10, location +0.10
    Rejected --> [*]: category -0.15, time -0.15, location -0.15
```

## Data Model

```mermaid
erDiagram
    discovered_urls {
        UInt64 url_hash PK
        String url
        String source
        DateTime64 discovered_at
        DateTime64 last_scraped_at
        String scrape_status
    }

    raw_pages {
        UInt64 url_hash FK
        String source
        String content
        DateTime64 scraped_at
        String parse_status
    }

    events {
        UUID event_id PK
        UInt64 url_hash FK
        UInt64 content_hash
        String title
        DateTime64 start_time
        String location_name
        String category
        UInt32 price_cents
    }

    user_preferences {
        String user_id PK
        String preference_type PK
        String key PK
        Float32 weight
    }

    recommendations {
        UUID rec_id PK
        String user_id FK
        UUID event_id FK
        Float32 score
        String status
        String calendar_event_id
    }

    agent_runs {
        UUID run_id PK
        String agent_name
        DateTime64 started_at
        UInt32 items_processed
    }

    discovered_urls ||--o{ raw_pages : "scraped into"
    raw_pages ||--o{ events : "parsed into"
    events ||--o{ recommendations : "scored for"
    user_preferences ||--o{ recommendations : "influences"
```

## Project Structure

```
src/event_scheduler/
├── config.py                 # Pydantic Settings (env vars)
├── db.py                     # ClickHouse client + migration runner
├── models.py                 # Pydantic data models
├── migrations/
│   └── 001_initial.sql       # ClickHouse CREATE TABLE statements
├── agents/
│   ├── base.py               # BaseAgent tick pattern
│   ├── ingest.py             # Discover + scrape + dedup (Nimble API)
│   ├── parser.py             # Raw HTML → structured events (Claude Haiku)
│   ├── recommender.py        # Two-stage score + rerank (Claude Sonnet)
│   └── delivery.py           # Calendar invite + RSVP poll + feedback
├── services/
│   ├── nimble.py             # Nimble API wrapper
│   ├── llm.py                # Anthropic client (parse + rerank)
│   ├── calendar.py           # Google Calendar API wrapper
│   └── preferences.py        # Preference weight CRUD + scoring
├── scheduler.py              # APScheduler entry point
├── api.py                    # FastAPI feedback endpoint
└── scripts/
    ├── run_agent.py           # Run one agent tick manually
    └── seed_preferences.py    # Bootstrap user preferences
```

## Tech Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Web scraping | [Nimble API](https://nimbleway.com) | Extract event data from secretnyc, luma |
| Storage | [ClickHouse](https://clickhouse.com) | All persistent state — events, preferences, recommendations |
| Event parsing | Claude Haiku | Structured extraction from raw HTML |
| Recommendation | Claude Sonnet | Rerank candidates with reasoning |
| Calendar | Google Calendar API | Create invites, detect accept/reject |
| Feedback | FastAPI | Accept/reject webhook endpoint |
| Scheduling | APScheduler | Run agents on cadences |
| Config | Pydantic Settings | Type-safe env var loading |

## Setup

```bash
# Install dependencies
uv sync

# Copy and fill in API keys
cp .env.example .env
# Edit .env with your keys

# Run ClickHouse migrations
uv run run-agent ingest --migrate

# Seed your preferences
uv run seed-prefs

# Start the agent scheduler
uv run event-scheduler

# In another terminal, start the feedback API
uv run event-api
```

## Running a Single Agent

```bash
uv run run-agent ingest       # Discover + scrape events
uv run run-agent parser       # Parse raw pages into structured events
uv run run-agent recommender  # Score and recommend events
uv run run-agent delivery     # Send calendar invites + poll RSVPs
```

## Cost Estimate (daily)

| Component | Cost |
|-----------|------|
| Nimble API | ~$5-15 (dominant) |
| Claude Haiku (parsing) | ~$0.05 |
| Claude Sonnet (rerank) | ~$0.10 |
| ClickHouse | Free tier / self-hosted |
| Google Calendar API | Free |
| **Total** | **~$5-15/day** |
