# Testing

The CI workflow runs on pushes to `main`, pull requests and manual dispatch.
It uses GitHub-hosted Linux runners and needs no cluster credentials or private
infrastructure. The repository remains experimental even when CI is green.

## Test pyramid

| Layer | What belongs here | Execution |
|---|---|---|
| Unit | Ownership/epoch rules, guarded state transitions, recovery refusal | Go matrix, every PR/push |
| Component/integration | TLS fencing protocol and refusal paths, fault-target ownership guards, real SQLite/Litestream correctness/resource tests | Fast checks and one resource job |
| Cluster end-to-end | Kubernetes scheduling, CSI mounts, real process termination, stale worker return, Gatus compatibility | Two Kubernetes versions |
| NAS integration/end-to-end | Real NFS and SMB CSI provisioning/mounts and NAS/worker fault boundaries | One pinned Kubernetes version |

Keep policy combinations in unit tests. The expensive suites cover crossings
between real components, with bounded waits for observable outcomes. They do
not repeat every policy permutation against every Kubernetes/backend combination.

## Go and chart checks

The Go matrix covers Go 1.26 and 1.27. `make verify test build` checks `gofmt`,
`go vet`, module integrity, race detection, randomized test order, coverage and
a Linux binary build. Coverage files are attached to each run. `make chart`
lints both the default chart and the opt-in fenced recovery configuration.

The tests follow the standard Go `_test.go` and `testing` conventions. External
services in unit tests use `httptest` and temporary directories; tests do not
connect to a real cluster, storage server or fencing authority.

## Kubernetes scenarios

`make image e2e` builds the runtime image and starts a uniquely named kind
cluster with one control-plane node and two workers. CI pins Kubernetes 1.35.8
and 1.36.4 node images by digest. The same Python harness can run locally with
Docker, kind, Helm, kubectl and OpenSSL installed.

Each run owns a separate Docker network, kubeconfig and workspace. The harness
checks node-container identities before injecting faults. It does not use the
current context or attach to an existing cluster. It collects diagnostic logs
before cleaning up only the resources it created.

The scenarios exercise:

- Dynamic PVC provisioning and real CSI node registration and mounts.
- Rejection of a second writer with `ReadWriteOncePod`.
- Integrity and a fresh replica restore of committed test records.
- A partitioned writer while independent fencing is denied.
- Storage-controller rescheduling after its worker is partitioned.
- Actual abrupt termination of the old worker through the test fencing service.
- Missing-replica refusal, followed by recovery when the replica returns.
- Same-PV recovery on the other worker and separate replication history.
- Old-worker return, stale-container/publisher checks, and clean rejoin.
- The real pinned Gatus example: successful monitoring rows, fresh replica
  restore, clean movement to another worker and preserved history plus new checks.

`make integration-test` exercises the independent Redfish authority's protocol
and refusal paths, plus fast NAS fault-target ownership guards. `make benchmark` runs three database correctness/resource
testcases in the built image; see [measurement limits](performance.md). These
are regression checks in CI, not optional manual experiments. Redfish BMC
responses are fixtures; physical server behavior is not established by them.

Artifacts include a JSON scenario summary, Kubernetes events and pod/driver
logs. Test tokens, TLS private keys and kubeconfigs are excluded from artifacts.
Failures must be diagnosed rather than bypassed by forcing volume ownership.

## Real NAS integration

```sh
make image nas-image nas-test
make image smb-image smb-test
```

This suite uses a separate userspace NFS-Ganesha container outside a three-node
kind cluster. The replica claim is dynamically provisioned by upstream
`nfs.csi.k8s.io` chart 4.13.5, downloaded from its release tag and checksum-checked.
The tests inspect actual worker mount types and sources to require NFSv4.1;
there is no shared host-directory mount into the Kubernetes nodes in this suite.
Only the NAS container sees the persistent export directory directly.

The SMB variant runs the same four cases against a separate Samba container and
the upstream `smb.csi.k8s.io` chart 1.19.1, with a checksum-checked archive. It
checks the CIFS mount type and server source on both workers. The disposable
share uses a fresh per-run password held in a private file and a Kubernetes Secret;
the server publishes no host port. Credentials are excluded from
the collected artifacts. SMB3, strict caching and `noserverino` are selected for the
fixture. The Linux host must provide the CIFS kernel module; CI loads it before
running this variant.

One cluster is reused, with a new application PVC and independent baseline records
for each of four named unittest cases. It stops after the first failed case rather
than manufacturing a healthy fixture by resetting ownership. Every case verifies
actual database records through a fresh Litestream restore, not just Pod readiness.

| Case | Required outcome |
|---|---|
| NAS process killed and restarted with its data | Published records survive and replication resumes |
| NAS disconnected during planned movement | Local commits still work; an observed unpublish cannot release ownership without verification; reconnect permits the move and preserves those commits |
| Worker partitioned while NAS is available | Independent fencing stops it; the same PV recovers elsewhere; returning worker is audited before deliberate rejoin |
| Worker fails while NAS is disconnected | Fencing still works; ownership stays locked and no replacement writer becomes ready until the NAS returns; recovery retains verified baseline records |

The combined-failure case acknowledges a local write during the NAS outage.
Preservation of that unpublished write is deliberately not promised; the known
published baseline must survive. Integrity, PV identity, epoch advancement and
separate replication history are asserted after recovery.

Negative observations start after evidence that clean unpublish or fenced recovery
has actually been attempted, then sample the invariant for eight seconds. They
complement the fast policy unit tests; they do not prove indefinite availability.
Waits have deadlines, and no failed assertion is retried as a whole test.

The job emits `cases.json`, `junit.xml`, topology/version details and diagnostic
logs. It runs on one pinned Kubernetes version in every CI/release run; the
shared-directory E2E suite retains the two-version compatibility matrix.

Run only on a disposable/trusted Linux Docker host with NFS client kernel support.
The test NAS is privileged for VFS file handles, uses a test-only unsquashed export,
and publishes no host port. Clients use **hard** mounts with shortened retransmission
timers and disabled attribute caching; the server retains a ten-second lease/grace
period. These fixture settings keep CI bounded and are not recommended NAS policy.
Actual NFS server packages are recorded with the result. The base image and chart
archive are pinned; this does not certify a vendor NAS firmware, SMB authentication or root-squash
policy, physical power-loss durability, or a Kubernetes control-plane outage.

## What these tests do not establish

The original two-version suite's replica backend is a shared host directory mounted into the test nodes.
This does not model a storage server's crash semantics, filesystem durability,
physical host power loss, or Kubernetes control-plane quorum failure. Test
commits are verified in a restored replica before the worker is terminated;
this does not prove zero loss of acknowledged but unpublished writes.

Validate the selected backing filesystem, external fencing implementation,
control-plane topology, application behavior and independent backup restore
before using real data. See [failure recovery](failure-recovery.md).

## Workflow references

The pipeline uses standard patterns from
[Kubernetes klog's Go test workflow](https://github.com/kubernetes/klog/blob/master/.github/workflows/test.yml),
[kind's Docker test workflow](https://github.com/kubernetes-sigs/kind/blob/main/.github/workflows/docker.yaml),
and [GitHub's Go CI guide](https://docs.github.com/en/actions/tutorials/build-and-test-code/go):
version matrices, race tests, bounded jobs, multi-node disposable clusters and
failure artifacts. It is a small project-specific workflow, not a copy of the
upstreams' full test infrastructure.
