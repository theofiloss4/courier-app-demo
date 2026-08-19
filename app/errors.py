# =============================================================================
# Custom exception types used by HTML (server-rendered) routes.
#
# The REST API (app/routers/api.py) uses FastAPI's normal HTTPException,
# which is automatically turned into a JSON error response. Browser pages
# need something different: a nicely formatted HTML error page, in the
# user's chosen language. HtmlError carries both an English and a Greek
# message so a single exception handler (see app/main.py) can render the
# correct one.
# =============================================================================
"""Application exceptions rendered as user-friendly HTML pages."""


class HtmlError(Exception):
    """A raisable error that becomes a bilingual, styled HTML error page.

    Usage pattern: anywhere in a router or service, raise
    `HtmlError(404, "Not found.", "Δεν βρέθηκε.")` instead of returning a
    response directly. The `render_html_error` handler registered in
    app/main.py catches it and renders `templates/error.html` with the
    correct status code and language.
    """

    def __init__(
        self,
        status_code: int,
        message_en: str,
        message_el: str,
        title_en: str = "Request error",
        title_el: str = "Σφάλμα αιτήματος",
    ) -> None:
        # Pass the English message to the base Exception so it shows up
        # sensibly in logs/tracebacks even though the handler picks the
        # language-specific text for the actual HTTP response.
        super().__init__(message_en)
        self.status_code = status_code
        self.message_en = message_en
        self.message_el = message_el
        self.title_en = title_en
        self.title_el = title_el
