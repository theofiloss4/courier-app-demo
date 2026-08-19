# =============================================================================
# Repository layer for Shipment and TrackingEvent.
#
# Same pattern as user_repository.py: every raw database query for these two
# tables lives here, so app/services/shipment_service.py can focus purely on
# business rules (weight calculation, status transition rules) without ever
# writing SQL/SQLModel queries itself.
# =============================================================================
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, col, select

from app.models.shipment import Shipment, TrackingEvent


class ShipmentRepository:
    """All database access for Shipment and TrackingEvent rows goes through here."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_all(self) -> list[Shipment]:
        """Return every shipment, most recently created first.

        Backs the staff "all shipments" management list.
        """
        statement = select(Shipment).order_by(col(Shipment.created_at).desc())
        return list(self.session.exec(statement).all())

    def get_by_id(self, shipment_id: int) -> Shipment | None:
        """Look up a shipment by its internal database id (not the public
        tracking number - see get_by_tracking_number for that).
        `session.get` is a SQLAlchemy shortcut for a primary-key lookup.
        """
        return self.session.get(Shipment, shipment_id)

    def get_by_tracking_number(self, tracking_number: str) -> Shipment | None:
        """Look up a shipment by its public tracking number.

        Used by the public tracking page, where any visitor (customer or
        anonymous) can paste in a tracking number to see the parcel's status.
        """
        # Tracking numbers are always stored upper-case (see
        # ShipmentService._generate_tracking_number), so normalize the
        # search term the same way to make the lookup case-insensitive from
        # the visitor's point of view.
        statement = select(Shipment).where(
            Shipment.tracking_number == tracking_number.upper()
        )
        return self.session.exec(statement).first()

    def add(self, shipment: Shipment) -> Shipment:
        """Insert a shipment with no associated tracking event.

        Not actually used for shipment creation in this app (which always
        creates an initial event too - see add_with_event) but kept as a
        simple, direct insert method for completeness/symmetry with save().
        """
        # commit persists the row; refresh retrieves generated values such as id.
        self.session.add(shipment)
        self.session.commit()
        self.session.refresh(shipment)
        return shipment

    def add_with_event(
        self, shipment: Shipment, event: TrackingEvent
    ) -> Shipment:
        """Insert a new shipment together with its very first tracking event,
        as a single atomic database transaction.

        The tricky part: `event.shipment_id` needs the shipment's own
        database-generated `id`, but that id does not exist until the
        shipment row is actually written. The solution is `flush()`, which
        sends the INSERT to the database and gets the generated id back
        WITHOUT committing the transaction yet. Only after the event is
        also queued up does the function `commit()` both rows together.
        If anything fails partway through, `rollback()` undoes everything -
        it should never be possible to end up with a shipment that has no
        initial history event, or an event pointing at a shipment that
        does not exist.
        """

        try:
            self.session.add(shipment)
            # flush creates the id without completing the transaction yet.
            self.session.flush()
            if shipment.id is None:
                raise RuntimeError("Shipment id was not generated")
            event.shipment_id = shipment.id
            self.session.add(event)
            self.session.commit()
            self.session.refresh(shipment)
            return shipment
        except SQLAlchemyError:
            self.session.rollback()
            raise

    def save(self, shipment: Shipment) -> Shipment:
        """Persist changes to an already-existing shipment with no new event
        (e.g. recording that the voucher email was sent).
        """
        self.session.add(shipment)
        self.session.commit()
        self.session.refresh(shipment)
        return shipment

    def save_with_event(
        self, shipment: Shipment, event: TrackingEvent
    ) -> Shipment:
        """Update a shipment's status AND append a new history event together,
        as a single atomic transaction - either both changes are saved, or
        neither is (see the rollback behavior below). Used whenever a
        shipment's status changes (see ShipmentService.update_status).
        """

        try:
            self.session.add(shipment)
            self.session.add(event)
            self.session.commit()
            self.session.refresh(shipment)
            return shipment
        except SQLAlchemyError:
            self.session.rollback()
            raise

    def add_event(self, event: TrackingEvent) -> TrackingEvent:
        """Insert a single tracking event on its own, with no shipment update.
        Provided for completeness; the app currently always adds events
        together with a shipment change via add_with_event/save_with_event.
        """
        self.session.add(event)
        self.session.commit()
        self.session.refresh(event)
        return event

    def list_events(self, shipment_id: int) -> list[TrackingEvent]:
        """Return the full history timeline for one shipment, newest first.

        Powers both the staff shipment detail page and the public tracking
        result page.
        """
        statement = (
            select(TrackingEvent)
            .where(TrackingEvent.shipment_id == shipment_id)
            .order_by(col(TrackingEvent.created_at).desc())
        )
        return list(self.session.exec(statement).all())

    def list_for_customer(self, customer_id: int) -> list[Shipment]:
        """Return every shipment linked to a specific Customer account,
        for their personal "my shipments" dashboard view.
        """
        statement = (
            select(Shipment)
            .where(Shipment.customer_id == customer_id)
            .order_by(col(Shipment.created_at).desc())
        )
        return list(self.session.exec(statement).all())

    def link_unassigned_for_customer(
        self,
        customer_id: int,
        email: str,
    ) -> int:
        """Retroactively attach a newly-registered customer's account to any
        shipments that were created for them (by sender email) BEFORE they
        had an account at all.

        Scenario this solves: an employee creates a shipment for
        "someone@example.com" who does not yet have an account. Later, that
        person registers as a Customer using the same email. Without this
        method, those earlier shipments would remain permanently
        unassigned/invisible in the customer's dashboard. Called once, right
        after a successful registration (see app/routers/accounts.py).
        Returns how many shipments were linked, purely for informational
        purposes (e.g. to show a "N shipments were linked" message).
        """

        statement = select(Shipment).where(
            col(Shipment.customer_id).is_(None),
            # Case-insensitive comparison at the database level, since the
            # sender's email was typed freely into a form and may not match
            # the stored account email's exact casing.
            func.lower(Shipment.sender_email) == email.strip().lower(),
        )
        shipments = list(self.session.exec(statement).all())
        for shipment in shipments:
            shipment.customer_id = customer_id
            self.session.add(shipment)
        # Skip the commit entirely when there is nothing to update, avoiding
        # an unnecessary database round-trip.
        if shipments:
            self.session.commit()
        return len(shipments)
