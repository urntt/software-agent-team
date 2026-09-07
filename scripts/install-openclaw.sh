#!/usr/bin/env bash
set -euo pipefail

# Dependency installation only: no remote installer, onboarding, or services.
task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
source "$task_root/configs/toolchain.sh"
task_prefix="${1:?private runtime prefix is required}"
task_marker="$task_prefix/.sat-owned-runtime"
fail() { echo "setup runtime: $1" >&2; exit 1; }
[[ "$HOME" == "$task_root/.sat/.install-home."* && -d "$HOME" && ! -L "$HOME" ]] || \
  fail "an isolated installer home is required"
unset NODE_OPTIONS NODE_PATH STATE_DIRECTORY
[[ "$task_prefix" == "$task_root/.sat/openclaw" && -d "$task_prefix" && ! -L "$task_prefix" ]] || \
  fail "refusing a runtime outside the private application path"
[[ -f "$task_marker" && ! -L "$task_marker" && \
   "$(sed -n '1p' "$task_marker")" == software-agent-team-openclaw-runtime-v1 && \
   "$(sed -n '2p' "$task_marker")" == "root=$task_prefix" ]] || \
  fail "private runtime ownership is invalid"
for task_command in curl tar sha256sum git; do
  command -v "$task_command" >/dev/null || fail "$task_command is required"
done
[[ "$(uname -s)" == Linux ]] || fail "only Linux and WSL are supported"
case "$(uname -m)" in
  x86_64|amd64) task_arch=x64; task_sha="$task_node_x64_sha256" ;;
  aarch64|arm64) task_arch=arm64; task_sha="$task_node_arm64_sha256" ;;
  *) fail "unsupported Node architecture" ;;
esac
task_stage="$(mktemp -d "$task_prefix/.download.XXXXXX")"
cleanup() { rm -rf -- "$task_stage"; }
trap cleanup EXIT
task_node_root="$task_prefix/tools/node-v$task_node_version"
[[ ! -L "$task_prefix/tools" && ! -L "$task_node_root" ]] || \
  fail "private Node storage must not be a symlink"
if [[ ! -x "$task_node_root/bin/node" ]]; then
  [[ ! -e "$task_node_root" ]] || fail "partial Node runtime exists; preserve it and reinstall this application version"
  echo "setup runtime: downloading pinned Node $task_node_version"
  curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 30 \
    --speed-limit 1 --speed-time 30 \
    "https://nodejs.org/dist/v$task_node_version/node-v$task_node_version-linux-$task_arch.tar.gz" \
    -o "$task_stage/node.tar.gz" || fail "Node download failed"
  [[ "$(sha256sum "$task_stage/node.tar.gz" | cut -d ' ' -f 1)" == "$task_sha" ]] || \
    fail "Node archive checksum mismatch; no downloaded code was executed"
  mkdir "$task_stage/node"
  tar -xzf "$task_stage/node.tar.gz" -C "$task_stage/node" --strip-components=1
  mkdir -p "$task_prefix/tools"
  mv "$task_stage/node" "$task_node_root"
fi
task_node="$task_node_root/bin/node"
[[ "$("$task_node" --version)" == "v$task_node_version" ]] || fail "Node version mismatch"
# Check linked SQLite instead of bypassing the upstream WAL-reset safety guard.
"$task_node" -e 'const {DatabaseSync}=require("node:sqlite"); const db=new DatabaseSync(":memory:"); try { const [a,b,c]=db.prepare("select sqlite_version() as v").get().v.split(".").map(Number); if (!(a>3 || (a===3 && (b>51 || (b===51 && c>=3) || (b===50 && c>=7) || (b===44 && c>=6))))) process.exit(1); } finally {db.close();}' || \
  fail "Node SQLite does not meet the WAL-reset safety boundary"
echo "setup runtime: installing pinned OpenClaw $task_openclaw_version"
curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 30 \
  --speed-limit 1 --speed-time 30 \
  "https://registry.npmjs.org/openclaw/-/openclaw-$task_openclaw_version.tgz" \
  -o "$task_stage/openclaw.tgz" || fail "OpenClaw package download failed"
[[ "$(sha256sum "$task_stage/openclaw.tgz" | cut -d ' ' -f 1)" == "$task_openclaw_sha256" ]] || \
  fail "OpenClaw package checksum mismatch; package was not installed"
task_npm_environment=(env -u NODE_OPTIONS -u NODE_PATH)
for task_name in "${!NPM_CONFIG_@}" "${!npm_config_@}"; do
  task_npm_environment+=(-u "$task_name")
done
"${task_npm_environment[@]}" PATH="$task_node_root/bin:$PATH" \
  NPM_CONFIG_USERCONFIG="$HOME/.npmrc" NPM_CONFIG_GLOBALCONFIG="$HOME/global-npmrc" \
  NPM_CONFIG_CACHE="$HOME/npm-cache" NPM_CONFIG_UPDATE_NOTIFIER=false \
  "$task_node" "$task_node_root/lib/node_modules/npm/bin/npm-cli.js" \
  install --global --prefix "$task_node_root" --no-audit --no-fund \
  "$task_stage/openclaw.tgz" || fail "pinned OpenClaw package installation failed"
task_entry="$task_node_root/lib/node_modules/openclaw/dist/entry.js"
[[ "$("$task_node" "$task_entry" --version)" == *"$task_openclaw_version"* ]] || \
  fail "installed OpenClaw version mismatch"
[[ ! -L "$task_prefix/bin" ]] || fail "private launcher directory must not be a symlink"
mkdir -p "$task_prefix/bin"
# Quote absolute paths for Bash; publish only after both version probes pass.
printf '#!/usr/bin/env bash\nset -euo pipefail\nexec %q %q "$@"\n' \
  "$task_node" "$task_entry" > "$task_stage/openclaw"
chmod 700 "$task_stage/openclaw"
mv -T "$task_stage/openclaw" "$task_prefix/bin/openclaw"
echo "setup runtime: private OpenClaw is ready"
