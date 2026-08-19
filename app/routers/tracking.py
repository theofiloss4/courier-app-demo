# =============================================================================
# Public shipment tracking pages — the ONE feature area that requires no
# login at all, by design: anyone with a tracking number (e.g. printed on a
# receipt) should be able to check a parcel's status without creating an
# account. Compare with app/routers/shipments.py, which is the staff-only
# equivalent that also allows changing a shipment's status.
# =============================================================================
from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.dependencies import OptionalUser, SessionDep
from app.repositories.shipment_repository import ShipmentRepository
from app.services.shipment_service import ShipmentNotFoundError, ShipmentService
from app.routers.shipments import status_labels_for


router = APIRouter(prefix="/track", tags=["tracking"])
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@router.get("", response_class=HTMLResponse)
def tracking_page(request: Request, user: OptionalUser):
    """GET /track — the empty tracking number search form.

    Auth: none required. `OptionalUser` is only used to decide whether to
    show a "logged in as ..." indicator in the page header, not to gate
    access to the page itself.
    """
    return templates.TemplateResponse(request, "tracking/search.html", {"user": user})


@router.get("/{tracking_number}", response_class=HTMLResponse)
def tracking_result(tracking_number: str, request: Request, session: SessionDep, user: OptionalUser):
    """GET /track/{tracking_number} — look up and display one shipment's status.

    Auth: none required — this is the whole point of a public tracking page.
    Path parameter: `tracking_number`, the public "CF-XXXXXXXXXX" code (not
    the internal database id), taken directly from the URL.
    On not found: re-renders the search form with a bilingual "not found"
    message and HTTP 404, instead of a generic error page — keeps the
    visitor able to immediately try a different number.
    On success: renders the full status + tracking-event timeline.
    """
    # The path parameter flows from router to service and then repository.
    service = ShipmentService(ShipmentRepository(session))
    try:
        shipment, events = service.track(tracking_number)
    except ShipmentNotFoundError:
        # For HTML, return the same form with a user-friendly message.
        return templates.TemplateResponse(
            request,
            "tracking/search.html",
            {
                "user": user,
                "error": (
                    "No shipment was found with this tracking number."
                    if request.cookies.get("language") == "en"
                    else "Δεν βρέθηκε αποστολή με αυτόν τον αριθμό tracking."
                ),
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return templates.TemplateResponse(
        request,
        "tracking/result.html",
        {"shipment": shipment, "events": events, "status_labels": status_labels_for(request), "user": user},
    )
