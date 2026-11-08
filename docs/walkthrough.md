# Why my SQLite app could move, but its database could not

I wanted to run ordinary self-hosted apps on a small Kubernetes cluster and learn
node maintenance and recovery. Many of those apps use SQLite because one process
and a file are enough. Adding a second worker made the limitation visible: a
local PVC kept the app on the machine containing its database.

The goal here is one instance of each app that can move. It is not several
instances writing the same SQLite file. Gatus provides a small example: its
configuration is a ConfigMap and its monitoring history is a database PVC.
Homebox adds a separate shared attachment volume. See the [Gatus testcase](gatus.md)
and [Homebox storage layout](homebox.md).

## Move the interface, preserve the constraints

`csi-driver-sqlite` presents a normal Kubernetes PVC. The active database stays
on local disk. A node agent runs Litestream for each active writable database,
publishing a replica to an existing shared RWX backend. Before exposing that
volume elsewhere, the replacement restores and validates its local copy.

Clean movement stops the application, verifies a final replica against the local
database and releases persistent ownership. A monotonically increasing epoch
distinguishes successive owners. A timer expiring cannot make two writers safe.

## The failure case that matters

A node that cannot reach Kubernetes might still run the app and access storage.
Declaring it dead after a missed heartbeat is insufficient. The driver keeps
ownership locked, even if Kubernetes schedules a replacement. Node-loss recovery
is manual: an operator must ensure the old writer cannot run and assess the
available database or backup. See the [recovery guidance](failure-recovery.md).

The same conservative rule applies to planned moves. If the NAS cannot supply a
verified final replica, the move waits. Fixing NAS access lets Kubernetes retry
the clean handover. There is no host power controller or automatic takeover path.

## Evidence is executable

The [test harness](testing.md) provisions its own kind cluster, verifies replica
records, blocks a second writer and moves the app cleanly while preserving its
PV and records. The real Gatus manifest must retain monitoring history across a
planned move and continue writing. A separate NFS suite checks server restart,
an outage blocking movement and failed replica writes blocking handover.

Separate [resource testcases](performance.md) exercise deterministic database
sizes, marker convergence, integrity and matching logical content. CI artifacts
contain results and failure diagnostics. The shared-directory suite and disposable NFS server do not establish physical
power-loss behavior or a particular NAS's durability.

## Where this belongs

This is experimental storage software for compatible, single-instance homelab
apps. Async replication can lose unpublished acknowledged writes. Independent
backups remain necessary, and a surviving Kubernetes control plane and replica
backend remain dependencies. [Alternatives](alternatives.md) may fit better.

The repository includes source, chart, tests and release automation under MIT.
An outside user reproducing the testcase is more useful evidence than a claim
that storage is solved. Start with disposable data and report the exact image,
backend and reproduction when behavior differs.

This article is a prepared announcement draft. It has not been posted to any
community. The historical first reference, before automatic takeover was removed, is [v0.2.0-alpha.1](https://github.com/Pukata/csi-driver-sqlite/releases/tag/v0.2.0-alpha.1),
tested at source `715a9a7c0dda5480a65193b1321596991ad36ed3` by the
[passing release workflow](https://github.com/Pukata/csi-driver-sqlite/actions/runs/42457082286).
See the [recorded assertions and measurements](results/README.md).
