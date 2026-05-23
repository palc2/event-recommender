import logging
from datetime import datetime

import uvicorn
from fastapi import FastAPI, HTTPException, Query

from event_scheduler.db import get_client
from event_scheduler.models import Event
from event_scheduler.services.preferences import update_from_feedback

logger = logging.getLogger(__name__)
app = FastAPI(title="Event Scheduler Feedback API")

DEFAULT_USER_ID = "default"


@app.get("/respond")
def respond(rec: str = Query(...), action: str = Query(...)):
    if action not in ("accept", "reject"):
        raise HTTPException(400, "action must be 'accept' or 'reject'")

    client = get_client()
    now = datetime.now()
    accepted = action == "accept"
    new_status = "accepted" if accepted else "rejected"

    rows = client.query(
        "SELECT rec_id, event_id FROM recommendations WHERE rec_id = {rid:String}",
        parameters={"rid": rec},
    )
    if not rows.result_rows:
        raise HTTPException(404, "Recommendation not found")

    _, event_id = rows.result_rows[0]

    client.command(
        f"ALTER TABLE recommendations UPDATE "
        f"status = '{new_status}', responded_at = '{now}' "
        f"WHERE rec_id = '{rec}'"
    )

    event = _load_event(client, event_id)
    if event:
        update_from_feedback(client, DEFAULT_USER_ID, event, accepted)

    return {
        "status": "ok",
        "action": action,
        "message": "Thanks! Your feedback helps improve future recommendations.",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


def _load_event(client, event_id) -> Event | None:
    rows = client.query(
        "SELECT event_id, url_hash, content_hash, title, description, "
        "start_time, end_time, location_name, location_address, "
        "lat, lon, category, tags, price_cents, source, source_url, "
        "image_url, parsed_at "
        "FROM events FINAL WHERE event_id = {eid:String}",
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


def main():
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
