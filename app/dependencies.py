# =============================================================================
# Reusable FastAPI dependencies: database session, login checks, role checks.
#
# This is the heart of the app's authentication/authorization system. FastAPI
# "dependencies" are functions that run automatically before a route handler,
# and their return value is injected as a parameter. By declaring a
# parameter's type as one of the aliases defined at the bottom of this file
# (e.g. `user: HtmlStaffUser`), a route gets login + role enforcement for
# free, without writing any `if` checks inside the route body itself.
#
# There are two parallel "tracks" of dependencies here because the app
# serves two different kinds of clients that must fail differently when
# unauthenticated:
#   - REST API clients (app/routers/api.py) should get a 401/403 JSON error.
#   - Browser page requests should be redirected to the /login page instead
#     of showing a raw error.
# =============================================================================
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Security, status
from fastapi.security import APIKeyCookie
from sqlmodel import Session

from app.database import get_session
from app.errors import HtmlError
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService


# Type alias that automatically requests a database Session from FastAPI.
# Any route/dependency that needs to query the database declares a
# parameter as `session: SessionDep` instead of writing the Depends() call
# out by hand every time.
SessionDep = Annotated[Session, Depends(get_session)]
# APIKeyCookie reads a named cookie and, because auto_error=False, does NOT
# automatically raise an error when the cookie is missing - that decision is
# left to the dependency functions below, since the two tracks (API vs HTML)
# need to react differently to a missing cookie.
access_token_cookie = APIKeyCookie(name="access_token", auto_error=False)
AccessTokenCookie = Annotated[str | None, Security(access_token_cookie)]
OptionalAccessTokenCookie = Annotated[
    str | None,
    Cookie(alias="access_token"),
]


class LoginRequiredError(Exception):
    """Raised by HTML routes to signal 'redirect the browser to /login'.

    This is caught by a dedicated exception handler in app/main.py
    (`redirect_to_login`) which converts it into an HTTP 303 redirect
    response. It intentionally carries no message - it is a pure control-flow
    signal, not a user-facing error.
    """


def get_optional_user(
    session: SessionDep,
    access_token: OptionalAccessTokenCookie = None,
) -> User | None:
    """Look up the current user WITHOUT requiring them to be logged in.

    Used on public pages (e.g. the home page, tracking search) that render
    slightly different content for logged-in visitors (e.g. showing their
    name in the header) but must still work for anonymous visitors.
    Returns None whenever there is no cookie, the token is invalid/expired,
    the account was deactivated, or the token was issued before the user's
    last password change (see AuthService.token_matches_user).
    """
    if not access_token:
        return None
    service = AuthService(UserRepository(session))
    user_id = service.decode_user_id(access_token)
    user = session.get(User, user_id) if user_id else None
    return (
        user
        if user
        and user.is_active
        and service.token_matches_user(access_token, user)
        else None
    )


def get_current_user(
    session: SessionDep,
    access_token: AccessTokenCookie,
) -> User:
    """Look up the current user and REQUIRE that they are logged in.

    This is the "strict" counterpart to get_optional_user, intended for the
    JSON REST API (see app/routers/api.py) where a missing/invalid token
    should immediately produce an HTTP 401 Unauthorized JSON response,
    rather than a browser redirect.
    """
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    service = AuthService(UserRepository(session))
    user_id = service.decode_user_id(access_token)
    user = session.get(User, user_id) if user_id else None
    if (
        not user
        or not user.is_active
        or not service.token_matches_user(access_token, user)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


# These aliases keep route signatures short and readable, e.g.
# `def my_route(user: CurrentUser): ...` instead of repeating
# `Depends(get_current_user)` everywhere.
CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def get_current_html_user(user: OptionalUser) -> User:
    """Require login for an HTML page, redirecting instead of erroring.

    Notice this depends on `OptionalUser` (which never raises) and only then
    decides to raise `LoginRequiredError` if no user was found. This keeps
    the "was there a valid cookie?" logic in one place (get_optional_user)
    while letting HTML and API routes react differently to the result.
    """

    if user is None:
        raise LoginRequiredError
    return user


CurrentHtmlUser = Annotated[User, Depends(get_current_html_user)]


def require_staff(user: CurrentUser) -> User:
    """API-track guard: allow any logged-in user EXCEPT a Customer.

    Used by REST API routes that only staff (Employee/Admin/Supervisor)
    should be able to call, such as creating shipments programmatically.
    """
    if user.role == UserRole.CUSTOMER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user


StaffUser = Annotated[User, Depends(require_staff)]


def require_html_staff(user: CurrentHtmlUser) -> User:
    """HTML-track guard: allow any logged-in user EXCEPT a Customer.

    The HTML equivalent of require_staff above - raises a bilingual
    HtmlError (rendered as a friendly error page) instead of a bare 403.
    """
    if user.role == UserRole.CUSTOMER:
        raise HtmlError(
            status.HTTP_403_FORBIDDEN,
            "This page is available to staff accounts only.",
            "Η σελίδα είναι διαθέσιμη μόνο σε λογαριασμούς προσωπικού.",
            "Access denied",
            "Δεν επιτρέπεται η πρόσβαση",
        )
    return user


def require_html_staff_manager(user: CurrentHtmlUser) -> User:
    """HTML-track guard: only Admin or Supervisor may manage staff accounts.

    Employees can create shipments but must NOT be able to create or
    deactivate other staff accounts - only Admin/Supervisor roles can.
    """
    if user.role not in {UserRole.ADMIN, UserRole.SUPERVISOR}:
        raise HtmlError(
            status.HTTP_403_FORBIDDEN,
            "You do not have permission to manage staff accounts.",
            "Δεν έχετε δικαίωμα διαχείρισης λογαριασμών προσωπικού.",
            "Access denied",
            "Δεν επιτρέπεται η πρόσβαση",
        )
    return user


def require_html_customer(user: CurrentHtmlUser) -> User:
    """HTML-track guard: only Customer accounts may access this page.

    Used for pages like the customer dashboard/profile, which make no sense
    for a staff account to view (staff have their own separate pages).
    """
    if user.role != UserRole.CUSTOMER:
        raise HtmlError(
            status.HTTP_403_FORBIDDEN,
            "This page is available to customer accounts only.",
            "Η σελίδα είναι διαθέσιμη μόνο σε λογαριασμούς πελατών.",
            "Access denied",
            "Δεν επιτρέπεται η πρόσβαση",
        )
    return user


# Final short aliases used directly in router function signatures.
HtmlStaffUser = Annotated[User, Depends(require_html_staff)]
HtmlStaffManager = Annotated[User, Depends(require_html_staff_manager)]
HtmlCustomerUser = Annotated[User, Depends(require_html_customer)]
