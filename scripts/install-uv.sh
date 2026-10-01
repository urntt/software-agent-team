#!/usr/bin/env bash
set -euo pipefail

# Bootstrap/setup share this authority; existing shared tools are never replaced.
task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
source "$task_root/configs/toolchain.sh"
task_target="${1:?uv executable path is required}"
fail() { echo "setup uv: $1" >&2; exit 1; }
[[ -x "$task_target" ]] && exit 0
[[ "$task_target" == /* && ! -e "$task_target" && ! -L "$task_target" ]] || \
  fail "uv target must be an absent absolute path; existing files are preserved"
[[ "$(uname -s)" == Linux ]] || fail "only Linux and WSL are supported"
case "$(uname -m)" in
  x86_64|amd64) task_arch=x86_64; task_sha="$task_uv_x64_sha256" ;;
  aarch64|arm64) task_arch=aarch64; task_sha="$task_uv_arm64_sha256" ;;
  *) fail "unsupported uv architecture" ;;
esac
for task_command in curl tar sha256sum ln; do
  command -v "$task_command" >/dev/null || fail "$task_command is required"
done
task_parent="$(dirname "$task_target")"
mkdir -p -- "$task_parent"
[[ -d "$task_parent" && ! -L "$task_parent" ]] || fail "uv parent must be a real directory"
task_stage="$(mktemp -d "$task_parent/.sat-uv.XXXXXX")"
cleanup() { rm -rf -- "$task_stage"; }
trap cleanup EXIT
task_archive="uv-$task_arch-unknown-linux-gnu"
curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 30 \
  --speed-limit 1 --speed-time 30 \
  "https://github.com/astral-sh/uv/releases/download/$task_uv_version/$task_archive.tar.gz" \
  -o "$task_stage/uv.tar.gz" || fail "pinned uv download failed"
[[ "$(sha256sum "$task_stage/uv.tar.gz" | cut -d ' ' -f 1)" == "$task_sha" ]] || \
  fail "uv archive checksum mismatch; no downloaded code was executed"
tar -xzf "$task_stage/uv.tar.gz" -C "$task_stage"
task_binary="$task_stage/$task_archive/uv"
[[ -f "$task_binary" && ! -L "$task_binary" ]] || fail "uv archive lacks a regular binary"
read -r task_name task_reported_version task_version_metadata <<< "$("$task_binary" --version)"
[[ "$task_name" == uv && "$task_reported_version" == "$task_uv_version" ]] || \
  fail "uv version mismatch"
chmod 755 "$task_binary"
ln -- "$task_binary" "$task_target" || fail "uv target appeared during installation; preserved it"
echo "setup uv: installed verified uv $task_uv_version"
