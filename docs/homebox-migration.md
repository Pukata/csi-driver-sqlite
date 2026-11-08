# Homebox: SQLite on local SSD, attachments on shared storage

Homebox is a composite workload: its database and uploaded files have separate
durability needs. This recipe targets Homebox v0.27.1's configuration interface.
Gatus is the [automated single-database compatibility case](gatus.md); this
Homebox recipe is not a claim of CI coverage for its whole application.

Keep three claims separate:

| Claim | Storage | Contents |
|---|---|---|
| `homebox-db` | `sqlite-csi`, ReadWriteOncePod | one `database.db`, including local SQLite WAL/SHM files |
| `homebox-attachments` | existing shared RWX provisioner | uploaded application files |
| driver's `sqlite-csi-replicas` | existing shared RWX provisioner | Litestream histories, not live app files |

Use one app replica and Recreate. Configure the app with:

```yaml
env:
  - name: HBOX_DATABASE_DRIVER
    value: sqlite3
  - name: HBOX_DATABASE_SQLITE_PATH
    value: /database/database.db?_pragma=busy_timeout=2000&_pragma=journal_mode=WAL&_fk=1&_time_format=sqlite
  - name: HBOX_STORAGE_CONN_STRING
    value: file:///attachments?no_tmp_dir=true
  - name: HBOX_STORAGE_PREFIX_PATH
    value: data
volumeMounts:
  - {name: database, mountPath: /database}
  - {name: attachments, mountPath: /attachments}
```

Match `data` to the existing application's actual storage prefix. Relative
attachment keys in the database must resolve to the same files after migration.
Run the app as UID/GID 1000; this driver's current ownership is fixed at that
identity. Check the selected Homebox image supports it. Do not apply this recipe
to a hardened image with incompatible filesystem permissions without validation.

## Offline migration

1. Save the existing manifests, image version, environment and secrets. Confirm
   an independent backup can restore both database and attachments.
2. Scale the old Homebox deployment to zero and wait for its Pod to terminate.
   Keep it stopped throughout copying. Do not upgrade Homebox during this move.
3. Mount the old claim on a one-shot maintenance Pod on its original node.
   Use SQLite's `Connection.backup()` to produce a consistent standalone file;
   do not copy just a live `.db` file and discard its WAL. Check the resulting
   backup with `PRAGMA integrity_check` and preserve it outside both claims.
4. Create `homebox-db` with `storageClassName: sqlite-csi` and RWOP. A maintenance
   Pod running as UID/GID 1000 can mount this claim and copy the offline backup
   to `/database/database.db`. Do not run the application at the same time.
5. Copy attachments to the separate shared claim, preserving the prefix layout.
   Compare filenames, sizes and hashes with the stopped source. Delete the import
   Pod normally and wait for successful clean volume release; it is the first
   writer of this new database claim and must publish a verified replica.
6. Start Homebox with the split mounts and settings above. Verify login, an old
   item, an old document download, a new item and a new upload. Verify the new
   database can be restored from the driver's replica independently of the app.
7. Perform a planned move using Recreate and node selection. Repeat those checks
   and confirm the same PV UID, preserved database records, and attachment hashes.

An offline backup from Python looks like:

```python
import sqlite3
with sqlite3.connect('/old/homebox.db') as source:
    with sqlite3.connect('/backup/database.db') as target:
        source.backup(target)
        assert target.execute('pragma integrity_check').fetchone()[0] == 'ok'
```

The old database path is installation-specific. Ensure the maintenance Pod has
exclusive writer access and write permission for SQLite's locking requirements.

## Rollback and recovery

Keep the original claim and manifests. Before allowing writes on the new setup,
rollback can stop the new app and restore the old configuration. After accepting
new writes, the old database is stale: stop the app, make a new consistent backup
of the current database, reconcile changed attachments, and restore those to the
old layout before restarting. Never start both copies or copy old state over the
new active database. Retain both histories until rollback is verified.

The default class does not automatically recover from an unreachable node.
Configure and test [independent fencing](failure-recovery.md) before opting into
latest-replica recovery. Back up attachments and the database together at an
application-consistent boundary when referential consistency matters; separate
storage does not make that transaction atomic.

Pinned upstream settings: [storage](https://github.com/sysadminsmedia/homebox/blob/v0.27.1/docs/src/content/docs/en/quick-start/configure/storage.mdx)
and [container defaults](https://github.com/sysadminsmedia/homebox/blob/v0.27.1/Dockerfile).
