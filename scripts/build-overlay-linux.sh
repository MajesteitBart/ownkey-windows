#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")/../overlay-ui"
if ! command -v cargo >/dev/null || ! command -v pnpm >/dev/null; then
    echo 'Install Rust/Cargo and Node.js with pnpm before building the overlay.' >&2
    echo 'On Ubuntu run scripts/setup-linux.sh; enable pnpm with corepack enable pnpm.' >&2
    exit 1
fi
pnpm install --frozen-lockfile
pnpm build:binary
