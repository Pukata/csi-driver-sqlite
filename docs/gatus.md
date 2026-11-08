# Gatus: one SQLite PVC, no attachment volume

[Gatus](https://github.com/TwiN/gatus) is a self-hosted monitoring dashboard.
Its SQLite storage mode keeps monitoring history in one database. The example
supplies configuration through a ConfigMap, so there is no second mutable
application directory to move. Configuration and secrets still need GitOps or
backup management; a database replica does not recreate them.

The checked-in example pins Gatus v5.38.0 and UID/GID 1000. It monitors its own
health endpoint without depending on Internet connectivity. After installing
the driver and its shared replica backend:

```sh
kubectl create namespace gatus
kubectl apply -n gatus -f examples/gatus.yaml
kubectl rollout status -n gatus deployment/gatus
kubectl port-forward -n gatus service/gatus 8080:8080
```

The database path is `/data/database.db`, matching the driver's default storage
class. Open localhost:8080 and let it record checks. For a planned move, change
the deployment's node selection to another enabled node; Recreate stops the old
instance before starting the replacement. Do not force-delete an unreachable
writer to manufacture a clean move.

## Executable acceptance case

`make e2e` runs this exact manifest in its own disposable cluster. It asserts:

1. The real Gatus image becomes ready on the first worker and writes successful checks.
2. A fresh Litestream restore contains those actual check records.
3. Recreate moves the app to the second worker with the same PV identity.
4. Original record IDs, results and timestamps survive, and new checks are added.
5. Another fresh replica restore retains the original history.

These checks run on both Kubernetes versions in CI, after the driver's denied
fencing, abrupt worker failure, missing archive and stale-worker return tests.
The Gatus case establishes clean movement for this pinned configuration. It
does not by itself establish every app's compatibility or Gatus-specific
sudden-failure durability. CI artifacts report each assertion.

This example starts with a new database. Moving existing application data is
outside the driver's scope; follow the application's backup/restore procedures.
The driver does not convert existing claims or automate application migrations.

Upstream references: [storage configuration](https://github.com/TwiN/gatus#storage)
and [pinned SQLite implementation](https://github.com/TwiN/gatus/blob/v5.38.0/storage/store/sql/specific_sqlite.go).
