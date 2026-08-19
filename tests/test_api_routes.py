# =============================================================================
# Integration tests for the JSON REST API (app/routers/api.py), the same
# endpoints documented and callable through Swagger UI at /docs.
# =============================================================================
from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.database import get_session
from app.dependencies import require_staff
from app.main import app
from app.models.shipment import Shipment
from app.models.user import User, UserRole
from app.services.email_service import EmailDeliveryStatus


def build_test_client() -> tuple[TestClient, Session]:
    """Set up an isolated in-memory database and a TestClient that is
    ALREADY authenticated as a fixed Employee user.

    Overriding the `require_staff` dependency directly (rather than going
    through a real /login POST like test_auth_security.py does) is a
    shortcut appropriate here because these tests are about the shipment
    API's behavior, not about the login flow itself - authentication is
    already covered separately.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    employee = User(
        id=1,
        email="api.employee@example.com",
        full_name="API Employee",
        password_hash="not-used",
        role=UserRole.EMPLOYEE,
    )
    session.add(employee)
    session.commit()

    def test_session() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[require_staff] = lambda: employee
    return TestClient(app), session


def test_api_creates_shipment_and_attempts_email(monkeypatch):
    """POST /api/shipments must create the shipment, automatically link it
    to the matching Customer account by sender email, attempt to send the
    voucher email, and record `email_sent_at` when that email "succeeds".

    `monkeypatch.setattr` replaces the real `send_voucher_email` function
    with a fake that just records which tracking number it was called
    with and reports success - this avoids the test depending on a real
    SMTP server (there is none in a unit-test environment) while still
    verifying the API route actually calls the email step at all.
    """
    client, session = build_test_client()
    email_calls: list[str] = []
    customer = User(
        email="sender@example.com",
        full_name="Linked Customer",
        password_hash="not-used",
        role=UserRole.CUSTOMER,
    )
    session.add(customer)
    session.commit()
    session.refresh(customer)

    def fake_email(shipment: Shipment) -> EmailDeliveryStatus:
        email_calls.append(shipment.tracking_number)
        return EmailDeliveryStatus.SENT

    monkeypatch.setattr("app.routers.api.send_voucher_email", fake_email)
    try:
        response = client.post(
            "/api/shipments",
            json={
                "sender_name": "API Sender",
                "sender_phone": "2101234567",
                "sender_email": "sender@example.com",
                "sender_address": "Patision 100, Athens",
                "recipient_name": "API Recipient",
                "recipient_phone": "6912345678",
                "delivery_address": "Akti Miaouli 25, Piraeus",
                "parcel_description": "Documents",
                "weight_kg": 1.5,
                "length_cm": 30,
                "width_cm": 20,
                "height_cm": 10,
                "amount_eur": 4.5,
            },
        )

        assert response.status_code == 201
        shipment = session.exec(select(Shipment)).one()
        assert email_calls == [shipment.tracking_number]
        assert shipment.email_sent_at is not None
        assert shipment.customer_id == customer.id
    finally:
        app.dependency_overrides.clear()
        session.close()
