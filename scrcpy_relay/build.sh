#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
sdk_root="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}}"
android_jar="$sdk_root/platforms/android-34/android.jar"
d8="$sdk_root/build-tools/34.0.0/d8"
build_dir="$script_dir/build"
output="${1:-$script_dir/../.tools/scrcpy-abstract-relay.jar}"
if [[ ! -f "$android_jar" || ! -x "$d8" ]]; then
  echo 'Android SDK platform 34 and build-tools 34.0.0 are required; set ANDROID_HOME.' >&2
  exit 2
fi
mkdir -p "$build_dir/classes" "$build_dir/dex" "$(dirname "$output")"
javac --release 8 -cp "$android_jar" -d "$build_dir/classes" "$script_dir/AbstractRelay.java"
"$d8" --min-api 29 --lib "$android_jar" --output "$build_dir/dex" \
  "$build_dir/classes/org/phonecapture/scrcpy/AbstractRelay.class"
jar --create --file "$output" -C "$build_dir/dex" classes.dex
echo "$output"
