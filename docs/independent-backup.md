# Independent backup restore

Keep backups on storage independent of both the worker and the shared replica,
with versioning and an application-consistent backup procedure. Litestream's
asynchronous replica is not an independent backup: it can be damaged with the
source, and a successful SQLite commit does not mean the replica has received
it. An outage can leave an unbounded window of unpublished writes. Record the
backup time and application checkpoint so the owner can judge acceptable loss.

This guidance verifies an independent SQLite backup in isolation and outlines
a proposed import to a **new** volume that still requires validation. It does
not change the old volume's ownership or provide an import mechanism for an
existing locked volume. Application-specific backups, schema migrations,
external files and cutover procedures remain the application's responsibility.

1. Stop the application and independently fence the old machine if it might
   still run the writer. Confirm it is stopped and cannot restart, including
   after network recovery. Keep it fenced until the old application and mounts
   are removed and the cutover is complete. Do not unlock or edit driver state.
2. Choose a backup from independent storage. Restore a copy into isolated local
   scratch, with no application writing to it. Open it read-only, run
   `PRAGMA integrity_check` and require exactly `ok`; check application-specific
   records, schema and checkpoint against independently recorded expectations.
   Integrity alone does not establish freshness or application consistency.
   For a live SQLite source, use the SQLite online backup API or an
   application-supported consistent backup, not a plain copy of a live database
   and its potentially changing WAL files.
3. The repository has not validated importing this backup into a driver-managed
   PVC. In particular, the node agent can start Litestream when a volume is
   published, so replacing the SQLite file under a running publisher is unsafe.
   Do not copy or rename a backup over that file. Before production use,
   develop and rehearse an application-supported import into a new volume using
   SQLite-aware operations that cooperate with the publisher, with no concurrent
   application writer. Confirm permissions suit UID/GID 1000. The import and
   cutover must be validated for the specific application and driver revision.
4. In that disposable rehearsal, require a clean unpublish, a fresh replica
   restore with integrity and expected-record checks, and a successful start of
   one application instance on the new PVC. Investigate any locked state without
   editing ownership. Preserve the old volume, backup and replica history for
   diagnosis and rollback planning. A standalone SQLite check does not establish
   that these driver and application steps work.

A disposable, cluster-free exercise of the backup/restore and integrity/record
checks is `python3 -m unittest discover -s tools -p 'test_independent_backup.py'`.
It uses SQLite's online backup API and removes the original database before
restoring. It does not test a Kubernetes PVC, the application's import, or
replica publication; rehearse those with a disposable deployment as well.
