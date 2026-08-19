# =============================================================================
# HTML routes for login and logout — the entry/exit points of a browser
# session. Registration and profile management live in
# app/routers/accounts.py instead; this file only handles the credential
# check and issuing/clearing the session cookie.
# =============================================================================
from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.dependencies import SessionDep
from app.i18n import template_context
from app.models.user import UserRole
from app.repositories.user_repository import UserRepository
from app.security import (
    CsrfProtection,
    clear_access_token_cookie,
    set_access_token_cookie,
)
from app.services.auth_service import AuthService


router = APIRouter(tags=["authentication"])
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    """GET /login — display the empty login form.

    Auth: none required (this page must be reachable while logged out).
    Read-only: a GET request never changes server state, so no CSRF
    protection is needed here (only the POST below needs it).
    """
    return templates.TemplateResponse(request, "auth/login.html", template_context(request))


@router.post("/login")
def login(
    request: Request,
    session: SessionDep,
    _: CsrfProtection,
    email: str = Form(),
    password: str = Form(),
):
    """POST /login — verify credentials and start a session.

    Auth: none required to call this (that is the point of a login form),
    but CSRF-protected (`_: CsrfProtection`) so a malicious third-party
    page cannot forge a login submission on a visitor's behalf.
    On failure: re-renders the login form with a bilingual error message
    and HTTP 401 — deliberately vague ("email or password is incorrect")
    rather than saying which one was wrong, to avoid confirming whether a
    given email has an account.
    On success: issues a signed JWT (via AuthService.create_access_token)
    stored in an HttpOnly cookie, then redirects — Customers land on their
    own dashboard, all staff roles land on the shipment management screen.
    """
    # The service handles the database lookup and password verification.
    service = AuthService(UserRepository(session))
    user = service.authenticate(email, password)
    if not user:
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            template_context(
                request,
                error="Το email ή ο κωδικός πρόσβασης δεν είναι σωστός."
                if request.cookies.get("language", "el") == "el"
                else "The email or password is incorrect.",
            ),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Customers return home, while staff members enter shipment management.
    destination = "/account" if user.role == UserRole.CUSTOMER else "/shipments"
    response = RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
    set_access_token_cookie(response, service.create_access_token(user))
    return response


@router.post("/logout")
def logout(_: CsrfProtection):
    """POST /logout — end the current session.

    Auth: implicitly, only a logged-in user would normally hit this (a
    logout link/button only appears in the UI once logged in), but the
    route itself does not require a valid session to run — it simply clears
    whatever access_token cookie is present. CSRF-protected so a third-party
    page cannot silently log a visitor out.
    """
    # Deleting the cookie ends the local authenticated session.
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    clear_access_token_cookie(response)
    return response
