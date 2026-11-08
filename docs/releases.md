# Installing and making experimental releases

Releases are explicit prereleases such as `v0.2.0-alpha.2`. The release workflow
runs all Go, chart, ownership, resource and Kubernetes testcases against the exact
source revision before publishing images, an OCI Helm chart, checksums and source
identity. No floating `latest` tag is published. Initial images are Linux amd64.

Check the [release page](https://github.com/Pukata/csi-driver-sqlite/releases)
for actually published versions. Workflow configuration alone is not a release.

## Install a published version

The current simplified release is **v0.2.0-alpha.2**. Alpha.1 is historical and
includes the retired automatic-recovery integration. See the
[README quickstart](../README.md#quickstart-gatus-on-a-movable-sqlite-pvc)
for installation and a working Gatus example.

You need Linux amd64 nodes, permission to run privileged CSI node agents, and an
existing shared RWX class whose replica mount is writable by UID/GID 1000.
Replace the node names and storage class below with your own.

```sh
kubectl create namespace sqlite-csi-system
kubectl label namespace sqlite-csi-system pod-security.kubernetes.io/enforce=privileged
kubectl label node worker-a worker-b sqlite.csi/enabled=true
helm upgrade --install sqlite-csi oci://ghcr.io/pukata/charts/sqlite-csi \
  --version 0.2.0-alpha.2 --namespace sqlite-csi-system \
  --set createBackingClaim=true --set backingStorageClass=YOUR_SHARED_RWX_CLASS \
  --set localPath=/var/lib/sqlite-csi-data --wait --timeout 5m
```

Alternatively, create a shared RWX PVC in the driver namespace, set
`backingClaim` to its name and omit `createBackingClaim`/`backingStorageClass`.
The application must use UID/GID 1000, one replica, Recreate and ReadWriteOncePod.

The packaged chart pins its corresponding image tag. For stricter reproducibility,
override `image` with the registry digest recorded in the release manifest:
`--set image=ghcr.io/pukata/csi-driver-sqlite@sha256:YOUR_VERIFIED_DIGEST`.

Download the chart and SHA256SUMS from the same release, then verify locally with
`sha256sum --check SHA256SUMS` after downloading all listed assets. These hashes
detect transfer changes; they are not independent publisher signatures. Keep
independent backups and use disposable data before upgrading pre-release state.

## Private registry access

Public images and charts need no registry login. If using a private fork or
package, use `helm registry login ghcr.io --username YOUR_USER --password-stdin`
with a token that has `read:packages`. Create an image pull Secret
in the driver namespace and set `pullSecret` to its name. Keep the token out of
Git and literal shell arguments. Repository visibility and GHCR package
visibility are separate settings.

## Maintainer procedure

1. Merge reviewed source and require green CI on main.
2. Dispatch `Release` on main with a new `vMAJOR.MINOR.PATCH-alpha.N` version.
3. All checks run again on the dispatch revision. Only their success permits
   package publication and creation of the tag/prerelease at that exact SHA.
4. Verify the release assets and image/chart registry pulls. Do not move a tag or
   overwrite a published package to fix a release. Use a new prerelease version.
5. If publication fails partway, inspect existing packages before retrying. The
   workflow is serialized, but publication of the driver image, chart and
   a GitHub release is not one atomic transaction. Use a new version if uncertain.

The workflow uses the repository-scoped GITHUB_TOKEN; only its publish job gets
contents:write and packages:write. It does not make the repository or packages
public. Public installation requires both the driver image and chart package
to be public in GHCR. No automatic community posting is configured.
