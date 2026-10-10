#!/usr/bin/env bash
# Scratch SQLite only. Leaves the evidence directory for inspection.
set -euo pipefail
umask 077

scratch=$(mktemp -d /tmp/moafunk-maintenance-snapshot.XXXXXX)
sqlite3 "$scratch/source.db" <<'SQL'
CREATE TABLE recordings (id INTEGER PRIMARY KEY, state TEXT NOT NULL);
INSERT INTO recordings VALUES (1, 'raw');
SQL
sqlite3 "$scratch/source.db" ".backup '$scratch/snapshot.db'"
[[ "$(sqlite3 "$scratch/snapshot.db" 'PRAGMA integrity_check;')" == "ok" ]]

# Simulate a failed candidate's schema/data change; rollback uses the backup.
sqlite3 "$scratch/source.db" <<'SQL'
ALTER TABLE recordings ADD COLUMN candidate_only TEXT;
UPDATE recordings SET state = 'changed';
SQL
sqlite3 "$scratch/restored.db" ".restore '$scratch/snapshot.db'"
[[ "$(sqlite3 "$scratch/restored.db" 'PRAGMA integrity_check;')" == "ok" ]]
[[ "$(sqlite3 "$scratch/restored.db" 'SELECT state FROM recordings WHERE id=1;')" == "raw" ]]
[[ "$(sqlite3 "$scratch/restored.db" "SELECT count(*) FROM pragma_table_info('recordings');")" == "2" ]]
[[ "$(sqlite3 "$scratch/source.db" 'SELECT state FROM recordings WHERE id=1;')" == "changed" ]]
echo "PASS: SQLite integrity, restored data/schema, candidate retained: $scratch"
