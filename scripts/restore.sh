#!/bin/sh
# Restores into a NEW database on the existing Postgres service.
# Never drops or overwrites a live database.
set -eu
backup_file=${1:?Usage: sh scripts/restore.sh backup.dump new_database_name}
target_db=${2:?Provide a new isolated database name}
case "$target_db" in ''|*[!a-z0-9_]*) echo 'Use lowercase a-z, 0-9 and underscore for the database name.' >&2; exit 1;; esac
case "$target_db" in leasefit|postgres|template0|template1) echo 'Choose a new isolated database name.' >&2; exit 1;; esac
docker compose exec -T db sh -c 'createdb -U "$POSTGRES_USER" "$1"' sh "$target_db"
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" --dbname="$1" --no-owner --exit-on-error' sh "$target_db" < "$backup_file"
echo "Restored into $target_db. Validate before switching POSTGRES_DB."
