# =============================================================================
# Shipment service: the business-rules layer for shipment lifecycle.
#
# This module is the "brain" of the courier domain. It knows nothing about
# HTTP, forms, or SQL directly - it only understands courier concepts:
# how a chargeable weight is computed, which status can follow which, and
# what happens (in terms of domain objects) when a shipment is created or
# its status changes. Persistence is delegated entirely to
# ShipmentRepository. This separation is what makes it possible to unit
# test all of this logic (see tests/test_shipment_service.py) without
# spinning up a web server or a real database transaction per assertion.
# =============================================================================
import secrets
import string
from datetime import datetime, timezone

from app.models.shipment import Shipment, ShipmentStatus, TrackingEvent
from app.config import get_settings
from app.repositories.shipment_repository import ShipmentRepository
from app.schemas.shipment import ShipmentCreate, ShipmentStatusUpdate


class ShipmentNotFoundError(Exception):
    """Raised when a shipment id/tracking number does not exist.

    A plain domain exception - it carries no HTTP knowledge. Routers catch
    this and translate it into an HTTP 404 response (see
    app/routers/shipments.py and app/routers/tracking.py).
    """
    pass


class InvalidStatusTransitionError(Exception):
    """Raised when a requested status change is not allowed by the state
    machine below, e.g. trying to jump straight from CREATED to DELIVERED.
    Routers catch this and show a validation error instead of applying it.
    """
    pass


# This dictionary IS the shipment state machine: each key is a status, and
# its value is the exact set of statuses that may legally follow it. Any
# transition not listed here is rejected by update_status(). Reading this
# table top to bottom traces a shipment's normal journey:
#   CREATED -> PICKED_UP -> IN_TRANSIT -> OUT_FOR_DELIVERY -> DELIVERED
# with CANCELLED reachable as an "escape hatch" from most states, and
# FAILED_DELIVERY allowing a retry (back to OUT_FOR_DELIVERY) or cancellation.
# DELIVERED and CANCELLED map to an empty set: they are final states with no
# legal next status.
ALLOWED_TRANSITIONS: dict[ShipmentStatus, set[ShipmentStatus]] = {
    ShipmentStatus.CREATED: {
        ShipmentStatus.PICKED_UP,
        ShipmentStatus.CANCELLED,
    },
    ShipmentStatus.PICKED_UP: {
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.CANCELLED,
    },
    ShipmentStatus.IN_TRANSIT: {
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.CANCELLED,
    },
    ShipmentStatus.OUT_FOR_DELIVERY: {
        ShipmentStatus.DELIVERED,
        ShipmentStatus.FAILED_DELIVERY,
    },
    ShipmentStatus.FAILED_DELIVERY: {
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.CANCELLED,
    },
    ShipmentStatus.DELIVERED: set(),
    ShipmentStatus.CANCELLED: set(),
}


class ShipmentService:
    """Enforces courier business rules on top of the shipment repository.

    Every public method here corresponds to one meaningful domain
    operation (create a shipment, look one up, change its status, etc.)
    rather than a raw database action - the low-level persistence details
    are delegated to `self.repository`.
    """

    def __init__(self, repository: ShipmentRepository) -> None:
        self.repository = repository

    def list_shipments(self) -> list[Shipment]:
        """Return every shipment for the staff management screen."""
        return self.repository.list_all()

    def get_shipment(self, shipment_id: int) -> Shipment:
        """Fetch one shipment by internal id, or raise if it does not exist.

        Centralizing this "raise if missing" check here means every caller
        (multiple routes) gets the same not-found behavior automatically,
        instead of each one having to check `if shipment is None` itself.
        """
        shipment = self.repository.get_by_id(shipment_id)
        if not shipment:
            raise ShipmentNotFoundError
        return shipment

    def track(self, tracking_number: str) -> tuple[Shipment, list[TrackingEvent]]:
        """Look up a shipment by its public tracking number, with its full history.

        Used by the public (no-login-required) tracking page, so both the
        current shipment record and its complete event timeline are
        returned together in one call.
        """
        # strip removes accidental whitespace before the database lookup.
        shipment = self.repository.get_by_tracking_number(tracking_number.strip())
        if not shipment or shipment.id is None:
            raise ShipmentNotFoundError
        return shipment, self.repository.list_events(shipment.id)

    def create_shipment(
        self, data: ShipmentCreate, user_id: int, customer_id: int | None = None
    ) -> Shipment:
        """Register a brand-new shipment, computing its pricing weight and
        assigning it a random tracking number, then persist it together
        with its first ("Shipment registered") tracking event.

        `data` has already been validated by the ShipmentCreate Pydantic
        schema (see app/schemas/shipment.py) before it reaches this method -
        this function only concerns itself with domain calculations, not
        input validation.
        """
        # model_dump converts the validated schema into a dictionary.
        settings = get_settings()
        # Volumetric ("dimensional") weight approximates how much space a
        # parcel occupies, independent of its actual weight - couriers use
        # this so that large-but-light packages are still billed fairly.
        volumetric_weight = round(
            (data.length_cm * data.width_cm * data.height_cm)
            / settings.volumetric_divisor,
            2,
        )
        # The customer is billed for whichever is larger: actual weight or
        # volumetric weight - this is standard courier industry practice.
        chargeable_weight = max(data.weight_kg, volumetric_weight)
        shipment = Shipment(
            **data.model_dump(),
            tracking_number=self._generate_tracking_number(),
            created_by_id=user_id,
            customer_id=customer_id,
            volumetric_weight_kg=volumetric_weight,
            chargeable_weight_kg=chargeable_weight,
        )
        # Every new shipment immediately receives its first history event,
        # so the tracking timeline is never empty for a registered shipment.
        # shipment_id=0 is a placeholder - the repository fills in the real,
        # database-generated id once the shipment row itself is inserted
        # (see ShipmentRepository.add_with_event).
        return self.repository.add_with_event(
            shipment,
            TrackingEvent(
                shipment_id=0,
                status=ShipmentStatus.CREATED,
                description=(
                    "Η αποστολή καταχωρίστηκε / Shipment registered"
                ),
                created_by_id=user_id,
            )
        )

    def allowed_next_statuses(
        self, shipment: Shipment
    ) -> tuple[ShipmentStatus, ...]:
        """List the statuses this shipment could legally move to next.

        Used by the shipment detail page to render only valid buttons/
        dropdown options for the staff member updating the status - the UI
        never even offers an illegal transition, though update_status()
        below still enforces it server-side regardless.
        """
        return tuple(
            status
            for status in ShipmentStatus
            if status in ALLOWED_TRANSITIONS[shipment.status]
        )

    def update_status(
        self, shipment_id: int, data: ShipmentStatusUpdate, user_id: int
    ) -> Shipment:
        """Move a shipment to a new status, recording a matching history event.

        This is the single place in the codebase where the state machine
        (ALLOWED_TRANSITIONS) is actually enforced: even if a request tries
        to skip straight from CREATED to DELIVERED, this check rejects it
        regardless of what the client sent.
        """
        shipment = self.get_shipment(shipment_id)
        # The change is rejected when it is absent from the transition map.
        if data.status not in ALLOWED_TRANSITIONS[shipment.status]:
            raise InvalidStatusTransitionError(
                f"Cannot change status from {shipment.status} to {data.status}"
            )

        # Update the current status and append a separate history event -
        # both persisted together in one transaction (see
        # ShipmentRepository.save_with_event).
        shipment.status = data.status
        shipment.updated_at = datetime.now(timezone.utc)
        return self.repository.save_with_event(
            shipment,
            TrackingEvent(
                shipment_id=shipment_id,
                status=data.status,
                location=data.location,
                description=data.description,
                created_by_id=user_id,
            )
        )

    def _generate_tracking_number(self) -> str:
        """Generate a unique, hard-to-guess public tracking number.

        Format: "CF-" followed by 10 random upper-case letters/digits (36^10
        possibilities), using `secrets.choice` (a cryptographically secure
        random source, unlike the plain `random` module) so tracking numbers
        cannot be predicted or enumerated by a malicious visitor trying
        nearby values. The while-loop guards against the astronomically
        unlikely case of generating a value that already exists.
        """
        alphabet = string.ascii_uppercase + string.digits
        while True:
            suffix = "".join(secrets.choice(alphabet) for _ in range(10))
            tracking_number = f"CF-{suffix}"
            if not self.repository.get_by_tracking_number(tracking_number):
                return tracking_number
