# =============================================================================
# Account-related HTML routes: this is the largest router in the app,
# covering several distinct account flows that all revolve around the User
# model:
#   - Public customer self-registration (/register)
#   - One-time first-Supervisor bootstrap (/setup)
#   - Language switching (/language/{lang})
#   - Staff account management: list/create/enable-disable/reset password
#     (/staff/*) - restricted to Admin/Supervisor
#   - Customer's own profile/dashboard/password (/account/*)
# =============================================================================
import re
from threading import Lock
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import EmailStr, TypeAdapter, ValidationError

from app.config import BASE_DIR
from app.dependencies import (
    HtmlCustomerUser,
    HtmlStaffManager,
    SessionDep,
)
from app.errors import HtmlError
from app.i18n import template_context
from app.models.user import User, UserRole
from app.repositories.shipment_repository import ShipmentRepository
from app.repositories.user_repository import UserRepository
from app.routers.shipments import status_labels_for
from app.security import CsrfProtection, set_access_token_cookie
from app.services.auth_service import AuthService, hash_password


router = APIRouter(tags=["accounts"])
templates = Jinja2Templates(directory=BASE_DIR / "templates")
# A reusable Pydantic validator for a single value (rather than a whole
# model) - used below to validate a raw email string outside of a full
# request-body schema.
email_adapter = TypeAdapter(EmailStr)
# Guards the "does a Supervisor already exist?" check-then-create sequence
# in the setup() route below against a race condition - see its docstring.
supervisor_setup_lock = Lock()


def normalized_phone(value: str) -> str | None:
    """Validate and normalize a Greek phone number, or return None if invalid.

    Same rules as the `validate_greek_phone` validator in
    app/schemas/shipment.py, duplicated here because this module handles
    plain HTML form fields directly rather than through a Pydantic schema.
    Returns None (instead of raising) so the caller can turn it into a
    bilingual form error message itself.
    """
    normalized = re.sub(r"[\s().-]", "", value)
    if normalized.startswith("+30"):
        normalized = normalized[3:]
    elif normalized.startswith("0030"):
        normalized = normalized[4:]
    return (
        normalized
        if re.fullmatch(r"(?:2\d{9}|69\d{8})", normalized)
        else None
    )


def valid_password(password: str) -> bool:
    """Enforce the app's minimum password strength rule.

    Requires at least 8 characters, containing at least one letter AND at
    least one digit (purely alphabetic or purely numeric passwords are
    rejected). Used consistently across registration, staff creation, and
    every password-change flow in this file.
    """
    return (
        len(password) >= 8
        and any(character.isalpha() for character in password)
        and any(character.isdigit() for character in password)
    )


def can_manage_staff(manager: User, target: User) -> bool:
    """Decide whether `manager` is allowed to enable/disable or reset the
    password of `target`'s staff account.

    Encodes the staff permission hierarchy described in the README:
      - Nobody can manage their own account through this mechanism
        (prevents accidentally locking yourself out).
      - A Supervisor can manage Employee and Admin accounts.
      - An Admin can manage Employee accounts only (not other Admins, and
        not Supervisors).
      - A plain Employee can manage no one (this function only receives
        managers that already passed the HtmlStaffManager dependency, but
        the explicit `return False` here is a safety net).
    """

    if manager.id == target.id:
        return False
    if manager.role == UserRole.SUPERVISOR:
        return target.role in {UserRole.EMPLOYEE, UserRole.ADMIN}
    if manager.role == UserRole.ADMIN:
        return target.role == UserRole.EMPLOYEE
    return False


def create_user_response(
    request: Request,
    session: SessionDep,
    template: str,
    full_name: str,
    email: str,
    password: str,
    role: UserRole,
    redirect_to: str,
    user=None,
    roles=None,
):
    """Shared account-creation logic reused by customer registration,
    the first-Supervisor setup, and staff account creation below.

    All three flows collect the same three fields (name, email, password)
    but differ only in which `role` is assigned and where to redirect
    afterward - rather than duplicating the validation and User-creation
    code three times, each route calls this one helper with its own
    specific role/template/redirect arguments.
    """

    repository = UserRepository(session)
    full_name = full_name.strip()
    email = email.strip().lower()
    error = None
    # The backend repeats checks that may also exist in the HTML form,
    # since client-side/HTML validation can always be bypassed by a direct
    # POST request - the server must never trust form validation alone.
    try:
        email = str(email_adapter.validate_python(email))
    except ValidationError:
        error = (
            "Δώστε έγκυρη διεύθυνση email."
            if template_context(request)["lang"] == "el"
            else "Enter a valid email address."
        )
    if not 2 <= len(full_name) <= 120:
        error = (
            "Το ονοματεπώνυμο πρέπει να έχει από 2 έως 120 χαρακτήρες."
            if template_context(request)["lang"] == "el"
            else "Full name must contain between 2 and 120 characters."
        )
    elif not error and repository.get_by_email(email):
        error = (
            "Αυτό το email χρησιμοποιείται ήδη."
            if template_context(request)["lang"] == "el"
            else "This email is already in use."
        )
    elif not valid_password(password):
        error = (
            "Ο κωδικός χρειάζεται τουλάχιστον 8 χαρακτήρες, ένα γράμμα και έναν αριθμό."
            if template_context(request)["lang"] == "el"
            else "Password needs at least 8 characters, one letter and one number."
        )
    if error:
        # On any validation failure, re-render the SAME form template with
        # the error message and the previously typed name/email so the
        # visitor does not have to retype everything (note: the password is
        # deliberately NOT echoed back, for security).
        return templates.TemplateResponse(
            request,
            template,
            template_context(
                request,
                error=error,
                form_data={"full_name": full_name, "email": email},
                user=user,
                roles=roles or [],
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    # The password is hashed before the User object is created - the
    # plaintext `password` variable is never written to the database.
    created_user = repository.add(
        User(
            full_name=full_name,
            email=email,
            password_hash=hash_password(password),
            role=role,
        )
    )
    if role == UserRole.CUSTOMER and created_user.id is not None:
        # If any shipments were created earlier for this email address
        # before an account existed, attach them to the new account now
        # (see ShipmentRepository.link_unassigned_for_customer).
        ShipmentRepository(session).link_unassigned_for_customer(
            created_user.id,
            created_user.email,
        )
    return RedirectResponse(redirect_to, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/language/{lang}")
def change_language(lang: str, request: Request):
    """GET /language/{lang} — switch the UI language and return to the
    same page the visitor was just on.

    Auth: none required. Reads the browser's "Referer" header (the page
    that linked here) to figure out where to redirect back to, then keeps
    only its path+query (discarding scheme/host) as a defensive measure so
    this redirect can never be turned into an open redirect to another site.
    """
    destination = request.headers.get("referer", "/")
    parsed = urlparse(destination)
    destination = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    response = RedirectResponse(destination or "/", status_code=status.HTTP_303_SEE_OTHER)
    # Store only the two languages supported by the application.
    if lang in {"el", "en"}:
        response.set_cookie("language", lang, max_age=31_536_000, samesite="lax")
    return response


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    """GET /register — display the empty customer self-registration form."""
    return templates.TemplateResponse(request, "auth/register.html", template_context(request))


@router.post("/register")
def register(
    request: Request,
    session: SessionDep,
    _: CsrfProtection,
    full_name: str = Form(),
    email: str = Form(),
    password: str = Form(),
):
    """POST /register — public self-service account creation.

    Auth: none required (this IS how an account gets created). CSRF
    protected. Security note: the role is hard-coded to `UserRole.CUSTOMER`
    below and never taken from the request - a visitor cannot submit a
    hidden/extra "role=admin" field to elevate their own privileges; staff
    accounts can only be created by an existing Admin/Supervisor via
    create_staff() further down this file.
    """
    return create_user_response(
        request,
        session,
        "auth/register.html",
        full_name,
        email,
        password,
        UserRole.CUSTOMER,
        "/login",
    )


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, session: SessionDep):
    """GET /setup — display the one-time "create the first Supervisor" form.

    Auth: none required (there is no admin to log in as yet on a brand-new
    deployment) - but this route self-disables permanently once a
    Supervisor account exists, redirecting to /login instead.
    """
    if UserRepository(session).exists_with_role(UserRole.SUPERVISOR):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "auth/setup.html", template_context(request))


@router.post("/setup")
def setup(
    request: Request,
    session: SessionDep,
    _: CsrfProtection,
    full_name: str = Form(),
    email: str = Form(),
    password: str = Form(),
):
    """POST /setup — actually create the first Supervisor account.

    Auth: none required, by design (bootstrapping problem: no admin exists
    yet to grant access). This is exactly why it is critical that it can
    only ever run once. The `supervisor_setup_lock` below closes a race
    condition: without it, two simultaneous POST requests could both pass
    the "does a Supervisor exist?" check (both see "no") before either has
    committed its new user, resulting in two Supervisor accounts being
    created. The lock forces the check-then-create sequence to run as one
    atomic unit within this single process.
    """
    with supervisor_setup_lock:
        repository = UserRepository(session)
        if repository.exists_with_role(UserRole.SUPERVISOR):
            return RedirectResponse(
                "/login",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        return create_user_response(
            request,
            session,
            "auth/setup.html",
            full_name,
            email,
            password,
            UserRole.SUPERVISOR,
            "/login",
        )


@router.get("/staff", response_class=HTMLResponse)
def staff_list(request: Request, session: SessionDep, user: HtmlStaffManager):
    """GET /staff — list every staff account (Employee/Admin/Supervisor).

    Auth: `HtmlStaffManager` requires Admin or Supervisor - a plain
    Employee gets a 403 error page and never reaches this code.
    Also computes `manageable_staff_ids`, the subset of listed accounts the
    CURRENT viewer is allowed to toggle/reset (via can_manage_staff), so
    the template can show or hide those action buttons per row.
    """
    staff = UserRepository(session).list_staff()
    return templates.TemplateResponse(
        request,
        "staff/list.html",
        template_context(
            request,
            user=user,
            staff=staff,
            manageable_staff_ids={
                member.id for member in staff if can_manage_staff(user, member)
            },
        ),
    )


@router.get("/staff/new", response_class=HTMLResponse)
def staff_form(request: Request, user: HtmlStaffManager):
    """GET /staff/new — display the "create a staff account" form.

    Auth: Admin or Supervisor. The list of selectable roles offered in the
    form depends on who is viewing it: an Admin may only create Employees,
    while a Supervisor may also create Admins (mirrors can_manage_staff's
    hierarchy, applied one level up at account-creation time instead of
    account-modification time).
    """
    roles = [UserRole.EMPLOYEE]
    if user.role == UserRole.SUPERVISOR:
        roles.append(UserRole.ADMIN)
    return templates.TemplateResponse(
        request,
        "staff/new.html",
        template_context(request, user=user, roles=roles),
    )


@router.post("/staff")
def create_staff(
    request: Request,
    session: SessionDep,
    user: HtmlStaffManager,
    _: CsrfProtection,
    full_name: str = Form(),
    email: str = Form(),
    password: str = Form(),
    role: UserRole = Form(),
):
    """POST /staff — actually create a new staff account.

    Auth: Admin or Supervisor. Security note: even though the HTML form
    (staff/new.html) only ever shows the roles staff_form() decided to
    offer, a malicious or modified client could still POST an arbitrary
    `role` value directly - so this check is repeated here, server-side, as
    the actual enforcement point. This is the same "never trust the
    client, the server owns validation" principle seen in
    create_user_response above.
    """
    allowed = (
        {UserRole.EMPLOYEE, UserRole.ADMIN}
        if user.role == UserRole.SUPERVISOR
        else {UserRole.EMPLOYEE}
    )
    if role not in allowed:
        raise HtmlError(
            status.HTTP_403_FORBIDDEN,
            "You cannot create an account with the selected role.",
            "Δεν μπορείτε να δημιουργήσετε λογαριασμό με τον επιλεγμένο ρόλο.",
            "Role not allowed",
            "Ο ρόλος δεν επιτρέπεται",
        )
    return create_user_response(
        request,
        session,
        "staff/new.html",
        full_name,
        email,
        password,
        role,
        "/staff",
        user,
        sorted(allowed, key=lambda item: item.value),
    )


@router.get("/account", response_class=HTMLResponse)
def customer_dashboard(
    request: Request,
    session: SessionDep,
    user: HtmlCustomerUser,
):
    """GET /account — the logged-in customer's personal dashboard.

    Auth: `HtmlCustomerUser` requires a logged-in Customer account
    specifically - a staff member visiting this URL gets a 403, since they
    have their own separate management screens instead.
    Shows the customer's profile plus every shipment linked to their
    account (see ShipmentRepository.list_for_customer).
    """
    repository = ShipmentRepository(session)
    return templates.TemplateResponse(
        request,
        "account/dashboard.html",
        template_context(
            request,
            user=user,
            shipments=repository.list_for_customer(user.id or 0),
            status_labels=status_labels_for(request),
        ),
    )


@router.post("/account/profile")
def update_profile(
    request: Request,
    session: SessionDep,
    user: HtmlCustomerUser,
    _: CsrfProtection,
    full_name: str = Form(),
    phone: str = Form(),
    address: str = Form(),
):
    """POST /account/profile — update the logged-in customer's own name,
    phone, and address.

    Auth: Customer only, and can only ever edit their OWN `user` object
    (there is no id parameter here - the target is always "whoever the
    valid session cookie belongs to"), so this endpoint cannot be used to
    edit anyone else's profile.
    On validation failure: re-renders the dashboard with an inline error
    instead of a separate error page, so the customer's shipment list stays
    visible.
    """
    phone_value = normalized_phone(phone)
    error = None
    full_name = full_name.strip()
    address = address.strip()
    if not 2 <= len(full_name) <= 120:
        error = (
            "Μη έγκυρο ονοματεπώνυμο."
            if template_context(request)["lang"] == "el"
            else "Invalid full name."
        )
    elif not phone_value:
        error = (
            "Μη έγκυρο ελληνικό τηλέφωνο."
            if template_context(request)["lang"] == "el"
            else "Invalid Greek phone number."
        )
    elif not 5 <= len(address) <= 255 or not re.search(r"\d", address):
        error = (
            "Η διεύθυνση πρέπει να έχει 5 έως 255 χαρακτήρες και να περιλαμβάνει αριθμό."
            if template_context(request)["lang"] == "el"
            else (
                "The address must contain 5 to 255 characters "
                "and include a street number."
            )
        )
    if error:
        repository = ShipmentRepository(session)
        return templates.TemplateResponse(
            request,
            "account/dashboard.html",
            template_context(
                request,
                user=user,
                error=error,
                shipments=repository.list_for_customer(user.id or 0),
                status_labels=status_labels_for(request),
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    user.full_name = full_name
    user.phone = phone_value
    user.address = address
    UserRepository(session).save(user)
    return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/account/password")
def change_customer_password(
    session: SessionDep,
    user: HtmlCustomerUser,
    _: CsrfProtection,
    current_password: str = Form(),
    new_password: str = Form(),
):
    """POST /account/password — let the logged-in customer change their
    own password.

    Auth: Customer only, own account only. Requires re-entering the
    CURRENT password (verified via AuthService.authenticate) before
    accepting a new one - this stops someone who has hijacked an already
    logged-in browser tab (e.g. left unlocked) from silently changing the
    password to lock the real owner out.
    On success: issues a brand-new JWT via set_access_token_cookie. This
    matters because of how AuthService.token_matches_user works (see
    app/services/auth_service.py) - changing the password invalidates the
    OLD token's "password_version" fingerprint, so without reissuing a
    fresh token here, the user would be immediately logged out by their own
    password change.
    """
    service = AuthService(UserRepository(session))
    if (
        not service.authenticate(user.email, current_password)
        or not valid_password(new_password)
    ):
        return RedirectResponse(
            "/account?password_error=1",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    user.password_hash = hash_password(new_password)
    UserRepository(session).save(user)
    response = RedirectResponse(
        "/account?password_changed=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    set_access_token_cookie(response, service.create_access_token(user))
    return response


@router.post("/staff/{staff_id}/toggle")
def toggle_staff(
    staff_id: int,
    session: SessionDep,
    user: HtmlStaffManager,
    _: CsrfProtection,
):
    """POST /staff/{staff_id}/toggle — enable or disable a staff account.

    Auth: Admin or Supervisor, AND `can_manage_staff` must approve this
    specific manager/target pairing (see that function's docstring for the
    exact hierarchy rules) - e.g. an Admin cannot toggle another Admin.
    Deactivating (rather than deleting) preserves the account's history —
    shipments and tracking events they created still reference a real user
    row, they just can no longer log in while `is_active` is False (see
    app/dependencies.py get_current_user, which checks this flag).
    """
    target = session.get(User, staff_id)
    if target is None:
        raise HtmlError(
            status.HTTP_404_NOT_FOUND,
            "The staff account no longer exists.",
            "Ο λογαριασμός προσωπικού δεν υπάρχει πλέον.",
            "Staff account not found",
            "Ο λογαριασμός δεν βρέθηκε",
        )
    if not can_manage_staff(user, target):
        raise HtmlError(
            status.HTTP_403_FORBIDDEN,
            "You cannot modify this staff account.",
            "Δεν μπορείτε να τροποποιήσετε αυτόν τον λογαριασμό προσωπικού.",
            "Access denied",
            "Δεν επιτρέπεται η πρόσβαση",
        )
    target.is_active = not target.is_active
    UserRepository(session).save(target)
    return RedirectResponse("/staff", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/staff/{staff_id}/password")
def reset_staff_password(
    staff_id: int,
    session: SessionDep,
    user: HtmlStaffManager,
    _: CsrfProtection,
    new_password: str = Form(),
):
    """POST /staff/{staff_id}/password — administratively reset another
    staff member's password (e.g. because they forgot it).

    Auth: Admin or Supervisor, AND `can_manage_staff` approval for this
    specific manager/target pairing, same as toggle_staff above. Unlike
    change_customer_password, this does NOT require knowing the target's
    current password (the whole point is recovering access when it is
    forgotten) - trust here comes entirely from the caller's own staff
    role and management permissions, not from proving knowledge of the old
    password.
    """
    target = session.get(User, staff_id)
    if target is None:
        raise HtmlError(
            status.HTTP_404_NOT_FOUND,
            "The staff account no longer exists.",
            "Ο λογαριασμός προσωπικού δεν υπάρχει πλέον.",
            "Staff account not found",
            "Ο λογαριασμός δεν βρέθηκε",
        )
    if not can_manage_staff(user, target):
        raise HtmlError(
            status.HTTP_403_FORBIDDEN,
            "You cannot modify this staff account.",
            "Δεν μπορείτε να τροποποιήσετε αυτόν τον λογαριασμό προσωπικού.",
            "Access denied",
            "Δεν επιτρέπεται η πρόσβαση",
        )
    if not valid_password(new_password):
        raise HtmlError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The password must contain at least eight characters, one letter, and one digit.",
            "Ο κωδικός πρέπει να περιέχει τουλάχιστον οκτώ χαρακτήρες, ένα γράμμα και έναν αριθμό.",
            "Invalid password",
            "Μη έγκυρος κωδικός",
        )
    target.password_hash = hash_password(new_password)
    UserRepository(session).save(target)
    return RedirectResponse("/staff", status_code=status.HTTP_303_SEE_OTHER)
