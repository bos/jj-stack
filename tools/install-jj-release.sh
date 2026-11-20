#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: tools/install-jj-release.sh <version> [install-dir]

Download a released jj binary for the current platform into an isolated path
without building from source. Prints the directory containing the installed
`jj` binary on stdout.

Examples:
  tools/install-jj-release.sh v0.47.1
  PATH="$(tools/install-jj-release.sh v0.47.1):$PATH" ./check.py
  tools/install-jj-release.sh 0.47.1 .tmp/jj/v0.47.1
EOF
}

python_command() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi
  echo "python3 or python is required" >&2
  exit 1
}

sha256_file() {
  python_bin="$(python_command)"
  "$python_bin" - "$1" <<'PY'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
with path.open("rb") as file:
    for chunk in iter(lambda: file.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

extract_zip() {
  python_bin="$(python_command)"
  "$python_bin" - "$1" "$2" <<'PY'
import pathlib
import sys
import zipfile

archive_path = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
with zipfile.ZipFile(archive_path) as archive:
    archive.extractall(destination)
PY
}

expected_sha256() {
  case "$1/$2" in
    v0.47.1/aarch64-apple-darwin)
      printf '%s\n' "50129b527f7cd6aef82aebd4bb2b670bb56766adc509ef3279c4327aa3592255"
      ;;
    v0.47.1/aarch64-pc-windows-msvc)
      printf '%s\n' "07f3cf6ed6db95f12be6c79c76aff948689375a9f201f521df4468d0a0c5de85"
      ;;
    v0.47.1/x86_64-apple-darwin)
      printf '%s\n' "ce7ac70c3b7cf90f2273b4fa62d24576727c5fd55c60dc3820221c05320c7589"
      ;;
    v0.47.1/x86_64-pc-windows-msvc)
      printf '%s\n' "72f9f80bc99d07a7f48d5f219cf5bab91f7e906e4b53f6c827948e646cd1152f"
      ;;
    v0.47.1/aarch64-unknown-linux-musl)
      printf '%s\n' "0f300e1576de8dabe0a9dedf0b87ae44f8accea5cf54d9a342a51061c1fb1bcd"
      ;;
    v0.47.1/x86_64-unknown-linux-musl)
      printf '%s\n' "d4b657f622ee50929c8916466dfa3d6a6c17a0a32b50e7b2c793707ec9ebcad7"
      ;;
    *)
      echo "unsupported jj version for checksum verification: $1" >&2
      exit 1
      ;;
  esac
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage >&2
  exit 2
fi

version="$1"
if [[ "$version" != v* ]]; then
  version="v$version"
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install_dir="${2:-$repo_root/.tmp/jj-releases/$version}"

platform="$(uname -s)"
arch="$(uname -m)"

case "$platform/$arch" in
  Darwin/arm64)
    target="aarch64-apple-darwin"
    ;;
  Darwin/x86_64)
    target="x86_64-apple-darwin"
    ;;
  Linux/aarch64)
    target="aarch64-unknown-linux-musl"
    ;;
  Linux/x86_64)
    target="x86_64-unknown-linux-musl"
    ;;
  MINGW*_NT*/aarch64 | MINGW*_NT*/arm64 | MSYS*_NT*/aarch64 | MSYS*_NT*/arm64)
    target="aarch64-pc-windows-msvc"
    ;;
  MINGW*_NT*/x86_64 | MSYS*_NT*/x86_64)
    target="x86_64-pc-windows-msvc"
    ;;
  *)
    echo "unsupported platform for release binaries: $platform/$arch" >&2
    exit 1
    ;;
esac

archive_extension="tar.gz"
exe_name="jj"
if [[ "$target" == *-pc-windows-msvc ]]; then
  archive_extension="zip"
  exe_name="jj.exe"
fi

bin_dir="$install_dir/bin"
jj_path="$bin_dir/$exe_name"
if [[ -x "$jj_path" ]]; then
  printf '%s\n' "$bin_dir"
  exit 0
fi

asset="jj-$version-$target.$archive_extension"
url="https://github.com/jj-vcs/jj/releases/download/$version/$asset"
expected_sha="$(expected_sha256 "$version" "$target")"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

mkdir -p "$bin_dir"
archive_path="$tmp_dir/$asset"
curl --fail --location --silent --show-error --output "$archive_path" "$url"
actual_sha="$(sha256_file "$archive_path")"
if [[ "$actual_sha" != "$expected_sha" ]]; then
  echo "checksum verification failed for $asset" >&2
  echo "expected: $expected_sha" >&2
  echo "actual:   $actual_sha" >&2
  exit 1
fi
case "$archive_extension" in
  tar.gz)
    tar -xzf "$archive_path" -C "$tmp_dir"
    ;;
  zip)
    extract_zip "$archive_path" "$tmp_dir"
    ;;
esac
install -m 0755 "$tmp_dir/$exe_name" "$jj_path"

printf '%s\n' "$bin_dir"
