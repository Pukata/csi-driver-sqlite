# v0.2.0-alpha.2: SQLite PVC mobility

SQLite PVC mobility for one replica of a self-hosted service, using local working
storage and Litestream replicas on an existing shared RWX filesystem.

- Kubernetes manages application lifecycle and scheduling.
- One writer per volume, persistent ownership and epochs, verified clean handover.
- Failed NAS verification blocks movement; an unreachable node retains ownership.
- Node-loss recovery is manual. Automatic takeover, external fencing, Redfish and
  the HA StorageClass have been removed.
- Legacy automatic-recovery volumes require migration before upgrading; their
  persisted state is retained and rejected rather than silently reinterpreted.
- Disposable clean-movement and NAS fault tests, driver restart/upgrade checks,
  real Gatus compatibility and correctness/resource testcases for three sizes.

Read README.md and docs/failure-recovery.md before upgrading. This is pre-release
storage software, not a durability or availability guarantee. Shared storage and
a surviving control plane are required for movement. Keep independent backups.

Images target Linux amd64. The chart archive pins the release image tag. Record
and pin the image digest from the attached manifest for repeatable installs.
Package visibility is separate from repository visibility; private packages
require registry credentials. Historical releases retain their original features
and recorded test results.
