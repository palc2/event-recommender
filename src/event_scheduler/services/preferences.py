from datetime import datetime

from clickhouse_connect.driver import Client

from event_scheduler.models import Event


def get_preference_weights(client: Client, user_id: str) -> dict[str, dict[str, float]]:
    """Load all preference weights grouped by type."""
    # Cloud (SharedMergeTree) rejects FINAL; dedup user_preferences manually.
    rows = client.query(
        "SELECT preference_type, key, weight FROM "
        "(SELECT * FROM user_preferences ORDER BY updated_at DESC "
        " LIMIT 1 BY user_id, preference_type, key) "
        "WHERE user_id = {user_id:String}",
        parameters={"user_id": user_id},
    )
    result: dict[str, dict[str, float]] = {}
    for ptype, key, weight in rows.result_rows:
        result.setdefault(ptype, {})[key] = weight
    return result


def upsert_weight(client: Client, user_id: str, ptype: str, key: str, weight: float) -> None:
    weight = max(-1.0, min(1.0, weight))
    client.insert(
        "user_preferences",
        [[user_id, ptype, key, weight, datetime.now()]],
        column_names=["user_id", "preference_type", "key", "weight", "updated_at"],
    )


def format_preference_summary(weights: dict[str, dict[str, float]]) -> str:
    lines = []
    for ptype, kv in sorted(weights.items()):
        top = sorted(kv.items(), key=lambda x: -x[1])[:5]
        bottom = sorted(kv.items(), key=lambda x: x[1])[:3]
        likes = ", ".join(f"{k} ({v:+.2f})" for k, v in top if v > 0)
        dislikes = ", ".join(f"{k} ({v:+.2f})" for k, v in bottom if v < 0)
        if likes:
            lines.append(f"{ptype} likes: {likes}")
        if dislikes:
            lines.append(f"{ptype} dislikes: {dislikes}")
    return "\n".join(lines) if lines else "No preferences yet — new user."


def classify_time_slot(event: Event) -> str:
    dt = event.start_time
    day = dt.strftime("%A").lower()
    hour = dt.hour
    is_weekend = day in ("saturday", "sunday")
    if hour < 12:
        period = "morning"
    elif hour < 17:
        period = "afternoon"
    else:
        period = "evening"
    prefix = "weekend" if is_weekend else "weekday"
    return f"{prefix}_{period}"


def classify_borough(address: str) -> str:
    addr_lower = address.lower()
    for borough in ("manhattan", "brooklyn", "queens", "bronx", "staten island"):
        if borough in addr_lower:
            return borough
    return "manhattan"  # default for NYC


def quick_score(event: Event, weights: dict[str, dict[str, float]]) -> float:
    score = 0.0
    cat_weights = weights.get("category", {})
    score += cat_weights.get(event.category.value, 0.0) * 2.0

    time_weights = weights.get("time", {})
    score += time_weights.get(classify_time_slot(event), 0.0)

    location_weights = weights.get("location", {})
    score += location_weights.get(classify_borough(event.location_address), 0.0)

    price_weight = weights.get("price", {}).get("sensitivity", 0.0)
    score += price_weight * (event.price_cents / 10000)

    kw_weights = weights.get("keyword", {})
    desc_lower = event.description.lower()
    for kw, w in kw_weights.items():
        if kw in desc_lower:
            score += w
    return score


ACCEPT_DELTA = 0.1
REJECT_DELTA = -0.15


def update_from_feedback(client: Client, user_id: str, event: Event, accepted: bool) -> None:
    delta = ACCEPT_DELTA if accepted else REJECT_DELTA
    weights = get_preference_weights(client, user_id)

    cat_w = weights.get("category", {}).get(event.category.value, 0.0)
    upsert_weight(client, user_id, "category", event.category.value, cat_w + delta)

    time_slot = classify_time_slot(event)
    time_w = weights.get("time", {}).get(time_slot, 0.0)
    upsert_weight(client, user_id, "time", time_slot, time_w + delta)

    borough = classify_borough(event.location_address)
    loc_w = weights.get("location", {}).get(borough, 0.0)
    upsert_weight(client, user_id, "location", borough, loc_w + delta)

    for tag in event.tags[:5]:
        kw_w = weights.get("keyword", {}).get(tag, 0.0)
        upsert_weight(client, user_id, "keyword", tag, kw_w + delta * 0.5)
