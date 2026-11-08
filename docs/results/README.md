# Recorded testcase results

## Current simplified driver

Source: `ee26bfca644e28f1ca67f479d160e933607a9f63`.
[Passing CI workflow](https://github.com/Pukata/csi-driver-sqlite/actions/runs/42511657962).

- Kubernetes 1.35.8 and 1.36.4 passed real CSI mounting, single-writer rejection,
  driver restart, clean movement and Gatus history-preservation checks.
- All three real-NFS cases passed: NAS restart, an unresponsive NAS blocking
  handover until recovery, and failed replica writes blocking handover until repair.
- Both Go versions, race checks, ownership mutations, chart/workflow checks,
  resource checks and SMB protocol tests passed.
- The upgrade-only job is reserved for manual/release validation.

These tests use disposable data, clusters and an NFS server. They do not certify
every NAS or promise preservation of unpublished writes after node loss.
See [current test scope](../testing.md).

## Historical results

The observations below apply to their named source revisions. The current driver
has removed automatic node-loss takeover and Redfish/fencing integration.
Earlier results are retained unchanged as evidence of those implementations.

## SMB protocol failure modes

The former SMB Kubernetes/NAS suite and its automatic CI job were removed.
The replacement speaks SMB3 with Samba and userspace `smbclient`: file round-trip,
failed authentication, missing/read-only shares, reset, timeout, unavailable
server, and contents surviving a clean server restart.

Eight cases passed in three consecutive local runs of the earlier harness, and
once after its Docker failure checks were added. Digests, image IDs, package
versions, scope and cleanup evidence are in [smb-protocol.json](smb-protocol.json).
The protocol suite also [passed on GitHub at source `3410200`](https://github.com/Pukata/csi-driver-sqlite/actions/runs/42503207412).
It now runs as the `smb-protocol` job in the shared CI workflow, including when
that workflow is reused for release validation. Manual dispatches and reruns
require fresh operator approval. These protocol checks make no kernel CIFS, CSI,
SQLite, fencing or physical-durability claim.

## Real NFS recovery

Source: `a755090fb2a637df2631cc0a47d773408090aaa3`.
[Passing CI workflow](https://github.com/Pukata/csi-driver-sqlite/actions/runs/42463780493).
All four real-NFS cases passed using a separate NFS-Ganesha server and a
three-node kind cluster: NAS restart, planned move during a NAS outage, worker
failure, and worker failure during a NAS outage. Each case uses a fresh PVC and
verifies database records through a fresh replica restore. The two-version
shared-directory suite, both Go jobs and resource checks also passed.

The suite exposed a recovery error when restoring a working database directly
onto NFS. Recovery now restores locally, validates SQLite, and flushes and checks
an immutable seed before publishing it on the NAS. Failed copies cannot publish
a partial seed or unlock ownership.

[nas.json](nas.json) records the source, backend versions, testcase outcomes and
durations. Durations include case setup and cleanup; they are not failover SLAs.
This is a disposable NFS integration result, not vendor NAS or physical power-loss
certification. See [test scope and limits](../testing.md#real-nas-integration).
This source is newer than the alpha.1 release below.

## Alpha.1 release baseline

Source: `715a9a7c0dda5480a65193b1321596991ad36ed3`. [Passing release workflow](https://github.com/Pukata/csi-driver-sqlite/actions/runs/42457082286).
These are observed CI results, not durability or performance guarantees.

Both Kubernetes 1.35.8 and 1.36.4 passed all 23 assertions: 17 ownership/recovery
checkpoints and 6 real Gatus application checks. Go 1.26/1.27, race checks,
the 7 Redfish protocol tests, chart checks, workflow lint and vulnerability
checking also passed. Physical BMC behavior is not certified by these tests.

The resource testcases use a local Docker-host directory, 2 CPU and a 512 MiB
container limit, Litestream v0.5.18, and one sample per fixture size.
Memory columns are sampled process RSS, not whole-node consumption. Commit
visibility includes polling and a fresh restore. See [method and limits](../performance.md).

| Fixture MiB | Publisher peak MiB | Digest peak MiB | Restore seconds | Commit to verified restore seconds |
|---|---|---|---|---|
| 1 | 40.7 | 18.5 | 0.032 | 0.976 |
| 16 | 41.1 | 18.7 | 0.205 | 0.920 |
| 64 | 52.0 | 18.7 | 0.796 | 2.575 |

Full resource observations are in [benchmark.json](benchmark.json); exact
Kubernetes assertions and Gatus movement times are in [kubernetes.json](kubernetes.json).
Tests generated disposable data and did not access an existing cluster or NAS.
