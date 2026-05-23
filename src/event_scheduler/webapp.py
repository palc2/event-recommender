"""Flask front door for the agentic event recommender.

Asks the user two things — who they are (any tag + their calendar email) and
what they want to see — then kicks off the full
ingest → parser → recommender → delivery pipeline, immediately and then
every 2 minutes. The page auto-refreshes its status + recent picks while
the loop is running, so you can leave it open and watch invites land.

Run with:  uv run event-web   (or just `event-web` inside the activated venv)
"""
from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlencode

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

from event_scheduler.agents import (
    DeliveryAgent,
    IngestAgent,
    ParserAgent,
    RecommenderAgent,
)
from event_scheduler.db import get_client, run_migrations

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

LOOP_INTERVAL_MINUTES = 2


@dataclass
class RunRecord:
    started_at: datetime
    finished_at: datetime | None = None
    user_id: str = ""
    email: str = ""
    user_query: str = ""
    per_agent: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds") if self.finished_at else None,
            "user_id": self.user_id,
            "email": self.email,
            "user_query": self.user_query,
            "per_agent": self.per_agent,
            "error": self.error,
        }


# Module-level state — single Flask process, so plain dicts/locks are fine.
# Session remembers the last identity entered so a refresh doesn't wipe it.
_scheduler: BackgroundScheduler | None = None
_session: dict = {"user_id": "", "email": "", "user_query": ""}
_runs: list[RunRecord] = []
_runs_lock = threading.Lock()
_run_in_progress = threading.Lock()


def _run_pipeline(user_id: str, email: str, user_query: str) -> RunRecord:
    """Run the full agent pipeline once.

    ingest → parser are global (they fill shared event tables);
    recommender → delivery are scoped to user_id. recommender takes the
    free-text `user_query` so the LLM rerank prioritizes it. delivery
    writes invites to `email` as the Google Calendar id (falls back to
    "primary" — the OAuth user's own calendar — when email is blank).
    """
    calendar_id = email.strip() or "primary"
    rec = RunRecord(
        started_at=datetime.now(),
        user_id=user_id,
        email=email,
        user_query=user_query,
    )

    if not _run_in_progress.acquire(blocking=False):
        rec.error = "another run already in progress; skipped"
        rec.finished_at = datetime.now()
        with _runs_lock:
            _runs.append(rec)
            del _runs[:-50]
        return rec

    try:
        steps = [
            ("ingest", IngestAgent()),
            ("parser", ParserAgent()),
            ("recommender", RecommenderAgent(user_id=user_id, user_query=user_query or None)),
            ("delivery", DeliveryAgent(user_id=user_id, calendar_id=calendar_id)),
        ]
        for name, agent in steps:
            try:
                agent.tick()
                # BaseAgent.tick() swallows internal exceptions and records them
                # on self.last_run. Read that to report the real outcome.
                lr = getattr(agent, "last_run", None)
                if lr is None:
                    rec.per_agent[name] = "ok"
                elif lr.error_message:
                    # Last non-empty line of the traceback is usually the
                    # exception type + message — that's what's actionable.
                    last = next(
                        (ln for ln in reversed(lr.error_message.splitlines()) if ln.strip()),
                        "unknown error",
                    )
                    rec.per_agent[name] = f"err: {last[:140]}"
                else:
                    rec.per_agent[name] = f"ok ({lr.items_processed})"
            except Exception as exc:
                rec.per_agent[name] = f"err: {exc}"
                logger.exception("agent %s failed", name)
    except Exception:
        rec.error = traceback.format_exc()
        logger.exception("pipeline failed")
    finally:
        rec.finished_at = datetime.now()
        with _runs_lock:
            _runs.append(rec)
            del _runs[:-50]
        _run_in_progress.release()
    return rec


def _start_loop(user_id: str, email: str, user_query: str) -> None:
    global _scheduler
    _session["user_id"] = user_id
    _session["email"] = email
    _session["user_query"] = user_query
    if _scheduler and _scheduler.running:
        _scheduler.remove_all_jobs()
    else:
        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.start()
    _scheduler.add_job(
        _run_pipeline,
        "interval",
        minutes=LOOP_INTERVAL_MINUTES,
        kwargs={"user_id": user_id, "email": email, "user_query": user_query},
        id="pipeline",
        next_run_time=datetime.now(),  # fire immediately, then every N minutes
        max_instances=1,
        coalesce=True,
    )


def _stop_loop() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.remove_all_jobs()
        _scheduler.shutdown(wait=False)
    _scheduler = None


def _loop_status() -> dict:
    job = _scheduler.get_job("pipeline") if _scheduler else None
    return {
        "running": bool(job),
        "user_id": _session["user_id"],
        "email": _session["email"],
        "user_query": _session["user_query"],
        "next_run": job.next_run_time.isoformat(timespec="seconds") if job and job.next_run_time else None,
        "interval_minutes": LOOP_INTERVAL_MINUTES,
    }


def _recent_picks(user_id: str, limit: int = 20) -> list[dict]:
    """Pull the latest recommendations + their event metadata for a user.

    Used by /picks.json so the UI can show the actual events the agent picked,
    not just per-agent ok/err counts. Returns most-recent first.
    """
    if not user_id:
        return []
    try:
        client = get_client()
    except Exception:
        logger.exception("could not connect to ClickHouse for /picks.json")
        return []
    try:
        # Outer LIMIT 1 BY (title, start_time) dedupes events that ended up
        # with multiple rec_ids (or multiple event_id's parsed from the same
        # source). Inner subqueries dedup ReplacingMergeTree tables manually
        # since SharedMergeTree on Cloud rejects FINAL.
        rows = client.query(
            "SELECT rec_id, score, status, reasoning, "
            "created_at, delivered_at, calendar_event_id, "
            "title, description, start_time, end_time, "
            "location_name, location_address, "
            "category, price_cents, source_url "
            "FROM ("
            "  SELECT r.rec_id, r.score, r.status, r.reasoning, "
            "         r.created_at, r.delivered_at, r.calendar_event_id, "
            "         e.title, e.description, e.start_time, e.end_time, "
            "         e.location_name, e.location_address, "
            "         e.category, e.price_cents, e.source_url "
            "  FROM recommendations r "
            "  JOIN (SELECT * FROM events ORDER BY parsed_at DESC "
            "        LIMIT 1 BY event_id) e "
            "    ON r.event_id = e.event_id "
            "  WHERE r.user_id = {uid:String} "
            "  ORDER BY r.created_at DESC, r.score DESC"
            ") "
            "LIMIT 1 BY title, start_time "
            "LIMIT {lim:UInt32}",
            parameters={"uid": user_id, "lim": limit},
        )
    except Exception:
        logger.exception("ClickHouse query for picks failed")
        return []

    picks = []
    for row in rows.result_rows:
        (rec_id, score, status, reasoning, created_at, delivered_at,
         cal_id, title, desc, start_time, end_time,
         loc_name, loc_addr, category, price_cents, source_url) = row
        picks.append({
            "rec_id": str(rec_id),
            "score": float(score),
            "status": status,
            "reasoning": reasoning,
            "created_at": created_at.isoformat(timespec="minutes") if isinstance(created_at, datetime) else str(created_at),
            "delivered_at": delivered_at.isoformat(timespec="minutes") if isinstance(delivered_at, datetime) else None,
            "calendar_event_id": cal_id,
            "title": title,
            "description": (desc or "")[:240],
            "start_time": start_time.isoformat(timespec="minutes") if isinstance(start_time, datetime) else str(start_time),
            "end_time": end_time.isoformat(timespec="minutes") if isinstance(end_time, datetime) else None,
            "location_name": loc_name,
            "location_address": loc_addr,
            "category": category,
            "price_cents": price_cents,
            "source_url": source_url,
            "gcal_url": _gcal_render_url(
                title=title, description=desc, reasoning=reasoning,
                start_time=start_time, end_time=end_time,
                location_name=loc_name, location_address=loc_addr,
                source_url=source_url,
            ),
        })
    return picks


def _gcal_render_url(*, title: str, description: str | None, reasoning: str | None,
                    start_time, end_time, location_name: str, location_address: str,
                    source_url: str | None) -> str:
    """Build a Google Calendar 'render' URL that opens a pre-filled event
    creation form in the user's browser. No OAuth required — anyone signed
    into a Google account can hit Save and the event lands on their calendar.

    Format: https://www.google.com/calendar/render?action=TEMPLATE&...
    Dates use YYYYMMDDTHHMMSS (floating local time — Google interprets in
    the signed-in user's timezone).
    """
    def fmt(dt) -> str:
        if not isinstance(dt, datetime):
            return ""
        return dt.strftime("%Y%m%dT%H%M%S")

    start = start_time if isinstance(start_time, datetime) else None
    end = end_time if isinstance(end_time, datetime) else None
    if start and not end:
        end = start + timedelta(hours=2)  # sensible default if event has no end

    dates_str = ""
    if start and end:
        dates_str = f"{fmt(start)}/{fmt(end)}"

    details_lines: list[str] = []
    if description:
        details_lines.append(description.strip())
    if reasoning:
        details_lines.append("")
        details_lines.append(f"Why: {reasoning.strip()}")
    if source_url:
        details_lines.append("")
        details_lines.append(f"Source: {source_url}")

    location = ", ".join(filter(None, [location_name, location_address]))

    params = {
        "action": "TEMPLATE",
        "text": title or "Event",
        "details": "\n".join(details_lines),
        "location": location,
    }
    if dates_str:
        params["dates"] = dates_str
    return "https://www.google.com/calendar/render?" + urlencode(params, safe=":/")


# ----------------------------------------------------------------------------- Flask

app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>What's good in NYC</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --ink: #1a1a2e;
    --paper: #fdf6ec;
    --card: #ffffff;
    --rule: #ecd9c2;
    --mute: #7a6f63;
    --coral: #ef5a3c;
    --coral-dark: #d3431f;
    --mint: #cfeede;
    --mint-ink: #1b6b48;
    --blush: #fde2dc;
    --blush-ink: #8a3a2a;
    --gold: #f0b429;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--paper); color: var(--ink); }
  body {
    font-family: 'Inter', -apple-system, system-ui, sans-serif;
    line-height: 1.55;
    min-height: 100vh;
    background:
      radial-gradient(1200px 400px at 80% -100px, #ffe2b8 0%, transparent 60%),
      radial-gradient(900px 300px at -10% 10%, #ffd1c4 0%, transparent 55%),
      var(--paper);
  }
  .wrap { max-width: 820px; margin: 0 auto; padding: 2.2em 1.2em 4em; }

  .brand { display: flex; align-items: baseline; gap: 0.6em; margin-bottom: 0.2em; }
  .brand .dot { width: 12px; height: 12px; border-radius: 999px; background: var(--coral); box-shadow: 0 0 0 4px rgba(239,90,60,0.18); }
  .brand .tag { font-size: 0.8em; letter-spacing: 0.14em; text-transform: uppercase; color: var(--mute); font-weight: 600; }

  h1 {
    font-family: 'Fraunces', Georgia, serif;
    font-weight: 700;
    font-size: clamp(2.1rem, 4.4vw, 3rem);
    line-height: 1.05;
    letter-spacing: -0.02em;
    margin: 0.1em 0 0.25em;
  }
  h1 em { font-style: italic; color: var(--coral); }
  .lede { font-size: 1.05rem; color: var(--mute); margin: 0 0 1.8em; max-width: 60ch; }

  .card {
    background: var(--card);
    border: 1px solid var(--rule);
    border-radius: 18px;
    padding: 1.6em 1.6em;
    margin: 1.1em 0;
    box-shadow: 0 1px 0 rgba(0,0,0,0.02), 0 18px 30px -22px rgba(60,30,10,0.18);
  }
  .card.hero { padding: 1.9em 1.8em; }

  label { display: block; font-weight: 600; margin: 1.1em 0 0.35em; font-size: 0.92rem; color: var(--ink); }
  label small { font-weight: 400; color: var(--mute); margin-left: 0.5em; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1.5fr; gap: 0.6em 1em; }
  .grid-2 > div { margin-top: 0; }
  .grid-2 label { margin-top: 0; }
  @media (max-width: 540px) { .grid-2 { grid-template-columns: 1fr; } }

  select, textarea, input[type=text], input[type=email] {
    width: 100%;
    font-size: 1rem;
    font-family: inherit;
    padding: 0.7em 0.9em;
    border: 1.5px solid var(--rule);
    border-radius: 12px;
    background: #fffdf9;
    color: var(--ink);
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  input:focus, textarea:focus {
    outline: none;
    border-color: var(--coral);
    box-shadow: 0 0 0 4px rgba(239,90,60,0.15);
  }
  textarea { min-height: 110px; resize: vertical; line-height: 1.5; }

  .btn-row { display: flex; gap: 0.7em; align-items: center; flex-wrap: wrap; margin-top: 1.2em; }
  button {
    font: inherit; font-weight: 600;
    padding: 0.8em 1.5em; border-radius: 999px; border: none; cursor: pointer;
    transition: transform 0.08s ease, box-shadow 0.15s, background 0.15s;
  }
  button.primary {
    background: linear-gradient(135deg, var(--coral) 0%, #f08055 100%);
    color: white; font-size: 1.02rem; padding: 0.85em 1.8em;
    box-shadow: 0 10px 22px -10px rgba(211,67,31,0.55), inset 0 1px 0 rgba(255,255,255,0.25);
  }
  button.primary:hover { transform: translateY(-1px); box-shadow: 0 14px 26px -10px rgba(211,67,31,0.6); }
  button.ghost {
    background: transparent; color: var(--mute); border: 1.5px solid var(--rule);
  }
  button.ghost:hover { color: var(--blush-ink); border-color: var(--blush-ink); }

  .helper { color: var(--mute); font-size: 0.9rem; }
  .helper b { color: var(--ink); }

  .pill {
    display: inline-flex; align-items: center; gap: 0.4em;
    padding: 0.25em 0.8em; border-radius: 999px;
    font-size: 0.78rem; font-weight: 600; letter-spacing: 0.02em;
  }
  .pill.on { background: var(--mint); color: var(--mint-ink); }
  .pill.on::before { content: ""; width: 7px; height: 7px; border-radius: 999px; background: var(--mint-ink); box-shadow: 0 0 0 4px rgba(27,107,72,0.15); animation: pulse 2s infinite; }
  .pill.off { background: var(--blush); color: var(--blush-ink); }
  .pill.delivered { background: var(--mint); color: var(--mint-ink); }
  .pill.pending { background: #fff3cf; color: #8a6500; }
  .pill.accepted { background: #d6eafe; color: #1f5aa0; }
  .pill.rejected { background: var(--blush); color: var(--blush-ink); }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

  .section-h { display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.8em; gap: 0.7em; flex-wrap: wrap; }
  .section-h h2 { font-family: 'Fraunces', Georgia, serif; font-weight: 500; font-size: 1.3rem; margin: 0; letter-spacing: -0.01em; }

  .status-grid { display: grid; grid-template-columns: auto 1fr; gap: 0.3em 1em; font-size: 0.95rem; }
  .status-grid dt { color: var(--mute); font-weight: 500; }
  .status-grid dd { margin: 0; color: var(--ink); font-weight: 500; }
  .status-grid dd .ask { font-style: italic; color: var(--coral-dark); }

  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  thead th { text-align: left; padding: 0.5em 0.6em; border-bottom: 1.5px solid var(--rule); color: var(--mute); font-weight: 600; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; }
  tbody td { padding: 0.6em 0.6em; border-bottom: 1px solid #f3e7d5; vertical-align: top; }
  tbody tr:last-child td { border-bottom: none; }
  td.ok { color: var(--mint-ink); font-weight: 600; }
  td.err { color: var(--blush-ink); font-weight: 600; }
  td.muted, .muted { color: var(--mute); }
  td.ask { font-style: italic; color: var(--ink); }

  .pick {
    border-top: 1px solid var(--rule);
    padding: 1em 0;
  }
  .pick:first-child { border-top: none; padding-top: 0.3em; }
  .pick-head { display: flex; align-items: baseline; justify-content: space-between; gap: 0.8em; flex-wrap: wrap; }
  .pick-title { font-family: 'Fraunces', Georgia, serif; font-weight: 500; font-size: 1.15rem; letter-spacing: -0.01em; color: var(--ink); }
  .pick-title a { color: inherit; text-decoration: none; border-bottom: 1.5px solid transparent; }
  .pick-title a:hover { border-bottom-color: var(--coral); }
  .pick-meta { font-size: 0.85rem; color: var(--mute); margin: 0.25em 0 0.5em; }
  .pick-meta span + span::before { content: " · "; color: var(--rule); }
  .pick-reasoning { font-size: 0.92rem; color: var(--ink); font-style: italic; margin-top: 0.4em; }
  .pick-reasoning::before { content: "“"; color: var(--coral); font-size: 1.4em; line-height: 0; vertical-align: -0.2em; margin-right: 0.1em; }
  .pick-reasoning::after { content: "”"; color: var(--coral); font-size: 1.4em; line-height: 0; vertical-align: -0.3em; margin-left: 0.1em; }
  .score {
    font-family: 'Fraunces', Georgia, serif; font-weight: 700;
    color: var(--coral-dark); font-size: 1.1rem;
  }
  .pick-actions { margin-top: 0.7em; display: flex; gap: 0.5em; flex-wrap: wrap; }
  .gcal-btn {
    display: inline-flex; align-items: center; gap: 0.4em;
    padding: 0.4em 0.85em;
    border-radius: 999px;
    background: var(--ink); color: white;
    font-size: 0.82rem; font-weight: 600;
    text-decoration: none;
    transition: background 0.15s, transform 0.08s;
  }
  .gcal-btn:hover { background: var(--coral-dark); transform: translateY(-1px); }
  .gcal-btn::before {
    content: ""; width: 14px; height: 14px;
    background: white; border-radius: 3px;
    -webkit-mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M19 4h-1V2h-2v2H8V2H6v2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 16H5V10h14v10zm0-12H5V6h14v2z'/></svg>") center/contain no-repeat;
            mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M19 4h-1V2h-2v2H8V2H6v2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 16H5V10h14v10zm0-12H5V6h14v2z'/></svg>") center/contain no-repeat;
  }

  .empty { color: var(--mute); padding: 0.4em 0; font-style: italic; }
  .footer { margin-top: 2.5em; color: var(--mute); font-size: 0.8rem; text-align: center; }

  .fade-in { animation: fade 0.25s ease; }
  @keyframes fade { from { opacity: 0.4; } to { opacity: 1; } }
</style>
</head><body>

<div class="wrap">

  <div class="brand">
    <span class="dot"></span>
    <span class="tag">NYC · personal event recommender</span>
  </div>

  <h1>What's <em>actually</em> worth&nbsp;going to?</h1>
  <p class="lede">Tell me what you're into. I'll find it in NYC and put it on your calendar.</p>

  <div class="card hero">
    <form method="POST" action="{{ url_for('go') }}">

      <div class="grid-2">
        <div>
          <label for="user_id">I'm <small>(any tag)</small></label>
          <input type="text" name="user_id" id="user_id" required autocomplete="username"
                 placeholder="paloma" value="{{ status.user_id }}">
        </div>
        <div>
          <label for="email">Calendar email <small>(invites land here)</small></label>
          <input type="email" name="email" id="email" required autocomplete="email"
                 placeholder="you@gmail.com" value="{{ status.email }}">
        </div>
      </div>

      <label for="user_query">What's the vibe? <small>(plain English — be specific or be vague, your call)</small></label>
      <textarea name="user_query" id="user_query"
        placeholder="like… 'low-key weeknight art shows in Brooklyn, free or cheap' or 'live jazz this Saturday' or 'fintech / AI infra meetups, beer optional'">{{ status.user_query }}</textarea>

      <div class="btn-row">
        <button class="primary" type="submit">Find me something</button>
        <span class="helper">runs <b>now</b>, then every <b>{{ interval }} min</b> until you stop</span>
      </div>
    </form>

    {% if status.running %}
    <form method="POST" action="{{ url_for('stop') }}" style="margin-top: 1em;">
      <button class="ghost" type="submit">Stop the loop</button>
    </form>
    {% endif %}
  </div>

  <div class="card" id="status-card">
    <div class="section-h">
      <h2>What's happening</h2>
      <span id="loop-pill" class="pill {{ 'on' if status.running else 'off' }}">
        {% if status.running %}live · next at {{ status.next_run or '—' }}{% else %}paused{% endif %}
      </span>
    </div>
    <div id="status-body">
      {% if status.running %}
        <dl class="status-grid">
          <dt>for</dt><dd>{{ status.user_id }}</dd>
          <dt>calendar</dt><dd>{{ status.email or 'primary (OAuth user)' }}</dd>
          <dt>asking for</dt>
          <dd>{% if status.user_query %}<span class="ask">"{{ status.user_query }}"</span>{% else %}<span class="muted">no current ask — using stored preferences only</span>{% endif %}</dd>
        </dl>
      {% else %}
        <div class="empty">nothing running. fill in the form above and tell me what you want.</div>
      {% endif %}
    </div>
  </div>

  <div class="card" id="picks-card">
    <div class="section-h">
      <h2>Your picks</h2>
      <span id="picks-meta" class="helper">events the recommender chose for you</span>
    </div>
    <div id="picks-body">
      {% if picks %}
        {% for p in picks %}
        <div class="pick">
          <div class="pick-head">
            <div class="pick-title">
              {% if p.source_url %}<a href="{{ p.source_url }}" target="_blank" rel="noopener">{{ p.title }}</a>{% else %}{{ p.title }}{% endif %}
            </div>
            <div>
              <span class="score">{{ '%.0f'|format(p.score * 100) }}%</span>
              <span class="pill {{ p.status }}">{{ p.status }}</span>
            </div>
          </div>
          <div class="pick-meta">
            <span>{{ p.start_time[:16].replace('T', ' ') }}</span>
            <span>{{ p.location_name }}{% if p.location_address %} — {{ p.location_address }}{% endif %}</span>
            <span>{{ p.category }}</span>
            {% if p.price_cents %}<span>${{ '%.0f'|format(p.price_cents / 100) }}</span>{% else %}<span>free</span>{% endif %}
          </div>
          {% if p.reasoning %}<div class="pick-reasoning">{{ p.reasoning }}</div>{% endif %}
          {% if p.gcal_url %}
          <div class="pick-actions">
            <a class="gcal-btn" href="{{ p.gcal_url }}" target="_blank" rel="noopener">Add to Google Calendar</a>
          </div>
          {% endif %}
        </div>
        {% endfor %}
      {% else %}
        <div class="empty">no picks yet — once the recommender finishes a tick they'll show up here, and on your calendar.</div>
      {% endif %}
    </div>
  </div>

  <div class="card">
    <div class="section-h">
      <h2>Recent runs</h2>
      <span class="helper">agent ticks (last 50 kept in memory)</span>
    </div>
    <div id="runs-body">
      {% if runs %}
        <table>
          <thead>
            <tr><th>started</th><th>for</th><th>asking for</th><th>ingest</th><th>parser</th><th>recommender</th><th>delivery</th><th>error</th></tr>
          </thead>
          <tbody>
            {% for r in runs %}
            <tr>
              <td class="muted">{{ r.started_at[11:] }}</td>
              <td>{{ r.user_id }}</td>
              <td class="ask">{{ (r.user_query[:48] + '…') if r.user_query and r.user_query|length > 48 else (r.user_query or '—') }}</td>
              {% for k in ['ingest','parser','recommender','delivery'] %}
                {% set v = r.per_agent.get(k, '—') %}
                <td class="{{ 'err' if v.startswith('err') else ('ok' if v.startswith('ok') else 'muted') }}">{{ v }}</td>
              {% endfor %}
              <td class="err">{{ (r.error[:50] + '…') if r.error and r.error|length > 50 else (r.error or '') }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <div class="empty">no runs yet — your history shows up here once you hit Find me something.</div>
      {% endif %}
    </div>
  </div>

  <div class="footer">NYC, on demand. four little agents doing the legwork while you do other things.</div>

</div>

<script>
(function () {
  // Live updates while the loop is running. We only repaint the dynamic cards
  // (status, picks, runs) — never the form, so the user can keep typing.
  const POLL_MS = 5000;
  const $ = (id) => document.getElementById(id);

  const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\\"": "&quot;", "'": "&#39;"
  }[c]));

  function renderStatus(loop) {
    const pill = $("loop-pill");
    if (!pill) return;
    pill.className = "pill " + (loop.running ? "on" : "off");
    pill.textContent = loop.running
      ? "live · next at " + (loop.next_run || "—")
      : "paused";
    const body = $("status-body");
    if (!body) return;
    if (loop.running) {
      const ask = loop.user_query
        ? `<span class="ask">"${escapeHtml(loop.user_query)}"</span>`
        : '<span class="muted">no current ask — using stored preferences only</span>';
      body.innerHTML = `
        <dl class="status-grid">
          <dt>for</dt><dd>${escapeHtml(loop.user_id)}</dd>
          <dt>calendar</dt><dd>${escapeHtml(loop.email || "primary (OAuth user)")}</dd>
          <dt>asking for</dt><dd>${ask}</dd>
        </dl>`;
    } else {
      body.innerHTML = '<div class="empty">nothing running. fill in the form above and tell me what you want.</div>';
    }
  }

  function renderPicks(picks) {
    const body = $("picks-body");
    if (!body) return;
    if (!picks.length) {
      body.innerHTML = '<div class="empty">no picks yet — once the recommender finishes a tick they\\'ll show up here, and on your calendar.</div>';
      $("picks-meta").textContent = "events the recommender chose for you";
      return;
    }
    $("picks-meta").textContent = picks.length + " event" + (picks.length === 1 ? "" : "s");
    body.innerHTML = picks.map((p) => {
      const titleHtml = p.source_url
        ? `<a href="${escapeHtml(p.source_url)}" target="_blank" rel="noopener">${escapeHtml(p.title)}</a>`
        : escapeHtml(p.title);
      const when = (p.start_time || "").slice(0, 16).replace("T", " ");
      const where = escapeHtml(p.location_name || "")
        + (p.location_address ? " — " + escapeHtml(p.location_address) : "");
      const price = p.price_cents
        ? "$" + Math.round(p.price_cents / 100)
        : "free";
      const reasoning = p.reasoning
        ? `<div class="pick-reasoning">${escapeHtml(p.reasoning)}</div>`
        : "";
      const gcalBtn = p.gcal_url
        ? `<div class="pick-actions"><a class="gcal-btn" href="${escapeHtml(p.gcal_url)}" target="_blank" rel="noopener">Add to Google Calendar</a></div>`
        : "";
      return `
        <div class="pick fade-in">
          <div class="pick-head">
            <div class="pick-title">${titleHtml}</div>
            <div>
              <span class="score">${Math.round(p.score * 100)}%</span>
              <span class="pill ${escapeHtml(p.status)}">${escapeHtml(p.status)}</span>
            </div>
          </div>
          <div class="pick-meta">
            <span>${escapeHtml(when)}</span>
            <span>${where}</span>
            <span>${escapeHtml(p.category || "")}</span>
            <span>${price}</span>
          </div>
          ${reasoning}
          ${gcalBtn}
        </div>`;
    }).join("");
  }

  function renderRuns(runs) {
    const body = $("runs-body");
    if (!body) return;
    if (!runs.length) {
      body.innerHTML = '<div class="empty">no runs yet — your history shows up here once you hit Find me something.</div>';
      return;
    }
    const agentKeys = ["ingest", "parser", "recommender", "delivery"];
    const rows = runs.map((r) => {
      const cells = agentKeys.map((k) => {
        const v = r.per_agent[k] || "—";
        const cls = v.startsWith("err") ? "err" : (v.startsWith("ok") ? "ok" : "muted");
        return `<td class="${cls}">${escapeHtml(v)}</td>`;
      }).join("");
      const ask = r.user_query && r.user_query.length > 48
        ? r.user_query.slice(0, 48) + "…"
        : (r.user_query || "—");
      const err = r.error && r.error.length > 50 ? r.error.slice(0, 50) + "…" : (r.error || "");
      return `
        <tr>
          <td class="muted">${escapeHtml(r.started_at.slice(11))}</td>
          <td>${escapeHtml(r.user_id)}</td>
          <td class="ask">${escapeHtml(ask)}</td>
          ${cells}
          <td class="err">${escapeHtml(err)}</td>
        </tr>`;
    }).join("");
    body.innerHTML = `
      <table>
        <thead>
          <tr><th>started</th><th>for</th><th>asking for</th><th>ingest</th><th>parser</th><th>recommender</th><th>delivery</th><th>error</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  async function refresh() {
    try {
      const [statusResp, picksResp] = await Promise.all([
        fetch("{{ url_for('status_json') }}", { cache: "no-store" }),
        fetch("{{ url_for('picks_json') }}", { cache: "no-store" }),
      ]);
      if (statusResp.ok) {
        const data = await statusResp.json();
        renderStatus(data.loop);
        renderRuns(data.runs);
      }
      if (picksResp.ok) {
        const data = await picksResp.json();
        renderPicks(data.picks);
      }
    } catch (e) {
      // network blip — try again next tick
    }
  }

  let timer = setInterval(refresh, POLL_MS);
  // immediate refresh so the picks card populates without waiting a full tick
  setTimeout(refresh, 800);

  // pause polling when tab is hidden, resume when it comes back
  document.addEventListener("visibilitychange", () => {
    clearInterval(timer);
    if (!document.hidden) {
      timer = setInterval(refresh, POLL_MS);
      refresh();
    }
  });
})();
</script>

</body></html>"""


@app.route("/")
def index():
    with _runs_lock:
        runs = [r.to_dict() for r in reversed(_runs[-15:])]
    picks = _recent_picks(_session["user_id"]) if _session["user_id"] else []
    return render_template_string(
        PAGE,
        status=_loop_status(),
        runs=runs,
        picks=picks,
        interval=LOOP_INTERVAL_MINUTES,
        url_for=url_for,
    )


@app.post("/go")
def go():
    """Main entry: capture identity + ask, kick off the pipeline loop."""
    user_id = (request.form.get("user_id") or "").strip()
    email = (request.form.get("email") or "").strip()
    user_query = (request.form.get("user_query") or "").strip()
    if not user_id or not email:
        # Mandatory fields — if missing just bounce back; HTML `required`
        # should normally prevent this.
        _session["user_id"] = user_id
        _session["email"] = email
        _session["user_query"] = user_query
        return redirect(url_for("index"))
    _start_loop(user_id, email, user_query)
    return redirect(url_for("index"))


@app.post("/stop")
def stop():
    _stop_loop()
    return redirect(url_for("index"))


@app.get("/status.json")
def status_json():
    with _runs_lock:
        runs = [r.to_dict() for r in reversed(_runs[-15:])]
    return jsonify(loop=_loop_status(), runs=runs)


@app.get("/picks.json")
def picks_json():
    return jsonify(picks=_recent_picks(_session["user_id"]))


def main():
    logger.info("Running ClickHouse migrations...")
    try:
        run_migrations()
        logger.info("Migrations complete.")
    except Exception:
        logger.exception("Migrations failed — webapp will still start; fix DB connection and retry.")
    # use_reloader=False so the BackgroundScheduler isn't created twice.
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
