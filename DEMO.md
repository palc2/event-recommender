# Demo script — Event Recommender

~5 minutes, live. Designed for a colleague-fluent audience (NYC + fintech +
loose engineering background). Spoken bits in normal text, **stage directions
in bold**, callouts in *italics*.

---

## 0. Before you start (30 sec, not on screen)

**Open three windows ahead of time:**

1. Terminal in the project dir with the venv activated.
2. Browser tab on `http://127.0.0.1:5050` (don't load yet).
3. Browser tab on `https://calendar.google.com` signed in as
   `you@gmail.com` (or whichever Google account has OAuth set up).

**In the terminal, start the server:**

```
event-web
```

Then alt-tab back so you're not staring at logs when you start.

---

## 1. The hook (45 sec)

> "Every Sunday I open Instagram and lose 20 minutes to 'NYC weekend roundup'
> posts. Half of them are stuff I'd never go to. So I built this — you tell it
> who you are and what you're in the mood for, and four little agents go
> scrape NYC event sites, score everything against your taste, and quietly
> put the keepers on your calendar."

**Switch to the browser tab on `http://127.0.0.1:5050`.**

> "Two questions, one button."

**Point at the headline, then the two inputs.**

---

## 2. The form (45 sec)

> "I tell it I'm `paloma`, the calendar's `you@gmail.com`, and what
> I want this week — say, family-friendly stuff in Brooklyn for the long
> weekend."

**Type as you talk:**

- Name/tag: `paloma`
- Calendar email: `you@gmail.com`
- Vibe: `kids-friendly things in Brooklyn this weekend, free or cheap`

> "The free text part matters. That's the ask the LLM gives the most weight
> to — it overrides what it remembers about my long-term preferences."

**Click "Find me something".**

> "It runs the whole pipeline immediately, then every 2 minutes after that."

---

## 3. The pipeline (90 sec, while it ticks)

**Scroll down to "Recent runs" while you talk.**

> "Four agents, in order:"

> "**Ingest** — uses Nimble to scrape secretnyc and lu.ma. Returns URLs +
> raw HTML."

> "**Parser** — sends each page to DeepSeek with a prompt that says 'extract
> events as JSON.' Title, time, venue, category, price."

> "**Recommender** — does two passes. First a cheap Python score that
> filters 200 events down to the top 20. Then DeepSeek reranks the 20 with
> reasoning, using my current ask as the dominant signal."

> "**Delivery** — takes anything scoring above the threshold and creates
> a Google Calendar invite. Marks it `delivered` in the DB."

**Point at the row that just filled in.**

> "You can see all four ran. The number in parens is how many items each
> agent actually processed. Recommender saw 53 candidate events and
> dropped, say, 4 onto my calendar."

*If runs are still blank, just say "give it 30 seconds" and move to point 5.*

---

## 4. The picks (60 sec)

**Scroll up to "Your picks".**

> "Here's what the recommender chose. Score in coral on the right —
> percentage match. Pill says whether it's pending, delivered, or rejected."

**Hover over one card.**

> "Each one shows where, when, category, price. The italicized bit is the
> reasoning — that's the recommender literally explaining why it picked
> this for me. Click the title and you go to the source page."

**Click the "Add to Google Calendar" button on one of them.**

> "This is the manual add — it opens Google's pre-filled add-event form,
> no OAuth dance. The auto-deliver button has already put the high-scoring
> ones on my calendar via the API; this is for ad-hoc 'I want this one
> specifically' moves."

---

## 5. The calendar (30 sec)

**Switch to the Google Calendar tab. Refresh.**

> "And here they are."

**Point at the events with the `[Recommended]` prefix on this week.**

> "Title prefix is `[Recommended]` so you can filter them. Description has
> the score and reasoning. If you don't want it, just delete it from the
> calendar — the delivery agent polls every loop, notices the cancellation,
> and writes that back as a preference update: `category -0.15`, `time
> -0.15`, `location -0.15`. Asymmetric — rejections hit harder than
> accepts because a bad recommendation is more annoying than a missed one."

---

## 6. The page auto-updates (15 sec)

**Switch back to the webapp tab. Don't reload.**

> "Page polls every 5 seconds — no manual refresh, you can leave it open
> and watch picks land while you work."

**Point at the live pulsing pill in "What's happening".**

---

## 7. Architecture in one slide (45 sec)

> "Stack:"

- **Nimble** for scraping (handles bot protection, JS rendering)
- **ClickHouse** for everything stateful — events, recs, preferences,
  observability. Single source of truth, agents are stateless between ticks.
- **DeepSeek** via an OpenAI-compatible gateway for the LLM parts (parse +
  rerank). Swap in any model behind that interface.
- **Google Calendar API** for delivery.
- **Flask + APScheduler** for the UI and the 2-minute loop.

> "Each agent loads its work queue from ClickHouse, does one batch, writes
> results back, exits. No long-running processes, no in-memory state, no
> coordination between agents."

> "Cost is roughly **$5–15/day**, dominated by Nimble. Claude/DeepSeek are
> pennies because the recommender pre-filters with cheap math before
> spending tokens."

---

## 8. Close (15 sec)

> "Future stuff: more sources, better preference modeling from accept/reject
> history, group mode where two calendars converge on the events both
> people would actually go to. Questions?"

---

## Cheat sheet for Q&A

| Likely question | One-liner answer |
|---|---|
| "Why not just have the LLM scrape directly?" | LLMs hallucinate URLs and can't bypass anti-bot. Nimble handles the scraping problem cleanly; LLM only sees text. |
| "Why ClickHouse?" | Cheap, fast, append-mostly workload. Could be Postgres, doesn't matter much at this scale. |
| "Why DeepSeek and not Claude or GPT?" | OpenAI-compatible endpoint, plug-and-play. The provider is one env var. |
| "What about privacy of the calendar data?" | Only metadata you provide hits external APIs (Nimble + DeepSeek). Calendar API is direct Google → you. |
| "How does it handle dupes?" | Two layers: events table dedups by `event_id` (ReplacingMergeTree); picks query collapses by `(title, start_time)` so re-parses of the same event don't double up. |
| "TLS-MITM on the corporate network?" | Yeah — `event_scheduler/__init__.py` turns off httpx verify and uses the OS trust store. Works behind corporate proxies (Zscaler / Netskope / similar) out of the box. |

---

## Fallback if the live demo breaks

- **No picks appear in 2 minutes:**
  Show the screenshot of a previous run (keep one handy). Say "ran out of new
  events in the catalogue — let me show you yesterday's picks instead."
- **`event-web` won't start:**
  Demo the architecture diagram in the README and walk through one of the
  agent files (`recommender.py` is the most interesting). Skip the live UI.
- **Calendar doesn't show events:**
  Show the ClickHouse query in `_recent_picks` — the data's in the DB, the
  Google API was hit (proven by `calendar_event_id` being populated).
  "The Calendar API confirms with the IDs you see here; if you don't see them
  in your client, that's a client-side filter issue, not a delivery issue."
