# AGENTS.md — orientation for AI assistants

Read this before touching code. README.md is the human-facing pitch; this file is the
operational map.

## What this project is

Agentic NYC event recommender. Four short-lived "tick" agents form a pipeline that
scrapes events, parses them, scores them per user, and writes calendar invites.
ClickHouse is the single source of truth — agents are stateless between ticks.

```
ingest  →  parser  →  recommender  →  delivery
(global)  (global)   (per-user)      (per-user, writes Google Calendar)
```

## Layout

```
src/event_scheduler/
├── config.py              # Pydantic Settings — reads .env
├── db.py                  # ClickHouse client + run_migrations()
├── models.py              # Pydantic data models (Event, Recommendation, AgentRun, …)
├── migrations/001_initial.sql
├── agents/
│   ├── base.py            # BaseAgent.tick() — wraps _execute() with agent_runs logging
│   ├── ingest.py          # Nimble discover + scrape, fills discovered_urls / raw_pages
│   ├── parser.py          # HTML → structured events via LLM
│   ├── recommender.py     # quick_score + LLM rerank, per user_id (ctor arg)
│   └── delivery.py        # Calendar invite + RSVP poll, per user_id + calendar_id
├── services/
│   ├── nimble.py          # Nimble API wrapper
│   ├── llm.py             # OpenAI-compatible client (parse + rerank)
│   ├── calendar.py        # Google Calendar — create_event / get_event_status
│   │                      #   both accept calendar_id="primary" by default
│   └── preferences.py     # Preference weights CRUD + scoring + feedback updates
├── scheduler.py           # APScheduler BlockingScheduler — original cron-style entry
├── api.py                 # FastAPI feedback endpoint (/respond, /health) — port 8000
├── webapp.py              # Flask control panel — port 5050, BackgroundScheduler @ 2 min
└── scripts/
    ├── run_agent.py        # CLI: `uv run run-agent <ingest|parser|recommender|delivery>`
    └── seed_preferences.py # CLI: `uv run seed-prefs` — interactive bootstrap
```

## Entrypoints

Defined in `pyproject.toml` `[project.scripts]`:

| Command | Purpose |
| --- | --- |
| `uv run event-web` | Flask control panel (this is the user-facing one). |
| `uv run event-scheduler` | Original cron-style blocking scheduler (ingest 1h, others 15m). |
| `uv run event-api` | FastAPI feedback endpoint on :8000. |
| `uv run run-agent <name>` | Run one tick of one agent manually. `--migrate` first runs migrations. |
| `uv run seed-prefs` | Interactive CLI to set initial preference weights. |
| `uv run oauth-setup` | One-time: capture Google OAuth refresh token, write it to `.env`. |

The Flask app and the BlockingScheduler are alternative drivers of the same agents —
do not run both at once or you will double up calendar invites.

## User model

`user_id` is the partition key for everything personalized — preferences,
recommendations. `DEFAULT_USER_ID = "default"` still exists in the agents as
a fallback default, but real callers pass `user_id` (and `calendar_id` for
delivery) as constructor args.

**There is no hardcoded user catalog.** The Flask webapp asks the user for
their name/tag and calendar email on the form; whatever they type becomes
`user_id` and `calendar_id`. The CLI driver (`event-scheduler`) still uses
`user_id="default"`. To seed preferences for a non-default user, run
`seed-prefs` after editing it (currently it's hardcoded to "default" — a
known limitation).

`calendar_id` is passed straight to the Google Calendar API:
- `"primary"` → whatever account owns `GOOGLE_REFRESH_TOKEN`.
- An email → that calendar, only if the OAuth user has share access.

## Environment

`.env` at repo root (Pydantic Settings auto-loads it). Keys consumed (see `config.py`):

```
NIMBLE_API_KEY, NIMBLE_BASE_URL
CLICKHOUSE_HOST/PORT/USER/PASSWORD/DATABASE (TLS, port 8443)
LLM_API_KEY, LLM_BASE_URL, LLM_PARSE_MODEL, LLM_RERANK_MODEL  (OpenAI-compatible)
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
FEEDBACK_API_BASE_URL  (used by delivery to build reject/accept links in invite body)
```

Tuning knobs (also in `config.py`, all defaulted):
`INGEST_BATCH_SIZE`, `PARSER_BATCH_SIZE`, `RECOMMENDER_TOP_K`, `RECOMMENDATION_THRESHOLD`.

## Tick pattern — important invariants

`BaseAgent.tick()` (`agents/base.py`):
1. Acquires a ClickHouse client.
2. Calls subclass `_execute(client) -> (processed, failed)`.
3. Logs an `agent_runs` row — even on exception.

Every agent _must_:
- Be safe to invoke any number of times (idempotency-ish — dedup happens via
  url_hash / event content_hash / `recommendations.event_id` uniqueness).
- Not keep state on `self` between ticks. Constructor args (`user_id`, `calendar_id`)
  are configuration, not state — they don't change mid-run.
- Read its work queue from ClickHouse, not from in-memory queues.

## Feedback / preference updates

`services/preferences.py::update_from_feedback` is the only place preference weights
mutate after seeding. Two trigger points:
- `api.py` `/respond?rec=...&action=accept|reject` — link in calendar event body.
- `agents/delivery.py::_poll_rsvps` — detects deleted calendar events as implicit rejects.

Asymmetric deltas: accept = +0.10, reject = −0.15. Weights clamp to [-1, 1]
(see `upsert_weight`).

## Gotchas

- **Never use `FINAL` in ClickHouse queries.** Cloud (the only deployment we
  target) uses `SharedMergeTree`, which rejects `FINAL` with `ILLEGAL_FINAL`.
  For `ReplacingMergeTree` tables (`events`, `user_preferences`,
  `discovered_urls`), dedup manually with a subquery:
  `FROM (SELECT * FROM <table> ORDER BY <version> DESC LIMIT 1 BY <order key>)`.
  See `recommender.py::_fetch_upcoming_events` for the canonical example.
  `recommendations` is plain `MergeTree`, so no dedup needed there.
- Calendar invites in the original code wrote to `"primary"`. After parameterization,
  always pass `calendar_id` from the agent's constructor — don't reintroduce a hardcoded
  `"primary"` literal in `delivery.py`.
- `webapp.py` uses a module-level `BackgroundScheduler`. Flask's `use_reloader=False`
  is set so the scheduler isn't created twice; do not enable the reloader.
- `_run_in_progress` lock in `webapp.py` prevents the periodic timer from racing with a
  manual click — keep it if you refactor.
- `BaseAgent.tick()` **swallows exceptions internally** (writes them to the
  `agent_runs` table + sets `self.last_run.error_message`) and never re-raises.
  Code that calls `agent.tick()` and wants to know if it actually worked must
  read `agent.last_run.error_message` / `items_processed` — not just "did tick()
  raise". `webapp.py::_run_pipeline` already does this; new callers should too.
- LLM is **DeepSeek via an OpenAI-compatible gateway** (`LLM_BASE_URL` →
  `opencode.ai/zen/v1`, models `deepseek-v4-flash-free`). `services/llm.py` is the
  only place that talks to it — uses plain `httpx`, not the Anthropic SDK.
- The webapp polls `/status.json` and `/picks.json` every 5s from the browser.
  Those endpoints must stay cheap — they hit ClickHouse on every poll. If you
  add expensive joins, cache or back off when nothing has changed.

## When making changes

- Per-user behavior → touch `RecommenderAgent`, `DeliveryAgent`, and `USERS` in
  `webapp.py` (and `services/preferences.py` if scoring logic changes).
- New data field on events → migration in `migrations/`, update `models.py::Event`,
  update parser prompt + SELECT lists in `recommender.py` / `delivery.py` / `api.py`.
- New agent → add file under `agents/`, register in `agents/__init__.py`, add to
  `AGENTS` dict in `scripts/run_agent.py`, and add a job in whichever scheduler
  (`scheduler.py` and/or `webapp.py::_run_pipeline`).
