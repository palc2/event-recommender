import logging
from datetime import datetime
from uuid import uuid4

from clickhouse_connect.driver import Client

from event_scheduler.agents.base import BaseAgent
from event_scheduler.config import settings
from event_scheduler.models import Event, EventCategory
from event_scheduler.services.llm import rerank_events
from event_scheduler.services.preferences import (
    format_preference_summary,
    get_preference_weights,
    quick_score,
)

logger = logging.getLogger(__name__)

DEFAULT_USER_ID = "default"


class RecommenderAgent(BaseAgent):
    name = "recommender"

    def _execute(self, client: Client) -> tuple[int, int]:
        user_id = DEFAULT_USER_ID
        weights = get_preference_weights(client, user_id)

        already_recommended = self._already_recommended_ids(client, user_id)

        events = self._fetch_upcoming_events(client)
        candidates = [e for e in events if str(e.event_id) not in already_recommended]
        if not candidates:
            return 0, 0

        scored = [(e, quick_score(e, weights)) for e in candidates]
        scored.sort(key=lambda x: -x[1])
        top = scored[: settings.recommender_top_k]

        if not top:
            return 0, 0

        pref_summary = format_preference_summary(weights)
        candidate_dicts = [
            {
                "event_id": str(e.event_id),
                "title": e.title,
                "description": e.description,
                "start_time": e.start_time.isoformat() if isinstance(e.start_time, datetime) else str(e.start_time),
                "location": f"{e.location_name}, {e.location_address}",
                "category": e.category.value if isinstance(e.category, EventCategory) else e.category,
                "price_cents": e.price_cents,
                "tags": e.tags,
                "pre_score": round(pre_score, 3),
            }
            for e, pre_score in top
        ]

        try:
            reranked = rerank_events(pref_summary, candidate_dicts)
        except Exception:
            logger.exception("LLM rerank failed, falling back to pre-scores")
            reranked = []

        now = datetime.now()
        inserted = 0
        if reranked:
            for item in reranked:
                if item.score >= settings.recommendation_threshold:
                    client.insert(
                        "recommendations",
                        [[
                            str(uuid4()), user_id, item.event_id,
                            item.score, item.reasoning, "pending",
                            None, now, None, None,
                        ]],
                        column_names=[
                            "rec_id", "user_id", "event_id", "score", "reasoning",
                            "status", "calendar_event_id", "created_at",
                            "delivered_at", "responded_at",
                        ],
                    )
                    inserted += 1
        else:
            for e, pre_score in top[:5]:
                if pre_score >= settings.recommendation_threshold:
                    client.insert(
                        "recommendations",
                        [[
                            str(uuid4()), user_id, str(e.event_id),
                            pre_score, "Based on preference weights",
                            "pending", None, now, None, None,
                        ]],
                        column_names=[
                            "rec_id", "user_id", "event_id", "score", "reasoning",
                            "status", "calendar_event_id", "created_at",
                            "delivered_at", "responded_at",
                        ],
                    )
                    inserted += 1

        return inserted, 0

    def _fetch_upcoming_events(self, client: Client) -> list[Event]:
        rows = client.query(
            "SELECT event_id, url_hash, content_hash, title, description, "
            "start_time, end_time, location_name, location_address, "
            "lat, lon, category, tags, price_cents, source, source_url, "
            "image_url, parsed_at "
            "FROM events FINAL "
            "WHERE start_time > now() "
            "ORDER BY start_time LIMIT 200"
        )
        events = []
        for row in rows.result_rows:
            events.append(Event(
                event_id=row[0], url_hash=row[1], content_hash=row[2],
                title=row[3], description=row[4], start_time=row[5],
                end_time=row[6], location_name=row[7], location_address=row[8],
                lat=row[9], lon=row[10], category=row[11], tags=row[12],
                price_cents=row[13], source=row[14], source_url=row[15],
                image_url=row[16], parsed_at=row[17],
            ))
        return events

    def _already_recommended_ids(self, client: Client, user_id: str) -> set[str]:
        rows = client.query(
            "SELECT event_id FROM recommendations WHERE user_id = {uid:String}",
            parameters={"uid": user_id},
        )
        return {str(row[0]) for row in rows.result_rows}
