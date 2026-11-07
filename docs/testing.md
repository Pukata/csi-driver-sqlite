# Testing

The CI workflow runs on pushes to `main`, pull requests and manual dispatch.
It uses GitHub-hosted Linux runners and needs no cluster credentials or private
infrastructure. The repository remains experimental even when CI is green.

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

Artifacts include a JSON scenario summary, Kubernetes events and pod/driver
logs. Test tokens, TLS private keys and kubeconfigs are excluded from artifacts.
Failures must be diagnosed rather than bypassed by forcing volume ownership.

## What these tests do not establish

The replica backend is a shared host directory mounted into the test nodes.
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
