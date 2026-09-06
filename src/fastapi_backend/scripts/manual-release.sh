#!/usr/bin/env bash
# Retired development-only release path.
#
# This file remains as an explicit guard for operators following historical
# handover notes. It intentionally does not build an image, assemble a release,
# read credentials, start a container, or provide an evaluation smoke path.
# Final evaluation/production releases must be created by the protected
# immutable-ncp-release workflow and started with compose-release.sh from its
# signed image digest, AgentReleaseManifest, DeploymentBinding, and artifacts.
set -euo pipefail

printf '%s\n' \
  '[manual-release] ERROR: this historical partial release path is retired.' \
  '[manual-release] Use the protected immutable-ncp-release workflow, then compose-release.sh.' >&2
exit 64
