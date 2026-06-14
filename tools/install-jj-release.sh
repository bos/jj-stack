#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: tools/install-jj-release.sh <version> [install-dir]

Download a released jj binary for the current platform into an isolated path
without building from source. Prints the directory containing the installed
`jj` binary on stdout.

Examples:
  tools/install-jj-release.sh v0.42.0
  PATH="$(tools/install-jj-release.sh v0.28.2):$PATH" ./check.py
  tools/install-jj-release.sh 0.42.0 .tmp/jj/v0.42.0
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
    v0.39.0/aarch64-apple-darwin)
      printf '%s\n' "525ee96fd1eda1be925b3827a964c58a9f14bc2bae411bd7d8422fe1af40ea19"
      ;;
    v0.39.0/aarch64-pc-windows-msvc)
      printf '%s\n' "c3da2f7bec13dd2f5360d60b479436f70908152ec348673360154399cf06ad70"
      ;;
    v0.39.0/x86_64-apple-darwin)
      printf '%s\n' "cdf0eb6f457165bfe5edc3afc16a5d10b3ea89cd682ebe333dabdec626373104"
      ;;
    v0.39.0/x86_64-pc-windows-msvc)
      printf '%s\n' "53be7e277e5f0396621ccdda509904e4f88fe8e517b78ce20176269b7e97d378"
      ;;
    v0.39.0/aarch64-unknown-linux-musl)
      printf '%s\n' "15bbb0199adf57929d1e3cd90ae0b47356858cbe374814769815a1fb87d5ad1d"
      ;;
    v0.39.0/x86_64-unknown-linux-musl)
      printf '%s\n' "8da8d96e9c8696c21ad47847a63d533e249acb0449d9af0f0562b5ea7b024f04"
      ;;
    v0.40.0/aarch64-apple-darwin)
      printf '%s\n' "8a1d713103bb968c771617c9b2c48b0b5982193090ee74dec935bff710af2082"
      ;;
    v0.40.0/aarch64-pc-windows-msvc)
      printf '%s\n' "662e6f0887b0bb4c3d8e9175491dd09595952ee814c0a113ac7128254a4d5e0e"
      ;;
    v0.40.0/x86_64-apple-darwin)
      printf '%s\n' "ce62cf26e3c6c72a295f5917056e33cfa972874f882a2d15b5a3687b3ddce1e5"
      ;;
    v0.40.0/x86_64-pc-windows-msvc)
      printf '%s\n' "63922bd257f9616553dec0869e2de99c1c0bf8d951c774d230af09eaeb2f5951"
      ;;
    v0.40.0/aarch64-unknown-linux-musl)
      printf '%s\n' "b26f24ff7a34838fbafe8788e6a94a9cdcf51601ef8c9af8fab4fa22c06ddbee"
      ;;
    v0.40.0/x86_64-unknown-linux-musl)
      printf '%s\n' "5c8979f46873e052f59bdd9535636dca6e6f9f70571b73f6d63c3b92acfaa037"
      ;;
    v0.41.0/aarch64-apple-darwin)
      printf '%s\n' "e84883b4fb42d1e0cb665efae95b44f387603c1280c893f8cbc7bbac7149ea30"
      ;;
    v0.41.0/aarch64-pc-windows-msvc)
      printf '%s\n' "9fce194dbce7393752ad562bba430027d7857ffb3e3f12c08e763c58b204c0c3"
      ;;
    v0.41.0/x86_64-apple-darwin)
      printf '%s\n' "b40d238bf9de4379be9bfd629cff92cd3ec14e2d072a8f7f7bbb929dac9d22f6"
      ;;
    v0.41.0/x86_64-pc-windows-msvc)
      printf '%s\n' "1c5ac3015caf0b15ae81cbafa1d94024dbd17b5dff933204d489787dfb95f835"
      ;;
    v0.41.0/aarch64-unknown-linux-musl)
      printf '%s\n' "cd75d0f920b2674147a48eac84ee4594f476fc8f98cd7e358b25750a51622d91"
      ;;
    v0.41.0/x86_64-unknown-linux-musl)
      printf '%s\n' "42181a80d316ac157874c817c9945e104275114fb461d99e06e2312502f08f99"
      ;;
    v0.42.0/aarch64-apple-darwin)
      printf '%s\n' "98764966f22b599dc0b19bb9bd00d21df86156aeca5827f8274900356768db08"
      ;;
    v0.42.0/aarch64-pc-windows-msvc)
      printf '%s\n' "0c6d6676a763f3a0514f83f45806f1df5b5af66e8e92ce38cdd8e6c136d99fc6"
      ;;
    v0.42.0/x86_64-apple-darwin)
      printf '%s\n' "ec04669e9b8decb4b0d63dc050a4275d2b5422efea502a0c208ebd4e53e7d053"
      ;;
    v0.42.0/x86_64-pc-windows-msvc)
      printf '%s\n' "866461102d87fb49fc67e6e76682635683963eb9fdd05264edda5f1c894d85a6"
      ;;
    v0.42.0/aarch64-unknown-linux-musl)
      printf '%s\n' "bc962ac57ec264541a62ed8492f080898380a277222b115e1ed96163196e6fc8"
      ;;
    v0.42.0/x86_64-unknown-linux-musl)
      printf '%s\n' "2d91e81d649e617a81608e7401ad1106029c15ece01ac928c4a351abef42be6a"
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
