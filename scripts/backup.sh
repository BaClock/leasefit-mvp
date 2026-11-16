#!/bin/sh
# Run from the repository root. Includes PDFs, attachments and database records.
set -eu
umask 077
backup_dir=${1:-./backups}
mkdir -p "$backup_dir"
backup_file="$backup_dir/leasefit-$(date -u +%Y%m%dT%H%M%SZ).dump"
temporary_file="$backup_file.tmp"
trap 'rm -f "$temporary_file"' EXIT HUP INT TERM
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner' > "$temporary_file"
test -s "$temporary_file"
mv "$temporary_file" "$backup_file"
sha256sum "$backup_file" > "$backup_file.sha256"
echo "$backup_file"
