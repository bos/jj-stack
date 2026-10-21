#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: tools/install-jj-release.sh <version> [install-dir]

Download a released jj binary for the current platform into an isolated path
without building from source. Prints the directory containing the installed
`jj` binary on stdout.

Examples:
  tools/install-jj-release.sh v0.45.1
  PATH="$(tools/install-jj-release.sh v0.45.1):$PATH" ./check.py
  tools/install-jj-release.sh 0.45.1 .tmp/jj/v0.45.1
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
    v0.45.1/aarch64-apple-darwin)
      printf '%s\n' "51ba42e3d0682616f6eb015045bfe45289b396f03511f9897f645ce8e9272743"
      ;;
    v0.45.1/aarch64-pc-windows-msvc)
      printf '%s\n' "0f869d108316149f211d2ba50a626b4fe8ef99232db52f13300bde106821be9e"
      ;;
    v0.45.1/x86_64-apple-darwin)
      printf '%s\n' "6171582d0b5a98a1005cd9643faebff7936812ec264d7968a39d9cef3654a99b"
      ;;
    v0.45.1/x86_64-pc-windows-msvc)
      printf '%s\n' "5dbf2619272c897394d34190a94b21a6ab8d1e8e59fcf7dfc173d8fa8aa90bf0"
      ;;
    v0.45.1/aarch64-unknown-linux-musl)
      printf '%s\n' "7349a43dd5a20dbc998b10114daa0ee63d2ab863fb822c7eb6b0ebca5903cc69"
      ;;
    v0.45.1/x86_64-unknown-linux-musl)
      printf '%s\n' "f35438350b5d61963aac5dd74ede510b31d6b9690769d1a6268cf058cc825f72"
      ;;
    v0.46.0/aarch64-apple-darwin)
      printf '%s\n' "43daa37ce0f9716cdf6fb65a29cbcae74a3160aef048cf3cb3d166d41cb4ab58"
      ;;
    v0.46.0/aarch64-pc-windows-msvc)
      printf '%s\n' "f6f0552d0e292e3e62aa7fd21f4b15e9b6644d4939db908ed6d6b04b5b0a7907"
      ;;
    v0.46.0/x86_64-apple-darwin)
      printf '%s\n' "10d941de276c7c5074ef5d395312813b4575f4ce92cb9c0b05738efb90bd3b60"
      ;;
    v0.46.0/x86_64-pc-windows-msvc)
      printf '%s\n' "c42316b10df04611bc65029a18bcb2d92636edddb6871d511f7bb4233e999b83"
      ;;
    v0.46.0/aarch64-unknown-linux-musl)
      printf '%s\n' "80e28f7501c11e50e0ca06cd6a1fa1d982cff9cc59bfe4411a9d36aa501d867e"
      ;;
    v0.46.0/x86_64-unknown-linux-musl)
      printf '%s\n' "fce0271158e665ceb82dc66c5eb95b1728649094e9133074f3b5baece345fc08"
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
