"""Bootstrap initial user preferences interactively."""
import sys

from event_scheduler.db import get_client, run_migrations
from event_scheduler.models import EventCategory
from event_scheduler.services.preferences import upsert_weight

DEFAULT_USER_ID = "default"

CATEGORY_QUESTIONS = {
    EventCategory.MUSIC: "Live music, concerts, DJ sets",
    EventCategory.TECH: "Tech meetups, hackathons, demos",
    EventCategory.FOOD: "Food festivals, tastings, pop-ups",
    EventCategory.ART: "Art shows, gallery openings, exhibits",
    EventCategory.FITNESS: "Runs, yoga, outdoor fitness",
    EventCategory.NIGHTLIFE: "Bars, clubs, rooftop parties",
    EventCategory.COMEDY: "Stand-up, improv, comedy shows",
    EventCategory.NETWORKING: "Professional networking, mixers",
    EventCategory.FAMILY: "Family-friendly, kids events",
}

TIME_SLOTS = [
    ("weekday_morning", "Weekday mornings"),
    ("weekday_afternoon", "Weekday afternoons"),
    ("weekday_evening", "Weekday evenings"),
    ("weekend_morning", "Weekend mornings"),
    ("weekend_afternoon", "Weekend afternoons"),
    ("weekend_evening", "Weekend evenings"),
]

BOROUGHS = ["manhattan", "brooklyn", "queens", "bronx", "staten island"]


def ask_rating(prompt: str) -> float:
    while True:
        val = input(f"  {prompt} (1=hate, 3=neutral, 5=love): ").strip()
        if val in ("1", "2", "3", "4", "5"):
            return (int(val) - 3) / 2.0  # maps 1-5 to -1.0 .. 1.0
        print("  Please enter 1-5.")


def main():
    run_migrations()
    client = get_client()
    user_id = DEFAULT_USER_ID

    print("\n=== Event Scheduler — Preference Setup ===\n")
    print("Rate each category 1 (hate) to 5 (love):\n")

    for cat, description in CATEGORY_QUESTIONS.items():
        weight = ask_rating(f"{cat.value.title()} ({description})")
        upsert_weight(client, user_id, "category", cat.value, weight)

    print("\nRate your preferred time slots:\n")
    for slot_key, label in TIME_SLOTS:
        weight = ask_rating(label)
        upsert_weight(client, user_id, "time", slot_key, weight)

    print("\nRate NYC boroughs:\n")
    for borough in BOROUGHS:
        weight = ask_rating(borough.title())
        upsert_weight(client, user_id, "location", borough, weight)

    print("\nPrice sensitivity (1=only free, 3=don't care, 5=prefer premium):")
    price_w = ask_rating("Price")
    upsert_weight(client, user_id, "price", "sensitivity", price_w)

    print("\nPreferences saved! The recommender will use these to score events.\n")


if __name__ == "__main__":
    main()
