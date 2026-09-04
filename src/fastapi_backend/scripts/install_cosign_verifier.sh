#!/usr/bin/env bash
set -euo pipefail

COSIGN_VERSION="v3.1.3"
COSIGN_SHA256="4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"
COSIGN_URL="https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}/cosign-linux-amd64"
COSIGN_TARGET="/usr/local/bin/cosign"

if [ "$(id -u)" -ne 0 ]; then
    echo "Install the release verifier as root." >&2
    exit 2
fi

temporary_directory="$(mktemp -d /tmp/finance-agent-cosign.XXXXXXXX)"
cleanup() {
    rm -rf -- "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

download="$temporary_directory/cosign-linux-amd64"
curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    "$COSIGN_URL" \
    --output "$download"
printf '%s  %s\n' "$COSIGN_SHA256" "$download" | sha256sum --check --strict

install -o root -g root -m 0755 "$download" "${COSIGN_TARGET}.new"
mv -f -- "${COSIGN_TARGET}.new" "$COSIGN_TARGET"

test "$(stat -c '%u:%g:%a' "$COSIGN_TARGET")" = "0:0:755"
test "$(sha256sum "$COSIGN_TARGET" | cut -d ' ' -f 1)" = "$COSIGN_SHA256"
"$COSIGN_TARGET" version | grep -F "$COSIGN_VERSION" >/dev/null
