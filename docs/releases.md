# Installing and making experimental releases

Releases are explicit prereleases such as `v0.2.0-alpha.1`. The release workflow
runs all Go, chart, fencing, resource and Kubernetes testcases against the exact
source revision before publishing images, an OCI Helm chart, checksums and source
identity. No floating `latest` tag is published. Initial images are Linux amd64.

Check the [release page](https://github.com/Pukata/csi-driver-sqlite/releases)
for actually published versions. Workflow configuration alone is not a release.

## Install a published version

For private packages, log in to GHCR using a token with read:packages and create
an image pull Secret in the driver namespace. Do not store the token in Git or
pass it as a literal shell argument. Public repository visibility does not
automatically make existing GHCR packages public.

```sh
# For private charts: helm registry login ghcr.io --username YOUR_USER --password-stdin
kubectl create namespace sqlite-csi-system
helm upgrade --install sqlite-csi oci://ghcr.io/pukata/charts/sqlite-csi \
  --version 0.2.0-alpha.1 --namespace sqlite-csi-system \
  --set createBackingClaim=true --set backingStorageClass=YOUR_SHARED_RWX_CLASS \
  --set localPath=/mnt/ssd/sqlite-csi
```

Select a real RWX class and label enabled nodes as described in the README.
For private images, additionally set `pullSecret` to your registry Secret's name.
The packaged chart pins its corresponding image tag. For stricter reproducibility,
override `image` with the registry digest recorded in the release manifest:
`--set image=ghcr.io/pukata/csi-driver-sqlite@sha256:YOUR_VERIFIED_DIGEST`.

Download the chart and SHA256SUMS from the same release, then verify locally with
`sha256sum --check SHA256SUMS` after downloading all listed assets. These hashes
detect transfer changes; they are not independent publisher signatures. Keep
independent backups and use disposable data before upgrading pre-release state.

## Maintainer procedure

1. Merge reviewed source and require green CI on main.
2. Dispatch `Release` on main with a new `vMAJOR.MINOR.PATCH-alpha.N` version.
3. All checks run again on the dispatch revision. Only their success permits
   package publication and creation of the tag/prerelease at that exact SHA.
4. Verify the release assets and image/chart registry pulls. Do not move a tag or
   overwrite a published package to fix a release. Use a new prerelease version.
5. If publication fails partway, inspect existing packages before retrying. The
   workflow is serialized, but publication across three registries/packages and
   a GitHub release is not one atomic transaction. Use a new version if uncertain.

The workflow uses the repository-scoped GITHUB_TOKEN; only its publish job gets
contents:write and packages:write. It does not make the repository or packages
public. Public launch additionally requires reviewing Actions logs/history and
package visibility. The [walkthrough](walkthrough.md) is ready for review as an
announcement draft; no automatic community posting is configured.
