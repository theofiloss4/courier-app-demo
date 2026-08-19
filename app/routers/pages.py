# =============================================================================
# Miscellaneous public HTML pages that do not belong to any single feature
# area (currently just the home page). Note the actual language-switch route
# (/language/{lang}) and the one-time /setup route live in
# app/routers/accounts.py, not here, despite this file's generic name.
# =============================================================================
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.dependencies import OptionalUser


router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@router.get("/", response_class=HTMLResponse)
def home(request: Request, user: OptionalUser):
    """GET / — the public landing page.

    Auth: none required. Uses `OptionalUser` (not `CurrentHtmlUser`) so the
    exact same template renders for anonymous visitors and logged-in users
    alike; the template itself decides what extra content/links to show
    based on whether `user` is None or a real account.
    """
    return templates.TemplateResponse(request, "home.html", {"user": user})
