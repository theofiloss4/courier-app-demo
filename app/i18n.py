# =============================================================================
# Lightweight internationalization (i18n) layer for Greek / English UI text.
#
# This is a minimal, hand-rolled alternative to a full i18n library. The
# selected language is stored in a plain browser cookie named "language"
# (set by the GET /language/{lang} route in app/routers/pages.py), and this
# module is responsible for turning that cookie into the correct dictionary
# of translated strings for Jinja2 templates.
# =============================================================================
from fastapi import Request


# Dictionary-of-dictionaries: top-level keys are language codes, and each
# inner dictionary maps a "translation key" (used in templates as t.track,
# t.login, etc.) to the actual display text. Both language blocks must stay
# in sync - if a key is added to one, it should be added to the other too,
# otherwise a template rendered in that language will raise a KeyError.
TRANSLATIONS = {
    "el": {
        "track": "Αναζήτηση αποστολής",
        "shipments": "Αποστολές",
        "staff": "Προσωπικό",
        "login": "Σύνδεση",
        "register": "Εγγραφή πελάτη",
        "logout": "Αποσύνδεση",
        "language": "Γλώσσα",
    },
    "en": {
        "track": "Track shipment",
        "shipments": "Shipments",
        "staff": "Staff",
        "login": "Log in",
        "register": "Customer registration",
        "logout": "Log out",
        "language": "Language",
    },
}


def language(request: Request) -> str:
    """Read the visitor's preferred language from their cookies.

    Falls back to Greek ("el") when the cookie is absent, or when it holds
    an unsupported value (e.g. a stale cookie from before a language was
    removed). This keeps the app safe from ever trying to look up a
    language code that does not exist in TRANSLATIONS.
    """
    return request.cookies.get("language", "el") if request.cookies.get("language") in TRANSLATIONS else "el"


def template_context(request: Request, **values):
    """Build the common dictionary passed into every Jinja2 template render.

    Every template needs access to the current language code (`lang`) and
    its translation table (`t`), so instead of repeating that in every
    router, this helper bundles them together with whatever page-specific
    `values` the caller supplies (e.g. shipment=..., user=...).
    """
    lang = language(request)
    return {"lang": lang, "t": TRANSLATIONS[lang], **values}
