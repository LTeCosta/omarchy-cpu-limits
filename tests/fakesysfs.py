"""Minimal intel_pstate write behavior for temporary sysfs trees."""

from pathlib import Path


class FakeKernel:
    def __init__(self, sysfs, turbo_max):
        self.cpu = sysfs / "devices/system/cpu"
        self.turbo_max = turbo_max
        self.writes = []
        # When set, a scaling_max_freq write shows up only after that many
        # flush_one() calls, like the cpufreq work item that applies it.
        self.defer_reads = 0
        self.pending = []

    def write(self, path, value):
        path = Path(path)
        self.writes.append((path, value))
        if path.name == "scaling_max_freq":
            maximum = int((path.parent / "cpuinfo_max_freq").read_text())
            value = min(value, maximum)
            if self.defer_reads:
                self.pending.append([path, value, self.defer_reads])
                return
        path.write_text(f"{value}\n")
        if path == self.cpu / "intel_pstate/no_turbo":
            for index, maximum in self.turbo_max.items():
                policy = self.cpu / f"cpufreq/policy{index}"
                maximum = (1600000 if index == 3 else 2200000) if value else maximum
                (policy / "cpuinfo_max_freq").write_text(f"{maximum}\n")
                ceiling = policy / "scaling_max_freq"
                # intel_pstate restores the available ceiling when turbo returns.
                ceiling.write_text(f"{min(int(ceiling.read_text()), maximum) if value else maximum}\n")

    def flush_one(self):
        """Advance deferred cpufreq updates by one step."""
        for entry in self.pending:
            entry[2] -= 1
            if entry[2] <= 0:
                entry[0].write_text(f"{entry[1]}\n")
        self.pending = [entry for entry in self.pending if entry[2] > 0]
