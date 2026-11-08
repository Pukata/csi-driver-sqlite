# Correctness and resource testcases

`make image benchmark` runs the shipped Litestream version and the exact digest
program extracted from `node.go`. It uses a disposable, network-disabled container
with 2 CPU and 512 MiB limits and a disposable host-disk directory. It never opens an
existing database. The generated JSON is uploaded with the Kubernetes CI artifacts.

Each of the 1, 16 and 64 MiB deterministic fixtures is a testcase, with assertions:

- SQLite WAL commits converge to a fresh replica restore within 90 seconds.
- A new committed marker becomes visible in a second verified restore.
- Graceful publisher exit succeeds; restored and original logical digests match.
- SQLite integrity checks pass.
- Each sampled digest process stays below a broad 384 MiB regression budget.

The test records fixture/database bytes, observed initial replication plus restore
time, commit-to-verified-restore time, restore/digest durations and sampled child
RSS. Timing includes polling/restore work; it is not pure network replication lag.
Publisher RSS is sampled separately for each database testcase. The Gatus
Kubernetes case records patch-to-ready-and-verified-history time in a separate
artifact; it includes scheduling and polling, unlike the isolated restore timings.
RSS is sampled every 5 ms and can miss short peaks. Limits also apply to the whole
container. A single large BLOB still makes a large SQL dump row: streaming the dump
avoids joining the entire database into memory, not all allocations.

These are measurements of one sample per size on the recorded runtime/kernel,
with a local host filesystem. They are not NAS numbers, a latency SLA,
whole-cluster memory measurements, or end-to-end Kubernetes failover times. The
kind tests establish behavior under node faults separately. Restore and integrity
verification cost grows with database size; unrelated volume operations currently
share a node-agent mutex and can wait behind a slow operation.

Unclean recovery restores and checks the database in the controller's local
temporary directory, then flushes and verifies an immutable seed on the shared
filesystem before ownership can change. Allow controller scratch disk space for
a restored database and Litestream's temporary files. NAS stable-write and rename
durability remain requirements; a verified readback cannot prove hardware power-loss
durability.

The chart requests 30m CPU/96Mi for its three controller containers and 35m
CPU/80Mi per enabled node for agent plus registrar. These are scheduling requests,
not measured consumption. Litestream runs as a child of the node agent for each
active writable database, sharing its 512Mi memory limit. Size and measure that
limit for your actual apps. Append-only publisher logs and retained histories also
consume disk; monitor them and manage retention before adopting many volumes.

Validated [result snapshots](results/README.md) include their exact CI source
revision and workflow link. Re-run the test rather than assuming a previous
runner's numbers predict your storage backend.
