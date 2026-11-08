# Redfish fencing authority

This optional service implements the driver's external fencing contract using
a BMC's standard Redfish ComputerSystem API. It is independent of Kubernetes
and must run on a management machine that survives loss of any fenced host.
It never powers hosts back on. The first implementation is protocol-tested in
CI; no particular physical server model is certified by this repository.

## Preconditions

- Dedicated BMC credentials scoped to the mapped machines and power control.
- Verified HTTPS for the BMC and the authority; no insecure TLS mode or redirects.
- Explicit Kubernetes node UID and Redfish ComputerSystem UUID mapping.
- `PowerRestorePolicy: AlwaysOff` already configured and supported by the BMC.
- **Exclusive manual power-on control:** disable watchdog restart, Wake-on-LAN,
  scheduled power-on, hypervisor/cluster auto-restart and other automation.
  Restrict other power-control credentials and keep fenced machines off until
  an operator has completed quarantine/rejoin checks.

`AlwaysOff` controls recovery after AC power returns. It does **not** block
another API client, a physical button or a watchdog from starting a machine.
`manualPowerOnOnly: true` is an operator assertion about these controls, not a
feature that creates them. If you cannot enforce that precondition, do not use
this authority for automatic takeover. This constraint is part of the proof's
trust boundary, not an unconditional restart-inhibition guarantee.

## Install outside the managed cluster

Copy `config.example.json`, replace all mappings and mount secret files readable
only by the service account (UID 1000 in the image). Use separate incoming bearer
token and outgoing BMC passwords. Supply a server certificate whose SAN matches
the address configured in the driver's `FencingConfig`. For example:

```sh
docker run -d --name sqlite-csi-fence --restart unless-stopped \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 128m --cpus 1 -p 9443:9443 \
  -v /etc/sqlite-csi-fence:/config:ro \
  ghcr.io/pukata/csi-driver-sqlite-fence-redfish:v0.2.0-alpha.1 \
  --config /config/config.json --certificate /config/tls.crt --key /config/tls.key
```

Restrict the listener with a management firewall. While packages are private,
authenticate to GHCR first. Local alternative: `docker build -f
integrations/redfish/Dockerfile -t sqlite-csi-fence-redfish:dev .`.
The listener binds all container interfaces; bind Docker's published port to a
specific management address when appropriate. Apply file rotations atomically;
token and password files are read per request. Configuration/cert changes need
a service restart. Do not put passwords in command lines or Git.

Configure the driver using [failure recovery](../../docs/failure-recovery.md).
Its `machineID` is `redfish:<ComputerSystem UUID>`, and the incoming token and
node UID must match this service. Obtain the UUID by inspecting the exact
configured System resource; do not use a human-readable host name as identity.

## What the service verifies

Every request validates identity before contacting a BMC. It checks the System
UUID and AlwaysOff policy, requires advertised ForceOff support, sends ForceOff
when needed, and polls until a fresh GET reports Off with the same identity and
policy. Timeout, denied credentials, wrong UUID, unsupported policy, invalid TLS,
redirects or unavailable BMC produce no proof. Even an already-off machine is
queried again on retries. The response echoes the driver's fresh challenge.

Run `make integration-test`. Assertions cover actual TLS/authentication at the
service boundary, token rotation, allowlisted identity, ForceOff, repeated
observation, missing policy/action and timeout. BMC power responses are fixtures;
before real use, run an acceptance case on a disposable physical worker and
verify the disabled restart mechanisms remain disabled after loss of power.
Do not fault-inject against a production cluster to satisfy that acceptance.

Protocol reference: [DMTF ComputerSystem schema](https://redfish.dmtf.org/schemas/v1/DSP2046_2025.2.html).
