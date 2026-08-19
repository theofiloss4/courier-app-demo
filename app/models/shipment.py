# =============================================================================
# Shipment domain models: Shipment (current state) and TrackingEvent (history).
#
# Together with User (app/models/user.py), these three tables form the
# complete domain model required by the assignment. The relationship is:
#   User (1) --creates--> (*) Shipment (1) --has--> (*) TrackingEvent
# A Shipment always reflects only its CURRENT status; every status change is
# additionally recorded as a new, never-modified TrackingEvent row, giving a
# full audit trail of how a parcel moved through the system.
# =============================================================================
from datetime import datetime, timezone
from enum import StrEnum

from sqlmodel import Field, SQLModel


class ShipmentStatus(StrEnum):
    """Every possible state a shipment can be in.

    Using an Enum (instead of a free-text string) prevents typos like
    "Delivered" vs "delivered" from ever creating an unrecognized status.
    The actual rules for which status can transition to which other status
    live in app/services/shipment_service.py (ALLOWED_TRANSITIONS), not
    here - this class only enumerates the possible values.
    """

    CREATED = "created"
    PICKED_UP = "picked_up"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    FAILED_DELIVERY = "failed_delivery"
    CANCELLED = "cancelled"


class Shipment(SQLModel, table=True):
    """A single parcel being shipped, holding its current state.

    This row is UPDATED in place as the shipment's status changes (unlike
    TrackingEvent below, which is append-only). Sender/recipient details are
    stored directly on the shipment (rather than only referencing a User)
    because a shipment can be created for a sender who has no account at
    all - see `customer_id` below for the optional link.
    """

    id: int | None = Field(default=None, primary_key=True)
    # Public-facing identifier customers use to look up their parcel (format:
    # "CF-" + 10 random alphanumeric characters, generated in
    # ShipmentService._generate_tracking_number). Deliberately NOT the
    # database's internal auto-increment `id`, so tracking numbers cannot be
    # guessed/enumerated sequentially.
    tracking_number: str = Field(index=True, unique=True, max_length=24)
    sender_name: str = Field(max_length=120)
    sender_phone: str = Field(max_length=30)
    sender_email: str = Field(max_length=255)
    recipient_name: str = Field(max_length=120)
    recipient_phone: str = Field(max_length=30)
    sender_address: str = Field(max_length=255)
    delivery_address: str = Field(max_length=255)
    # Indexed because the staff shipment list can filter/sort by status.
    status: ShipmentStatus = Field(default=ShipmentStatus.CREATED, index=True)
    notes: str | None = Field(default=None, max_length=1000)
    parcel_description: str | None = Field(default=None, max_length=255)
    # Physical measurements used to compute the shipping charge.
    weight_kg: float = Field(default=0)
    length_cm: float = Field(default=0)
    width_cm: float = Field(default=0)
    height_cm: float = Field(default=0)
    # Computed once at creation time by ShipmentService (length * width *
    # height / VOLUMETRIC_DIVISOR) - stored rather than recalculated on
    # every read, since the dimensions do not change after creation.
    volumetric_weight_kg: float = Field(default=0)
    # The greater of weight_kg and volumetric_weight_kg - couriers bill
    # based on whichever is larger, since a large-but-light parcel still
    # takes up as much space in a vehicle as a heavier one.
    chargeable_weight_kg: float = Field(default=0)
    # Indicative charge only; actual payment is completed through an
    # external point-of-sale system, not processed by this application.
    amount_eur: float = Field(default=0)
    # Set when the voucher email successfully sends; None if it was never
    # sent or the send failed (see app/services/email_service.py).
    email_sent_at: datetime | None = Field(default=None)
    # Optional link to a registered Customer account, set automatically when
    # the sender's email matches an existing customer (see
    # UserRepository.link_unassigned_for_customer). A shipment can exist
    # with no linked customer_id if the sender never creates an account.
    customer_id: int | None = Field(default=None, foreign_key="user.id", index=True)
    # Always required: records which staff member registered the shipment.
    created_by_id: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Bumped every time the status changes (see ShipmentService.update_status).
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TrackingEvent(SQLModel, table=True):
    """One immutable entry in a shipment's status history timeline.

    Rows in this table are only ever INSERTed, never updated or deleted -
    this is what makes the tracking history trustworthy: once an event is
    recorded (e.g. "picked up in Athens"), it stays in the history
    permanently, even after the shipment's overall status moves on.
    """

    # Many TrackingEvent rows can belong to the same Shipment (one-to-many).
    id: int | None = Field(default=None, primary_key=True)
    shipment_id: int = Field(foreign_key="shipment.id", index=True)
    status: ShipmentStatus
    location: str | None = Field(default=None, max_length=150)
    description: str | None = Field(default=None, max_length=500)
    # Which staff member triggered this particular status change.
    created_by_id: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
