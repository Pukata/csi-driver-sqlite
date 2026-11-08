# Homebox storage layout

Homebox has a database and uploaded files. This driver replicates one SQLite
database, not a complete application data directory. A suitable layout is:

| Volume | Backend | Purpose |
|---|---|---|
| Database | `sqlite-csi`, ReadWriteOncePod | one SQLite database and its local WAL/SHM files |
| Attachments | your existing shared storage provisioner | uploaded application files |
| Driver replica claim | existing shared RWX provisioner | Litestream histories |

Homebox v0.27.1 exposes separate database and attachment settings:
`HBOX_DATABASE_SQLITE_PATH`, `HBOX_STORAGE_CONN_STRING` and
`HBOX_STORAGE_PREFIX_PATH`. Configure the database filename to match the storage
class, and use one app replica with Recreate and UID/GID 1000 for this driver.
An application image with different permission requirements needs validation.

This is a storage-layout note, not a migration tool or a claim of complete
Homebox compatibility testing. Existing-data migration, attachment consistency
and application rollback belong to the application's documented procedures and
your backup tooling. The driver does not move or convert an existing PVC.

Upstream references: [Homebox storage](https://github.com/sysadminsmedia/homebox/blob/v0.27.1/docs/src/content/docs/en/quick-start/configure/storage.mdx)
and [container database defaults](https://github.com/sysadminsmedia/homebox/blob/v0.27.1/Dockerfile).
For an executable single-database example, see [Gatus](gatus.md).
