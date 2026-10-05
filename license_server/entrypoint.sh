#!/usr/bin/env sh
# Container entrypoint: bring the database schema to the current head BEFORE
# starting the app server, so every environment (fresh compose volume, staging,
# production) is migrated automatically with no manual step.
#
# `set -e` makes a failed migration fatal: we would rather the container stop
# loudly than serve licenses against a stale/wrong schema.
#
# db_bootstrap.py runs first because this project has the create_all/alembic
# drift problem: app/main.py calls Base.metadata.create_all() at import, and
# there is no initial table-creation migration (revision 001 starts by adding
# indexes to an already-existing `licenses` table). A bare `alembic upgrade
# head` therefore fails on a brand-new database — UndefinedTable if nothing
# created the table, DuplicateTable if create_all did (models.py declares the
# very four indexes 001 creates). db_bootstrap.py reconciles alembic_version
# with what is actually in the database so the upgrade below always has a sane
# starting point; it is a no-op on an already-stamped database.
set -e

echo "[entrypoint] Reconciling alembic revision state"
python db_bootstrap.py

echo "[entrypoint] Applying database migrations: alembic upgrade head"
alembic upgrade head

echo "[entrypoint] Migrations applied. Launching: $*"
exec "$@"
