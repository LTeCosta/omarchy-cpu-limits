#!/usr/bin/env python3
"""Tests for backend/cpu-limits.py against a simulated sysfs tree.

Run: python3 -B -m unittest discover -s tests
"""

import importlib.machinery
import importlib.util
import json
import contextlib
import io
import os
import sys
import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fakesysfs import FakeKernel

SCRIPT = Path(__file__).resolve().parents[1] / "backend/cpu-limits.py"
loader = importlib.machinery.SourceFileLoader("cpu_limits", str(SCRIPT))
spec = importlib.util.spec_from_loader("cpu_limits", loader)
cpu_limits = importlib.util.module_from_spec(spec)
loader.exec_module(cpu_limits)

POLICIES = {0: 5600000, 1: 5600000, 2: 5800000, 3: 4100000}
CONFIG = """\
w /sys/devices/system/cpu/intel_pstate/max_perf_pct - - - - 100
w /sys/devices/system/cpu/cpufreq/policy[01]/scaling_max_freq - - - - 5300000
w /sys/devices/system/cpu/cpufreq/policy2/scaling_max_freq - - - - 5400000
w /sys/devices/system/cpu/cpufreq/policy3/scaling_max_freq - - - - 4000000
"""


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n")


class CpuLimitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.sys = root / "sys"
        self.proc = root / "proc"
        cpu = self.sys / "devices/system/cpu"
        for index, maximum in POLICIES.items():
            policy = cpu / f"cpufreq/policy{index}"
            put(policy / "cpuinfo_max_freq", maximum)
            put(policy / "cpuinfo_min_freq", 800000)
            put(policy / "scaling_max_freq", maximum)
            put(policy / "scaling_cur_freq", 2000000 + index)
            put(policy / "energy_performance_preference", "performance")
            put(policy / "scaling_governor", "powersave")
        put(cpu / "intel_pstate/max_perf_pct", 80)
        put(cpu / "intel_pstate/no_turbo", 0)
        put(cpu / "cpu0/thermal_throttle/package_throttle_total_time_ms", 10)
        put(cpu / "cpu0/thermal_throttle/package_throttle_count", 1)
        put(self.sys / "firmware/acpi/platform_profile", "performance")
        put(self.sys / "firmware/acpi/platform_profile_choices", "quiet balanced performance")
        zone = self.sys / "class/powercap/intel-rapl:0"
        put(zone / "name", "package-0")
        put(zone / "energy_uj", 1000000)
        put(zone / "max_energy_range_uj", 262143328850)
        for index, (name, value) in enumerate(
            [("long_term", 160000000), ("short_term", 175000000), ("peak_power", 215000000)]
        ):
            put(zone / f"constraint_{index}_name", name)
            put(zone / f"constraint_{index}_power_limit_uw", value)
        hwmon = self.sys / "class/hwmon/hwmon10"
        put(hwmon / "name", "coretemp")
        put(hwmon / "temp1_label", "Package id 0")
        put(hwmon / "temp1_input", 61000)
        put(hwmon / "temp1_crit", 100000)
        fans = self.sys / "class/hwmon/hwmon8"
        put(fans / "name", "asus")
        put(fans / "fan1_input", 2400)
        put(fans / "fan1_label", "cpu_fan")
        put(self.proc / "stat", "cpu  100 0 100 800 0 0 0 0 0 0")
        config = root / "cpu-performance.conf"
        config.write_text(CONFIG)
        self.kernel = FakeKernel(self.sys, POLICIES)
        self.writer = patch.object(cpu_limits, "write_value", self.kernel.write)
        self.writer.start()
        self.addCleanup(self.writer.stop)
        self.controller = cpu_limits.Controller(
            sysfs=self.sys, config=config, state_dir=root / "lib", run_dir=root / "run"
        )
        self.controller.discover()

    def tearDown(self):
        self.tmp.cleanup()

    def value(self, relative):
        return int((self.sys / relative).read_text())

    def ceiling(self, index):
        return self.value(f"devices/system/cpu/cpufreq/policy{index}/scaling_max_freq")

    def pl(self, index):
        return self.value(f"class/powercap/intel-rapl:0/constraint_{index}_power_limit_uw")

    def settings(self, p=5000, preferred=5200, e=3600, turbo=True, pl1=None, pl2=None):
        return {"ceilings_mhz": {"p": p, "preferred": preferred, "e": e}, "turbo": turbo, "pl1_w": pl1, "pl2_w": pl2}

    def set_profile(self, name):
        put(self.sys / "firmware/acpi/platform_profile", name)

    def test_groups_follow_turbo_ceilings(self):
        groups = self.controller.topology.groups
        self.assertEqual([g["id"] for g in groups], ["preferred", "p", "e"])
        self.assertEqual([g["cpus"] for g in groups], [[2], [0, 1], [3]])
        self.assertEqual([g["default_mhz"] for g in groups], [5400, 5300, 4000])
        self.assertEqual(self.controller.topology.pl_max_w, 215)

    def test_defaults_apply_and_leave_power_limits_alone(self):
        changed = self.controller.reconcile()
        self.assertIn("global range", changed)
        self.assertEqual([self.ceiling(i) for i in range(4)], [5300000, 5300000, 5400000, 4000000])
        self.assertEqual((self.pl(0), self.pl(1)), (160000000, 175000000))
        self.assertEqual(self.controller.reconcile(), [])

    def test_settings_follow_the_active_profile(self):
        self.controller.save("quiet", self.settings(p=3000, preferred=3000, e=2500))
        self.controller.reconcile()
        self.assertEqual(self.ceiling(0), 5300000)
        self.set_profile("quiet")
        self.controller.reconcile()
        self.assertEqual([self.ceiling(i) for i in range(4)], [3000000, 3000000, 3000000, 2500000])
        self.controller.save("quiet", None)
        self.controller.reconcile()
        self.assertEqual(self.ceiling(0), 5300000)

    def test_power_limits_override_and_restore_firmware_values(self):
        self.controller.reconcile()
        self.controller.save("all", self.settings(p=5300, preferred=5400, e=4000, pl1=120, pl2=150))
        self.assertEqual(sorted(self.controller.reconcile()), ["PL1", "PL2"])
        self.assertEqual((self.pl(0), self.pl(1)), (120000000, 150000000))
        self.controller.save("performance", self.settings(p=5300, preferred=5400, e=4000, pl1=125, pl2=150))
        self.assertEqual(self.controller.reconcile(), ["PL1"])
        self.controller.save("performance", None)
        self.controller.reconcile()
        self.assertEqual((self.pl(0), self.pl(1)), (160000000, 175000000))
        self.assertEqual(self.controller.runtime["power"], {"pl1": None, "pl2": None})

    def test_firmware_rewrite_on_profile_change_becomes_that_profile_baseline(self):
        self.controller.save("all", self.settings(pl1=120, pl2=150))
        self.controller.reconcile()
        self.set_profile("quiet")
        put(self.sys / "class/powercap/intel-rapl:0/constraint_0_power_limit_uw", 45000000)
        put(self.sys / "class/powercap/intel-rapl:0/constraint_1_power_limit_uw", 65000000)
        self.assertIn("PL1", self.controller.reconcile())
        self.assertEqual((self.pl(0), self.pl(1)), (120000000, 150000000))
        self.assertEqual(self.controller.runtime["firmware_w"]["quiet"], {"pl1": 45, "pl2": 65})
        self.controller.save("quiet", None)
        self.controller.reconcile()
        self.assertEqual((self.pl(0), self.pl(1)), (45000000, 65000000))

    def test_turbo_off_keeps_kernel_clamped_ceilings(self):
        self.controller.save("performance", self.settings(turbo=False))
        changed = self.controller.reconcile()
        self.assertIn("turbo", changed)
        self.assertEqual(self.value("devices/system/cpu/intel_pstate/no_turbo"), 1)
        self.assertEqual(self.ceiling(0), 2200000)
        self.assertEqual(self.controller.reconcile(), [])

    def test_discovery_probes_turbo_ceilings_only_for_the_service(self):
        put(self.sys / "devices/system/cpu/intel_pstate/no_turbo", 1)
        fresh = cpu_limits.Controller(
            sysfs=self.sys, config=self.controller.config,
            state_dir=Path(self.tmp.name) / "lib2", run_dir=Path(self.tmp.name) / "run2",
        )
        with self.assertRaises(ValueError):
            fresh.discover()
        fresh.discover(allow_turbo_probe=True)
        self.assertEqual(self.value("devices/system/cpu/intel_pstate/no_turbo"), 1)

    def test_rapid_saves_are_each_seen(self):
        for ceiling in (5000, 4900, 4800):
            self.controller.save("performance", self.settings(p=ceiling))
            self.controller.reconcile()
            self.assertEqual(self.ceiling(0), ceiling * 1000)
        self.assertEqual(self.controller.stored()[1], 3)

    def test_validation(self):
        cases = [
            (self.settings(p=5050), "100 MHz"),
            (self.settings(e=4200), "E-cores"),
            (self.settings(pl1=200, pl2=150), "PL2"),
            (self.settings(pl1=10), "Power limits"),
            ({"ceilings_mhz": {"p": 5000}, "turbo": True, "pl1_w": None, "pl2_w": None}, "each CPU group"),
        ]
        for values, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.controller.save("performance", values)
        with self.assertRaisesRegex(ValueError, "Unknown profile"):
            self.controller.save("turbo-max", self.settings())

    def test_status_reports_profiles_and_telemetry(self):
        moments = iter([0, 0.1])
        telemetry = self.telemetry(clock=lambda: next(moments))
        self.controller.save("quiet", self.settings())
        self.controller.reconcile()
        cpu_limits.build_status(self.controller, telemetry, None, None)
        put(self.sys / "class/powercap/intel-rapl:0/energy_uj", 1000000 + 90 * 1000000)
        put(self.sys / "devices/system/cpu/cpu0/thermal_throttle/package_throttle_total_time_ms", 20)
        put(self.proc / "stat", "cpu  200 0 200 900 0 0 0 0 0 0")
        status = cpu_limits.build_status(self.controller, telemetry, None, None)
        json.dumps(status)
        self.assertEqual(status["profile"], "performance")
        self.assertTrue(status["profiles"]["quiet"]["custom"])
        self.assertFalse(status["profiles"]["performance"]["custom"])
        self.assertEqual(status["profiles"]["performance"]["firmware_pl1_w"], 160)
        self.assertGreater(status["power"]["package_w"], 0)
        self.assertEqual(status["thermal"]["package_c"], 61)
        self.assertTrue(status["thermal"]["throttling"])
        self.assertEqual(status["fans"], [{"label": "CPU", "rpm": 2400}])
        self.assertEqual(status["load_pct"], 67)
        self.assertEqual(status["groups"][1]["ceiling_mhz"], [5300])


    def telemetry(self, **kwargs):
        telemetry = cpu_limits.Telemetry(self.sys, self.controller.topology, proc=self.proc, **kwargs)
        self.addCleanup(telemetry.close)
        return telemetry

    def service(self, **kwargs):
        return cpu_limits.Service(self.controller, self.telemetry(), **kwargs)

    def restart(self, persist=True):
        controller = cpu_limits.Controller(
            sysfs=self.sys, config=self.controller.config,
            state_dir=self.controller.settings_path.parent, run_dir=self.controller.runtime_path.parent,
        )
        controller.discover(persist=persist)
        return controller

    def published(self):
        return json.loads(self.controller.status_path.read_text())

    def test_turbo_off_below_base_and_back_on(self):
        self.controller.save("performance", self.settings(p=1200, preferred=1500, e=900, turbo=False))
        service = self.service()
        status = service.tick()
        self.assertIsNone(status["error"])
        self.assertTrue(status["applied"])
        self.assertEqual([self.ceiling(i) for i in range(4)], [1200000, 1200000, 1500000, 900000])
        self.assertEqual(self.controller.reconcile(), [])
        self.controller.save("performance", self.settings())
        status = service.tick()
        self.assertTrue(status["turbo"])
        self.assertTrue(status["applied"])
        self.assertEqual([self.ceiling(i) for i in range(4)], [5000000, 5000000, 5200000, 3600000])
        # The fake kernel itself clamps a direct ceiling write while turbo is off.
        self.kernel.write(self.controller.pstate / "no_turbo", 1)
        policy = self.sys / "devices/system/cpu/cpufreq/policy0"
        cpu_limits.write_value(policy / "scaling_max_freq", 5000000)
        self.assertEqual(self.ceiling(0), 2200000)

    def test_snapshot_save_during_reconcile_does_not_confirm_new_revision(self):
        first = self.controller.save("performance", self.settings())
        reconcile = self.controller.reconcile

        def save_during_reconcile(snapshot, profile):
            changed = reconcile(snapshot, profile)
            self.controller.save("performance", self.settings(p=4900))
            return changed

        service = self.service()
        with patch.object(self.controller, "load_settings", wraps=self.controller.load_settings) as load:
            with patch.object(self.controller, "reconcile", side_effect=save_during_reconcile):
                status = service.tick()
            self.assertEqual(load.call_count, 2)  # Tick once, concurrent save once.
        self.assertEqual(status["revision"], first)
        self.assertEqual(status["applied_revision"], first)
        self.assertEqual(status["profiles"]["performance"]["ceilings_mhz"]["p"], 5000)
        self.assertEqual(self.controller.load_settings().revision, first + 1)
        self.assertEqual(service.tick()["applied_revision"], first + 1)

    def test_tick_loads_settings_once(self):
        with patch.object(self.controller, "load_settings", wraps=self.controller.load_settings) as load:
            self.service().tick()
        load.assert_called_once_with()

    def test_invalid_settings_keep_last_snapshot_and_tick_alive(self):
        revision = self.controller.save("performance", self.settings(p=3100))
        service = self.service()
        self.assertEqual(service.tick()["applied_revision"], revision)
        invalid = [
            ([], "root must be an object"),
            ({"version": 2, "revision": None, "profiles": {}}, "revision"),
            ({"version": 2, "revision": 1, "profiles": {"quiet": 5}}, "profile quiet"),
            ({"version": 1, "revision": 0, "profiles": {}}, "version"),
            ({"version": 2, "revision": -1, "profiles": {}}, "revision"),
            ({"version": 2, "revision": True, "profiles": {}}, "revision"),
            ({"version": 2, "revision": 0, "profiles": []}, "profiles"),
            ({"version": 2, "revision": 0, "profiles": {"unknown": {}}}, "Unknown saved profile"),
            ("malformed", "root must be an object"),
        ]
        for data, message in invalid:
            with self.subTest(data=data):
                cpu_limits.write_json(self.controller.settings_path, data)
                self.kernel.write(self.sys / "devices/system/cpu/cpufreq/policy0/scaling_max_freq", 5300000)
                status = service.tick()
                self.assertIn(message, status["error"])
                self.assertEqual(status["revision"], -1)
                self.assertEqual(status["applied_revision"], -1)
                self.assertFalse(status["applied"])
                self.assertEqual(self.ceiling(0), 3100000)
                self.assertEqual(self.published(), status)
        self.controller.settings_path.write_text('{')
        self.assertIn("settings.json is invalid", service.tick()["error"])
        self.controller.save("all", self.settings(p=3200))
        self.assertTrue(service.tick()["applied"])
        self.assertEqual(self.ceiling(0), 3200000)

    def test_invalid_settings_without_last_snapshot_reconcile_defaults(self):
        cpu_limits.write_json(self.controller.settings_path, [])
        status = self.service().tick()
        self.assertEqual(status["revision"], -1)
        self.assertIn("invalid", status["error"])
        self.assertEqual(self.ceiling(0), 5300000)

    def test_unreadable_settings_publishes_error_and_uses_last_snapshot(self):
        service = self.service()
        service.tick()
        original = cpu_limits.load_json

        def unreadable(path, default):
            if path == self.controller.settings_path:
                raise PermissionError("unreadable settings")
            return original(path, default)

        with patch.object(cpu_limits, "load_json", side_effect=unreadable):
            self.assertIn("unreadable settings", service.tick()["error"])
        self.assertTrue(service.tick()["applied"])

    def test_reset_and_set_all_recover_invalid_settings(self):
        for values in (None, self.settings()):
            with self.subTest(values=values):
                self.controller.settings_path.parent.mkdir(exist_ok=True)
                self.controller.settings_path.write_text('[]\n')
                cpu_limits.write_json(self.controller.settings_path.with_name("settings.json.invalid"), {})
                self.assertEqual(self.controller.save("all", values), 1)
                self.assertEqual(self.controller.settings_path.with_name("settings.json.invalid").read_text(), '[]\n')
                snapshot = self.controller.load_settings()
                self.assertEqual(snapshot.profiles, {} if values is None else {p: values for p in self.controller.profile_choices})
                self.assertTrue(self.controller.settings_path.with_name("settings.json.lock").exists())

    def test_set_one_invalid_settings_fails_without_replacing(self):
        cpu_limits.write_json(self.controller.settings_path, [])
        with self.assertRaisesRegex(cpu_limits.SettingsError, r"settings.json is invalid: root must be an object; run --reset all"):
            self.controller.save("quiet", self.settings())
        self.assertEqual(json.loads(self.controller.settings_path.read_text()), [])
        self.assertFalse(self.controller.settings_path.with_name("settings.json.invalid").exists())

    def test_invalid_runtime_shapes_are_empty(self):
        invalid = [[], {"turbo_max_khz": {}}, {"schema": 1},
                   {"schema": 1, "turbo_max_khz": [], "power": {}, "firmware_w": {}},
                   {"schema": 1, "turbo_max_khz": {}, "power": {"pl1": 5, "pl2": None}, "firmware_w": {}},
                   {"schema": 1, "turbo_max_khz": {}, "power": {"pl1": None, "pl2": None}, "firmware_w": []}]
        for data in invalid:
            with self.subTest(data=data), contextlib.redirect_stdout(io.StringIO()) as output:
                cpu_limits.write_json(self.controller.runtime_path, data)
                self.controller.runtime = None
                runtime = self.controller.load_runtime()
                self.assertEqual(runtime, {"schema": 1, "turbo_max_khz": {}, "power": {"pl1": None, "pl2": None}, "firmware_w": {}})
                self.assertIn("Ignoring invalid runtime.json", output.getvalue())
        self.controller.runtime_path.write_text('{')
        self.controller.runtime = None
        self.assertEqual(self.controller.load_runtime()["power"], {"pl1": None, "pl2": None})

    def test_startup_discovery_failure_retries_after_30_seconds(self):
        controller = self.restart()
        controller.topology = None
        now = [100.0]
        service = cpu_limits.Service(controller, self.telemetry(), clock=lambda: now[0])
        with patch.object(controller, "discover", side_effect=ValueError("discovery unavailable")) as discover:
            status = service.tick()
            self.assertIn("discovery unavailable", status["error"])
            self.assertFalse(status["applied"])
            self.assertEqual(status["schema"], 1)
            self.assertEqual(status["groups"], [])
            self.assertEqual(status["profiles"], {})
            self.assertIsNone(status["power"])
            now[0] += 29
            service.tick()
            self.assertEqual(discover.call_count, 1)
            now[0] += 1
            service.tick()
            self.assertEqual(discover.call_count, 2)
        now[0] += 30
        with patch.object(cpu_limits, "Telemetry", return_value=service.telemetry):
            self.assertTrue(service.tick()["applied"])

    def test_turbo_probe_restores_on_topology_and_read_failure(self):
        self.kernel.write(self.controller.pstate / "no_turbo", 1)
        for failing in ("Topology", "read"):
            with self.subTest(failing=failing):
                fresh = self.restart()
                fresh.runtime["turbo_max_khz"] = {}
                if failing == "Topology":
                    context = patch.object(cpu_limits, "Topology", side_effect=RuntimeError("probe failure"))
                else:
                    original = cpu_limits.read_int
                    def fail_max(path):
                        if path.name == "cpuinfo_max_freq":
                            raise OSError("probe failure")
                        return original(path)
                    context = patch.object(cpu_limits, "read_int", side_effect=fail_max)
                with context, self.assertRaisesRegex((OSError, RuntimeError), "probe failure"):
                    fresh.discover(allow_turbo_probe=True)
                self.assertEqual(self.value("devices/system/cpu/intel_pstate/no_turbo"), 1)

    def test_turbo_cache_refreshes_when_on_and_helper_never_writes_runtime(self):
        self.controller.reconcile()
        policy = self.sys / "devices/system/cpu/cpufreq/policy2/cpuinfo_max_freq"
        put(policy, 5900000)
        self.controller.reconcile()
        self.assertEqual(json.loads(self.controller.runtime_path.read_text())["turbo_max_khz"]["policy2"], 5900000)
        put(policy, 6000000)
        original = self.controller.runtime_path.read_bytes()
        helper = self.restart(persist=False)
        self.assertEqual(helper.topology.groups[0]["max_mhz"], 6000)
        self.assertEqual(self.controller.runtime_path.read_bytes(), original)
        self.controller.runtime_path.unlink()
        self.restart(persist=False)
        self.assertFalse(self.controller.runtime_path.exists())

    def test_profile_read_failure_or_unknown_value_writes_nothing(self):
        service = self.service()
        for bad in (None, "unexpected"):
            with self.subTest(profile=bad):
                if bad is None:
                    self.controller.profile_path.unlink()
                else:
                    put(self.controller.profile_path, bad)
                self.kernel.writes.clear()
                status = service.tick()
                self.assertIsNotNone(status["error"])
                self.assertFalse(status["applied"])
                self.assertEqual(self.kernel.writes, [])
        put(self.controller.profile_path, "quiet")
        self.assertTrue(service.tick()["applied"])

    def test_missing_profile_at_start_is_a_published_error_until_it_appears(self):
        self.controller.profile_path.unlink()
        service = cpu_limits.Service(cpu_limits.Controller(
            sysfs=self.sys, config=self.controller.config,
            state_dir=self.controller.settings_path.parent, run_dir=self.controller.status_path.parent))
        self.kernel.writes.clear()
        status = service.tick()
        self.assertFalse(status["applied"])
        self.assertIn("Platform profile interface is not available", status["error"])
        self.assertEqual(self.kernel.writes, [])
        put(self.controller.profile_path, "performance")
        service.discovery_at = float("-inf")
        self.assertTrue(service.tick()["applied"])

    def test_plan_power_table(self):
        record = {"uw": 120, "original_uw": 160, "previous_uw": None, "profile": "quiet"}
        pending = {"uw": 125, "original_uw": 160, "previous_uw": 120, "profile": "quiet"}
        restoring = {"uw": 160, "original_uw": 160, "previous_uw": 120, "profile": "quiet"}
        cases = [
            (160, None, None, None, None),
            # Restore: write the original and keep ownership until it reads back.
            (120, record, None, 160, restoring),
            (160, restoring, None, None, None),
            (120, restoring, None, 160, restoring),
            (110, record, None, None, None),
            (160, None, 120, 120, {"uw": 120, "original_uw": 160, "previous_uw": None}),
            # Update: the old value stays ours until the new one reads back.
            (120, record, 125, 125, pending),
            (120, pending, 125, 125, pending),
            (125, pending, 125, None, {**pending, "previous_uw": None}),
            (110, record, 125, 125, {"uw": 125, "original_uw": 110, "previous_uw": None}),
            (120, record, 120, None, record),
            (160, None, 160, None, None),
            (110, record, 110, None, None),
        ]
        for current, previous, desired, write, new in cases:
            with self.subTest(current=current, previous=previous, desired=desired):
                before = dict(previous) if previous else None
                self.assertEqual(cpu_limits.plan_power(current, previous, desired), (write, new))
                self.assertEqual(previous, before)

    def test_power_write_ahead_survives_pl2_failure_and_restart(self):
        self.controller.save("performance", self.settings(pl1=120, pl2=150))
        pl2_path = self.controller.topology.rapl["msr"]["pl2"]

        def fail_pl2(path, value):
            if path == pl2_path:
                # Both intents are durable before the failing write is attempted.
                runtime = json.loads(self.controller.runtime_path.read_text())
                self.assertEqual(runtime["power"]["pl1"]["original_uw"], 160000000)
                self.assertEqual(runtime["power"]["pl2"]["original_uw"], 175000000)
                raise OSError("PL2 write failed")
            self.kernel.write(path, value)

        with patch.object(cpu_limits, "write_value", side_effect=fail_pl2):
            status = self.service().tick()
        self.assertIn("PL2 write failed", status["error"])
        self.assertEqual((self.pl(0), self.pl(1)), (120000000, 175000000))
        restarted = self.restart()
        restarted.save("all", None)
        restarted.reconcile()
        self.assertEqual((self.pl(0), self.pl(1)), (160000000, 175000000))
        self.assertEqual(restarted.runtime["power"], {"pl1": None, "pl2": None})

    def test_effective_pair_inversion_makes_no_power_writes(self):
        for values in (self.settings(pl1=200), self.settings(pl2=150)):
            with self.subTest(values=values):
                self.controller.save("performance", values)
                self.kernel.writes.clear()
                status = self.service().tick()
                self.assertIn("PL2 below PL1 in performance", status["error"])
                self.assertFalse(any("power_limit_uw" in str(p) for p, v in self.kernel.writes))
                self.assertEqual((self.pl(0), self.pl(1)), (160000000, 175000000))

    def test_profile_change_with_unchanged_msr_retains_true_original(self):
        self.controller.save("all", self.settings(pl1=120, pl2=150))
        self.controller.reconcile()
        self.set_profile("quiet")
        self.controller.reconcile()
        self.assertNotIn("quiet", {k: v for k, v in self.controller.runtime["firmware_w"].items() if v})
        self.controller.save("quiet", None)
        self.controller.reconcile()
        self.assertEqual((self.pl(0), self.pl(1)), (160000000, 175000000))

    def add_mmio(self, pl1=55, pl2=125):
        zone = self.sys / "class/powercap/intel-rapl-mmio:0"
        put(zone / "name", "package-0")
        for index, (name, value) in enumerate((("long_term", pl1), ("short_term", pl2))):
            put(zone / f"constraint_{index}_name", name)
            put(zone / f"constraint_{index}_power_limit_uw", value * 1000000)
        self.controller.discover()
        return zone

    def test_mmio_effective_limits_and_warning_are_read_only(self):
        self.add_mmio()
        service = self.service()
        status = service.tick()
        self.assertEqual(status["power"]["mmio"], {"pl1_w": 55, "pl2_w": 125})
        self.assertEqual(status["power"]["effective_pl1_w"], 55)
        self.assertEqual(status["power"]["effective_pl2_w"], 125)
        self.assertIn("MMIO RAPL zone limits PL1 to 55 W (thermald)", status["warnings"])
        self.assertFalse(any("mmio" in str(p) for p, v in self.kernel.writes))
        self.controller.save("performance", self.settings(pl1=45, pl2=65))
        status = service.tick()
        self.assertEqual(status["power"]["effective_pl1_w"], 45)
        self.assertEqual(status["power"]["effective_pl2_w"], 65)
        self.assertEqual(status["warnings"], [])

    def test_no_rapl_is_nonfatal_warning_and_null_power(self):
        put(self.sys / "class/powercap/intel-rapl:0/name", "package-1")
        self.controller.discover()
        status = self.service().tick()
        self.assertTrue(status["applied"])
        self.assertIsNone(status["power"])
        self.assertEqual(status["warnings"], ["RAPL MSR zone not found; power limits unavailable"])
        with self.assertRaisesRegex(ValueError, "not available"):
            self.controller.validate(self.settings(pl1=120))

    def test_throttle_percentage_uses_elapsed_time_and_threshold(self):
        now = [0.0]
        telemetry = self.telemetry(clock=lambda: now[0])
        self.assertIsNone(telemetry.sample()["throttle_pct"])
        throttle = self.sys / "devices/system/cpu/cpu0/thermal_throttle/package_throttle_total_time_ms"
        for timestamp, total, expected, active in ((2.0, 11, 0.1, False), (4.0, 111, 5.0, True),
                                                   (6.0, 361, 12.5, True), (8.0, 361, 0.0, False)):
            now[0] = timestamp
            put(throttle, total)
            sample = telemetry.sample()
            self.assertEqual(sample["throttle_pct"], expected)
            self.assertEqual(sample["throttling"], active)

    def test_load_excludes_guest_and_guest_nice(self):
        telemetry = self.telemetry()
        telemetry.sample()
        put(self.proc / "stat", "cpu  200 0 100 900 0 0 0 0 100 40")
        self.assertEqual(telemetry.sample()["load_pct"], 50)

    def test_telemetry_reopens_failed_descriptor(self):
        telemetry = self.telemetry()
        path = self.proc / "stat"
        self.assertIsNotNone(telemetry.read(path))
        descriptor = telemetry.files[path]
        with patch.object(cpu_limits.os, "pread", side_effect=OSError("read failed")):
            self.assertIsNone(telemetry.read(path))
        self.assertNotIn(path, telemetry.files)
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        with patch.object(cpu_limits.os, "open", wraps=os.open) as opened:
            self.assertIsNotNone(telemetry.read(path))
            opened.assert_called_once_with(path, os.O_RDONLY)

    def test_tick_catches_unexpected_reconcile_and_publication_errors(self):
        revision = self.controller.save("performance", self.settings())
        service = self.service()
        with patch.object(self.controller, "reconcile", side_effect=TypeError("unexpected reconcile")):
            self.assertIn("unexpected reconcile", service.tick()["error"])
        with patch.object(cpu_limits, "build_status", side_effect=AttributeError("unexpected status")):
            status = service.tick()
            self.assertIn("Status publication failed: unexpected status", status["error"])
            self.assertEqual(self.published()["error"], status["error"])
            self.assertEqual(status["revision"], revision)
            self.assertEqual(status["applied_revision"], -1)
        self.assertTrue(service.tick()["applied"])
        with patch.object(cpu_limits, "write_json", side_effect=OSError("disk unavailable")):
            self.assertIn("disk unavailable", service.tick()["error"])
        self.assertTrue(service.tick()["applied"])

    def test_distinct_errors_are_logged_once(self):
        service = self.service()
        with patch.object(self.controller, "reconcile", side_effect=TypeError("unique failure")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                service.tick()
                service.tick()
        self.assertEqual(output.getvalue().count("unique failure"), 1)

    def helper_status(self, revision, **kwargs):
        status = cpu_limits.minimal_status(None, cpu_limits.time.time())
        status.update(profile="performance", revision=revision, applied_revision=revision, applied=True)
        status.update(kwargs)
        return status

    def confirm(self, statuses, timeout=1):
        now = [0.0]
        sequence = iter(statuses)
        def sleep(seconds):
            now[0] += seconds
        def read(_controller):
            status = next(sequence, {})
            if status:
                status["updated"] = cpu_limits.time.time()
            return status
        with patch.object(cpu_limits.subprocess, "run") as signal_service:
            with patch.object(cpu_limits, "read_status", side_effect=read):
                result = cpu_limits.save_and_confirm(self.controller, "performance", self.settings(),
                                                    timeout=timeout, clock=lambda: now[0], sleep=sleep)
        signal_service.assert_called_once_with(
            ["systemctl", "kill", "--kill-whom=main", "--signal=SIGHUP", "cpu-frequency-limits.service"], check=False)
        return result

    def test_helper_waits_for_applied_revision_and_superseded_save(self):
        result = self.confirm([self.helper_status(1, applied_revision=0), self.helper_status(2)])
        self.assertEqual(result, {"profile": "performance", "applied": True, "revision": 1, "superseded": True})
        result = self.confirm([self.helper_status(2)])
        self.assertEqual(result["revision"], 2)
        self.assertFalse(result["superseded"])

    def test_helper_ignores_unrelated_errors_but_reports_confirmed_error(self):
        result = self.confirm([self.helper_status(0, error="unrelated"), self.helper_status(1)])
        self.assertTrue(result["applied"])
        failing = lambda: self.helper_status(99, applied_revision=-1, error="PL2 below PL1 in performance")
        # The service read the save but could not apply it: the error is reported
        # once a later publication still carries it, well before the timeout.
        with self.assertRaisesRegex(RuntimeError, "PL2 below PL1"):
            self.confirm([failing(), failing()], timeout=60)
        # A readback error that clears on the next publication is transient.
        result = self.confirm([self.helper_status(99, applied_revision=-1, error="Readback does not match settings: policy0"),
                               self.helper_status(99)])
        self.assertTrue(result["applied"])

    def test_helper_timeout_and_stale_status(self):
        stale = self.helper_status(99, updated=cpu_limits.time.time() - 20)
        cpu_limits.write_json(self.controller.status_path, stale)
        with self.assertRaisesRegex(RuntimeError, "not publishing"):
            cpu_limits.read_status(self.controller)
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        with patch.object(cpu_limits.subprocess, "run"), self.assertRaisesRegex(RuntimeError, "Saved, but .* did not confirm"):
            cpu_limits.save_and_confirm(self.controller, "performance", self.settings(), timeout=0.3,
                                        clock=lambda: now[0], sleep=sleep)
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            self.confirm([self.helper_status(99, applied_revision=-1)], timeout=0.3)

    def test_helper_waits_for_publication_after_save(self):
        # A reset of invalid settings starts at revision 1; an older high revision
        # must not confirm it, even if the status is younger than STALE_SECONDS.
        cpu_limits.write_json(self.controller.settings_path, [])
        old = self.helper_status(99)
        cpu_limits.write_json(self.controller.status_path, old)
        now, publications = [0.0], []
        service = self.service()
        def sleep(seconds):
            now[0] += seconds
            publications.append(service.tick())
        with patch.object(cpu_limits.subprocess, "run"):
            result = cpu_limits.save_and_confirm(self.controller, "all", None, timeout=1,
                                                clock=lambda: now[0], sleep=sleep)
        self.assertEqual(len(publications), 1)
        self.assertEqual(result, {"profile": "performance", "applied": True, "revision": 1, "superseded": False})

    def test_profile_disappearance_during_discovery_retry_stays_an_error(self):
        self.controller.profile_path.unlink()
        with self.assertRaisesRegex(ValueError, "Platform profile interface is not available"):
            self.controller.discover()
        self.kernel.writes.clear()
        status = self.service().tick()
        self.assertFalse(status["applied"])
        self.assertIsNotNone(status["error"])
        self.assertEqual(self.kernel.writes, [])

    def test_deferred_ceiling_update_settles_without_an_error(self):
        # cpufreq applies scaling_max_freq through a work item: the first reads
        # after a write may still show the old ceiling.
        self.controller.reconcile()
        self.kernel.defer_reads = 3
        sleeps = []
        self.controller.sleep = lambda seconds: (sleeps.append(seconds), self.kernel.flush_one())
        self.controller.save("performance", self.settings(p=5000))
        status = self.service().tick()
        self.assertTrue(status["applied"], status["error"])
        self.assertEqual(self.ceiling(0), 5000000)
        self.assertTrue(sleeps)

    def test_ceiling_that_never_settles_is_a_readback_error_with_changes_logged(self):
        self.controller.reconcile()
        self.kernel.defer_reads = 1000
        self.controller.sleep = lambda seconds: None
        self.controller.save("performance", self.settings(p=5000))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = self.service().tick()
        self.assertFalse(status["applied"])
        self.assertIn("Readback does not match settings", status["error"])
        applied = [line for line in output.getvalue().splitlines() if line.startswith("Applied performance settings:")]
        self.assertEqual(len(applied), 1)
        self.assertIn("policy0", applied[0])

    def power_failure(self, key):
        path = self.controller.topology.rapl["msr"][key]
        original = self.kernel.write
        def write(target, value):
            if Path(target) == path:
                raise OSError("simulated RAPL write failure")
            original(target, value)
        return patch.object(cpu_limits, "write_value", write)

    def test_failed_restore_keeps_ownership_and_restores_after_restart(self):
        self.controller.save("performance", self.settings(pl1=120, pl2=150))
        self.controller.reconcile()
        self.controller.save("performance", self.settings())
        with self.power_failure("pl1"), self.assertRaises(OSError):
            self.controller.reconcile()
        self.assertEqual(self.pl(0), 120000000)
        controller = self.restart()
        controller.reconcile()
        self.assertEqual((self.pl(0), self.pl(1)), (160000000, 175000000))
        self.assertEqual(controller.runtime["power"], {"pl1": None, "pl2": None})

    def test_failed_override_update_keeps_the_original_firmware_value(self):
        self.controller.save("performance", self.settings(pl1=120, pl2=150))
        self.controller.reconcile()
        self.controller.save("performance", self.settings(pl1=125, pl2=150))
        with self.power_failure("pl1"), self.assertRaises(OSError):
            self.controller.reconcile()
        controller = self.restart()
        controller.save("performance", self.settings())
        controller.reconcile()
        self.assertEqual(self.pl(0), 160000000)

    def test_failed_provenance_persistence_writes_no_power_limit(self):
        self.controller.save("performance", self.settings(pl1=120, pl2=150))
        with patch.object(cpu_limits, "write_json", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.controller.reconcile()
        self.assertEqual((self.pl(0), self.pl(1)), (160000000, 175000000))
        self.assertEqual(self.controller.runtime["power"], {"pl1": None, "pl2": None})
        self.controller.reconcile()
        self.assertEqual((self.pl(0), self.pl(1)), (120000000, 150000000))

    def test_contract_recursive_structure_and_types(self):
        self.add_mmio()
        self.controller.save("quiet", self.settings(pl1=45, pl2=65))
        status = self.service().tick()
        fixture = json.loads((Path(__file__).parent / "fixtures/status.json").read_text())
        def compare(actual, expected, path="status"):
            if isinstance(expected, dict):
                self.assertIsInstance(actual, dict, path)
                self.assertEqual(set(actual), set(expected), path)
                for key, value in expected.items():
                    compare(actual[key], value, f"{path}.{key}")
            elif isinstance(expected, list):
                self.assertIsInstance(actual, list, path)
                if expected:
                    self.assertTrue(actual, path)
                    for item in actual:
                        exemplar = next((e for e in expected if isinstance(e, dict) and e.get("id") == item.get("id")), expected[0]) if isinstance(item, dict) else expected[0]
                        compare(item, exemplar, path + "[]")
            elif expected is None:
                allowed = (str,) if path.endswith(".error") else (int, float)
                self.assertTrue(actual is None or type(actual) in allowed, path)
            elif type(expected) in (int, float):
                self.assertTrue(actual is None or type(actual) is type(expected), f"{path}: {actual!r}")
            else:
                self.assertIs(type(actual), type(expected), path)
        compare(status, fixture)

    def test_main_exit_codes_and_messages(self):
        cases = [
            (["--set", "quiet", "{}"], 1000, "Administrator authentication is required"),
            (["--set", "quiet", "{"], 0, "Invalid settings JSON"),
            (["--set", "quiet", "null"], 0, "Settings need"),
            (["--set", "unknown", json.dumps(self.settings())], 0, "Unknown profile unknown"),
        ]
        for argv, uid, message in cases:
            with self.subTest(argv=argv):
                with patch.object(cpu_limits, "Controller", return_value=self.controller), \
                     patch.object(cpu_limits.os, "geteuid", return_value=uid), \
                     patch.object(sys, "argv", ["cpu-limits"] + argv), \
                     contextlib.redirect_stderr(io.StringIO()) as output:
                    with self.assertRaises(SystemExit) as exited:
                        cpu_limits.main()
                    self.assertEqual(exited.exception.code, 1)
                    self.assertIn(message, output.getvalue())
        status = self.service().tick()
        with patch.object(cpu_limits, "Controller", return_value=self.controller), \
             patch.object(cpu_limits.os, "geteuid", return_value=1000), \
             patch.object(sys, "argv", ["cpu-limits", "--status"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(cpu_limits.main())
            self.assertEqual(json.loads(output.getvalue()), status)
        confirmed = {"profile": "quiet", "applied": True, "revision": 1, "superseded": False}
        with patch.object(cpu_limits, "Controller", return_value=self.controller), \
             patch.object(cpu_limits.os, "geteuid", return_value=0), \
             patch.object(sys, "argv", ["cpu-limits", "--reset", "all"]), \
             patch.object(cpu_limits, "save_and_confirm", return_value=confirmed), \
             patch.object(self.controller, "discover", wraps=self.controller.discover) as discover, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cpu_limits.main()
            discover.assert_called_once_with(persist=False)
            self.assertEqual(json.loads(output.getvalue()), confirmed)

    def test_sd_notify_unset_path_and_abstract_socket(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(cpu_limits.socket, "socket") as opened:
            cpu_limits.sd_notify("READY=1")
            opened.assert_not_called()
        for configured, address in (("/run/notify", "/run/notify"), ("@notify", "\0notify")):
            with patch.dict(os.environ, {"NOTIFY_SOCKET": configured}), \
                 patch.object(cpu_limits.socket, "socket") as opened:
                cpu_limits.sd_notify("WATCHDOG=1")
                opened.assert_called_once_with(cpu_limits.socket.AF_UNIX, cpu_limits.socket.SOCK_DGRAM)
                opened.return_value.__enter__.return_value.sendto.assert_called_once_with(b"WATCHDOG=1", address)
        with patch.dict(os.environ, {"NOTIFY_SOCKET": "/unavailable"}), \
             patch.object(cpu_limits.socket, "socket", side_effect=OSError("notification unavailable")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cpu_limits.sd_notify("READY=1")
            self.assertIn("notification unavailable", output.getvalue())

    def test_watch_wrapper_continues_and_notifies_on_failed_discovery(self):
        callbacks, signals = [], {}
        loop = Mock()
        glib = types.SimpleNamespace(SOURCE_CONTINUE=True, PRIORITY_DEFAULT=0, MainLoop=lambda: loop,
                                     timeout_add_seconds=lambda seconds, fn: callbacks.append(fn))
        unix = types.SimpleNamespace(signal_add=lambda priority, sig, fn: signals.setdefault(sig, fn))
        bus = Mock()
        gio = types.SimpleNamespace(bus_get_sync=lambda *args: bus, BusType=types.SimpleNamespace(SYSTEM=0),
                                    DBusSignalFlags=types.SimpleNamespace(NONE=0))
        repository = types.ModuleType("gi.repository")
        repository.Gio, repository.GLib, repository.GLibUnix = gio, glib, unix
        gi = types.ModuleType("gi")
        gi.require_version = Mock()
        service = self.service()
        self.controller.topology = None
        def run_callbacks():
            self.assertTrue(callbacks[0]())
            self.assertTrue(signals[cpu_limits.signal.SIGHUP]())
        loop.run.side_effect = run_callbacks
        with patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}), \
             patch.object(cpu_limits, "Service", return_value=service), \
             patch.object(self.controller, "discover", side_effect=RuntimeError("startup failure")), \
             patch.object(cpu_limits, "sd_notify") as notify:
            cpu_limits.watch(self.controller)
        self.assertIn("startup failure", self.published()["error"])
        self.assertEqual([call.args[0] for call in notify.call_args_list],
                         ["READY=1", "WATCHDOG=1", "WATCHDOG=1", "WATCHDOG=1"])
        self.assertEqual(bus.signal_unsubscribe.call_count, 2)


if __name__ == "__main__":
    unittest.main()
