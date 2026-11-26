# CPU Limits

An [Omarchy](https://omarchy.org) bar widget and system service that keep, for each platform profile, a frequency ceiling per core group, the turbo switch and optionally the RAPL package power limits PL1 and PL2, and show live package power, temperature, clocks, load, fans and throttling.

The power profile still chooses the governor, minimum frequency and energy-performance preference; this decides how far each profile may go.

## Requirements

- An Intel CPU under the active `intel_pstate` driver, with cpufreq policies.
- `/sys/firmware/acpi/platform_profile` (switched by `power-profiles-daemon`, or by `asusd` on ASUS laptops).
- Optional: the RAPL powercap zones `intel-rapl:0` (MSR) and `intel-rapl-mmio:0` for the power limits and package power.
- Python 3, systemd, PolicyKit (`pkexec`), Omarchy's shell.

## Components

| Part | Installed at | Source |
|---|---|---|
| Helper and service | `/usr/local/libexec/workstation-cpu-limits` (root, 0755) | `backend/cpu-limits.py` |
| systemd unit | `/etc/systemd/system/cpu-frequency-limits.service` | `examples/cpu-frequency-limits.service` |
| PolicyKit actions | `/usr/share/polkit-1/actions/org.workstation.cpu-limits.policy` | `examples/org.workstation.cpu-limits.policy` |
| Default ceilings | `/etc/tmpfiles.d/cpu-performance.conf` | `examples/cpu-performance.conf`, adjusted to the CPU |
| Bar widget and plugin service | `~/.config/omarchy/plugins/io.github.ltecosta.cpu-limits/` | this directory |
| Check | run from the checkout | `backend/check.py` |

## Install

```bash
sudo install -o root -g root -m 0755 backend/cpu-limits.py /usr/local/libexec/workstation-cpu-limits
sudo install -o root -g root -m 0644 examples/cpu-frequency-limits.service /etc/systemd/system/
sudo install -o root -g root -m 0644 examples/org.workstation.cpu-limits.policy /usr/share/polkit-1/actions/
sudo install -o root -g root -m 0644 your-cpu-performance.conf /etc/tmpfiles.d/cpu-performance.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/cpu-performance.conf
sudo systemctl daemon-reload
sudo systemctl enable --now cpu-frequency-limits.service

# from the repository root
scripts/install cpu-limits
omarchy plugin enable io.github.ltecosta.cpu-limits
```

Write `cpu-performance.conf` for your CPU from the example: one `scaling_max_freq` line per cpufreq policy, the same value for every policy of a core group.

## Service

`cpu-frequency-limits.service` (`Type=notify`, `WatchdogSec=30s`) runs the helper with `--watch`. It is meant to be the only writer of these CPU controls at runtime.

The service groups the cpufreq policies that share a turbo ceiling: three groups are `preferred`, `p` and `e`, any other count is named by frequency. Each group's default ceiling comes from `/etc/tmpfiles.d/cpu-performance.conf`. Each tick reads `settings.json` once, reconciles the active profile (writing a control only when it differs, and keeping `max_perf_pct` at 100) and publishes `revision` and `applied_revision`. A tick runs every 2 s and on a power-profile change, `PrepareForSleep` (suspend and resume) and `SIGHUP`.

- An invalid `settings.json` puts an error in the status; the service keeps applying the last settings it loaded, or the defaults when it has loaded none.
- With turbo off, ceilings below the base clock are applied and the kernel holds the others at the base clock. On the first start of a boot with turbo off, the service briefly turns turbo on to read the turbo ceilings and restores it.
- cpufreq applies a ceiling write through a work item, so a policy written in a tick is read back for up to 100 ms before it counts as divergent; a divergence is an error.
- A missing `platform_profile` is a discovery error; an unreadable one, or a value outside `platform_profile_choices`, is an error and that tick writes nothing. A discovery failure publishes an error status, writes nothing and is retried every 30 s.

### Power limits

Before an MSR PL1 or PL2 write, its provenance is saved in `runtime.json`: the original firmware value, the value being written and, until the write reads back, the value the limit held before. A write never happens without its record on disk, so a failed write or a restart still recognizes both values as the service's. The original value is written back when the profile returns to the firmware, and the record is dropped once it reads back. A value the service did not write is recorded as that profile's firmware value. An effective PL2 below PL1 is refused.

The service only reads the MMIO zone, which `thermald` may manage. Effective PL1 and PL2 are the minimum of the MSR and MMIO readings present, and the status warns when MMIO PL1 is below MSR PL1: the CPU obeys the lower of the two.

## Files

| Path | Content |
|---|---|
| `/var/lib/workstation-cpu-limits/settings.json` | `{"version": 2, "revision": N, "profiles": {...}}`. Each profile has `ceilings_mhz` (per group id), `turbo`, `pl1_w` and `pl2_w` (`null` follows the firmware). A missing profile uses the default ceilings, turbo on and firmware power limits. |
| `/var/lib/workstation-cpu-limits/settings.json.invalid` | An invalid `settings.json` moved aside by `--reset all` or `--set all`. |
| `/var/lib/workstation-cpu-limits/settings.json.lock` | Exclusive lock held around the atomic save. |
| `/run/workstation-cpu-limits/status.json` | Schema 1, rewritten every tick. |
| `/run/workstation-cpu-limits/runtime.json` | Schema 1: `turbo_max_khz` per policy, the `power.pl1` and `power.pl2` records (`uw`, `original_uw`, `previous_uw`, `profile`) and `firmware_w` per profile. Kept across service restarts within a boot. |

### status.json

| Field | Content |
|---|---|
| `schema`, `updated`, `pid` | `1`, publication time in seconds, service pid. |
| `revision`, `applied_revision` | The revision loaded this tick, or `-1` when settings were not loaded. `applied_revision` matches it when the tick applied the loaded settings with no error; otherwise `-1`. |
| `profile`, `applied`, `error`, `warnings`, `changed_at` | Active profile, whether the tick applied, error text, warnings, time of the last control write. |
| `profiles.<name>` | `ceilings_mhz`, `turbo`, `pl1_w`, `pl2_w`, `custom`, `firmware_pl1_w`, `firmware_pl2_w`. |
| `turbo`, `limits` | Live turbo flag; `limits` carries `step_mhz` (100), `power_step_w` (5), `stale_after_s` (10), `min_w` (15) and `max_w` (PL4). |
| `groups[]` | `id`, `label`, `cpus`, `min_mhz`, `max_mhz`, `default_mhz`, `avg_mhz`, `peak_mhz`, `ceiling_mhz`. |
| `power` | MSR `pl1_w`, `pl2_w`, `pl4_w`; `mmio.pl1_w`, `mmio.pl2_w`; `effective_pl1_w`, `effective_pl2_w`; `package_w`. |
| `thermal` | `package_c`, `crit_c`, `throttle_count`, `throttle_ms`, `throttle_pct`; `throttling` is true at 5 % or more of the sample interval. |
| `fans`, `load_pct`, `epp`, `governor` | Fan labels and rpm (the `asus` hwmon driver), CPU load without guest time, and the energy-performance preference and governor of the first group's first policy. |

## Helper

```bash
/usr/local/libexec/workstation-cpu-limits --status
pkexec /usr/local/libexec/workstation-cpu-limits --set <profile|all> '<json>'
pkexec /usr/local/libexec/workstation-cpu-limits --reset <profile|all>
```

`--status` prints the published status when it is at most 10 s old. `--set` and `--reset` run as root through the PolicyKit actions, which use `auth_admin_keep`: the first save asks for administrator authentication and a recent authorization is reused. The JSON object has exactly `ceilings_mhz`, `turbo`, `pl1_w` and `pl2_w`. Ceilings are whole 100 MHz steps inside each group's range; power limits are integers from 15 W to PL4, and PL2 is at least PL1. The helper writes `revision + 1` atomically under the lock, signals the service with `SIGHUP`, waits up to 6 s for `applied_revision` ≥ `revision`, and prints `{"profile","applied","revision","superseded"}`. When the service has read the save and still reports an error, the helper fails with that error (exit 1); the settings stay saved.

An invalid `settings.json` is recovered with `--reset all` or `--set all '<json>'`, which move it to `settings.json.invalid` and continue from an empty profile map. Any other target refuses with a message that names `--reset all`.

## Bar widget

The plugin's service (`Service.qml`) reads `status.json` in the shell process once for every monitor's bar, every 2 s while any monitor's panel is open and every 10 s otherwise. Each monitor's widget binds to it and keeps its own panel, drafts and keyboard cursor; after a save, the service re-reads the status so every bar shows it. The widget works under the built-in bar and under replacement bars: it takes the service from `bar.shell.serviceFor()` when the bar's shell hands it out, and otherwise from `ServiceLink.js`, a registry the service joins when it starts and leaves when it stops, so a service that loads late or is reloaded is picked up. Until a service is there, the widget shows a neutral "Reading CPU status…" and asks for a refresh over IPC. A status with another schema is refused, and the panel asks to restart the shell or the service. The icon takes the bar's urgent color on an error or a stale status (older than 10 s, a stopped service) and the accent color while throttling. The tooltip shows temperature, power, load and ceilings. The `barLabel` setting (`None`, `Temperature`, `Power`) adds a reading beside the icon.

The panel shows package power, temperature, load and fan speed, the throttling line and any warnings. For the selected profile it has one row per core group (ceiling, a marker at the live average clock, and the peak clock), the turbo switch, and PL1/PL2 rows. A power row shows the profile's MSR setting, or its firmware value, or the live MSR reading; its caption gives the effective limit and the MMIO value, and turns to the accent color when the shown value is above the MMIO limit.

Edits are drafts per profile, marked `*` on that profile; a draft equal to the saved settings is dropped. **Apply** saves the selected profile. **All profiles** writes the shown settings to every profile and discards the other drafts. **Restore the default limits** removes the selected profile's saved settings. Saves go through `pkexec`: exit 126 is a cancelled authentication dialog, 127 is not authorized. A save that a later save replaced before the service applied it is reported as such.

| Key | |
|---|---|
| `j` / `k` | Move |
| `h` / `l` | Adjust the focused row, or switch profile and turbo |
| Enter | Activate; on a power row with a custom value, return it to the firmware |
| `f` | Return the focused power row to the firmware |
| `t` | Toggle turbo |
| `a` / `u` | Apply / discard |
| `1`–`3` | Select a profile |

```bash
omarchy-shell io.github.ltecosta.cpu-limits toggle   # open, close, show, hide
omarchy-shell io.github.ltecosta.cpu-limits state
omarchy-shell io.github.ltecosta.cpu-limits refresh
```

The service owns the IPC target. `open`, `show` and `toggle` act on the focused monitor's panel through `omarchy-shell shell summon|toggle`, and `close` and `hide` close the open panel through `omarchy-shell shell hide`. `state` prints `kind`, the active `profile` and `error` from the status, `selected` and `dirty` from an open panel (else from one monitor's widget), and `opened`, whether any panel is open. `refresh` re-reads the status.

## Checks

```bash
python3 -B backend/check.py
journalctl -u cpu-frequency-limits.service -b
```

`backend/check.py` checks a fresh applied status in schema 1, a panel IPC answer without an error, and the shell log since the shell started for QML warnings or errors from the plugin. Run it after installing changes and after `omarchy update`.

## Remove

Disable `cpu-frequency-limits.service`, run `omarchy plugin disable io.github.ltecosta.cpu-limits`, remove `/etc/tmpfiles.d/cpu-performance.conf` and reboot.

## Development

```bash
node --test                                  # Model.js and ServiceLink.js
python3 -B -m unittest discover -s tests     # the helper against a simulated sysfs, and the check
bash tests/service.smoke.sh                  # Service.qml and two widgets in a headless Quickshell
```

## License

MIT
