import logging
from datetime import datetime

from clickhouse_connect.driver import Client

from event_scheduler.agents.base import BaseAgent
from event_scheduler.config import settings
from event_scheduler.models import Event, EventCategory
from event_scheduler.services.calendar import create_event, get_event_status
from event_scheduler.services.preferences import update_from_feedback

logger = logging.getLogger(__name__)

DEFAULT_USER_ID = "default"


class DeliveryAgent(BaseAgent):
    name = "delivery"

    def __init__(self, user_id: str = DEFAULT_USER_ID, calendar_id: str = "primary"):
        super().__init__()
        self.user_id = user_id
        self.calendar_id = calendar_id

    def _execute(self, client: Client) -> tuple[int, int]:
        delivered, d_failed = self._deliver_pending(client)
        feedback, f_failed = self._poll_rsvps(client)
        return delivered + feedback, d_failed + f_failed

    def _deliver_pending(self, client: Client) -> tuple[int, int]:
        rows = client.query(
            "SELECT r.rec_id, r.event_id, r.score, r.reasoning, "
            "e.title, e.description, e.start_time, e.end_time, "
            "e.location_name, e.location_address "
            "FROM recommendations r "
            "JOIN events e ON r.event_id = e.event_id "
            "WHERE r.user_id = {uid:String} AND r.status = 'pending' "
            "ORDER BY r.score DESC LIMIT 10",
            parameters={"uid": self.user_id},
        )
        delivered = 0
        failed = 0
        now = datetime.now()

        for row in rows.result_rows:
            rec_id, event_id, score, reasoning = row[0], row[1], row[2], row[3]
            title, description, start_time, end_time = row[4], row[5], row[6], row[7]
            location_name, location_address = row[8], row[9]

            feedback_url = f"{settings.feedback_api_base_url}/respond"
            cal_description = (
                f"{description}\n\n"
                f"Score: {score:.0%} — {reasoning}\n\n"
                f"Not interested? {feedback_url}?rec={rec_id}&action=reject\n"
                f"Love it! {feedback_url}?rec={rec_id}&action=accept"
            )

            start_str = start_time.isoformat() if isinstance(start_time, datetime) else str(start_time)
            end_str = end_time.isoformat() if (end_time and isinstance(end_time, datetime)) else None

            try:
                cal_id = create_event(
                    title=f"[Recommended] {title}",
                    description=cal_description,
                    start_time=start_str,
                    end_time=end_str,
                    location=f"{location_name}, {location_address}",
                    rec_id=str(rec_id),
                    calendar_id=self.calendar_id,
                )
                client.command(
                    f"ALTER TABLE recommendations UPDATE "
                    f"status = 'delivered', calendar_event_id = '{cal_id}', "
                    f"delivered_at = '{now}' "
                    f"WHERE rec_id = '{rec_id}'"
                )
                delivered += 1
            except Exception:
                logger.exception("Failed to deliver rec %s", rec_id)
                failed += 1

        return delivered, failed

    def _poll_rsvps(self, client: Client) -> tuple[int, int]:
        rows = client.query(
            "SELECT rec_id, event_id, calendar_event_id "
            "FROM recommendations "
            "WHERE user_id = {uid:String} "
            "AND status = 'delivered' AND calendar_event_id IS NOT NULL",
            parameters={"uid": self.user_id},
        )
        processed = 0
        failed = 0
        now = datetime.now()

        for rec_id, event_id, cal_event_id in rows.result_rows:
            try:
                status = get_event_status(cal_event_id, calendar_id=self.calendar_id)
                if status == "deleted":
                    self._handle_feedback(
                        client, rec_id, event_id, accepted=False, now=now
                    )
                    processed += 1
            except Exception:
                logger.exception("Failed to poll RSVP for rec %s", rec_id)
                failed += 1

        return processed, failed

    def _handle_feedback(
        self, client: Client, rec_id, event_id, *, accepted: bool, now: datetime
    ) -> None:
        new_status = "accepted" if accepted else "rejected"
        client.command(
            f"ALTER TABLE recommendations UPDATE "
            f"status = '{new_status}', responded_at = '{now}' "
            f"WHERE rec_id = '{rec_id}'"
        )

        event = self._load_event(client, event_id)
        if event:
            update_from_feedback(client, self.user_id, event, accepted)

    def _load_event(self, client: Client, event_id) -> Event | None:
        rows = client.query(
            "SELECT event_id, url_hash, content_hash, title, description, "
            "start_time, end_time, location_name, location_address, "
            "lat, lon, category, tags, price_cents, source, source_url, "
            "image_url, parsed_at "
            "FROM (SELECT * FROM events ORDER BY parsed_at DESC "
            "      LIMIT 1 BY content_hash, event_id) "
            "WHERE event_id = {eid:String}",
            parameters={"eid": str(event_id)},
        )
        if not rows.result_rows:
            return None
        row = rows.result_rows[0]
        return Event(
            event_id=row[0], url_hash=row[1], content_hash=row[2],
            title=row[3], description=row[4], start_time=row[5],
            end_time=row[6], location_name=row[7], location_address=row[8],
            lat=row[9], lon=row[10], category=row[11], tags=row[12],
            price_cents=row[13], source=row[14], source_url=row[15],
            image_url=row[16], parsed_at=row[17],
        )
