# Failure recovery

Clean handover and unclean takeover have different guarantees. Clean handover
compares the local database with a freshly restored replica before releasing
ownership. Sudden failure cannot prove that every committed write was replicated.

## Defaults and data loss

The default `sqlite-csi` class requires manual recovery after an unclean node
failure. No lease timeout or failed health probe releases a prepared writer.

The optional `sqlite-csi-ha` class uses `recoveryPolicy: latest-replica`.
It requires an independently secured fencing authority and accepts loss of
unpublished writes. A successful SQLite commit is not a remote durability
acknowledgement. A replica outage can extend the loss window indefinitely.
Zero acknowledged-write loss requires synchronous durability and is not provided.

## Recovery order

1. Observe the owner node as NotReady or Unknown past the configured grace.
2. Journal recovery intent against the current owner and epoch using API CAS.
3. Recheck the node UID and failure transition.
4. Ask the independent authority to stop the mapped machine and inhibit restart.
5. Require fresh, matching power-off confirmation over authenticated TLS.
6. Restore and integrity-check an immutable seed from the shared replica.
7. Cordon the failed node and add the out-of-service NoExecute taint.
8. Release the same guarded owner/epoch and restore the seed on a replacement.
9. Publish the replacement into a new replica directory, separate from old history.

Failed authentication, wrong identity, unavailable fencing, missing/corrupt
replicas, or failed quarantine leaves ownership locked. Missing data never
becomes permission to initialize an empty replacement database.

## External fencing API

The driver has no infrastructure-specific power-management backend. An operator
supplies a service implementing this contract and connects it to an independent
power or storage fence. That service must remain available outside the failed
node, verify immutable machine identities, and prevent independent auto-restarts.

The controller sends `POST <endpoint>/v1/fence` with a projected bearer token:

```json
{
  "nodeName": "worker-a",
  "nodeUID": "kubernetes-node-object-uid",
  "machineID": "immutable-machine-identity",
  "operationID": "recovery-operation-id",
  "challenge": "fresh-random-challenge"
}
```

Only after independently confirming the old writer is stopped and cannot
restart, the service returns HTTP 200 with the same five fields plus:

```json
{
  "state": "off",
  "restartInhibited": true,
  "observedAt": "2026-01-01T12:00:00Z"
}
```

Those snippets describe one combined response object, not two separate JSON
documents. Echo the exact challenge and identities. Verify physical stop on
every request, including retries of the same operation ID. A queued stop request,
cordon, taint, stale lease, process probe or successful HTTP request is insufficient.
Never fabricate confirmation when the underlying fencing mechanism is unreachable.

The driver rejects redirects, identity/challenge mismatch, non-`off` state,
allowed restarts, stale proofs, unknown fields and invalid TLS. Clocks must be
synchronized: the response observation must be within the current request's
time window with up to five seconds of skew, and no older than one minute.
Requests time out after 60 seconds; timeout means no proof. Token files are
reread for every request to support projected-secret rotation.

The shared operation ID identifies an incident; the fresh challenge prevents
reusing an earlier response as evidence that a machine is still stopped.
The driver necessarily trusts the external authority's implementation of this
contract. The included Docker service is a **test fixture**, not a deployable
production fencing service.

## Configuration

Mount a Secret containing `config.json`, `ca.pem` and `token`. The token file
contains the raw bearer token, without a `Bearer ` prefix:

```json
{
  "endpoint": "https://fencing.example:9443",
  "caFile": "/fencing/ca.pem",
  "tokenFile": "/fencing/token",
  "manualPowerOnOnly": true,
  "graceSeconds": 120,
  "nodes": {
    "worker-a": {
      "nodeUID": "replace-with-node-object-uid",
      "machineID": "replace-with-immutable-machine-identity"
    }
  }
}
```

The service endpoint must be HTTPS with no credentials, query or fragment in
the URL. Trust its CA explicitly, scope its credentials to the exact nodes, and
restrict access to the Secret. `manualPowerOnOnly` is an operator guarantee;
setting it does not disable an external auto-restart manager.

Enable `failover.enabled`, set `failover.configSecret`, and select the separate
`sqlite-csi-ha` class only for workloads accepting this policy. Existing manual
volumes do not automatically opt in when fencing is enabled. Replacing a node
object or machine requires deliberately updating its identity mapping.

## Rejoin and limitations

The driver never powers a node back on or clears its quarantine automatically.
Keep it cordoned and out-of-service while confirming recovery, removal of old
application containers, cleanup of stale mounts, and absence of stale publishers.
Preserve its old local database and replica history for diagnosis. Never copy
the old database over the new writer. Rejoin is a separate operator action.

Automatic recovery also requires a surviving Kubernetes control plane and a
reachable replica backend. A single-server cluster cannot reschedule workloads
when its only server fails. Read [test coverage and limits](testing.md); passing
container-based worker tests does not prove physical-host or backend durability.
