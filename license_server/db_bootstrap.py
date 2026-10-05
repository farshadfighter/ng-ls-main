"""
Reconcile alembic_version with what is actually in the database, so that the
`alembic upgrade head` in entrypoint.sh always has a valid starting point.

Why this is needed
------------------
app/main.py calls ``Base.metadata.create_all()`` at import time, and the
migration history has no initial create-tables revision: revision 001 begins by
adding four indexes to an existing ``licenses`` table. That leaves a bare
``alembic upgrade head`` broken at both ends:

* on an empty database it fails with UndefinedTable — no ``licenses`` table yet;
* after create_all has run it fails with DuplicateTable — app/models.py declares
  the same four index names that 001 creates.

Neither is a schema this script invents; it just records the truth in
alembic_version so the upgrade can proceed.

Decision table
--------------
============================  ==========================================
database state                action
============================  ==========================================
alembic_version present       nothing (upgrade head does the rest)
no tables at all              create_all, then stamp head
tables, four indexes present  stamp 001 (its work is already done)
tables, indexes missing       leave at base so 001 runs
============================  ==========================================

Revision 002 (the legacy plan-type enum remap) is never stamped over: it is
left for `alembic upgrade head` to apply, because a database created before the
plan rename genuinely needs it, and it is a harmless no-op on one created after.

Idempotent: safe to run on every container start.
"""
import logging
import sys
import time

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.core.config import settings
from app.database import Base, engine
import app.models  # noqa: F401  -- registers License on Base.metadata

logging.basicConfig(level=logging.INFO, format="[db_bootstrap] %(message)s")
logger = logging.getLogger("db_bootstrap")

# The indexes revision 001 creates, which app/models.py also declares.
MIGRATION_001_INDEXES = {
    "idx_license_key_active",
    "idx_org_token_active",
    "idx_expires_at",
    "idx_last_heartbeat",
}
CONNECT_ATTEMPTS = 30
CONNECT_DELAY_SECONDS = 1


def wait_for_database() -> None:
    """Block until Postgres accepts a connection, or give up loudly.

    Compose already gates this container on a healthy postgres, so this is for
    the cases compose does not cover: a manual `docker run`, or a database that
    is listening before it finishes recovery.
    """
    last_error = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except Exception as exc:  # driver-specific; any failure means "not yet"
            last_error = exc
            if attempt == 1:
                logger.info("Waiting for the database to accept connections...")
            time.sleep(CONNECT_DELAY_SECONDS)
    raise SystemExit(
        f"Database at {settings.DATABASE_URL.rsplit('@', 1)[-1]} did not accept "
        f"a connection after {CONNECT_ATTEMPTS}s: {last_error}"
    )


def alembic_config() -> Config:
    """alembic.ini as the CLI would load it (env.py injects the real URL)."""
    return Config("alembic.ini")


def main() -> int:
    wait_for_database()

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if "alembic_version" in tables:
        logger.info("alembic_version present; leaving revision state alone.")
        return 0

    if "licenses" not in tables:
        logger.info("Empty database: creating the schema from the models.")
        Base.metadata.create_all(bind=engine)
        # create_all builds exactly the head schema (indexes included), and the
        # 002 enum remap is a no-op against freshly created labels.
        command.stamp(alembic_config(), "head")
        logger.info("Schema created and stamped at head.")
        return 0

    # Tables exist but Alembic never recorded a revision — the create_all drift.
    existing_indexes = {
        index["name"] for index in inspector.get_indexes("licenses")
    }
    if MIGRATION_001_INDEXES <= existing_indexes:
        logger.info(
            "Unstamped database with 001's indexes already present; "
            "stamping 001 so the upgrade resumes at 002."
        )
        command.stamp(alembic_config(), "001")
    else:
        missing = sorted(MIGRATION_001_INDEXES - existing_indexes)
        logger.info(
            "Unstamped database missing %s; leaving at base so 001 runs.",
            ", ".join(missing),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
