#!/usr/bin/env python3
"""Tests for the shell log filter of backend/check.py."""

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "backend/check.py"
loader = importlib.machinery.SourceFileLoader("check_cpu_limits", str(SCRIPT))
spec = importlib.util.spec_from_loader("check_cpu_limits", loader)
check = importlib.util.module_from_spec(spec)
loader.exec_module(check)

PANEL = "qs:///home/user/.config/omarchy/plugins/io.github.ltecosta.cpu-limits/Panel.qml"


class PluginLogTest(unittest.TestCase):
    def test_reports_plugin_errors_and_skips_known_platform_warnings(self):
        lines = [
            f"\x1b[33m  WARN\x1b[97m scene\x1b[0m: {PANEL}[481:-1]: TypeError: Cannot read property 'epp' of null",
            f"  WARN scene: QML ShellIpc at {PANEL}[207:3]: Handler was registered but will not be used because another handler is registered",
            '  WARN quickshell.qmlscanner: Module path "/home/user/.config/omarchy/plugins/io.github.ltecosta.cpu-limits" is outside of the config folder.',
            "  WARN scene: qs:///usr/share/omarchy/shell/plugins/panels/power/Panel.qml[10:1]: TypeError: other plugin",
            f" DEBUG qml: {PANEL} loaded",
        ]
        problems = check.plugin_log_problems(lines)
        self.assertEqual(len(problems), 1)
        self.assertIn("TypeError: Cannot read property 'epp' of null", problems[0])


if __name__ == "__main__":
    unittest.main()
