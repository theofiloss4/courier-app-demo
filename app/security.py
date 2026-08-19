# =============================================================================
# Security helpers: authentication cookies + CSRF protection.
#
# This module has two related but distinct responsibilities:
#
# 1. Managing the "access_token" cookie that holds the signed JWT used to
#    recognize a logged-in user (set_access_token_cookie / clear_access_token_cookie).
#
# 2. CSRF (Cross-Site Request Forgery) protection for the server-rendered
#    HTML forms. Because the browser automatically attaches cookies to every
#    request, a malicious website could otherwise trick a logged-in user's
#    browser into submitting a form on this app without their knowledge. The
#    fix is the "double submit cookie" pattern implemented below: a random
#    token is stored both in a cookie AND embedded as a hidden field in every
#    form; a real form submission from our own pages will include both, but a
#    forged cross-site request cannot read the cookie value to copy it into
#    the hidden field.
# =============================================================================
"""CSRF protection helpers for server-rendered HTML forms."""

import secrets
from collections.abc import Awaitable, Callable
from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, Form, Request, Response, status

from app.config import get_settings
from app.errors import HtmlError


CSRF_COOKIE_NAME = "csrf_token"
ACCESS_TOKEN_COOKIE_NAME = "access_token"


def set_access_token_cookie(response: Response, token: str) -> None:
    """Attach the signed JWT session token to the browser as a secure cookie.

    Called after a successful login (see app/routers/auth.py). Key flags:
    - httponly=True: JavaScript in the browser cannot read this cookie,
      which blocks a large class of XSS-based token theft.
    - secure=<setting>: when True, the browser only sends the cookie over
      HTTPS. Disabled locally (COOKIE_SECURE=false) so plain HTTP works
      during development, but must be True in production.
    - samesite="lax": the cookie is not sent on cross-site requests
      initiated by other sites, which helps prevent CSRF at the browser level.
    """

    settings = get_settings()
    response.set_cookie(
        ACCESS_TOKEN_COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
    )


def clear_access_token_cookie(response: Response) -> None:
    """Delete the session cookie, effectively logging the user out client-side.

    The delete_cookie flags must match the flags used in set_access_token_cookie,
    otherwise some browsers will not recognize it as the same cookie to remove.
    """

    response.delete_cookie(
        ACCESS_TOKEN_COOKIE_NAME,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
    )


async def csrf_cookie_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """FastAPI middleware that runs on every request to manage the CSRF cookie.

    Middleware differs from a route dependency: it wraps ALL requests, not
    just ones on a specific route. Here it does two things:

    1. Reads the existing "csrf_token" cookie, or generates a new random one
       with `secrets.token_urlsafe` if the visitor has none yet.
    2. Stores that token on `request.state.csrf_token` so Jinja2 templates
       can read it (via `request.state.csrf_token`) and embed it as a hidden
       `<input>` in every `<form>`.

    The cookie itself is only (re)written to the response when it did not
    already exist, avoiding an unnecessary Set-Cookie header on every request.
    """

    token = request.cookies.get(CSRF_COOKIE_NAME)
    created = token is None
    if token is None:
        token = secrets.token_urlsafe(32)
    request.state.csrf_token = token

    # Let the actual route handler run and produce its response first.
    response = await call_next(request)
    if created:
        response.set_cookie(
            CSRF_COOKIE_NAME,
            token,
            httponly=True,
            secure=get_settings().cookie_secure,
            samesite="lax",
        )
    return response


def validate_csrf_token(
    request: Request,
    csrf_token: Annotated[str | None, Form()] = None,
) -> None:
    """FastAPI dependency that enforces CSRF protection on a state-changing route.

    Compares the token embedded in the submitted form (`csrf_token` form
    field) against the token stored in the visitor's cookie. They must both
    be present and match exactly, or the request is rejected with 403.

    `compare_digest` (instead of `==`) is used deliberately: it runs in
    constant time regardless of where the strings first differ, which
    prevents a timing-attack from being used to guess the correct token
    character by character.

    Any route that changes server state (POST endpoints that create/update/
    delete data) should include `_: CsrfProtection` in its parameters to run
    this check automatically.
    """

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if (
        not cookie_token
        or not csrf_token
        or not compare_digest(cookie_token, csrf_token)
    ):
        raise HtmlError(
            status.HTTP_403_FORBIDDEN,
            "Security validation failed. Reload the form and try again.",
            "Ο έλεγχος ασφαλείας απέτυχε. Ανανεώστε τη φόρμα και δοκιμάστε ξανά.",
            "Security check failed",
            "Αποτυχία ελέγχου ασφαλείας",
        )


# Type alias used in route signatures, e.g. `_: CsrfProtection`, to run the
# validate_csrf_token dependency without needing its return value (it either
# passes silently or raises HtmlError).
CsrfProtection = Annotated[None, Depends(validate_csrf_token)]
