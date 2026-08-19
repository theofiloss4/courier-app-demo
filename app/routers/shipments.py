# =============================================================================
# HTML routes used by STAFF (Employee/Admin/Supervisor) to manage shipments:
# list, create, view detail, change status, resend the voucher email, and
# print the voucher/receipt. This is the staff-facing counterpart to the
# public, read-only tracking pages in app/routers/tracking.py, and shares
# most of its underlying logic with the JSON REST API in
# app/routers/api.py (both call into the same ShipmentService).
# =============================================================================
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.config import BASE_DIR
from app.dependencies import HtmlStaffUser, SessionDep
from app.errors import HtmlError
from app.models.shipment import ShipmentStatus
from app.models.user import UserRole
from app.repositories.shipment_repository import ShipmentRepository
from app.repositories.user_repository import UserRepository
from app.schemas.shipment import ShipmentCreate, ShipmentStatusUpdate
from app.security import CsrfProtection
from app.services.shipment_service import (
    InvalidStatusTransitionError,
    ShipmentNotFoundError,
    ShipmentService,
)
from app.services.email_service import EmailDeliveryStatus, send_voucher_email


router = APIRouter(prefix="/shipments", tags=["shipments"])
templates = Jinja2Templates(directory=BASE_DIR / "templates")
# Labels belong to presentation while Enum values remain stable in the database.
STATUS_LABELS = {
    ShipmentStatus.CREATED: "Καταχωρίστηκε",
    ShipmentStatus.PICKED_UP: "Παραλήφθηκε",
    ShipmentStatus.IN_TRANSIT: "Σε μεταφορά",
    ShipmentStatus.OUT_FOR_DELIVERY: "Προς παράδοση",
    ShipmentStatus.DELIVERED: "Παραδόθηκε",
    ShipmentStatus.FAILED_DELIVERY: "Αποτυχημένη παράδοση",
    ShipmentStatus.CANCELLED: "Ακυρώθηκε",
}
STATUS_LABELS_EN = {
    ShipmentStatus.CREATED: "Created",
    ShipmentStatus.PICKED_UP: "Picked up",
    ShipmentStatus.IN_TRANSIT: "In transit",
    ShipmentStatus.OUT_FOR_DELIVERY: "Out for delivery",
    ShipmentStatus.DELIVERED: "Delivered",
    ShipmentStatus.FAILED_DELIVERY: "Failed delivery",
    ShipmentStatus.CANCELLED: "Cancelled",
}


def status_labels_for(request: Request):
    """Pick the Greek or English status-label dictionary for the visitor.

    Imported and reused by other routers too (app/routers/tracking.py,
    app/routers/accounts.py) so the same shipment status displays
    consistently everywhere in the UI, in whichever language is active.
    """
    return STATUS_LABELS_EN if request.cookies.get("language") == "en" else STATUS_LABELS


def get_service(session: SessionDep) -> ShipmentService:
    """Small factory that wires a ShipmentService together with a
    ShipmentRepository bound to the current request's database session.
    Called at the top of nearly every route below instead of repeating
    `ShipmentService(ShipmentRepository(session))` everywhere.
    """
    return ShipmentService(ShipmentRepository(session))


def shipment_not_found_error() -> HtmlError:
    """Build the standard bilingual 404 error used whenever a shipment id
    does not exist, so every route below raises an identical error instead
    of each writing its own slightly different message.
    """
    return HtmlError(
        status.HTTP_404_NOT_FOUND,
        "The requested shipment could not be found.",
        "Η αποστολή που ζητήθηκε δεν βρέθηκε.",
        "Shipment not found",
        "Η αποστολή δεν βρέθηκε",
    )


@router.get("", response_class=HTMLResponse)
def shipment_list(request: Request, session: SessionDep, user: HtmlStaffUser):
    """GET /shipments — the staff "all shipments" management table.

    Auth: `HtmlStaffUser` requires any logged-in staff role (Employee,
    Admin, or Supervisor) - a Customer gets a 403 error page.
    """
    shipments = get_service(session).list_shipments()
    return templates.TemplateResponse(
        request,
        "shipments/list.html",
        {"shipments": shipments, "status_labels": status_labels_for(request), "user": user},
    )


@router.get("/new", response_class=HTMLResponse)
def shipment_form(request: Request, user: HtmlStaffUser):
    """GET /shipments/new — the empty "register a new shipment" form.

    Auth: staff only.
    """
    return templates.TemplateResponse(
        request, "shipments/new.html", {"user": user}
    )


@router.post("")
def create_shipment(
    request: Request,
    session: SessionDep,
    user: HtmlStaffUser,
    _: CsrfProtection,
    sender_name: str = Form(),
    sender_phone: str = Form(),
    sender_email: str = Form(),
    recipient_name: str = Form(),
    recipient_phone: str = Form(),
    sender_address: str = Form(),
    delivery_address: str = Form(),
    notes: str | None = Form(default=None),
    parcel_description: str | None = Form(default=None),
    weight_kg: float = Form(),
    length_cm: float = Form(),
    width_cm: float = Form(),
    height_cm: float = Form(),
    amount_eur: float = Form(),
):
    """POST /shipments — register a new shipment from the staff HTML form.

    Auth: staff only, CSRF-protected.
    This is the HTML equivalent of `POST /api/shipments` in
    app/routers/api.py - both ultimately call the same
    `ShipmentService.create_shipment`, but this route additionally has to
    handle re-displaying the form with errors (an API client instead just
    gets a JSON 422 response automatically from FastAPI).
    On success: redirects to the new shipment's detail page with query
    flags (`created=1`, `email=<status>`) the template uses to show a
    one-time success/warning banner.
    """
    # Preserve submitted values so they are not lost after validation errors.
    form_data = {
        "sender_name": sender_name,
        "sender_phone": sender_phone,
        "sender_email": sender_email,
        "recipient_name": recipient_name,
        "recipient_phone": recipient_phone,
        "sender_address": sender_address,
        "delivery_address": delivery_address,
        "notes": notes or "",
        "parcel_description": parcel_description or "",
        "weight_kg": weight_kg,
        "length_cm": length_cm,
        "width_cm": width_cm,
        "height_cm": height_cm,
        "amount_eur": amount_eur,
    }
    try:
        # The schema turns raw form strings into validated data.
        data = ShipmentCreate(**form_data)
    except ValidationError as exc:
        # Convert technical Pydantic errors into a list for the template.
        errors = [
            error["msg"].removeprefix("Value error, ") for error in exc.errors()
        ]
        return templates.TemplateResponse(
            request,
            "shipments/new.html",
            {"user": user, "errors": errors, "form_data": form_data},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    customer = UserRepository(session).get_by_email(sender_email.strip())
    if customer and (
        customer.role != UserRole.CUSTOMER or not customer.is_active
    ):
        customer = None
    shipment = get_service(session).create_shipment(
        data, user.id or 0, customer.id if customer else None
    )
    email_status = send_voucher_email(shipment)
    if email_status == EmailDeliveryStatus.SENT:
        shipment.email_sent_at = datetime.now(timezone.utc)
        ShipmentRepository(session).save(shipment)
    return RedirectResponse(
        f"/shipments/{shipment.id}?created=1&email={email_status.value}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/{shipment_id}", response_class=HTMLResponse)
def shipment_detail(
    shipment_id: int,
    request: Request,
    session: SessionDep,
    user: HtmlStaffUser,
):
    """GET /shipments/{shipment_id} — full detail view of one shipment.

    Auth: staff only.
    Path parameter: `shipment_id` is the internal database id (staff
    navigate here by clicking a row in the shipment list, not by typing a
    tracking number), unlike the public tracking route which uses the
    tracking number instead.
    Combines the current shipment record, its full event history, and the
    list of statuses it could legally move to next (so the template can
    render only valid status-change buttons).
    """
    service = get_service(session)
    try:
        shipment = service.get_shipment(shipment_id)
        _, events = service.track(shipment.tracking_number)
    except ShipmentNotFoundError as exc:
        raise shipment_not_found_error() from exc
    return templates.TemplateResponse(
        request,
        "shipments/detail.html",
        {
            "shipment": shipment,
            "events": events,
            "statuses": service.allowed_next_statuses(shipment),
            "status_labels": status_labels_for(request),
            "user": user,
        },
    )


@router.post("/{shipment_id}/status")
def update_status(
    shipment_id: int,
    request: Request,
    session: SessionDep,
    user: HtmlStaffUser,
    _: CsrfProtection,
    new_status: ShipmentStatus = Form(),
    location: str | None = Form(default=None),
    description: str | None = Form(default=None),
):
    """POST /shipments/{shipment_id}/status — change a shipment's status
    from the staff detail page.

    Auth: staff only, CSRF-protected.
    HTML equivalent of `PATCH /api/shipments/{id}/status` - both call the
    same `ShipmentService.update_status` and are bound by the same state
    machine (see ALLOWED_TRANSITIONS in app/services/shipment_service.py).
    Where this route differs from the API version: instead of returning a
    raw error status, an illegal transition re-renders the shipment detail
    page itself with an inline bilingual error message, so staff stay on
    the page and can immediately try a different status.
    """
    # The schema validates lengths and requires a valid status Enum value.
    data = ShipmentStatusUpdate(
        status=new_status,
        location=location or None,
        description=description or None,
    )
    try:
        get_service(session).update_status(shipment_id, data, user.id or 0)
    except ShipmentNotFoundError as exc:
        raise shipment_not_found_error() from exc
    except InvalidStatusTransitionError as exc:
        # A valid status value does not necessarily make the transition legal.
        service = get_service(session)
        shipment = service.get_shipment(shipment_id)
        _, events = service.track(shipment.tracking_number)
        message = (
            "Η συγκεκριμένη αλλαγή κατάστασης δεν επιτρέπεται."
            if request.cookies.get("language", "el") == "el"
            else "This status transition is not allowed."
        )
        return templates.TemplateResponse(
            request,
            "shipments/detail.html",
            {
                "shipment": shipment,
                "events": events,
                "statuses": service.allowed_next_statuses(shipment),
                "status_labels": status_labels_for(request),
                "user": user,
                "error": message,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(
        f"/shipments/{shipment_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{shipment_id}/email")
def resend_voucher_email(
    shipment_id: int,
    session: SessionDep,
    user: HtmlStaffUser,
    _: CsrfProtection,
):
    """POST /shipments/{shipment_id}/email — manually (re)send the voucher email.

    Auth: staff only, CSRF-protected.
    Useful when the automatic email sent at creation time failed (e.g. SMTP
    was temporarily down), or the customer says they never received it.
    """
    service = get_service(session)
    try:
        shipment = service.get_shipment(shipment_id)
    except ShipmentNotFoundError as exc:
        raise shipment_not_found_error() from exc

    email_status = send_voucher_email(shipment)
    if email_status == EmailDeliveryStatus.SENT:
        shipment.email_sent_at = datetime.now(timezone.utc)
        ShipmentRepository(session).save(shipment)
    return RedirectResponse(
        f"/shipments/{shipment_id}?email={email_status.value}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/{shipment_id}/voucher", response_class=HTMLResponse)
def print_voucher(
    shipment_id: int,
    request: Request,
    session: SessionDep,
    user: HtmlStaffUser,
):
    """GET /shipments/{shipment_id}/voucher — a print-friendly voucher page.

    Auth: staff only. Renders a minimal, printer-oriented HTML page (no
    site navigation/header) meant to be printed and attached to the
    physical parcel, containing the tracking number and shipment details.
    """
    try:
        shipment = get_service(session).get_shipment(shipment_id)
    except ShipmentNotFoundError as exc:
        raise shipment_not_found_error() from exc
    return templates.TemplateResponse(
        request, "shipments/voucher.html", {"shipment": shipment, "user": user}
    )


@router.get("/{shipment_id}/receipt", response_class=HTMLResponse)
def print_receipt(
    shipment_id: int,
    request: Request,
    session: SessionDep,
    user: HtmlStaffUser,
):
    """GET /shipments/{shipment_id}/receipt — a print-friendly receipt page.

    Auth: staff only. Similar to print_voucher above, but formatted as a
    customer-facing payment receipt (indicative amount only - actual
    payment happens on the store's external POS terminal, not in this app).
    """
    try:
        shipment = get_service(session).get_shipment(shipment_id)
    except ShipmentNotFoundError as exc:
        raise shipment_not_found_error() from exc
    return templates.TemplateResponse(
        request, "shipments/receipt.html", {"shipment": shipment, "user": user}
    )
