# =============================================================================
# Integration tests for app/routers/accounts.py: public customer
# registration, profile validation, and the staff-management permission
# hierarchy (who can enable/disable which other staff accounts).
# =============================================================================
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.database import get_session
from app.dependencies import require_html_customer, require_html_staff_manager
from app.main import app
from app.models.shipment import Shipment
from app.models.user import User, UserRole
from app.routers.accounts import can_manage_staff


CSRF_TOKEN = "test-csrf-token"


def build_test_client() -> tuple[TestClient, Session]:
    """Fresh in-memory database + TestClient with no pre-authenticated user
    (unlike test_shipment_routes.py/test_api_routes.py) - registration
    itself must work for a completely anonymous visitor.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    def test_session() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_session] = test_session
    client = TestClient(app)
    client.cookies.set("csrf_token", CSRF_TOKEN)
    return client, session


def clear_overrides(session: Session) -> None:
    app.dependency_overrides.clear()
    session.close()


def test_valid_registration_creates_customer():
    """A valid registration form submission must create a User row with
    role=CUSTOMER, and the stored password_hash must NOT equal the
    plaintext password (confirming it was actually hashed, not stored raw).
    """
    client, session = build_test_client()
    try:
        response = client.post(
            "/register",
            data={
                "full_name": "Maria Customer",
                "email": "maria@example.com",
                "password": "customer123",
                "csrf_token": CSRF_TOKEN,
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        customer = session.exec(select(User)).one()
        assert customer.role == UserRole.CUSTOMER
        assert customer.password_hash != "customer123"
    finally:
        clear_overrides(session)


def test_invalid_email_is_rejected():
    """A malformed email must be rejected with 422 and, critically, no User
    row should be created at all - a failed validation must not have any
    side effect on the database.
    """
    client, session = build_test_client()
    try:
        response = client.post(
            "/register",
            data={
                "full_name": "Maria Customer",
                "email": "not-an-email",
                "password": "customer123",
                "csrf_token": CSRF_TOKEN,
            },
        )

        assert response.status_code == 422
        assert session.exec(select(User)).first() is None
    finally:
        clear_overrides(session)


@pytest.mark.parametrize(
    ("manager_role", "target_role", "expected"),
    [
        (UserRole.ADMIN, UserRole.EMPLOYEE, True),
        (UserRole.ADMIN, UserRole.ADMIN, False),
        (UserRole.ADMIN, UserRole.SUPERVISOR, False),
        (UserRole.SUPERVISOR, UserRole.EMPLOYEE, True),
        (UserRole.SUPERVISOR, UserRole.ADMIN, True),
        (UserRole.SUPERVISOR, UserRole.SUPERVISOR, False),
    ],
)
def test_staff_management_boundaries(
    manager_role: UserRole,
    target_role: UserRole,
    expected: bool,
):
    """Table-driven test of the entire can_manage_staff permission matrix
    from app/routers/accounts.py: an Admin may manage an Employee but not
    another Admin or a Supervisor; a Supervisor may manage both Employees
    and Admins but not another Supervisor. Every combination in the
    hierarchy is checked in one parametrized test instead of six separate
    near-duplicate test functions.
    """
    manager = User(
        id=1,
        email="manager@example.com",
        full_name="Manager",
        password_hash="unused",
        role=manager_role,
    )
    target = User(
        id=2,
        email="target@example.com",
        full_name="Target",
        password_hash="unused",
        role=target_role,
    )

    assert can_manage_staff(manager, target) is expected


def test_profile_rejects_address_longer_than_database_limit():
    """An address longer than the database column's max_length (255 chars,
    see app/models/user.py) must be rejected with 422 BEFORE it ever
    reaches the database - confirms the profile update route validates
    length itself rather than relying on the database to reject it (which
    would surface as an ugly 500 error instead of a clean validation message).
    """
    client, session = build_test_client()
    customer = User(
        email="profile@example.com",
        full_name="Profile Customer",
        password_hash="unused",
        role=UserRole.CUSTOMER,
    )
    session.add(customer)
    session.commit()
    session.refresh(customer)
    app.dependency_overrides[require_html_customer] = lambda: customer
    try:
        response = client.post(
            "/account/profile",
            data={
                "full_name": "Profile Customer",
                "phone": "6912345678",
                "address": f"{'A' * 255} 1",
                "csrf_token": CSRF_TOKEN,
            },
        )

        assert response.status_code == 422
        session.refresh(customer)
        assert customer.address is None
    finally:
        clear_overrides(session)


def test_admin_cannot_toggle_another_admin():
    """End-to-end (HTTP-level) confirmation of the can_manage_staff rule:
    an Admin trying to enable/disable another Admin account via POST
    /staff/{id}/toggle must get 403, and the target account's is_active
    flag must remain completely unchanged.
    """
    client, session = build_test_client()
    manager = User(
        email="manager@example.com",
        full_name="Admin Manager",
        password_hash="unused",
        role=UserRole.ADMIN,
    )
    target = User(
        email="target@example.com",
        full_name="Admin Target",
        password_hash="unused",
        role=UserRole.ADMIN,
    )
    session.add_all([manager, target])
    session.commit()
    session.refresh(target)
    app.dependency_overrides[require_html_staff_manager] = lambda: manager
    try:
        response = client.post(
            f"/staff/{target.id}/toggle",
            data={"csrf_token": CSRF_TOKEN},
            follow_redirects=False,
        )

        assert response.status_code == 403
        assert response.headers["content-type"].startswith("text/html")
        assert "Δεν μπορείτε να τροποποιήσετε" in response.text
        session.refresh(target)
        assert target.is_active is True
    finally:
        clear_overrides(session)


def test_registration_links_earlier_shipments_by_sender_email():
    """If a shipment was already created (by staff) for a sender email
    BEFORE that person had an account, registering with that same email
    must retroactively link the earlier shipment to the new account -
    confirms ShipmentRepository.link_unassigned_for_customer is actually
    invoked as part of the /register flow (see create_user_response in
    app/routers/accounts.py).
    """
    client, session = build_test_client()
    employee = User(
        email="employee@example.com",
        full_name="Employee",
        password_hash="unused",
        role=UserRole.EMPLOYEE,
    )
    session.add(employee)
    session.commit()
    session.refresh(employee)
    shipment = Shipment(
        tracking_number="CF-EARLIER01",
        sender_name="History Customer",
        sender_phone="6912345678",
        sender_email="history@example.com",
        recipient_name="Recipient",
        recipient_phone="2101234567",
        sender_address="Patision 100, Athens",
        delivery_address="Akti Miaouli 25, Piraeus",
        created_by_id=employee.id or 0,
    )
    session.add(shipment)
    session.commit()
    session.refresh(shipment)
    try:
        response = client.post(
            "/register",
            data={
                "full_name": "History Customer",
                "email": "history@example.com",
                "password": "customer123",
                "csrf_token": CSRF_TOKEN,
            },
            follow_redirects=False,
        )
        customer = session.exec(
            select(User).where(User.email == "history@example.com")
        ).one()
        session.refresh(shipment)

        assert response.status_code == 303
        assert shipment.customer_id == customer.id
    finally:
        clear_overrides(session)


def test_supervisor_can_toggle_admin():
    """The positive counterpart to test_admin_cannot_toggle_another_admin:
    a Supervisor (unlike an Admin) IS allowed to disable an Admin account,
    and the request must actually flip is_active to False.
    """
    client, session = build_test_client()
    manager = User(
        email="supervisor@example.com",
        full_name="Supervisor",
        password_hash="unused",
        role=UserRole.SUPERVISOR,
    )
    target = User(
        email="admin@example.com",
        full_name="Admin Target",
        password_hash="unused",
        role=UserRole.ADMIN,
    )
    session.add_all([manager, target])
    session.commit()
    session.refresh(target)
    app.dependency_overrides[require_html_staff_manager] = lambda: manager
    try:
        response = client.post(
            f"/staff/{target.id}/toggle",
            data={"csrf_token": CSRF_TOKEN},
            follow_redirects=False,
        )

        assert response.status_code == 303
        session.refresh(target)
        assert target.is_active is False
    finally:
        clear_overrides(session)
