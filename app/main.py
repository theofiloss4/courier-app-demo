# =============================================================================
# Application entry point.
#
# This is the file Uvicorn loads (`uvicorn app.main:app`). Its job is purely
# "wiring": create the FastAPI app instance, register startup behavior,
# attach global middleware/exception handlers, and mount every feature
# router (pages, auth, accounts, tracking, shipments, the REST API). No
# business logic lives here - that all belongs in app/services/.
# =============================================================================
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

from app.config import BASE_DIR, get_settings
from app.database import create_db_and_tables
from app.dependencies import LoginRequiredError, SessionDep
from app.errors import HtmlError
from app.routers import accounts, api, auth, pages, shipments, tracking
from app.security import csrf_cookie_middleware


@asynccontextmanager
async def lifespan(_: FastAPI):
    """FastAPI startup/shutdown hook.

    Code before `yield` runs once when the server process starts (here:
    make sure the database schema exists). Code after `yield` would run on
    shutdown; this app has nothing to clean up there, so the function simply
    ends after yielding.
    """
    create_db_and_tables()
    yield


# The module-level `app` object is what Uvicorn imports and runs
# (see the `app.main:app` reference in the Dockerfile CMD and README).
settings = get_settings()
templates = Jinja2Templates(directory=BASE_DIR / "templates")
app = FastAPI(
    title=settings.app_name,
    description="Courier shipment management and public tracking API.",
    version="0.1.0",
    lifespan=lifespan,
)
# Registers the CSRF-token-management middleware (see app/security.py) to
# run on every single incoming request, before any router handles it.
app.middleware("http")(csrf_cookie_middleware)


@app.exception_handler(LoginRequiredError)
async def redirect_to_login(
    _: Request, __: LoginRequiredError
) -> RedirectResponse:
    """Global handler: whenever an HTML route raises LoginRequiredError,
    send the browser to /login instead of returning a raw 401/500 error.
    Both parameters are unused (the request and the exception instance
    itself carry no data we need), but FastAPI's exception handler
    signature requires accepting them.
    """

    return RedirectResponse("/login", status_code=303)


@app.exception_handler(HtmlError)
async def render_html_error(
    request: Request,
    error: HtmlError,
):
    """Global handler: turn any raised HtmlError into a styled HTML page.

    This is what allows routers and services to simply `raise HtmlError(...)`
    from deep inside business logic without worrying about how to render a
    response - this single handler is responsible for picking the correct
    language (from the "language" cookie) and rendering templates/error.html
    with the right status code.
    """

    use_english = request.cookies.get("language") == "en"
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "status_code": error.status_code,
            "title": error.title_en if use_english else error.title_el,
            "message": error.message_en if use_english else error.message_el,
            "user": None,
        },
        status_code=error.status_code,
    )


# Serves everything under app/static/ (currently just styles.css) at the
# URL path /static/... . Jinja2 templates reference these via <link href="/static/...">.
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Each router below owns one feature area of the application and is defined
# in its own file under app/routers/. Order does not affect routing here
# since their URL prefixes do not overlap.
app.include_router(pages.router)      # home page, language switch, initial /setup
app.include_router(auth.router)       # login / logout
app.include_router(accounts.router)   # customer registration, profile, staff management
app.include_router(tracking.router)   # public shipment tracking (no login required)
app.include_router(shipments.router)  # staff-only shipment management HTML pages
app.include_router(api.router)        # JSON REST API under /api/shipments


@app.get("/health", tags=["system"])
def health_check(session: SessionDep) -> dict[str, str]:
    """Liveness/readiness probe used by Docker Compose's healthcheck.

    Beyond confirming the HTTP server process is responding at all, this
    also runs a trivial `SELECT 1` query to confirm the database connection
    is actually working - a process that is "up" but cannot reach its
    database is not actually healthy, so this returns 503 in that case.
    """
    try:
        session.exec(select(1)).one()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        ) from exc
    return {"status": "ok"}
