# Testing

The CI workflow runs on pushes to `main`, pull requests and manual dispatch.
It uses GitHub-hosted Linux runners and needs no cluster credentials or private
infrastructure. The repository remains experimental even when CI is green.
Validate locally first; do not dispatch or rerun Actions without fresh operator
approval. Historical results in `docs/results/` cover their named revisions and
may describe features and cases that have since been removed.

## Test pyramid

| Layer | What belongs here | Execution |
|---|---|---|
| Unit | Ownership/epoch rules, guarded state transitions, dirty-release and legacy-state refusal | Go matrix, every PR/push |
| Component/integration | Volume diagnosis, backup restore, fault-target ownership guards, real SQLite/Litestream correctness/resource tests | Fast checks and one resource job |
| Cluster end-to-end | Kubernetes scheduling, CSI mounts, clean movement, driver restarts, Gatus compatibility; baseline upgrade on manual/release runs | Two Kubernetes versions |
| NAS integration/end-to-end | Real NFS CSI provisioning/mounts and NAS fault boundaries | One pinned Kubernetes version |

Keep policy combinations in unit tests. The expensive suites cover crossings
between real components, with bounded waits for observable outcomes. They do
not repeat every policy permutation against every Kubernetes/backend combination.

## Go and chart checks

The Go matrix covers Go 1.26 and 1.27. `make verify test build` checks `gofmt`,
`go vet`, module integrity, race detection, randomized test order, coverage and
a Linux binary build. Coverage files are attached to each run. `make chart`
lints the default chart and verifies that obsolete `failover.enabled=true` is rejected.

The tests follow the standard Go `_test.go` and `testing` conventions. External
services in unit tests use `httptest` and temporary directories; tests do not
connect to a real cluster or storage server. The ownership
state-machine suite records a seed and trace on failure. A separate lightweight
CI job runs `make mutation-test`: it makes temporary source copies and requires
focused tests to reject representative ownership, epoch and dirty-release mutations.
A compile or setup error does not count as detecting a mutation.

## Kubernetes scenarios

`make image e2e` builds the runtime image and starts a uniquely named kind
cluster with one control-plane node and two workers. CI pins Kubernetes 1.35.8
and 1.36.4 node images by digest. The workflow builds the current driver image once and shares it across cluster jobs.
The same Python harness can run locally with
Docker, kind, Helm and kubectl installed.

Each run owns a separate Docker network, kubeconfig and workspace. The harness
checks node-container identities before injecting faults. It does not use the
current context or attach to an existing cluster. It collects diagnostic logs
before cleaning up only the resources it created.

The scenarios exercise:

- Dynamic PVC provisioning and real CSI node registration and mounts.
- Rejection of a second writer with `ReadWriteOncePod`.
- Controller and node-agent restarts during replication, retaining owner, epoch, records, integrity and a fresh replica restore.
- Integrity and a fresh replica restore of committed test records.
- A planned move to another worker after Kubernetes stops the old Pod.
- Preservation of the PV identity and all baseline records, with epoch advancement.
- Continued writes and replication from the destination node.
- The real pinned Gatus example: successful monitoring rows, fresh replica
  restore, clean movement to another worker and preserved history plus new checks.

CI runs Python fixture tests once, in the Go 1.27 job.
`make integration-test` exercises the read-only volume diagnostic and an isolated
SQLite online-backup restore, plus fast NAS fault-target ownership guards.
`make benchmark` runs three database correctness/resource
testcases in the built image; see [measurement limits](performance.md). These
are regression checks in CI and can also run locally.

Artifacts include a JSON scenario summary, Kubernetes events and pod/driver
logs. Kubeconfigs are excluded from artifacts.
Failures must be diagnosed rather than bypassed by forcing volume ownership.

## Upgrade exercise

On manual and release runs, CI builds the pinned `v0.2.0-alpha.1` baseline
(commit `94832bcf35f0cb3159e9f7e51b9efad590c7c993`) and reuses the current image built for the workflow, then upgrades an existing
volume in an isolated Kubernetes 1.36.4 kind cluster. Ownership, records,
integrity and fresh replica restores are checked across the upgrade.
This exercises that baseline-to-current transition on the shared-directory
backend, not arbitrary versions, physical hosts or NAS failure semantics.

To run locally, prepare a separate checkout at that commit and build it as
`sqlite-csi:baseline`, build the current image with `make image`, then run
`make maintenance-test BASELINE_REPO=/path/to/baseline`. The harness needs
Docker, kind, Helm and kubectl and creates its own cluster. Keep the
baseline checkout outside the current Docker build context. Artifacts are
collected under `.e2e/artifacts/`; failures do not reset ownership.

## Real NAS integration

```sh
make image nas-image nas-test
```

This suite uses a separate userspace NFS-Ganesha container outside a three-node
kind cluster. The replica claim is dynamically provisioned by upstream
`nfs.csi.k8s.io` chart 4.13.5, downloaded from its release tag and checksum-checked.
The tests inspect actual worker mount types and sources to require NFSv4.1;
there is no shared host-directory mount into the Kubernetes nodes in this suite.
Only the NAS container sees the persistent export directory directly.


One cluster is reused, with a new application PVC and independent baseline records
for each of three named unittest cases. It stops after the first failed case rather
than manufacturing a healthy fixture by resetting ownership. Every case verifies
actual database records through a fresh Litestream restore, not just Pod readiness.

| Case | Required outcome |
|---|---|
| NAS process killed and restarted with its data | Published records survive and replication resumes |
| NAS container paused during planned movement | Local commits still work; an observed unpublish cannot release ownership without verification; unpausing permits the move and preserves those commits |
| Replica path obstructed | A write through the mounted share fails; unpublish remains locked until the original history is restored, then a clean handover retains local commits |

The outage and write-failure cases commit locally before requesting movement.
Those records must survive after NAS repair and a verified clean handover.
No worker-loss recovery is attempted.

Negative observations start after evidence that clean unpublish
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
archive are pinned; this does not certify a vendor NAS firmware or root-squash
policy, physical power-loss durability, or a Kubernetes control-plane outage.
The write failure uses a path obstruction; it does not simulate
ENOSPC or failure of an entire share.

## What these tests do not establish

The original two-version suite's replica backend is a shared host directory mounted into the test nodes.
This does not model a storage server's crash semantics, filesystem durability,
physical host power loss, or Kubernetes control-plane quorum failure. Test
commits are verified in a restored replica before planned movement;
this does not prove preservation of unpublished writes after node loss.

Validate the selected backing filesystem, manual recovery procedure,
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

## SMB protocol failure modes

```sh
make smb-protocol-image smb-protocol-test
```

This replacement uses a disposable Samba server and fresh `smbclient` processes
speaking SMB3. Clients have all Linux capabilities dropped. It needs Docker and
Python, with no Kubernetes setup, kernel filesystem mount or production shares.
Per-run credentials are passed in private files and removed even on test failure.
Cleanup checks exact Docker identities and run labels before removing resources.

The cases cover byte-for-byte file transfer, rejected credentials, a missing
share, denied writes through a read-only share (including an absent-file check),
TCP reset, negotiation timeout, server unavailability, and file contents surviving
a clean Samba restart. Reset and timeout peers accept a request before injecting
the fault. Assertions require specific SMB status codes or the explicit
negotiation-timeout diagnostic; an arbitrary command failure cannot pass.

The old SMB Kubernetes/NAS failure suite and its automatic CI matrix entry were
removed. SMB protocol checks run as the `smb-protocol` job in the shared
`.github/workflows/ci.yml` when relevant paths change in routine CI, and always
on manual and release runs. Release validation reuses that same workflow. Validate locally first and obtain fresh
operator approval before manually dispatching or rerunning Actions.
It does not validate kernel CIFS cache/reconnect behavior, CSI ownership, node-loss recovery,
SQLite replication, interrupted file-transfer recovery or physical power-loss
durability. Those claims require separate tests. Results record the image ID,
Samba/client package versions, test-source digest and protocol observations under
`.e2e/artifacts/`. The [smbclient manual](https://www.samba.org/samba/docs/current/man-html/smbclient.1.html)
describes the authentication-file and timeout options.
