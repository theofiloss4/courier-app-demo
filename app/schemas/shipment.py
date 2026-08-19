# =============================================================================
# Pydantic schemas: input validation and output shaping for shipments.
#
# These are NOT database models (compare with app/models/shipment.py, which
# defines the actual table). Instead, they define the exact shape of data
# crossing the "wire" — what a client is allowed to send in, and what shape
# the server promises to send back — for both the HTML form endpoints and
# the JSON REST API. Keeping this separate from the database model means the
# database can have internal-only fields (like created_by_id) that are never
# exposed to or accepted from an API client.
# =============================================================================
from datetime import datetime
import re

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.shipment import ShipmentStatus


class ShipmentCreate(BaseModel):
    """Everything required to register a new shipment (used by both the
    HTML "new shipment" form and the POST /api/shipments endpoint).

    Every `Field(...)` constraint below (min/max length, gt/le numeric
    bounds) is enforced automatically by Pydantic before the request ever
    reaches business logic — invalid data never gets as far as
    ShipmentService. `EmailStr` similarly rejects malformed email addresses
    at the validation stage.
    """

    sender_name: str = Field(min_length=2, max_length=120)
    sender_phone: str = Field(min_length=5, max_length=30)
    sender_email: EmailStr
    recipient_name: str = Field(min_length=2, max_length=120)
    recipient_phone: str = Field(min_length=5, max_length=30)
    sender_address: str = Field(min_length=5, max_length=255)
    delivery_address: str = Field(min_length=5, max_length=255)
    notes: str | None = Field(default=None, max_length=1000)
    parcel_description: str | None = Field(default=None, max_length=255)
    # gt=0 (greater than) rejects zero/negative weight or dimensions; le
    # (less-or-equal) caps them at a sane maximum to catch data-entry typos
    # (e.g. accidentally typing 5000 instead of 5.0 kg).
    weight_kg: float = Field(gt=0, le=1000)
    length_cm: float = Field(gt=0, le=500)
    width_cm: float = Field(gt=0, le=500)
    height_cm: float = Field(gt=0, le=500)
    amount_eur: float = Field(ge=0, le=100000)

    @field_validator("sender_phone", "recipient_phone")
    @classmethod
    def validate_greek_phone(cls, value: str) -> str:
        """Custom validator: accept common Greek phone number formats and
        normalize them all down to a plain 10-digit form.

        Runs automatically whenever a ShipmentCreate is constructed, for
        both the `sender_phone` and `recipient_phone` fields (Pydantic
        calls this once per field named in the decorator above). Raising
        `ValueError` here causes Pydantic to report a validation error back
        to the caller with this exact Greek message.
        """
        # Strip spaces, parentheses, dots, and dashes so "69 123-45678" and
        # "6912345678" are treated identically.
        normalized = re.sub(r"[\s().-]", "", value)
        if normalized.startswith("+30"):
            normalized = normalized[3:]
        elif normalized.startswith("0030"):
            normalized = normalized[4:]

        # Accept a 10-digit landline starting with 2 (e.g. Athens "210...")
        # or a 10-digit mobile starting with 69 — Greece's actual numbering
        # plan — and reject anything else.
        if not re.fullmatch(r"(?:2\d{9}|69\d{8})", normalized):
            raise ValueError(
                "Δώστε έγκυρο ελληνικό σταθερό ή κινητό τηλέφωνο."
            )
        return normalized

    @field_validator("sender_address", "delivery_address")
    @classmethod
    def validate_address_number(cls, value: str) -> str:
        """Custom validator: require a street number to be present.

        A delivery address with no house/street number is not usable by a
        courier in practice, so this rejects addresses that contain no
        digit at all (e.g. "Ermou" without a number), while still allowing
        any address format that does include one.
        """
        cleaned = value.strip()
        if not re.search(r"\d", cleaned):
            raise ValueError("Η διεύθυνση πρέπει να περιλαμβάνει αριθμό.")
        return cleaned


class ShipmentStatusUpdate(BaseModel):
    """Payload for changing a shipment's status (HTML form POST and the
    PATCH /api/shipments/{id}/status endpoint share this same schema).
    Only carries the new status plus optional context for the resulting
    tracking event — everything else about the shipment is left untouched.
    """
    status: ShipmentStatus
    location: str | None = Field(default=None, max_length=150)
    description: str | None = Field(default=None, max_length=500)


class ShipmentRead(BaseModel):
    """The shape of a shipment as returned BY the REST API.

    Deliberately narrower than the full Shipment database model — internal
    fields like `created_by_id`, `notes`, or `customer_id` are left out on
    purpose, so the JSON API never leaks data that a client has no
    legitimate need to see. FastAPI uses this class both to filter outgoing
    data (via `response_model=` in app/routers/api.py) and to generate the
    Swagger/OpenAPI documentation of exactly what a response looks like.
    """
    id: int
    tracking_number: str
    sender_name: str
    sender_email: str
    recipient_name: str
    sender_address: str
    delivery_address: str
    status: ShipmentStatus
    weight_kg: float
    volumetric_weight_kg: float
    chargeable_weight_kg: float
    amount_eur: float
    created_at: datetime

    # By default, Pydantic models only populate themselves from dicts.
    # `from_attributes=True` additionally allows building a ShipmentRead
    # directly from a SQLModel `Shipment` object's attributes (shipment.id,
    # shipment.tracking_number, ...), which is how app/routers/api.py can
    # simply `return shipment` and have FastAPI convert it automatically.
    model_config = ConfigDict(from_attributes=True)
