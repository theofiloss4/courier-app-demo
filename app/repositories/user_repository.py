# =============================================================================
# Repository layer for User accounts.
#
# A "repository" wraps every direct database query for one model behind a
# small set of plain Python methods. The rest of the app (services, routers)
# never writes raw SQLModel `select(...)` statements themselves for users -
# they call methods like `get_by_email` instead. This keeps all knowledge of
# how users are stored/queried in exactly one place, and makes it possible
# to unit test services by swapping in a fake repository if needed.
# =============================================================================
from sqlmodel import Session, select

from app.models.user import User, UserRole


class UserRepository:
    """All database access for the User table goes through this class."""

    def __init__(self, session: Session) -> None:
        # The same request-scoped Session (see app/database.py get_session)
        # is reused for every query made through this repository instance.
        self.session = session

    def get_by_email(self, email: str) -> User | None:
        """Find a user by email, or None if no account uses that address.

        Used by login (to find the account, then verify the password
        hash) and by registration (to reject a duplicate email).
        """
        # Emails are always normalized to lowercase before being stored, so
        # the lookup must also lowercase its input, otherwise "A@b.com" and
        # "a@b.com" would be treated as different accounts.
        statement = select(User).where(User.email == email.lower())
        return self.session.exec(statement).first()

    def add(self, user: User) -> User:
        """Insert a brand-new user row and return it with its generated id.

        `commit()` writes the row to the database; `refresh()` reloads the
        object afterward so that database-generated fields (like the
        auto-increment `id`) are populated on the returned object.
        """
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def list_staff(self) -> list[User]:
        """Return every non-Customer account, sorted by name, for the staff
        management screen (see app/routers/accounts.py staff_list route).
        Customers are deliberately excluded - that screen is only for
        managing Employee/Admin/Supervisor accounts.
        """
        statement = (
            select(User)
            .where(User.role != UserRole.CUSTOMER)
            .order_by(User.full_name)
        )
        return list(self.session.exec(statement).all())

    def exists_with_role(self, role: UserRole) -> bool:
        """Check whether at least one account with the given role exists.

        Used specifically to check "does a Supervisor already exist?" - the
        public /setup route (see app/routers/accounts.py) that creates the
        very first Supervisor account must permanently disable itself once
        one exists, otherwise anyone could create additional Supervisor
        accounts without logging in.
        """
        return self.session.exec(select(User).where(User.role == role)).first() is not None

    def save(self, user: User) -> User:
        """Persist changes to an already-existing user (e.g. after editing
        their profile, changing their password, or toggling is_active).
        Functionally identical to `add`, but named separately to make the
        caller's intent (insert vs. update) clear when reading route code.
        """
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user
