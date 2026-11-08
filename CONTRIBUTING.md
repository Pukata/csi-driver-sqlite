# Contributing

The project is experimental. Small, reviewable changes with reproducible tests
are welcome. Open an issue to discuss storage-format or recovery-policy changes.

## Development

Use Linux, a supported Go toolchain, Make and Helm 3. The runtime uses Linux
mounts and process credentials; native Windows and macOS builds are not targets.
Keep changes formatted with `gofmt` and dependencies in `go.mod` / `go.sum`.

```sh
make fmt
make verify test build chart
```

Go tests use the race detector and randomized test order. Coverage is reported
as an artifact, without a percentage target that encourages superficial tests.
Add tests for changed behavior, especially ownership conflicts and unsafe
recovery attempts. Missing data must never silently become a fresh database.

## Kubernetes tests

Install Docker, kind v0.33.0, kubectl, Helm 3, Python 3, then run:

```sh
make image e2e
```

The harness creates a uniquely named kind cluster and a separate kubeconfig.
It never uses the active Kubernetes context or an existing cluster. Test data is
disposable. Cleanup checks ownership of the resources created by that run.
Logs and a result summary are saved under `.e2e/`.

CI tests the same harness on two Kubernetes minor versions. It covers real CSI
mounts, single-writer enforcement, verified replica contents, same-PV clean
movement and preservation of real Gatus history.
An external shared filesystem is simulated with a directory mounted into the
test nodes. These checks do not validate a particular storage server, physical
host failure or control-plane quorum loss.

## Pull requests

Explain the behavior being changed, why it matters, and how you tested it.
Include relevant versions and sanitized logs. Never include database contents,
kubeconfigs, private keys or credentials. Recovery changes need a negative
test showing that an unsafe takeover remains blocked.

Use GitHub's noreply address if you want to keep your email out of public Git
history. Dependency and GitHub Actions updates are proposed by Dependabot.
