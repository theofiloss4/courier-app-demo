# =============================================================================
# Unit tests for ShipmentService and the ShipmentCreate validation schema.
#
# These are "unit" tests in the strict sense: they exercise the business
# logic layer directly (ShipmentService + ShipmentRepository), completely
# bypassing HTTP/FastAPI. Each test gets its own brand-new, isolated
# in-memory SQLite database (see build_session below), so tests never touch
# the real development PostgreSQL database, never depend on each other's
# data, and can run in any order or in parallel safely.
# =============================================================================
import pytest
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.shipment import ShipmentStatus
from app.models.user import User, UserRole
from app.repositories.shipment_repository import ShipmentRepository
from app.schemas.shipment import ShipmentCreate, ShipmentStatusUpdate
from app.services.auth_service import hash_password
from app.services.shipment_service import (
    InvalidStatusTransitionError,
    ShipmentService,
)


def build_session() -> Session:
    """Create a fresh in-memory SQLite database with one seed Employee user.

    `StaticPool` keeps a SINGLE connection alive for the whole in-memory
    database's lifetime (normally each new connection to "sqlite://" would
    get its own separate, empty database) - this is what makes the schema
    created here actually persist across multiple queries within one test.
    A single Employee user is seeded because shipments always require a
    valid `created_by_id` foreign key.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    session.add(
        User(
            email="employee@example.com",
            full_name="Test Employee",
            password_hash=hash_password("secret"),
            role=UserRole.EMPLOYEE,
        )
    )
    session.commit()
    return session


def sample_data() -> ShipmentCreate:
    """A known-valid ShipmentCreate payload, reused as a baseline by every
    test below. Individual tests only override the one field they are
    actually testing (via `.model_copy(update={...})` or by editing the
    `.model_dump()` dict), which keeps each test focused on a single
    behavior instead of repeating every field.
    """
    return ShipmentCreate(
        sender_name="Maria Sender",
        sender_phone="2100000000",
        sender_email="maria@example.com",
        recipient_name="Nikos Recipient",
        recipient_phone="6900000000",
        sender_address="Patision 100, Athens",
        delivery_address="Akti Miaouli 25, Piraeus",
        parcel_description="Books",
        weight_kg=2.0,
        length_cm=50,
        width_cm=40,
        height_cm=30,
        amount_eur=5.50,
    )


def test_create_shipment_adds_initial_tracking_event():
    """Creating a shipment must also create exactly one TrackingEvent
    (status CREATED) in the same operation - a shipment's history should
    never start out empty.
    """
    # Arrange - Act - Assert: setup, action, verification.
    session = build_session()
    service = ShipmentService(ShipmentRepository(session))

    shipment = service.create_shipment(sample_data(), user_id=1)
    tracked, events = service.track(shipment.tracking_number)

    assert tracked.status == ShipmentStatus.CREATED
    assert shipment.tracking_number.startswith("CF-")
    assert len(events) == 1
    assert events[0].status == ShipmentStatus.CREATED


def test_status_transition_updates_shipment_and_history():
    """A legal status change (CREATED -> PICKED_UP) must update the
    shipment's current status AND append a second history event, without
    removing or altering the first one.
    """
    session = build_session()
    service = ShipmentService(ShipmentRepository(session))
    shipment = service.create_shipment(sample_data(), user_id=1)

    updated = service.update_status(
        shipment.id or 0,
        ShipmentStatusUpdate(
            status=ShipmentStatus.PICKED_UP,
            location="Athens Hub",
        ),
        user_id=1,
    )

    assert updated.status == ShipmentStatus.PICKED_UP
    assert len(service.track(shipment.tracking_number)[1]) == 2


def test_invalid_status_transition_is_rejected():
    """The state machine (ALLOWED_TRANSITIONS) must reject an illegal jump
    such as CREATED straight to DELIVERED, skipping every intermediate step.
    """
    session = build_session()
    service = ShipmentService(ShipmentRepository(session))
    shipment = service.create_shipment(sample_data(), user_id=1)

    try:
        service.update_status(
            shipment.id or 0,
            ShipmentStatusUpdate(status=ShipmentStatus.DELIVERED),
            user_id=1,
        )
    except InvalidStatusTransitionError:
        pass
    else:
        raise AssertionError("Expected InvalidStatusTransitionError")


@pytest.mark.parametrize(
    "phone",
    [
        "2101234567",
        "6912345678",
        "+30 210 123 4567",
        "0030 6912345678",
    ],
)
def test_valid_greek_phone_numbers_are_accepted(phone: str):
    """Every accepted input format (plain 10-digit, +30 prefix, 0030
    prefix, with spaces) must normalize down to a clean 10-digit number -
    confirms validate_greek_phone in app/schemas/shipment.py handles all
    the formats a real user might type.
    """
    data = sample_data().model_copy(update={"sender_phone": phone})

    validated = ShipmentCreate.model_validate(data.model_dump())

    assert len(validated.sender_phone) == 10


@pytest.mark.parametrize("phone", ["12345", "6812345678", "21012345678", "abc"])
def test_invalid_phone_numbers_are_rejected(phone: str):
    """Too short, wrong mobile prefix (68 instead of 69), too long, and
    non-numeric input must all be rejected by ShipmentCreate validation.
    """
    values = sample_data().model_dump()
    values["sender_phone"] = phone

    with pytest.raises(ValidationError):
        ShipmentCreate(**values)


def test_address_without_street_number_is_rejected():
    """An address containing no digit at all (no street number) must fail
    validation - a courier cannot deliver to an address with no number.
    """
    values = sample_data().model_dump()
    values["delivery_address"] = "Akti Miaouli, Piraeus"

    with pytest.raises(ValidationError):
        ShipmentCreate(**values)


def test_customer_link_returns_only_customer_shipments():
    """`list_for_customer` must return ONLY shipments explicitly linked to
    that customer_id, excluding any other shipment even if it was created
    by the same staff member around the same time.
    """
    session = build_session()
    repository = ShipmentRepository(session)
    service = ShipmentService(repository)

    linked = service.create_shipment(sample_data(), user_id=1, customer_id=1)
    service.create_shipment(sample_data(), user_id=1)

    customer_shipments = repository.list_for_customer(1)

    assert [shipment.id for shipment in customer_shipments] == [linked.id]


def test_volumetric_weight_is_used_when_it_is_greater():
    """A large-but-light parcel (50x40x30cm, 2kg) must be billed using its
    volumetric weight (50*40*30/5000 = 12kg), since that exceeds its actual
    weight - confirms the "larger of the two" chargeable-weight rule.
    """
    session = build_session()
    service = ShipmentService(ShipmentRepository(session))

    shipment = service.create_shipment(sample_data(), user_id=1)

    assert shipment.volumetric_weight_kg == 12.0
    assert shipment.chargeable_weight_kg == 12.0


def test_actual_weight_is_used_when_it_is_greater():
    """A dense, heavy parcel (same dimensions but 15kg) must be billed
    using its actual weight instead, since 15kg exceeds the 12kg
    volumetric weight for these dimensions.
    """
    session = build_session()
    service = ShipmentService(ShipmentRepository(session))
    values = sample_data().model_dump()
    values["weight_kg"] = 15.0

    shipment = service.create_shipment(ShipmentCreate(**values), user_id=1)

    assert shipment.volumetric_weight_kg == 12.0
    assert shipment.chargeable_weight_kg == 15.0
