#!/usr/bin/python3 -I
"""Maintain per-profile CPU limits and publish live CPU telemetry.

The --watch service is the only writer of CPU controls. It applies the settings
of the active platform profile (frequency ceilings per core group, turbo and the
RAPL package power limits PL1/PL2), reconciles them every two seconds and on
profile, settings and resume events, and publishes
/run/workstation-cpu-limits/status.json. --set and --reset (run through pkexec)
only save settings, signal the service and wait for its confirmation.
"""

import argparse
import copy
import fcntl
import glob
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

CONFIG = Path("/etc/tmpfiles.d/cpu-performance.conf")
STATE_DIR = Path("/var/lib/workstation-cpu-limits")
RUN_DIR = Path("/run/workstation-cpu-limits")
POLL_SECONDS = 2
DISCOVERY_SECONDS = 30
CONFIRM_SECONDS = 6
# cpufreq applies a scaling_max_freq write through a work item; readback of a
# policy written in this tick is retried briefly before it counts as divergent.
SETTLE_TRIES = 10
SETTLE_SECONDS = 0.01
STALE_SECONDS = 10
STEP_MHZ = 100
PL_MIN_W = 15
PL_FALLBACK_MAX_W = 250
GROUP_LABELS = {"preferred": "Preferred P-cores", "p": "P-cores", "e": "E-cores"}
POWER_KEYS = {"long_term": "pl1", "short_term": "pl2", "peak_power": "pl4"}
SETTING_KEYS = {"ceilings_mhz", "turbo", "pl1_w", "pl2_w"}


def read_text(path):
    return Path(path).read_text().strip()


def read_int(path):
    return int(read_text(path))


def write_value(path, value):
    Path(path).write_text(f"{value}\n")


def write_json(path, data, mode=0o644):
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, separators=(",", ":"))
            stream.write("\n")
            os.fchmod(stream.fileno(), mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


class SettingsError(ValueError):
    """The saved settings cannot be safely interpreted."""


class ReadbackError(RuntimeError):
    """Controls were written but do not read back as requested."""

    def __init__(self, message, changed):
        super().__init__(message)
        self.changed = changed


class Snapshot(NamedTuple):
    profiles: dict
    revision: int


def parse_default_ceilings(config, sysfs):
    defaults = {}
    for line in config.read_text().splitlines():
        fields = line.split()
        if len(fields) < 7 or fields[0] != "w" or "/cpufreq/policy" not in fields[1]:
            continue
        if not fields[1].endswith("/scaling_max_freq"):
            raise ValueError(f"Unexpected CPU policy setting: {fields[1]}")
        for name in glob.glob(str(sysfs / fields[1].removeprefix("/sys/"))):
            policy = Path(name).parent.name
            if policy in defaults:
                raise ValueError(f"Overlapping default ceilings for {policy}")
            defaults[policy] = int(fields[6])
    return defaults


def group_policies(sysfs, defaults, turbo_max):
    policies = sorted((sysfs / "devices/system/cpu/cpufreq").glob("policy*"), key=lambda p: int(p.name[6:]))
    if not policies:
        raise ValueError("No CPU frequency policies")
    by_max = {}
    for policy in policies:
        if policy.name not in defaults:
            raise ValueError(f"No default ceiling for {policy.name}")
        if policy.name not in turbo_max:
            raise ValueError(f"Unknown turbo ceiling for {policy.name}")
        by_max.setdefault(turbo_max[policy.name], []).append(policy)
    ordered = sorted(by_max.items(), reverse=True)
    ids = ["preferred", "p", "e"] if len(ordered) == 3 else [f"g{m // 1000}" for m, _ in ordered]
    groups = []
    for gid, (maximum, members) in zip(ids, ordered, strict=True):
        values = {defaults[p.name] for p in members}
        if len(values) != 1:
            raise ValueError(f"{gid}: policies in one group need one default ceiling")
        groups.append({
            "id": gid,
            "label": GROUP_LABELS.get(gid, f"{maximum // 1000} MHz cores"),
            "policies": members,
            "cpus": [int(p.name[6:]) for p in members],
            "min_mhz": max(read_int(p / "cpuinfo_min_freq") for p in members) // 1000,
            "max_mhz": maximum // 1000,
            "default_mhz": values.pop() // 1000,
        })
    return groups


def find_rapl(sysfs):
    zones = {"msr": None, "mmio": None}
    for kind, pattern in (("msr", "intel-rapl:*"), ("mmio", "intel-rapl-mmio:*")):
        for zone in sorted((sysfs / "class/powercap").glob(pattern)):
            if zone.name.count(":") != 1 or read_text(zone / "name") != "package-0":
                continue
            constraints = {"pl4": None}
            for name_file in zone.glob("constraint_*_name"):
                key = POWER_KEYS.get(read_text(name_file))
                if key:
                    index = name_file.name.split("_")[1]
                    constraints[key] = zone / f"constraint_{index}_power_limit_uw"
            if "pl1" in constraints and "pl2" in constraints:
                zones[kind] = {"zone": zone, **constraints}
            break
    return zones


class Topology:
    """Discovered frequency groups and the MSR and MMIO RAPL zones."""

    def __init__(self, sysfs, config, turbo_max):
        self.defaults = parse_default_ceilings(config, sysfs)
        self.groups = group_policies(sysfs, self.defaults, turbo_max)
        self.rapl = find_rapl(sysfs)
        peak = self.rapl["msr"]["pl4"] if self.rapl["msr"] else None
        self.pl_max_w = read_int(peak) // 1_000_000 if peak else PL_FALLBACK_MAX_W


def plan_power(current_uw, record, desired_uw):
    """Plan one MSR limit: return (value to write or None, new provenance record).

    A record marks the limit as ours: `uw` is the value we wrote or are writing,
    `previous_uw` the value ours held before a write that is not yet confirmed,
    and `original_uw` the firmware value to hand back. A reading that matches
    neither `uw` nor `previous_uw` means something else (the firmware) changed it.
    """
    ours = record is not None and current_uw in (record["uw"], record.get("previous_uw"))
    if not ours:
        record = None
    if desired_uw is None:
        if record is None or current_uw == record["original_uw"]:
            return None, None
        # Hand the firmware value back; keep the record until it reads back.
        return record["original_uw"], {**record, "uw": record["original_uw"], "previous_uw": current_uw}
    if current_uw != desired_uw:
        original = record["original_uw"] if record is not None else current_uw
        previous = current_uw if record is not None else None
        return desired_uw, {**(record or {}), "uw": desired_uw, "original_uw": original, "previous_uw": previous}
    if record is None:
        return None, None
    settled = {key: value for key, value in record.items() if key != "previous_uw"}
    return None, {**settled, "uw": desired_uw, "previous_uw": None}


class Controller:
    def __init__(self, sysfs=Path("/sys"), config=CONFIG, state_dir=STATE_DIR, run_dir=RUN_DIR):
        self.sysfs = sysfs
        self.config = config
        self.settings_path = state_dir / "settings.json"
        self.status_path = run_dir / "status.json"
        self.runtime_path = run_dir / "runtime.json"
        self.pstate = sysfs / "devices/system/cpu/intel_pstate"
        self.profile_path = sysfs / "firmware/acpi/platform_profile"
        self.runtime = None
        self.topology = None
        self.profile_choices = None
        self.sleep = time.sleep

    def load_runtime(self):
        if self.runtime is not None:
            return self.runtime
        empty = {"schema": 1, "turbo_max_khz": {}, "power": {"pl1": None, "pl2": None}, "firmware_w": {}}
        try:
            data = load_json(self.runtime_path, empty)
            if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1:
                raise ValueError("expected runtime schema 1")
            turbo, power, firmware = data.get("turbo_max_khz"), data.get("power"), data.get("firmware_w")
            if not isinstance(turbo, dict) or any(
                not isinstance(k, str) or type(v) is not int or v <= 0 for k, v in turbo.items()
            ):
                raise ValueError("invalid turbo_max_khz")
            if not isinstance(power, dict) or set(power) != {"pl1", "pl2"}:
                raise ValueError("invalid power records")
            for record in power.values():
                if record is None:
                    continue
                if not isinstance(record, dict) or set(record) != {"uw", "original_uw", "previous_uw", "profile"}:
                    raise ValueError("invalid power record")
                previous = record["previous_uw"]
                if (any(type(record[k]) is not int or record[k] < 0 for k in ("uw", "original_uw"))
                        or not (previous is None or (type(previous) is int and previous >= 0))
                        or not isinstance(record["profile"], str)):
                    raise ValueError("invalid power record values")
            if not isinstance(firmware, dict) or any(
                not isinstance(name, str) or not isinstance(values, dict)
                or not set(values) <= {"pl1", "pl2"}
                or any(type(v) is not int or v < 0 for v in values.values())
                for name, values in firmware.items()
            ):
                raise ValueError("invalid firmware_w")
            self.runtime = data
        except (OSError, ValueError) as error:
            print(f"Ignoring invalid runtime.json: {error}", flush=True)
            self.runtime = empty
        return self.runtime

    def save_runtime(self):
        write_json(self.runtime_path, self.runtime)

    def refresh_turbo_max(self, persist=True):
        runtime = self.load_runtime()
        if read_int(self.pstate / "no_turbo") == 0:
            values = {p.name: read_int(p / "cpuinfo_max_freq") for p in
                      (self.sysfs / "devices/system/cpu/cpufreq").glob("policy*")}
            if values != runtime["turbo_max_khz"]:
                runtime["turbo_max_khz"] = values
                if persist:
                    self.save_runtime()
                return True
        return False

    def discover(self, allow_turbo_probe=False, persist=True):
        runtime = self.load_runtime()
        policies = list((self.sysfs / "devices/system/cpu/cpufreq").glob("policy*"))
        if not policies:
            raise ValueError("No CPU frequency policies")
        no_turbo = self.pstate / "no_turbo"
        previous = read_int(no_turbo)
        probe = previous and any(p.name not in runtime["turbo_max_khz"] for p in policies)
        if probe and (not allow_turbo_probe or not persist):
            raise ValueError("Turbo ceilings are unknown while turbo is off; start the service")
        try:
            if probe:
                write_value(no_turbo, 0)
            self.refresh_turbo_max(persist=persist)
            topology = Topology(self.sysfs, self.config, runtime["turbo_max_khz"])
            if not self.profile_path.exists():
                raise ValueError(f"Platform profile interface is not available: {self.profile_path}")
            choices = read_text(self.profile_path.with_name("platform_profile_choices")).split()
            if not choices:
                raise ValueError("No platform profile choices")
        finally:
            if probe:
                write_value(no_turbo, previous)
        self.topology = topology
        self.profile_choices = choices
        self.profiles()
        return topology

    def profiles(self):
        choices = self.profile_choices
        if choices is None:
            raise RuntimeError("Platform profiles are not discovered")
        active = read_text(self.profile_path)
        if active not in choices:
            raise ValueError(f"Unknown active platform profile: {active}")
        return choices, active

    def defaults(self):
        return {
            "ceilings_mhz": {g["id"]: g["default_mhz"] for g in self.topology.groups},
            "turbo": True,
            "pl1_w": None,
            "pl2_w": None,
        }

    def load_settings(self):
        try:
            data = load_json(self.settings_path, {"version": 2, "revision": 0, "profiles": {}})
            if not isinstance(data, dict):
                raise ValueError("root must be an object")
            if type(data.get("version")) is not int or data["version"] != 2:
                raise ValueError("version must be 2")
            revision = data.get("revision")
            if type(revision) is not int or revision < 0:
                raise ValueError("revision must be a non-negative integer")
            profiles = data.get("profiles")
            if not isinstance(profiles, dict):
                raise ValueError("profiles must be an object")
            for name, values in profiles.items():
                if name not in self.profile_choices:
                    raise ValueError(f"Unknown saved profile: {name}")
                try:
                    self.validate(values)
                except ValueError as error:
                    raise ValueError(f"profile {name}: {error}") from error
            return Snapshot(copy.deepcopy(profiles), revision)
        except (OSError, ValueError) as error:
            raise SettingsError(str(error)) from error

    def stored(self):
        return self.load_settings()

    def effective(self, profile, snapshot=None):
        if snapshot is None:
            snapshot = self.load_settings()
        return snapshot.profiles.get(profile, self.defaults())

    def validate(self, values):
        if not isinstance(values, dict) or set(values) != SETTING_KEYS:
            raise ValueError("Settings need ceilings_mhz, turbo, pl1_w and pl2_w")
        ceilings = values["ceilings_mhz"]
        if not isinstance(ceilings, dict) or set(ceilings) != {g["id"] for g in self.topology.groups}:
            raise ValueError("Provide one ceiling for each CPU group")
        for group in self.topology.groups:
            value = ceilings[group["id"]]
            if type(value) is not int or value % STEP_MHZ:
                raise ValueError("Use whole 100 MHz steps")
            if not group["min_mhz"] <= value <= group["max_mhz"]:
                raise ValueError(f"{group['label']}: ceiling must be {group['min_mhz']}–{group['max_mhz']} MHz")
        if type(values["turbo"]) is not bool:
            raise ValueError("Turbo must be true or false")
        for key in ("pl1_w", "pl2_w"):
            value = values[key]
            if value is None:
                continue
            if not self.topology.rapl["msr"]:
                raise ValueError("Package power limits are not available")
            if type(value) is not int or not PL_MIN_W <= value <= self.topology.pl_max_w:
                raise ValueError(f"Power limits must be {PL_MIN_W}–{self.topology.pl_max_w} W")
        if values["pl1_w"] is not None and values["pl2_w"] is not None and values["pl2_w"] < values["pl1_w"]:
            raise ValueError("PL2 must be at least PL1")
        return values

    def reconcile(self, snapshot=None, profile=None):
        """Bring the hardware to the active profile's settings; return the changed controls."""
        # Read the active profile before any control writes; read failures are fatal.
        _choices, active = self.profiles()
        if profile is not None and profile != active:
            raise RuntimeError("Platform profile changed during tick")
        profile = active
        want = self.effective(profile, snapshot)
        changed, mismatched = [], []
        global_path = self.pstate / "max_perf_pct"
        if read_int(global_path) != 100:
            write_value(global_path, 100)
            changed.append("global range")
        no_turbo = 0 if want["turbo"] else 1
        if read_int(self.pstate / "no_turbo") != no_turbo:
            write_value(self.pstate / "no_turbo", no_turbo)
            changed.append("turbo")
        written = []
        for group in self.topology.groups:
            target = want["ceilings_mhz"][group["id"]] * 1000
            for policy in group["policies"]:
                path = policy / "scaling_max_freq"
                if no_turbo:
                    # The kernel clamps ceilings to the base clock without turbo
                    # and restores them when turbo returns; do not fight it.
                    expected = min(target, read_int(policy / "cpuinfo_max_freq"))
                else:
                    expected = target
                if read_int(path) != expected:
                    write_value(path, target)
                    changed.append(policy.name)
                    written.append((policy.name, path, expected))
        for attempt in range(SETTLE_TRIES):
            written = [entry for entry in written if read_int(entry[1]) != entry[2]]
            if not written:
                break
            if attempt < SETTLE_TRIES - 1:
                self.sleep(SETTLE_SECONDS)
        mismatched += [name for name, _path, _expected in written]
        if self.refresh_turbo_max():
            self.topology = Topology(self.sysfs, self.config, self.runtime["turbo_max_khz"])
        if self.topology.rapl["msr"]:
            changed += self.reconcile_power(profile, want, mismatched)
        if mismatched:
            raise ReadbackError(f"Readback does not match settings: {', '.join(mismatched)}", changed)
        return changed

    def reconcile_power(self, profile, want, mismatched):
        runtime = self.load_runtime()
        state = copy.deepcopy(runtime)
        firmware = state["firmware_w"].setdefault(profile, {})
        plans, effective = {}, {}
        # Validate the entire effective pair before persisting or writing either limit.
        for key in ("pl1", "pl2"):
            current = read_int(self.topology.rapl["msr"][key])
            record = state["power"][key]
            if record is None or current not in (record["uw"], record.get("previous_uw")):
                firmware[key] = current // 1_000_000
            desired = want[f"{key}_w"]
            desired = desired * 1_000_000 if desired is not None else None
            write, new_record = plan_power(current, record, desired)
            if new_record is not None and write is not None:
                new_record["profile"] = profile
            plans[key] = write
            state["power"][key] = new_record
            effective[key] = write if write is not None else current
        if effective["pl2"] < effective["pl1"]:
            raise ValueError(f"PL2 below PL1 in {profile}")
        if state != runtime:
            # Write-ahead: the provenance of every planned write is on disk before
            # any sysfs write, and becomes current only once it is saved.
            write_json(self.runtime_path, state)
            self.runtime = state
        changed, confirmed = [], copy.deepcopy(state)
        for key, write in plans.items():
            if write is None:
                continue
            path = self.topology.rapl["msr"][key]
            write_value(path, write)
            changed.append(key.upper())
            if read_int(path) != write:
                mismatched.append(key.upper())
                continue
            # The write reads back: a restore ends ownership, an override settles.
            record = confirmed["power"][key]
            confirmed["power"][key] = None if record["uw"] == record["original_uw"] and want[f"{key}_w"] is None \
                else {**record, "previous_uw": None}
        if confirmed != state:
            write_json(self.runtime_path, confirmed)
            self.runtime = confirmed
        return changed

    def save(self, profile, values):
        """Validate and atomically save settings while holding the settings lock."""
        choices, _active = self.profiles()
        if profile == "all":
            targets = choices
        elif profile in choices:
            targets = [profile]
        else:
            raise ValueError(f"Unknown profile {profile}; use one of {', '.join(choices)} or all")
        if values is not None:
            self.validate(values)
        self.settings_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with open(self.settings_path.with_name("settings.json.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                snapshot = self.load_settings()
            except SettingsError as error:
                if profile != "all":
                    raise SettingsError(f"settings.json is invalid: {error}; run --reset all") from error
                os.replace(self.settings_path, self.settings_path.with_name("settings.json.invalid"))
                snapshot = Snapshot({}, 0)
            stored = copy.deepcopy(snapshot.profiles)
            for target in targets:
                if values is None:
                    stored.pop(target, None)
                else:
                    stored[target] = copy.deepcopy(values)
            revision = snapshot.revision + 1
            write_json(self.settings_path, {"version": 2, "revision": revision, "profiles": stored})
        return revision


class Telemetry:
    """Repeated sysfs reads through descriptors kept open for the service lifetime."""

    def __init__(self, sysfs, topology, proc=Path("/proc"), clock=time.monotonic):
        self.files = {}
        self.clock = clock
        self.sysfs = sysfs
        self.proc = proc
        self.topology = topology
        self.previous_energy = None
        self.previous_cpu = None
        self.previous_throttle_ms = None
        self.temp_path = None
        self.temp_crit = None
        self.fans = []
        for hwmon in sorted((sysfs / "class/hwmon").glob("hwmon*")):
            try:
                name = read_text(hwmon / "name")
            except OSError:
                continue
            if name == "coretemp":
                for label in hwmon.glob("temp*_label"):
                    if read_text(label) == "Package id 0":
                        prefix = label.name.removesuffix("_label")
                        self.temp_path = hwmon / f"{prefix}_input"
                        crit = hwmon / f"{prefix}_crit"
                        self.temp_crit = read_int(crit) // 1000 if crit.exists() else None
            elif name == "asus":
                for fan in sorted(hwmon.glob("fan*_input")):
                    label = fan.with_name(fan.name.replace("_input", "_label"))
                    text = read_text(label) if label.exists() else fan.name[:4]
                    self.fans.append((text.removesuffix("_fan").upper(), fan))

    def read(self, path):
        try:
            descriptor = self.files.get(path)
            if descriptor is None:
                descriptor = self.files[path] = os.open(path, os.O_RDONLY)
            return os.pread(descriptor, 256, 0).decode().strip()
        except OSError:
            descriptor = self.files.pop(path, None)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            return None

    def close(self):
        for descriptor in self.files.values():
            os.close(descriptor)
        self.files.clear()

    def number(self, path):
        try:
            return int(self.read(path))
        except (TypeError, ValueError):
            return None

    def sample(self):
        now = self.clock()
        result = {"groups": {}, "fans": []}
        for group in self.topology.groups:
            freqs = [f for f in (self.number(p / "scaling_cur_freq") for p in group["policies"]) if f]
            ceilings = {self.number(p / "scaling_max_freq") for p in group["policies"]}
            result["groups"][group["id"]] = {
                "avg_mhz": round(sum(freqs) / len(freqs) / 1000) if freqs else None,
                "peak_mhz": max(freqs) // 1000 if freqs else None,
                "ceiling_mhz": sorted(c // 1000 for c in ceilings if c is not None),
            }
        rapl = self.topology.rapl["msr"]
        result["package_w"] = None
        if rapl:
            energy = self.number(rapl["zone"] / "energy_uj")
            if energy is not None and self.previous_energy and now > self.previous_energy[1]:
                delta = energy - self.previous_energy[0]
                if delta < 0:
                    delta += self.number(rapl["zone"] / "max_energy_range_uj") or 0
                result["package_w"] = round(delta / (now - self.previous_energy[1]) / 1_000_000, 1)
            self.previous_energy = (energy, now) if energy is not None else None
            result["power_uw"] = {k: self.number(rapl[k]) for k in ("pl1", "pl2", "pl4") if rapl.get(k)}
        mmio = self.topology.rapl["mmio"]
        result["mmio_uw"] = {k: self.number(mmio[k]) for k in ("pl1", "pl2")} if mmio else None
        temp = self.number(self.temp_path) if self.temp_path else None
        result["package_c"] = round(temp / 1000) if temp is not None else None
        result["crit_c"] = self.temp_crit
        throttle = self.sysfs / "devices/system/cpu/cpu0/thermal_throttle"
        throttle_ms = self.number(throttle / "package_throttle_total_time_ms")
        result["throttle_count"] = self.number(throttle / "package_throttle_count")
        result["throttle_ms"] = throttle_ms
        result["throttle_pct"] = None
        if throttle_ms is not None and self.previous_throttle_ms is not None:
            previous, sampled_at = self.previous_throttle_ms
            if now > sampled_at:
                result["throttle_pct"] = round(max(0, throttle_ms - previous) / ((now - sampled_at) * 1000) * 100, 1)
        result["throttling"] = result["throttle_pct"] is not None and result["throttle_pct"] >= 5
        self.previous_throttle_ms = (throttle_ms, now) if throttle_ms is not None else None
        result["fans"] = [{"label": label, "rpm": self.number(path)} for label, path in self.fans]
        result["load_pct"] = None
        stat = self.read(self.proc / "stat")
        if stat:
            fields = [int(v) for v in stat.splitlines()[0].split()[1:]]
            idle, total = fields[3] + fields[4], sum(fields[:8])
            if self.previous_cpu and total > self.previous_cpu[1]:
                span = total - self.previous_cpu[1]
                result["load_pct"] = round(100 * (span - (idle - self.previous_cpu[0])) / span)
            self.previous_cpu = (idle, total)
        policy0 = self.topology.groups[0]["policies"][0]
        result["epp"] = self.read(policy0 / "energy_performance_preference")
        result["governor"] = self.read(policy0 / "scaling_governor")
        return result


def minimal_status(error, updated, changed_at=None):
    """Publish the complete schema even while hardware discovery is unavailable."""
    return {
        "schema": 1, "updated": updated, "pid": os.getpid(),
        "revision": -1, "applied_revision": -1, "profile": None, "profiles": {},
        "applied": False, "error": error, "warnings": [], "changed_at": changed_at,
        "turbo": None,
        "limits": {"step_mhz": STEP_MHZ, "power_step_w": 5, "stale_after_s": STALE_SECONDS,
                   "min_w": PL_MIN_W, "max_w": PL_FALLBACK_MAX_W},
        "groups": [], "power": None,
        "thermal": {"package_c": None, "crit_c": None, "throttle_count": None,
                    "throttle_ms": None, "throttle_pct": None, "throttling": False},
        "fans": [], "load_pct": None, "epp": None, "governor": None,
    }


def build_status(controller, telemetry, error, changed_at, snapshot=None, *,
                 profile=None, revision=None, applied_revision=None, updated=None):
    # Service always supplies its snapshot; direct callers may load one explicitly here.
    if snapshot is None:
        snapshot = controller.load_settings()
    if profile is None:
        _choices, profile = controller.profiles()
    topology = controller.topology
    telemetry.topology = topology
    sample = telemetry.sample()
    runtime = controller.load_runtime()
    profiles = {}
    for name in controller.profile_choices:
        firmware = runtime["firmware_w"].get(name, {})
        profiles[name] = {
            **controller.effective(name, snapshot), "custom": name in snapshot.profiles,
            "firmware_pl1_w": firmware.get("pl1"), "firmware_pl2_w": firmware.get("pl2"),
        }
    groups = [
        {key: group[key] for key in ("id", "label", "cpus", "min_mhz", "max_mhz", "default_mhz")}
        | sample["groups"][group["id"]] for group in topology.groups
    ]
    warnings, power = [], None
    if topology.rapl["msr"]:
        live = {k: v // 1_000_000 if v is not None else None for k, v in sample["power_uw"].items()}
        mmio = {f"{k}_w": v // 1_000_000 if v is not None else None
                for k, v in sample["mmio_uw"].items()} if sample["mmio_uw"] else None
        power = {f"{k}_w": live.get(k) for k in ("pl1", "pl2", "pl4")}
        power.update({"mmio": mmio, "package_w": sample["package_w"]})
        for key in ("pl1_w", "pl2_w"):
            values = [v for v in (power[key], mmio[key] if mmio else None) if v is not None]
            power[f"effective_{key}"] = min(values) if values else None
        if mmio and mmio["pl1_w"] is not None and power["pl1_w"] is not None and mmio["pl1_w"] < power["pl1_w"]:
            warnings.append(f"MMIO RAPL zone limits PL1 to {mmio['pl1_w']:g} W (thermald)")
    else:
        warnings.append("RAPL MSR zone not found; power limits unavailable")
    status = minimal_status(error, time.time() if updated is None else updated, changed_at)
    status.update({
        "revision": snapshot.revision if revision is None else revision,
        "applied_revision": (snapshot.revision if error is None else -1) if applied_revision is None else applied_revision,
        "profile": profile, "profiles": profiles, "applied": error is None,
        "warnings": warnings, "turbo": read_int(controller.pstate / "no_turbo") == 0,
        "groups": groups, "power": power,
        "thermal": {key: sample[key] for key in
                    ("package_c", "crit_c", "throttle_count", "throttle_ms", "throttle_pct", "throttling")},
        "fans": sample["fans"], "load_pct": sample["load_pct"],
        "epp": sample["epp"], "governor": sample["governor"],
    })
    status["limits"]["max_w"] = topology.pl_max_w
    return status


class Service:
    """One snapshot, reconciliation and status publication per tick."""

    def __init__(self, controller, telemetry=None, clock=time.time):
        self.controller = controller
        self.telemetry = telemetry
        self.clock = clock
        self.last_snapshot = None
        self.changed_at = None
        self.discovery_at = float("-inf")
        self.discovery_error = None
        self.logged = set()

    def log(self, message):
        if message not in self.logged:
            print(message, flush=True)
            self.logged.add(message)

    def tick(self):
        controller, now = self.controller, self.clock()
        status = minimal_status(None, now, self.changed_at)
        errors, snapshot, profile = [], self.last_snapshot or Snapshot({}, 0), None
        revision = applied_revision = -1
        try:
            if controller.topology is None or self.telemetry is None:
                if now - self.discovery_at >= DISCOVERY_SECONDS:
                    self.discovery_at = now
                    try:
                        controller.discover(allow_turbo_probe=True)
                        self.telemetry = Telemetry(controller.sysfs, controller.topology)
                        self.discovery_error = None
                    except Exception as error:
                        controller.topology = None
                        self.discovery_error = str(error)
                if self.discovery_error:
                    raise RuntimeError(self.discovery_error)
            try:
                snapshot = controller.load_settings()
                self.last_snapshot = snapshot
                revision = snapshot.revision
            except SettingsError as error:
                errors.append(f"settings.json is invalid: {error}")
            _choices, profile = controller.profiles()
            try:
                changed = controller.reconcile(snapshot, profile)
            except ReadbackError as error:
                changed = error.changed
                errors.append(str(error))
            if changed:
                self.changed_at = now
                print(f"Applied {profile} settings: {', '.join(changed)}", flush=True)
            if not errors:
                applied_revision = snapshot.revision
        except Exception as error:
            errors.append(str(error))
        for error in errors:
            self.log(f"CPU settings error: {error}")
        if not errors and self.logged:
            print("CPU settings verification recovered", flush=True)
            self.logged.clear()
        error = "; ".join(errors) or None
        try:
            if controller.topology is not None and self.telemetry is not None and profile is not None:
                status = build_status(controller, self.telemetry, error, self.changed_at, snapshot,
                                      profile=profile, revision=revision,
                                      applied_revision=applied_revision, updated=now)
            else:
                status = minimal_status(error, now, self.changed_at)
                status["revision"] = revision
            write_json(controller.status_path, status)
        except Exception as failure:
            message = f"Status publication failed: {failure}"
            self.log(message)
            status.update(revision=revision, applied=False, applied_revision=-1,
                          error="; ".join(filter(None, (error, message))))
            # A transient sample/publication error must itself be visible to readers.
            try:
                write_json(controller.status_path, status)
            except Exception as retry_error:
                self.log(f"Status publication failed: {retry_error}")
        return status


def sd_notify(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
            connection.sendto(message.encode(), address)
    except OSError as error:
        print(f"Service notification failed: {error}", flush=True)


def watch(controller):
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib, GLibUnix

    service = Service(controller)
    ready = False

    def tick(*_args):
        nonlocal ready
        service.tick()
        if not ready:
            sd_notify("READY=1")
            ready = True
        sd_notify("WATCHDOG=1")
        return GLib.SOURCE_CONTINUE

    # Publish even when discovery or the system bus is unavailable at startup.
    tick()
    subscriptions, bus = [], None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        for sender, interface, member in (
            ("org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep"),
            ("org.freedesktop.UPower.PowerProfiles", "org.freedesktop.DBus.Properties", "PropertiesChanged"),
        ):
            subscriptions.append(bus.signal_subscribe(sender, interface, member, None, None,
                                                      Gio.DBusSignalFlags.NONE, tick))
    except Exception as error:
        service.log(f"System bus subscription failed: {error}")
    loop = GLib.MainLoop()
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: loop.quit())
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: loop.quit())
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP, tick)
    GLib.timeout_add_seconds(POLL_SECONDS, tick)
    try:
        loop.run()
    finally:
        if bus is not None:
            for subscription in subscriptions:
                bus.signal_unsubscribe(subscription)
        if service.telemetry is not None:
            service.telemetry.close()


def read_status(controller):
    status = json.loads(controller.status_path.read_text())
    if not isinstance(status, dict) or type(status.get("updated")) not in (int, float):
        raise ValueError("Invalid published CPU status")
    if not 0 <= time.time() - status["updated"] <= STALE_SECONDS:
        raise RuntimeError("cpu-frequency-limits.service is not publishing status")
    return status


def save_and_confirm(controller, profile, values, timeout=CONFIRM_SECONDS, *,
                     clock=time.monotonic, sleep=time.sleep):
    revision = controller.save(profile, values)
    saved_at = time.time()
    try:
        subprocess.run(["systemctl", "kill", "--kill-whom=main", "--signal=SIGHUP",
                        "cpu-frequency-limits.service"], check=False)
    except OSError:
        pass
    deadline = clock() + timeout
    first_error = None
    while clock() < deadline:
        try:
            status = read_status(controller)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            status = None
        if status and status["updated"] >= saved_at:
            applied = status.get("applied_revision")
            if type(applied) is int and applied >= revision and not status.get("error"):
                return {"profile": status["profile"], "applied": True,
                        "revision": revision, "superseded": applied > revision}
            seen = status.get("revision")
            if status.get("error") and type(seen) is int and seen >= revision:
                # The service read this save and could not apply it; report the
                # error once a later publication confirms it is not transient.
                if first_error is None:
                    first_error = status["updated"]
                elif status["updated"] > first_error:
                    raise RuntimeError(status["error"])
        sleep(0.2)
    raise RuntimeError("Saved, but cpu-frequency-limits.service did not confirm the change")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--status", action="store_true", help="print the published status")
    actions.add_argument("--watch", action="store_true", help="run the service")
    actions.add_argument("--set", nargs=2, metavar=("PROFILE", "JSON"), help="save a profile's settings ('all' for every profile)")
    actions.add_argument("--reset", metavar="PROFILE", help="restore a profile's defaults ('all' for every profile)")
    args = parser.parse_args()
    controller = Controller()
    try:
        if args.status:
            print(json.dumps(read_status(controller), indent=2))
            return
        if os.geteuid() != 0:
            raise PermissionError("Administrator authentication is required")
        if args.watch:
            watch(controller)
            return
        controller.discover(persist=False)
        values = None
        if args.set:
            try:
                values = json.loads(args.set[1])
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid settings JSON: {error}") from error
            controller.validate(values)
        status = save_and_confirm(controller, args.set[0] if args.set else args.reset, values)
        print(json.dumps(status))
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
