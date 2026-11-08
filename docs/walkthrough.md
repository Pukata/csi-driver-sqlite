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
Declaring it dead after a missed heartbeat is insufficient. Automatic takeover
requires an independent authority to stop the exact mapped machine and confirm
it cannot restart under the configured operational controls. Only then does the
driver verify a recovery seed, quarantine the old node and release ownership.
The new writer uses a separate replication history.

The [Redfish integration](../integrations/redfish/README.md) supplies one authority
implementation. Its manual-power-on precondition is explicit; it does not pretend
that a BMC setting prevents every other actor from starting a host.

## Evidence is executable

The [CI harness](testing.md) provisions its own kind cluster and asserts denied
fencing preserves ownership while the isolated writer still commits. It then
kills the old worker, checks a missing replica blocks takeover, restores the
archive, verifies the same PV recovers elsewhere, returns the stale worker under
quarantine, and checks clean rejoin. The real Gatus manifest must retain original
monitoring rows across a planned move and in fresh replica restores.

Separate [resource testcases](performance.md) exercise deterministic database
sizes, marker convergence, integrity and matching logical content. CI artifacts
contain results and failure diagnostics. These tests model worker containers
and a shared local directory, not physical power or a particular NAS's durability.

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
community. Link a tagged release and its passing CI run when publishing it.
