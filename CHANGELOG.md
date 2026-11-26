# Changelog

## 3.1.0

- A plugin service reads the CPU status once for every monitor's bar, every 2 s while any monitor's panel is open and every 10 s otherwise, and owns the IPC target; a save on one monitor refreshes every bar.
- `open`, `show`, `close`, `hide` and `toggle` act on the focused monitor's panel; `state` also reports whether a panel is open, and `refresh` re-reads the status.

## 3.0.0

- The widget's id and IPC target are `io.github.ltecosta.cpu-limits`.
- The panel calls the tmpfiles ceilings "default limits".
- Ships with generic examples of the systemd unit, the PolicyKit actions and the default-ceilings file.

## 2.0.0

- Per-profile frequency ceilings for each core group, turbo, and RAPL PL1/PL2 with firmware provenance, applied by a hardened notify service and saved through PolicyKit.
- Bar widget with live package power, temperature, clocks, load, fans and throttling, per-profile drafts and keyboard control.
