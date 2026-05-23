import json
import re
from html.parser import HTMLParser

import httpx

from event_scheduler.config import settings
from event_scheduler.models import EventCategory, ParsedEventData, ScoredEvent


class _HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__()
        self._pieces: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True
        elif tag in ("br", "p", "div", "h1", "h2", "h3", "h4", "li", "tr"):
            self._pieces.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._pieces.append(data)

    def get_text(self) -> str:
        raw = "".join(self._pieces)
        lines = [line.strip() for line in raw.splitlines()]
        lines = [l for l in lines if l]
        return "\n".join(lines)


def html_to_text(html: str) -> str:
    parser = _HTMLToText()
    parser.feed(html)
    return parser.get_text()


def _chat(model: str, system: str, user_msg: str) -> str:
    """Call an OpenAI-compatible chat completions endpoint."""
    resp = httpx.post(
        f"{settings.llm_base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.2,
            "max_tokens": 16384,
        },
        timeout=180,
    )
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    content = msg.get("content", "") or ""
    if not content and msg.get("reasoning_content"):
        content = msg["reasoning_content"]
    return content


def _extract_json_array(text: str) -> list:
    start = text.find("[")
    end = text.rfind("]") + 1
    if start == -1 or end == 0:
        return []
    candidate = text[start:end]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        candidate = re.sub(r"```json\s*", "", candidate)
        candidate = re.sub(r"```\s*", "", candidate)
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return []


PARSE_SYSTEM = """You extract structured event data from raw web page content.
Return a JSON array of event objects. Each object must have these fields:
- title (string)
- description (string, 1-2 sentences)
- start_time (ISO 8601 with timezone America/New_York, e.g. "2026-05-24T19:00:00-04:00")
- end_time (ISO 8601 or null)
- location_name (string, venue name)
- location_address (string, full address if available, else city/neighborhood)
- category (one of: music, tech, food, art, fitness, nightlife, comedy, networking, family, other)
- tags (array of lowercase keyword strings)
- price_cents (integer, 0 if free, in USD cents)

Today's date is {today}. Resolve relative dates ("this Saturday", "tomorrow") to absolute dates.
If information is missing, use reasonable defaults. Never invent events not present in the content.
Return ONLY the JSON array, no other text."""

RERANK_SYSTEM = """You are a personal event recommender for a user in New York City.
Given the user's preference profile and a list of candidate events, score each event
from 0.0 (terrible match) to 1.0 (perfect match).

Return a JSON array of objects with:
- event_id (string, from the input)
- score (float 0.0 to 1.0)
- reasoning (one sentence explaining why this score)

Consider: category fit, timing preferences, location convenience, price sensitivity,
and keyword matches. Rank honestly — it's better to give low scores than to inflate them.
Return ONLY the JSON array, no other text."""


def parse_events(raw_content: str, *, today: str) -> list[ParsedEventData]:
    text = _chat(
        model=settings.llm_parse_model,
        system=PARSE_SYSTEM.format(today=today),
        user_msg=f"Extract all events from this page:\n\n{raw_content[:8000]}",
    )
    items = _extract_json_array(text)
    results = []
    for item in items:
        cat = item.get("category", "other")
        if cat not in {e.value for e in EventCategory}:
            cat = "other"
        item["category"] = cat
        results.append(ParsedEventData(**item))
    return results


def rerank_events(
    preference_summary: str,
    candidates: list[dict],
) -> list[ScoredEvent]:
    events_text = json.dumps(candidates, indent=2, default=str)
    text = _chat(
        model=settings.llm_rerank_model,
        system=RERANK_SYSTEM,
        user_msg=(
            f"User preference profile:\n{preference_summary}\n\n"
            f"Candidate events:\n{events_text}"
        ),
    )
    items = _extract_json_array(text)
    return [ScoredEvent(**item) for item in items]
