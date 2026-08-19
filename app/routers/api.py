# =============================================================================
# JSON REST API — the machine-readable counterpart to the HTML pages.
#
# Everything in this file is served under the "/api/shipments" URL prefix,
# returns JSON (not HTML), and is fully documented automatically by FastAPI's
# built-in Swagger UI at GET /docs (raw schema at GET /openapi.json). This is
# what satisfies the assignment's "REST API + Swagger documentation"
# requirement.
#
# Every endpoint here requires an authenticated STAFF account (Employee,
# Admin, or Supervisor - see the `StaffUser` dependency import below).
# Plain Customer accounts and anonymous visitors cannot call this API; they
# only get the read-only, no-login public tracking page instead (see
# app/routers/tracking.py).
#
# Authentication for this API reuses the SAME "access_token" cookie as the
# HTML pages (there is no separate API key/token system) — a staff member
# who is logged in via the browser can immediately open /docs in that same
# browser tab and call these endpoints interactively, since the cookie is
# sent automatically.
# =============================================================================
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.dependencies import SessionDep, StaffUser
from app.models.user import UserRole
from app.repositories.shipment_repository import ShipmentRepository
from app.repositories.user_repository import UserRepository
from app.schemas.shipment import ShipmentCreate, ShipmentRead, ShipmentStatusUpdate
from app.services.email_service import EmailDeliveryStatus, send_voucher_email
from app.services.shipment_service import (
    InvalidStatusTransitionError,
    ShipmentNotFoundError,
    ShipmentService,
)


# Every route defined below is automatically prefixed with "/api/shipments",
# and grouped under the "shipment-api" tag/section in the Swagger UI.
router = APIRouter(prefix="/api/shipments", tags=["shipment-api"])


@router.get("", response_model=list[ShipmentRead])
def list_shipments(session: SessionDep, user: StaffUser):
    """GET /api/shipments — list every shipment in the system.

    Auth: requires a logged-in staff account (Employee/Admin/Supervisor);
    returns 401 if not logged in, 403 if logged in as a Customer.
    Response: a JSON array of shipments shaped by the `ShipmentRead` schema
    (app/schemas/shipment.py) — note this deliberately excludes internal/
    sensitive fields such as `created_by_id` or `notes`.
    """
    # `response_model=list[ShipmentRead]` does two things automatically:
    # (1) filters the returned Shipment objects down to only the fields
    # declared on ShipmentRead, and (2) documents the exact response shape
    # in the Swagger/OpenAPI schema.
    return ShipmentService(ShipmentRepository(session)).list_shipments()


@router.post("", response_model=ShipmentRead, status_code=201)
def create_shipment(
    data: ShipmentCreate, session: SessionDep, user: StaffUser
):
    """POST /api/shipments — register a new shipment via the API.

    Auth: staff only (same as list_shipments above).
    Request body: validated against the `ShipmentCreate` schema — this
    enforces field lengths, positive weight/dimensions, and Greek phone
    number format before any of this code runs (see app/schemas/shipment.py).
    Behavior: mirrors the HTML "create shipment" flow in
    app/routers/shipments.py — computes chargeable weight, generates a
    tracking number, saves the shipment with its first tracking event, and
    attempts to email a voucher to the sender.
    Response: 201 Created with the new shipment as JSON.
    """
    repository = ShipmentRepository(session)
    # If the sender's email matches an existing, active Customer account,
    # automatically link this shipment to that account so it shows up in
    # their personal dashboard — the API caller does not need to know or
    # supply a customer id explicitly.
    customer = UserRepository(session).get_by_email(str(data.sender_email))
    customer_id = (
        customer.id
        if customer
        and customer.role == UserRole.CUSTOMER
        and customer.is_active
        else None
    )
    shipment = ShipmentService(repository).create_shipment(
        data,
        user.id or 0,
        customer_id,
    )
    # API-created shipments trigger the same automatic voucher email as
    # shipments created through the HTML form — the notification behavior
    # does not depend on which "front door" was used to create the shipment.
    if send_voucher_email(shipment) == EmailDeliveryStatus.SENT:
        shipment.email_sent_at = datetime.now(timezone.utc)
        repository.save(shipment)
    return shipment


@router.patch("/{shipment_id}/status", response_model=ShipmentRead)
def update_status(
    shipment_id: int,
    data: ShipmentStatusUpdate,
    session: SessionDep,
    user: StaffUser,
):
    """PATCH /api/shipments/{shipment_id}/status — change a shipment's status.

    Auth: staff only.
    Path parameter: `shipment_id` — the shipment's internal database id
    (not its public tracking number).
    Request body: `ShipmentStatusUpdate` — the new status, plus optional
    free-text location/description for the resulting tracking event.
    Business rule enforcement: the actual state-machine check (is this
    status change legal from the shipment's current status?) happens inside
    `ShipmentService.update_status`, not here — this route's only job is to
    translate the service's domain exceptions into the correct HTTP status
    codes for an API client.
    """
    try:
        return ShipmentService(ShipmentRepository(session)).update_status(
            shipment_id, data, user.id or 0
        )
    except ShipmentNotFoundError as exc:
        # Unknown shipment id -> standard "resource not found".
        raise HTTPException(status_code=404, detail="Shipment not found") from exc
    except InvalidStatusTransitionError as exc:
        # Shipment exists, but this status change breaks the state machine
        # (e.g. CREATED -> DELIVERED) -> "bad request", not "not found".
        raise HTTPException(status_code=400, detail=str(exc)) from exc
