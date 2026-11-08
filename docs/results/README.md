# Recorded testcase results

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
