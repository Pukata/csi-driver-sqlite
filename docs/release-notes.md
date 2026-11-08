# First experimental distribution

SQLite PVC mobility for single-instance self-hosted apps, using local working
storage and Litestream replicas on an existing shared RWX filesystem.

- Normal Kubernetes PVC interface, one writer per volume, verified clean handover.
- Opt-in latest-replica recovery after independently confirmed fencing.
- Redfish fencing authority for explicitly configured, manually powered-on hosts.
- Disposable multi-node recovery tests, real Gatus compatibility test and
  correctness/resource testcases for three database sizes.

Read README.md and docs/failure-recovery.md before enabling recovery. This is
pre-release storage software, not a durability or availability guarantee.
Default recovery after sudden failure remains manual. Shared storage and a
surviving control plane are required. Maintain independent backups.

Initial images target Linux amd64 only. The chart archive pins the release
image tag. Record and pin the image digest from the attached manifest for
repeatable installs. Package visibility is separate from repository visibility;
private packages require registry credentials.
