#!/usr/bin/env bash
# Smoke test of Service.qml and Panel.qml in a headless Quickshell: one
# service and two bar widgets (two monitors) built on the installed Omarchy
# shell kit, with a stand-in KeyboardPanel and stand-in bars, one of whose
# shells has no plugin services (a replacement bar). It checks that both
# widgets bind to the one service, that only it registers the IPC target, the
# panel count and `state` answer, a save on one monitor refreshing both, the
# routing of panel IPC through the shell, a reloaded service, the fallback
# once it is gone, a service that loads later and a rebuilt bar.
#
# Everything runs in a temporary home with a fake omarchy-shell and pkexec
# first on PATH and a status file in the temporary directory; the live shell,
# the real service and Hyprland are never touched. Skips itself when qs or the
# shell kit is missing.
set -uo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
plugin=$(dirname "$here")
kit=${OMARCHY_SHELL_DIR:-/usr/share/omarchy/shell}

if ! command -v qs >/dev/null; then
  echo "service.smoke.sh: skipped: qs not installed"
  exit 0
fi
if [[ ! -d $kit/Ui || ! -d $kit/Commons ]]; then
  echo "service.smoke.sh: skipped: no Omarchy shell kit at $kit"
  exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
config="$tmp/config"
mkdir -p "$config/Ui" "$tmp/bin" "$tmp/fake" "$tmp/home" "$tmp/run"
chmod 700 "$tmp/run"
cp "$plugin"/*.qml "$plugin"/*.js "$plugin/manifest.json" "$config/"
cp "$here/smoke/shell.qml" "$config/shell.qml"
ln -s "$kit/Commons" "$config/Commons"
for file in "$kit"/Ui/*; do
  [[ ${file##*/} == KeyboardPanel.qml ]] || ln -s "$file" "$config/Ui/"
done
cp "$here/smoke/KeyboardPanel.qml" "$config/Ui/KeyboardPanel.qml"

# write-status <package_c>: the fixture, published now with that temperature,
# replaced atomically as the service does.
cp "$here/fixtures/status.json" "$tmp/fake/fixture.json"
cat > "$tmp/bin/write-status" <<'SCRIPT'
#!/usr/bin/env bash
set -e
jq --argjson now "$(date +%s)" --argjson temp "$1" '.updated = $now | .thermal.package_c = $temp' \
  "${FAKE_DIR:?}/fixture.json" > "$SMOKE_STATUS.tmp"
mv -f "$SMOKE_STATUS.tmp" "$SMOKE_STATUS"
SCRIPT
# omarchy-shell logs its arguments. pkexec logs them, publishes a status at
# 60 °C as if the service had applied the save, and answers as the helper does.
cat > "$tmp/bin/omarchy-shell" <<'SCRIPT'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_DIR:?}/calls"
SCRIPT
cat > "$tmp/bin/pkexec" <<'SCRIPT'
#!/usr/bin/env bash
printf 'pkexec %s\n' "$*" >> "${FAKE_DIR:?}/calls"
write-status 60
echo '{"profile":"quiet","applied":true,"revision":5,"superseded":false}'
SCRIPT
chmod 755 "$tmp/bin/write-status" "$tmp/bin/omarchy-shell" "$tmp/bin/pkexec"
: > "$tmp/fake/calls"
status_file="$tmp/run/status.json"
PATH="$tmp/bin:$PATH" FAKE_DIR="$tmp/fake" SMOKE_STATUS="$status_file" "$tmp/bin/write-status" 77 || exit 1

output=$(cd "$tmp" && env -u HYPRLAND_INSTANCE_SIGNATURE -u WAYLAND_DISPLAY -u DISPLAY -u QT_QPA_PLATFORMTHEME -u QT_IM_MODULE \
  HOME="$tmp/home" XDG_RUNTIME_DIR="$tmp/run" XDG_STATE_HOME="$tmp/home/.local/state" XDG_CONFIG_HOME="$tmp/home/.config" \
  XDG_CACHE_HOME="$tmp/home/.cache" XDG_DATA_HOME="$tmp/home/.local/share" \
  PATH="$tmp/bin:$PATH" FAKE_DIR="$tmp/fake" SMOKE_STATUS="$status_file" SMOKE_WRITE_STATUS="$tmp/bin/write-status" \
  QT_QPA_PLATFORM=offscreen NO_COLOR=1 \
  timeout 150 qs -p "$config/shell.qml" 2>&1)
status=$?

results=$(printf '%s\n' "$output" | sed 's/\x1b\[[0-9;]*m//g' | grep -o 'SMOKE .*' | sed 's/^SMOKE //')
printf '%s\n' "$results" | grep -v '^done ' | sed 's/^/service.smoke.sh: /'
done_line=$(printf '%s\n' "$results" | grep '^done ' | tail -n 1)
checks=$(printf '%s\n' "$results" | grep -c '^ok')
if [[ $done_line != "done 0" ]] || printf '%s\n' "$results" | grep -q '^not ok'; then
  echo "service.smoke.sh: FAILED (qs exit $status${done_line:+, $done_line})" >&2
  [[ -z $done_line ]] && printf '%s\n' "$output" | tail -n 40 >&2
  exit 1
fi
# The plugin's own QML must run without warnings. The theme's colours do not
# resolve in the headless run (the live shell assigns them), so those are not
# counted.
warnings=$(printf '%s\n' "$output" | sed 's/\x1b\[[0-9;]*m//g' | grep -E 'WARN|ERROR' \
  | grep -E '@(Service|Panel|ActionButton|StatTile|StepperRow|TurboRow)\.qml\[' | grep -v 'Unable to assign \[undefined\] to QColor' || true)
if [[ -n $warnings ]]; then
  printf '%s\n' "$warnings" | sed 's/^/service.smoke.sh: /' >&2
  echo "service.smoke.sh: FAILED (warnings from the plugin's QML)" >&2
  exit 1
fi
echo "service.smoke.sh: passed ($checks checks)"
