from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EventCategory(str, Enum):
    MUSIC = "music"
    TECH = "tech"
    FOOD = "food"
    ART = "art"
    FITNESS = "fitness"
    NIGHTLIFE = "nightlife"
    COMEDY = "comedy"
    NETWORKING = "networking"
    FAMILY = "family"
    OTHER = "other"


class DiscoveredUrl(BaseModel):
    url_hash: int
    url: str
    source: str
    discovered_at: datetime = Field(default_factory=datetime.now)
    last_scraped_at: datetime | None = None
    scrape_status: str = "pending"


class RawPage(BaseModel):
    url_hash: int
    source: str
    content: str
    scraped_at: datetime = Field(default_factory=datetime.now)
    nimble_task_id: str | None = None
    parse_status: str = "pending"


class Event(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    url_hash: int = 0
    content_hash: int = 0
    title: str
    description: str
    start_time: datetime
    end_time: datetime | None = None
    location_name: str
    location_address: str
    lat: float | None = None
    lon: float | None = None
    category: EventCategory = EventCategory.OTHER
    tags: list[str] = Field(default_factory=list)
    price_cents: int = 0
    source: str = ""
    source_url: str = ""
    image_url: str | None = None
    parsed_at: datetime = Field(default_factory=datetime.now)


class ParsedEventData(BaseModel):
    """Schema for LLM structured output when parsing raw HTML."""
    title: str
    description: str
    start_time: str  # ISO 8601
    end_time: str | None = None
    location_name: str
    location_address: str
    category: EventCategory = EventCategory.OTHER
    tags: list[str] = Field(default_factory=list)
    price_cents: int = 0


class UserPreference(BaseModel):
    user_id: str
    preference_type: str  # category | time | location | keyword | price
    key: str
    weight: float
    updated_at: datetime = Field(default_factory=datetime.now)


class Recommendation(BaseModel):
    rec_id: UUID = Field(default_factory=uuid4)
    user_id: str
    event_id: UUID
    score: float
    reasoning: str
    status: str = "pending"
    calendar_event_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    delivered_at: datetime | None = None
    responded_at: datetime | None = None


class ScoredEvent(BaseModel):
    """Output from the LLM reranking step."""
    event_id: str
    score: float
    reasoning: str


class AgentRun(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    agent_name: str
    started_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None
    items_processed: int = 0
    items_failed: int = 0
    error_message: str | None = None
