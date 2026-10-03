#!/usr/bin/env bash
set -euo pipefail

version=v4.1
archive=scrcpy-linux-x86_64-${version}.tar.gz
expected=ad56ae8bfeedf41e824945c11dbf55fcb092b3e615b9b486f48a50e30d389635
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$root/.tools"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fL --retry 3 "https://github.com/Genymobile/scrcpy/releases/download/${version}/${archive}" -o "$tmp/$archive"
printf '%s  %s\n' "$expected" "$tmp/$archive" | sha256sum -c -
tar -xzf "$tmp/$archive" -C "$root/.tools"
printf 'Installed %s\n' "$root/.tools/scrcpy-linux-x86_64-${version}/scrcpy"
