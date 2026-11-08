# Choosing storage for a self-hosted SQLite app

This driver targets one app instance, one database file per PVC, local writes,
and controlled movement between nodes. It adds a shared replica backend and
ownership machinery. It is not automatically the simplest choice.

| Approach | Good fit | Tradeoff |
|---|---|---|
| Local PV plus independent backups | One node is fine; manual restore is acceptable | App stays with its disk; recovery is manual |
| Litestream sidecar plus init restore | A few apps whose manifests you maintain | Configure each app; ownership/fencing still needs care |
| This CSI driver | Several compatible single-instance SQLite apps needing a common PVC interface | Experimental driver, shared RWX replica dependency, asynchronous loss window, independent fence for automatic recovery |
| Replicated block storage, such as Longhorn | Whole application directories or many file types need portable block volumes | Operate a general storage system, replicas and its recovery behavior |
| NAS-backed block storage via iSCSI | Central storage is acceptable and supported by your platform | Network/storage availability and exclusive attachment become dependencies |
| PostgreSQL, where the app supports it | Database service operation or multi-instance app requirements justify it | Migration, credentials, upgrades, backups and HA policy for another service |

Use a supported local filesystem for the live SQLite database. A shared file
mount is not universally safe for SQLite's WAL/locking requirements. This driver
uses the shared filesystem for Litestream replica objects, not for live DB writes.

Choose another approach if the app has several coupled SQLite databases that
must recover atomically, requires arbitrary file replication, cannot use UID
1000, or must preserve every acknowledged transaction after abrupt failure.
Multiple claims do not provide a cross-database consistent snapshot. A second
SQLite writer is not enabled by creating another app replica.

Sources: [SQLite network filesystems](https://sqlite.org/useovernet.html),
[Litestream Kubernetes integration](https://litestream.io/guides/kubernetes/),
[Longhorn architecture](https://longhorn.io/docs/latest/concepts/),
[Kubernetes persistent volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/).
Evaluate the current requirements of each alternative against your own setup;
this table is a scope comparison, not a performance benchmark.
