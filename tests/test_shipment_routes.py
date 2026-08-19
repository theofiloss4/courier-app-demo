# =============================================================================
# Integration tests for app/routers/shipments.py: the staff-facing HTML
# flow for registering an in-store shipment, viewing its detail page, and
# printing voucher/receipt documents.
# =============================================================================
from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.database import get_session
from app.dependencies import require_html_staff
from app.main import app
from app.models.shipment import Shipment
from app.models.user import User, UserRole
from app.services.email_service import EmailDeliveryStatus


CSRF_TOKEN = "test-csrf-token"


def build_test_client() -> tuple[TestClient, Session]:
    # Temporary SQLite keeps route tests isolated from development PostgreSQL.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    employee = User(
        id=1,
        email="employee@example.com",
        full_name="Test Employee",
        password_hash="not-used-in-this-test",
        role=UserRole.EMPLOYEE,
    )
    session.add(employee)
    session.commit()

    def test_session() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[require_html_staff] = lambda: employee
    client = TestClient(app)
    client.cookies.set("csrf_token", CSRF_TOKEN)
    return client, session


def clear_overrides(session: Session) -> None:
    app.dependency_overrides.clear()
    session.close()


def test_new_shipment_page_explains_indicative_pos_charge():
    """The "new shipment" form page must mention "POS" - a content check
    confirming the page correctly explains that payment happens on the
    store's external point-of-sale terminal, not through this application
    (the displayed amount is indicative only - see app/models/shipment.py).
    """
    client, session = build_test_client()
    try:
        response = client.get("/shipments/new")

        assert response.status_code == 200
        assert "POS" in response.text
    finally:
        clear_overrides(session)


def test_staff_can_create_measured_in_store_shipment(monkeypatch):
    """End-to-end test of the full "staff registers a shipment" HTML flow:
    submits the form, confirms the redirect carries the email-delivery
    status in its query string, and checks the persisted Shipment row has
    the correct address, computed volumetric/chargeable weight, amount, and
    that it was automatically linked to the matching Customer account.
    `monkeypatch` replaces the real email-sending function with a fake
    "not configured" result so the test never attempts a real SMTP
    connection.
    """
    client, session = build_test_client()
    customer = User(
        email="maria@example.com",
        full_name="Maria Customer",
        password_hash="not-used",
        role=UserRole.CUSTOMER,
    )
    session.add(customer)
    session.commit()
    session.refresh(customer)
    # Unit/integration tests never make a real SMTP connection.
    monkeypatch.setattr(
        "app.routers.shipments.send_voucher_email",
        lambda _: EmailDeliveryStatus.NOT_CONFIGURED,
    )
    try:
        response = client.post(
            "/shipments",
            data={
                "sender_name": "Maria Sender",
                "sender_phone": "2101234567",
                "sender_email": "maria@example.com",
                "sender_address": "Patision 100, Athens",
                "recipient_name": "Nikos Recipient",
                "recipient_phone": "6912345678",
                "delivery_address": "Akti Miaouli 25, Piraeus",
                "parcel_description": "Books",
                "weight_kg": "2",
                "length_cm": "50",
                "width_cm": "40",
                "height_cm": "30",
                "amount_eur": "5.50",
                "notes": "",
                    "csrf_token": CSRF_TOKEN,
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "email=not_configured" in response.headers["location"]
        shipment = session.exec(select(Shipment)).one()
        assert shipment.sender_address == "Patision 100, Athens"
        assert shipment.volumetric_weight_kg == 12.0
        assert shipment.chargeable_weight_kg == 12.0
        assert shipment.amount_eur == 5.50
        assert shipment.customer_id == customer.id
    finally:
        clear_overrides(session)


def test_shipment_detail_only_offers_legal_next_statuses():
    """The shipment detail page must render form options only for statuses
    that are actually legal next steps from the shipment's current status
    (CREATED here) - "picked_up" and "cancelled" must appear, but
    "delivered" (an illegal jump) must NOT appear anywhere in the HTML,
    confirming `allowed_next_statuses` correctly drives the template.
    """
    client, session = build_test_client()
    try:
        from app.repositories.shipment_repository import ShipmentRepository
        from app.schemas.shipment import ShipmentCreate
        from app.services.shipment_service import ShipmentService

        shipment = ShipmentService(ShipmentRepository(session)).create_shipment(
            ShipmentCreate(
                sender_name="Maria Sender",
                sender_phone="2101234567",
                sender_email="maria@example.com",
                sender_address="Patision 100, Athens",
                recipient_name="Nikos Recipient",
                recipient_phone="6912345678",
                delivery_address="Akti Miaouli 25, Piraeus",
                parcel_description="Books",
                weight_kg=2,
                length_cm=50,
                width_cm=40,
                height_cm=30,
                amount_eur=5.50,
            ),
            user_id=1,
        )

        response = client.get(f"/shipments/{shipment.id}")

        assert response.status_code == 200
        assert 'value="picked_up"' in response.text
        assert 'value="cancelled"' in response.text
        assert 'value="delivered"' not in response.text
    finally:
        clear_overrides(session)


def test_missing_printable_documents_return_404():
    """Requesting the voucher or receipt page for a non-existent shipment
    id (999) must return a proper bilingual HTML 404 page, not a raw
    error or a crash.
    """
    client, session = build_test_client()
    try:
        voucher_response = client.get("/shipments/999/voucher")
        receipt_response = client.get("/shipments/999/receipt")

        assert voucher_response.status_code == 404
        assert voucher_response.headers["content-type"].startswith("text/html")
        assert "Η αποστολή που ζητήθηκε δεν βρέθηκε" in voucher_response.text
        assert receipt_response.status_code == 404
        assert receipt_response.headers["content-type"].startswith("text/html")
    finally:
        clear_overrides(session)
