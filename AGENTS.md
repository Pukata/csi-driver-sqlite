# Working on this repository

This is an experimental SQLite-aware CSI driver for Kubernetes homelabs.
It supports one application Pod per database volume. Preserve independent
fencing and verified restore before any unclean ownership takeover.

Use `gofmt`. Run `make verify test build chart` for relevant code/chart changes.
Run `make image e2e` when changing storage ownership, restore, mounts or recovery.
The end-to-end harness creates its own kind cluster and kubeconfig; never point
fault injection at an existing cluster. Keep test credentials out of Git.

For AI-assisted commits, retain the human author and add a `Co-authored-by`
trailer identifying the AI tool and model actually used. Use a confirmed model
name; do not invent one. This project's initial work used the maintainer-confirmed
attribution `GPT-6.1 Sol (via Codex) <noreply@openai.com>`.
Use a GitHub noreply address for the human identity and do not include personal
email addresses. Attribution records assistance, not independent review.
