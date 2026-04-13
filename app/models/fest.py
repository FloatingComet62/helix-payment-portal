from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl, field_serializer


class AddonPricing(BaseModel):
    accommodation_per_day: Decimal
    food_per_day: Decimal
    bundled_discount_percent: Decimal  # e.g. Decimal("10.00") means 10% off when bundled

    @field_serializer("accommodation_per_day", "food_per_day", "bundled_discount_percent")
    def serialize_decimal(self, v: Decimal) -> str:
        return str(v)


class PricingConfig(BaseModel):
    """
    mode:
      - "fixed"   : separate fixed prices for food, accommodation, and bundle (food_price, accommodation_price, bundled_price)
    - "per_day" : accommodation_per_day and food_per_day rates (addon required)
      - "bundled" : accommodation + food add-ons with optional bundle discount (addon required)
    gst_percent is always required (e.g. Decimal("18.00") for 18% GST).
    """

    mode: Literal["fixed", "per_day", "bundled"]
    fixed_rate: Optional[Decimal] = None
    food_price: Optional[Decimal] = None
    accommodation_price: Optional[Decimal] = None
    bundled_price: Optional[Decimal] = None
    per_day_rate: Optional[Decimal] = None
    num_days: Optional[int] = None
    addon: Optional[AddonPricing] = None
    gst_percent: Decimal

    @field_serializer("fixed_rate", "food_price", "accommodation_price", "bundled_price", "per_day_rate", "gst_percent")
    def serialize_decimal(self, v: Optional[Decimal]) -> Optional[str]:
        return str(v) if v is not None else None


class FestEvent(BaseModel):
    """A single event within a fest that attendees can register for."""
    event_id: str          # UUID, generated server-side on creation
    name: str
    description: Optional[str] = None


class EventSelectionConfig(BaseModel):
    """
    Controls whether/how users must select events when booking.
    enabled=False means the event selection UI is hidden entirely.
    required=True means the user must pick at least one event before proceeding.
    max_selections caps how many events a single person can select.
    """
    enabled: bool = False
    required: bool = False
    max_selections: int = 1


class FestCreate(BaseModel):
    fest_id: str          # Unique slug, e.g. "HackTU_2026"
    legal_name: str       # Full legal name of the fest organizer
    event_dates: Optional[str] = None
    short_description: Optional[str] = None
    long_description: Optional[str] = None
    authorized_url: str   # Origin URL of the fest frontend (for future CORS/validation)
    callback_url: HttpUrl # Where we POST after successful/failed payment
    pricing: PricingConfig
    available_options: list[str] = ["accommodation", "food", "bundled"]
    total_accommodation_seats: Optional[int] = None  # None = unlimited; set to enforce seat cap
    events: List[FestEvent] = Field(default_factory=list)
    event_selection: EventSelectionConfig = Field(default_factory=EventSelectionConfig)


class FestDB(FestCreate):
    """Internal representation stored in MongoDB. Includes the generated secret_key."""
    secret_key: str       # 64-char hex token; used to sign our callback payloads
    created_at: datetime


class FestPublic(BaseModel):
    """Returned to admin API consumers — secret_key is intentionally excluded."""
    fest_id: str
    legal_name: str
    event_dates: Optional[str] = None
    short_description: Optional[str] = None
    long_description: Optional[str] = None
    authorized_url: str
    callback_url: HttpUrl
    pricing: PricingConfig
    available_options: list[str] = ["accommodation", "food", "bundled"]
    total_accommodation_seats: Optional[int] = None
    events: List[FestEvent] = Field(default_factory=list)
    event_selection: EventSelectionConfig = Field(default_factory=EventSelectionConfig)
    created_at: datetime


class FestUpdate(BaseModel):
    """PATCH body for updating editable fest fields."""
    legal_name: Optional[str] = None
    event_dates: Optional[str] = None
    short_description: Optional[str] = None
    long_description: Optional[str] = None
    authorized_url: Optional[str] = None
    callback_url: Optional[HttpUrl] = None
    pricing: Optional[PricingConfig] = None
    available_options: Optional[list[str]] = None
    total_accommodation_seats: Optional[int] = None
    event_selection: Optional[EventSelectionConfig] = None


class FestListing(BaseModel):
    """
    Minimal fest info returned by the public /portal/fests endpoint.
    Safe to expose without auth — no callback_url, no authorized_url, no secret_key.
    Used by the portal frontend to render the fest selection screen.
    """
    fest_id: str
    legal_name: str
    event_dates: Optional[str] = None
    short_description: Optional[str] = None
    long_description: Optional[str] = None
    pricing: PricingConfig
    available_options: list[str] = ["accommodation", "food", "bundled"]
    events: List[FestEvent] = Field(default_factory=list)
    event_selection: EventSelectionConfig = Field(default_factory=EventSelectionConfig)
