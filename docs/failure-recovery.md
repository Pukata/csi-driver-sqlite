# Failure recovery

The driver supports one application writer, local SQLite, shared NAS replication
and verified clean moves. Kubernetes manages the application lifecycle. There is
no automatic node-loss takeover, external fencing integration or host power control.

## Clean movement

Kubernetes stops the old application Pod. The node driver unmounts its target,
stops replication, restores a fresh copy from the shared replica and compares
SQLite integrity and logical contents with the local database. Only successful
verification marks the volume clean and permits controller detach. The next node
claims a new epoch and restores the replica before exposing the volume.

If the NAS is unavailable, replica writes fail, or verification fails, ownership
stays locked. Restore NAS access, inspect node-driver logs and let Kubernetes
retry clean unpublish. Do not clear ownership or mark the volume clean by hand.
An empty, never-initialized volume can detach without a database; a missing
database from an initialized volume is an error, not permission to start empty.

## Node loss and data loss

An unreachable node may still run its application. No timeout, failed health
probe, Pod deletion, cordon or taint proves that it has stopped. The driver never
automatically releases its prepared owner or changes its epoch after node loss.
A replacement Pod may wait for storage indefinitely. This is the intended contract.

A successful SQLite commit is not a remote durability acknowledgement. Replication
is asynchronous; recent unpublished writes may be lost after sudden failure, and
a NAS outage can extend that loss window indefinitely. Keep independent backups.

## Diagnose a locked volume

Read the PV's `spec.csi.volumeHandle`, then run:

```sh
python3 tools/volume-status.py --context <context> -n <driver-namespace> <volume-id>
```

This only reads the corresponding ConfigMap and requires permission to get it.
Use `--file <captured-configmap.json>` for offline inspection. Without `--context`,
the tool uses the current kubectl context. Review captured state before sharing it.

A prepared, dirty owner is normal while an app is writing. Check application and
node health, Kubernetes events, and node-driver/Litestream logs to distinguish
an active writer from a failed clean move. The state does not persist replication
or verification timestamps or the last error; the tool cannot infer freshness.

## Manual node-loss recovery

1. Keep ownership locked. Identify the original machine and ensure the old
   application cannot write or restart, including after networking returns.
   Stopping or isolating the machine is an operator responsibility, outside this
   driver. Kubernetes API state alone cannot establish this.
2. Preserve its local database, WAL files, driver state and shared replica history.
   If the original node can safely return, prefer restoring its connectivity and
   NAS access, then stopping the app through Kubernetes for a verified clean move.
   Keep any alternative application instance stopped during this work.
3. If the original node cannot return, restore an independent backup or a copy
   of the latest usable replica to isolated local scratch. Verify SQLite integrity
   and application records, and assess missing writes. Preserve the originals.
4. Recovery onto a new PVC needs an application-specific, rehearsed import and
   cutover. The driver has no force-release or backup-import command. Follow the
   [independent backup guidance](independent-backup.md); it describes the limits
   of the currently validated restore checks. Never replace a database file under
   a running application or Litestream publisher.
5. Before reusing the old node, remove stale application containers, mounts and
   publishers under operator control. Keep its old data for diagnosis and never
   copy it over the new writer. Only allow one application instance after cutover.

Do not patch owner, epoch, clean flags, recovery fields or replica paths to force
same-volume takeover. A surviving control plane and reachable replica storage
are needed for Kubernetes to move the application, but do not resolve a lost owner.

## Upgrading legacy automatic-recovery volumes

Older revisions offered `sqlite-csi-ha`, `recoveryPolicy: latest-replica` and
`failover.enabled`. These are removed. Helm rejects `failover.enabled=true`, and
new volumes accept only an omitted policy or `manual`.

Existing automatic-recovery policy, recovery journal/seed fields or nonstandard
replica directories require migration before use. The driver preserves legacy
JSON state and rejects attachment, replication restart and clean release for
such volumes. This also applies to sessions loaded after a node-agent restart;
an old matching owner and epoch do not make the legacy history supported.

Before upgrading, stop and back up affected applications with the previous
version and rehearse migration to new manual-policy PVCs. Changing only a
StorageClass, clearing legacy fields, or removing the Helm flag does not migrate
stored volume or local session state. If already upgraded into a blocked state,
keep data intact and use the previous pinned version under operator control to
prepare migration. The status tool identifies unsupported legacy state without
interpreting old recovery phases as permission to resume a takeover.

Existing manual volumes using the default `replica` history remain compatible.
Historical release tests describe their original revisions, not the current
feature set; see [test scope](testing.md) and [recorded results](results/README.md).
