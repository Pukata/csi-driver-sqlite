# Working on this repository

This is an experimental SQLite-aware CSI driver for Kubernetes homelabs.
It supports one application Pod per database volume. Preserve independent
fencing and verified restore before any unclean ownership takeover.

Use `gofmt`. Run `make verify test build chart` for relevant code/chart changes.
Run `make image e2e` when changing storage ownership, restore, mounts or recovery.
The end-to-end harness creates its own kind cluster and kubeconfig; never point
fault injection at an existing cluster. Keep test credentials out of Git.
Run `make integration-test` for Redfish authority changes and `make image benchmark`
for resource/digest changes. Keep application migration tooling outside this project.
Run `make image nas-image nas-test` for changes to NAS fixtures or backend recovery.
The NAS suite creates its own NFS server and cluster; never substitute an existing NAS.
SMB protocol tests live separately under `test/protocols/smb`. Validate them
locally before considering CI. Do not dispatch or rerun GitHub Actions without
fresh operator approval; Actions cost is an explicit constraint.

For AI-assisted commits, retain the human author and use this repository's
established attribution trailer exactly:
`Co-authored-by: GPT-6.1 Sol (via Codex) <noreply@openai.com>`.
Keep this convention unless the user requests a change. The trailer records the
user-requested project attribution, not independent review or mailbox ownership.
Use a GitHub noreply address for the human identity and do not include personal
email addresses.
