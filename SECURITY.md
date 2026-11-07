# Security and data integrity

This is experimental storage software. Keep independent backups before using
it with important data. There is no production support or availability promise.

Report security issues through the repository's private vulnerability reporting
feature when available. Do not publish credentials, database contents, or an
exploit against someone else's cluster in an issue. General bug reports can use
GitHub issues with sanitized reproduction steps.

The node agent is privileged to perform CSI mounts. Install it only on trusted
Linux nodes. Restrict access to the driver namespace, its volume state and its
shared replica filesystem. A fencing authority can stop machines and must have
its own independently secured credentials and identity checks.

Asynchronous replication does not guarantee preservation of every acknowledged
SQLite commit. Do not substitute a stale lease, failed probe or Kubernetes taint
for confirmed fencing. See [recovery guarantees](docs/failure-recovery.md).
