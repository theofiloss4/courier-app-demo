# =============================================================================
# Database connection setup and schema bootstrapping.
#
# This module owns the single SQLAlchemy/SQLModel "engine" (the object that
# knows how to open connections to the configured database) and exposes:
#   - create_db_and_tables(): called once at startup to create tables.
#   - get_session(): a FastAPI dependency that hands each request its own
#     database Session, then closes it automatically when the request ends.
# =============================================================================
from collections.abc import Generator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
# This import has no direct use in this file, but it is required: importing
# app.models runs the User/Shipment/TrackingEvent class definitions, which
# registers their table schemas into SQLModel.metadata. Without this import,
# SQLModel.metadata.create_all() below would not know these tables exist.
from app import models  # noqa: F401


# The "engine" is the central object SQLAlchemy uses to manage a pool of
# database connections. It is created once per process and shared by every
# request (each request still gets its own Session - see get_session below).
settings = get_settings()
# SQLite has a quirk: by default, a connection can only be used from the
# thread that created it. FastAPI can serve a request on a different thread
# from where the connection was opened, so check_same_thread must be
# disabled for SQLite specifically. PostgreSQL has no such restriction.
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)


def create_db_and_tables() -> None:
    """Create the database schema on startup (called once from app/main.py).

    This project does not use a dedicated migration tool (like Alembic).
    Instead, it uses a two-step, idempotent approach that is safe to run on
    every single application startup:

      1. `SQLModel.metadata.create_all(engine)` creates any table that is
         completely missing (e.g. on a brand-new, empty database). It never
         touches a table that already exists.
      2. For PostgreSQL specifically, a series of `ALTER TABLE ... ADD
         COLUMN IF NOT EXISTS` statements patch any older/existing database
         so that its columns match the current model definitions. This
         covers the case where the code evolved (new fields were added, or a
         column was renamed) after a database had already been created.

    Every statement below is written to be safely re-run: IF NOT EXISTS /
    IF EXISTS guards mean running this function 100 times has the same
    effect as running it once.
    """

    # Step 1: create any tables that do not exist at all yet.
    SQLModel.metadata.create_all(engine)

    if settings.database_url.startswith("postgresql"):
        # AUTOCOMMIT is used here (instead of a single transaction) so that
        # each ALTER TABLE takes effect immediately and independently; if a
        # later statement in this block failed, earlier ones would not be
        # rolled back, since these are non-destructive additive changes.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            # --- Step 2: additive/patch migrations for pre-existing databases ---
            connection.execute(text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS phone VARCHAR(30)'))
            connection.execute(text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS address VARCHAR(255)'))
            connection.execute(text('ALTER TABLE shipment ADD COLUMN IF NOT EXISTS customer_id INTEGER REFERENCES "user"(id)'))
            # The "sender_address" column used to be called "pickup_address".
            # This block renames it without losing any existing data:
            # add the new column, copy the old values over, then drop the old column.
            connection.execute(text("ALTER TABLE shipment ADD COLUMN IF NOT EXISTS sender_address VARCHAR(255)"))
            legacy_address_exists = connection.execute(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'shipment' AND column_name = 'pickup_address'"
                    ")"
                )
            ).scalar()
            if legacy_address_exists:
                # Copy data only into rows that do not already have a value,
                # so re-running this block never overwrites newer data.
                connection.execute(
                    text(
                        "UPDATE shipment SET sender_address = pickup_address "
                        "WHERE sender_address IS NULL"
                    )
                )
            # Once every row has a value, the column can safely be made
            # mandatory to match the (non-nullable) model definition.
            connection.execute(text("ALTER TABLE shipment ALTER COLUMN sender_address SET NOT NULL"))
            # The legacy column is only dropped after its data has been
            # copied into sender_address above - never drop-then-copy.
            connection.execute(
                text("ALTER TABLE shipment DROP COLUMN IF EXISTS pickup_address")
            )
            connection.execute(text("ALTER TABLE shipment ADD COLUMN IF NOT EXISTS sender_email VARCHAR(255)"))
            # Shipments created before this column existed have no sender
            # email on file; give them an obviously-fake placeholder so the
            # column can then be made NOT NULL without breaking old rows.
            connection.execute(
                text(
                    "UPDATE shipment SET sender_email = 'unknown@example.invalid' "
                    "WHERE sender_email IS NULL"
                )
            )
            connection.execute(text("ALTER TABLE shipment ALTER COLUMN sender_email SET NOT NULL"))
            # Remaining columns are optional/numeric fields added later in
            # development; they are safe to add with a default value and no
            # further data backfill is required.
            connection.execute(text("ALTER TABLE shipment ADD COLUMN IF NOT EXISTS parcel_description VARCHAR(255)"))
            connection.execute(text("ALTER TABLE shipment ADD COLUMN IF NOT EXISTS weight_kg DOUBLE PRECISION DEFAULT 0"))
            connection.execute(text("ALTER TABLE shipment ADD COLUMN IF NOT EXISTS length_cm DOUBLE PRECISION DEFAULT 0"))
            connection.execute(text("ALTER TABLE shipment ADD COLUMN IF NOT EXISTS width_cm DOUBLE PRECISION DEFAULT 0"))
            connection.execute(text("ALTER TABLE shipment ADD COLUMN IF NOT EXISTS height_cm DOUBLE PRECISION DEFAULT 0"))
            connection.execute(text("ALTER TABLE shipment ADD COLUMN IF NOT EXISTS volumetric_weight_kg DOUBLE PRECISION DEFAULT 0"))
            connection.execute(text("ALTER TABLE shipment ADD COLUMN IF NOT EXISTS chargeable_weight_kg DOUBLE PRECISION DEFAULT 0"))
            connection.execute(text("ALTER TABLE shipment ADD COLUMN IF NOT EXISTS amount_eur DOUBLE PRECISION DEFAULT 0"))
            connection.execute(text("ALTER TABLE shipment ADD COLUMN IF NOT EXISTS email_sent_at TIMESTAMP WITH TIME ZONE"))


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency that provides one database Session per request.

    FastAPI recognizes the `yield` pattern as a "dependency with cleanup":
    everything before `yield` runs before the route handler, the yielded
    value (`session`) is injected into the route, and everything after
    `yield` (here, nothing extra - the `with` block itself closes the
    session) runs after the route has finished, even if it raised an error.
    This guarantees every request gets an isolated Session that is always
    closed, preventing connection leaks.
    """
    with Session(engine) as session:
        yield session
