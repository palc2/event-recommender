from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from event_scheduler.config import settings

_service = None


def get_calendar_service():
    global _service
    if _service is None:
        creds = Credentials(
            token=None,
            refresh_token=settings.google_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )
        _service = build("calendar", "v3", credentials=creds)
    return _service


def create_event(
    *,
    title: str,
    description: str,
    start_time: str,
    end_time: str | None,
    location: str,
    rec_id: str,
    calendar_id: str = "primary",
) -> str:
    """Create a calendar event and return its ID."""
    service = get_calendar_service()
    body: dict = {
        "summary": title,
        "description": description,
        "location": location,
        "start": {"dateTime": start_time, "timeZone": "America/New_York"},
        "extendedProperties": {"private": {"recommendation_id": rec_id}},
    }
    if end_time:
        body["end"] = {"dateTime": end_time, "timeZone": "America/New_York"}
    else:
        body["end"] = body["start"]

    result = service.events().insert(calendarId=calendar_id, body=body).execute()
    return result["id"]


def get_event_status(calendar_event_id: str, calendar_id: str = "primary") -> str | None:
    """Check if a calendar event still exists.
    Returns 'active' if present, 'deleted' if removed (reject signal), None on error.
    """
    service = get_calendar_service()
    try:
        event = service.events().get(
            calendarId=calendar_id, eventId=calendar_event_id
        ).execute()
        if event.get("status") == "cancelled":
            return "deleted"
        return "active"
    except HttpError as e:
        if e.resp.status == 404:
            return "deleted"
        return None
