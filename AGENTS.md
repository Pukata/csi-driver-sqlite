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

For AI-assisted commits, retain the human author and add a `Co-authored-by`
trailer identifying the AI tool and model when independently confirmed. Do not
invent a model identity or an official attribution mailbox. If the runtime model
is unverified, identify only the tool, for example
`Co-authored-by: Codex (AI assistance) <ai-assistance@example.invalid>`.
The reserved invalid address records assistance without claiming an OpenAI account.
Use a GitHub noreply address for the human identity and do not include personal
email addresses. Attribution records assistance, not independent review.
