# =============================================================================
# User domain model.
#
# SQLModel classes serve a dual purpose in this project: they are both the
# Python domain object used throughout the code AND the definition of the
# actual database table (because the class inherits from SQLModel with
# `table=True`). This single class is what app/database.py uses to generate
# the "user" table's columns.
# =============================================================================
from datetime import datetime, timezone
from enum import StrEnum

from sqlmodel import Field, SQLModel


class UserRole(StrEnum):
    """The fixed set of account types the application understands.

    Using a StrEnum (rather than a free-text string column) means the
    database and the Python code can never end up with an invalid or
    misspelled role - only these four values are possible. The permission
    hierarchy, from least to most privileged, is:
      CUSTOMER   -> manages only their own profile/shipments.
      EMPLOYEE   -> can create/update shipments.
      ADMIN      -> Employee permissions + manage Employee accounts.
      SUPERVISOR -> Admin permissions + manage Admin accounts too.
    See app/dependencies.py for where each role is enforced.
    """

    CUSTOMER = "customer"
    ADMIN = "admin"
    EMPLOYEE = "employee"
    SUPERVISOR = "supervisor"


class User(SQLModel, table=True):
    """Represents one account row: a Customer, Employee, Admin, or Supervisor.

    `table=True` tells SQLModel this class is also a real database table
    (named "user" by default, from the lower-cased class name), not just a
    plain data-validation schema.
    """

    id: int | None = Field(default=None, primary_key=True)
    # unique=True enforces at the database level that no two accounts can
    # share an email; index=True makes lookups by email fast (used on
    # every login).
    email: str = Field(index=True, unique=True, max_length=255)
    full_name: str = Field(max_length=120)
    phone: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=255)
    # Only the Argon2 hash of the password is ever stored (see
    # app/services/auth_service.py) - the plaintext password is never
    # persisted anywhere, following standard security practice.
    password_hash: str
    # New accounts default to the least-privileged role; only public
    # self-registration exists for Customers, staff accounts are created by
    # an Admin/Supervisor explicitly (see app/routers/accounts.py).
    role: UserRole = Field(default=UserRole.CUSTOMER)
    # Deactivating an account (is_active=False) is used instead of deleting
    # it, so shipment history that references this user as its creator
    # remains intact.
    is_active: bool = Field(default=True)
    # Always stored in UTC to avoid ambiguity when the server or its
    # visitors are in different time zones.
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
