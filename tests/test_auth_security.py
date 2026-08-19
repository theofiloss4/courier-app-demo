# =============================================================================
# Integration tests for authentication, authorization, and CSRF controls.
#
# Unlike test_shipment_service.py (pure business-logic unit tests), these
# tests drive the app through FastAPI's `TestClient`, which sends real
# HTTP-like requests through the full middleware/dependency/router stack -
# closer to how a real browser or API client would interact with the app.
# Each test still uses its own isolated in-memory SQLite database (see
# build_test_client below), swapped in via FastAPI's `dependency_overrides`
# mechanism instead of a real PostgreSQL connection.
# =============================================================================
"""Integration tests for authentication, authorization, and CSRF controls."""

import re
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.config import BASE_DIR, Settings
from app.database import get_session
from app.i18n import TRANSLATIONS
from app.main import app
from app.models.user import User, UserRole
from app.routers.shipments import STATUS_LABELS
from app.repositories.user_repository import UserRepository
from app.services.auth_service import hash_password


CSRF_TOKEN = "test-csrf-token"


def build_test_client(*users: User) -> tuple[TestClient, Session]:
    """Spin up a FastAPI TestClient wired to a fresh in-memory SQLite database
    pre-populated with the given users.

    `app.dependency_overrides[get_session]` is FastAPI's built-in mechanism
    for replacing a dependency during tests - every route that would
    normally connect to the real PostgreSQL database via `get_session`
    instead receives this test-only in-memory session. A fixed CSRF cookie
    is also pre-set on the client so POST requests in these tests do not
    each need to fetch a token first.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    session.add_all(users)
    session.commit()

    def test_session() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_session] = test_session
    client = TestClient(app)
    client.cookies.set("csrf_token", CSRF_TOKEN)
    return client, session


def clear_overrides(session: Session) -> None:
    """Undo build_test_client's dependency override and close the SQLite
    session, so state from one test can never leak into the next.
    """
    app.dependency_overrides.clear()
    session.close()


def make_user(email: str, role: UserRole) -> User:
    """Build a User with a real (hashed) password of "password123" for a
    given role - a convenience shared by nearly every test in this file.
    """
    return User(
        email=email,
        full_name=f"Test {role.value.title()}",
        password_hash=hash_password("password123"),
        role=role,
    )


def login(client: TestClient, email: str) -> str:
    """Log in as the given user through the real /login endpoint, assert it
    succeeded, and return the resulting access_token cookie value for tests
    that need to inspect or manipulate it directly.
    """
    response = client.post(
        "/login",
        data={
            "email": email,
            "password": "password123",
            "csrf_token": CSRF_TOKEN,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    token = client.cookies.get("access_token")
    assert token
    return token


def test_login_sets_protected_session_cookie():
    """A successful login must set the access_token cookie with all three
    security flags (HttpOnly, SameSite=Lax, an expiry) and redirect staff
    to /shipments. Also confirms email is trimmed of surrounding whitespace
    before the lookup (the leading/trailing spaces around employee.email).
    """
    employee = make_user("employee@example.com", UserRole.EMPLOYEE)
    client, session = build_test_client(employee)
    try:
        response = client.post(
            "/login",
            data={
                "email": f"  {employee.email}  ",
                "password": "password123",
                "csrf_token": CSRF_TOKEN,
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/shipments"
        cookie = response.headers["set-cookie"].lower()
        assert "access_token=" in cookie
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert "max-age=" in cookie
    finally:
        clear_overrides(session)


def test_html_redirects_to_login_while_api_returns_401():
    """The two authentication "tracks" described in app/dependencies.py must
    behave differently for an anonymous visitor: an HTML page redirects
    (303 to /login), while the JSON API returns a plain 401, never a redirect.
    """
    client, session = build_test_client()
    try:
        html_response = client.get("/shipments", follow_redirects=False)
        api_response = client.get("/api/shipments")

        assert html_response.status_code == 303
        assert html_response.headers["location"] == "/login"
        assert api_response.status_code == 401
    finally:
        clear_overrides(session)


def test_customer_cannot_access_staff_api():
    """A logged-in Customer (not just an anonymous visitor) must be blocked
    with 403 from both the staff HTML pages and the staff JSON API, each
    returning the correct content-type for its track (JSON vs HTML error page).
    """
    customer = make_user("customer@example.com", UserRole.CUSTOMER)
    client, session = build_test_client(customer)
    try:
        login(client, customer.email)

        api_response = client.get("/api/shipments")
        html_response = client.get("/shipments")

        assert api_response.status_code == 403
        assert api_response.headers["content-type"].startswith("application/json")
        assert html_response.status_code == 403
        assert html_response.headers["content-type"].startswith("text/html")
        assert "Η σελίδα είναι διαθέσιμη μόνο" in html_response.text
    finally:
        clear_overrides(session)


def test_html_form_rejects_missing_csrf_token():
    """A POST /login submitted WITHOUT the csrf_token form field must be
    rejected with 403 by validate_csrf_token (app/security.py), even though
    the email/password themselves would otherwise be valid.
    """
    employee = make_user("employee@example.com", UserRole.EMPLOYEE)
    client, session = build_test_client(employee)
    try:
        response = client.post(
            "/login",
            data={"email": employee.email, "password": "password123"},
        )

        assert response.status_code == 403
        assert response.headers["content-type"].startswith("text/html")
        assert "Ο έλεγχος ασφαλείας απέτυχε" in response.text
    finally:
        clear_overrides(session)


def test_password_change_invalidates_existing_token():
    """After a password is changed directly in the database (simulating an
    admin reset or the customer's own change), a JWT issued BEFORE that
    change must stop working - it should be treated as logged-out and
    redirect to /login, proving the "password_version" fingerprint check in
    AuthService.token_matches_user actually takes effect.
    """
    customer = make_user("customer@example.com", UserRole.CUSTOMER)
    client, session = build_test_client(customer)
    try:
        old_token = login(client, customer.email)
        stored_customer = session.exec(
            select(User).where(User.email == customer.email)
        ).one()
        stored_customer.password_hash = hash_password("new-password123")
        UserRepository(session).save(stored_customer)
        client.cookies.set("access_token", old_token)

        response = client.get("/account", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    finally:
        clear_overrides(session)


def test_password_change_reissues_current_session_token():
    """When a customer changes their OWN password through /account/password,
    the response must set a brand-new access_token cookie immediately - the
    user who just changed their password should stay logged in, not get
    logged out by their own action (see change_customer_password's docstring
    in app/routers/accounts.py for why this matters).
    """
    customer = make_user("customer@example.com", UserRole.CUSTOMER)
    client, session = build_test_client(customer)
    try:
        old_token = login(client, customer.email)

        response = client.post(
            "/account/password",
            data={
                "current_password": "password123",
                "new_password": "new-password123",
                "csrf_token": CSRF_TOKEN,
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/account?password_changed=1"
        assert client.cookies.get("access_token") != old_token
        assert client.get("/account").status_code == 200
    finally:
        clear_overrides(session)


def test_staff_password_reset_invalidates_target_sessions():
    """When a Supervisor resets an Employee's password through
    /staff/{id}/password, the EMPLOYEE's own already-issued session token
    must become invalid immediately, forcing them to log in again with the
    new password - an administratively forced password reset must actually
    kick out any existing session, not just change future logins.
    """
    supervisor = make_user("supervisor@example.com", UserRole.SUPERVISOR)
    employee = make_user("employee@example.com", UserRole.EMPLOYEE)
    client, session = build_test_client(supervisor, employee)
    try:
        employee_token = login(client, employee.email)
        login(client, supervisor.email)
        response = client.post(
            f"/staff/{employee.id}/password",
            data={
                "new_password": "replacement123",
                "csrf_token": CSRF_TOKEN,
            },
            follow_redirects=False,
        )
        client.cookies.clear()
        client.cookies.set("csrf_token", CSRF_TOKEN)
        client.cookies.set("access_token", employee_token)

        protected_response = client.get("/shipments", follow_redirects=False)

        assert response.status_code == 303
        assert protected_response.status_code == 303
        assert protected_response.headers["location"] == "/login"
    finally:
        clear_overrides(session)


def test_setup_is_locked_after_supervisor_exists():
    """Once a Supervisor account exists, POSTing to /setup again must NOT
    create a second Supervisor - it should silently redirect to /login
    instead, leaving exactly one Supervisor in the database. This is the
    critical security guarantee behind the /setup bootstrap route.
    """
    supervisor = make_user("supervisor@example.com", UserRole.SUPERVISOR)
    client, session = build_test_client(supervisor)
    try:
        response = client.post(
            "/setup",
            data={
                "full_name": "Second Supervisor",
                "email": "second@example.com",
                "password": "password123",
                "csrf_token": CSRF_TOKEN,
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        supervisors = session.exec(
            select(User).where(User.role == UserRole.SUPERVISOR)
        ).all()
        assert len(supervisors) == 1
    finally:
        clear_overrides(session)


def test_openapi_documents_cookie_authentication():
    """The auto-generated OpenAPI/Swagger schema must correctly document
    that every shipment API endpoint requires the "access_token" cookie -
    this is what makes Swagger UI show a lock icon and let a logged-in
    staff member try these endpoints interactively from /docs.
    """
    specification = app.openapi()

    assert specification["components"]["securitySchemes"]["APIKeyCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "access_token",
    }
    for path, method in [
        ("/api/shipments", "get"),
        ("/api/shipments", "post"),
        ("/api/shipments/{shipment_id}/status", "patch"),
    ]:
        assert specification["paths"][path][method]["security"] == [
            {"APIKeyCookie": []}
        ]


def test_greek_application_strings_are_valid_unicode():
    """Guards against a font/encoding regression: the Greek UI strings in
    TRANSLATIONS and STATUS_LABELS must contain correctly encoded Greek
    characters, not mis-decoded "mojibake" bytes (this project previously
    had exactly this problem when a PDF/text source used a broken encoding -
    see the "Ξ" check, a character that should never legitimately appear here).
    """
    combined = " ".join(
        [
            *TRANSLATIONS["el"].values(),
            *STATUS_LABELS.values(),
        ]
    )

    assert "Αναζήτηση αποστολής" in combined
    assert "Καταχωρίστηκε" in combined
    assert "Ξ" not in combined


def test_python_sources_do_not_contain_mojibake():
    """Scans every .py source file under app/ for the Unicode replacement
    character ("�") or the specific mis-decoded "Ξ" pattern, both signs
    that a Greek string got corrupted by a wrong text encoding at some
    point (e.g. a file saved as Latin-1 instead of UTF-8).
    """
    for source_path in BASE_DIR.rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        assert "Ξ" not in source, source_path
        assert "�" not in source, source_path


def test_all_post_forms_include_csrf_token():
    """Static/structural check across every Jinja2 template: any <form
    method="post"> must contain a hidden csrf_token field. This catches the
    easy mistake of adding a brand-new POST form later and forgetting to
    include the CSRF token, which would otherwise only surface as a
    confusing 403 the first time someone submits that form.
    """
    for template_path in (BASE_DIR / "templates").rglob("*.html"):
        template = template_path.read_text(encoding="utf-8")
        forms = re.findall(r"<form\b.*?</form>", template, flags=re.DOTALL)
        for form in forms:
            if re.search(r'method=["\']post["\']', form, flags=re.IGNORECASE):
                assert 'name="csrf_token"' in form, template_path


def test_production_rejects_insecure_session_settings():
    """Constructing Settings with environment="production" but an
    insecure/example secret_key and COOKIE_SECURE=False must raise a
    validation error at construction time - confirms the safety check in
    Settings.validate_production_security (app/config.py) actually fires.
    """
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            secret_key="short",
            cookie_secure=False,
        )


def test_health_returns_503_when_database_is_unavailable():
    """If the database connection fails, GET /health must report 503
    Service Unavailable (not crash with a raw 500 or falsely report 200) -
    confirms the try/except in app/main.py's health_check actually converts
    a database error into the correct HTTP status for monitoring tools.
    """
    class BrokenSession:
        def exec(self, _):
            raise SQLAlchemyError("database offline")

    app.dependency_overrides[get_session] = lambda: BrokenSession()
    client = TestClient(app, raise_server_exceptions=False)
    try:
        response = client.get("/health")

        assert response.status_code == 503
        assert response.json() == {"detail": "Database unavailable"}
    finally:
        app.dependency_overrides.clear()
