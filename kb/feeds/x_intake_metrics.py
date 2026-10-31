"""Monotonic wall timings; cooldown/storage are subsets, never additive stage totals."""
from contextlib import contextmanager
import os
import time


def seconds(name, default):
    try:
        value = float(os.environ.get(name, default))
        return max(0.1, min(value, 3600))
    except ValueError:
        return float(default)


class Metrics:
    def __init__(self, budget):
        self.started = time.monotonic()
        self.deadline = self.started + max(0.1, budget)
        reserve = min(30.0, max(0.1, budget) * 0.2)
        self.network_deadline = self.deadline - reserve
        self.data = dict(budget_seconds=budget, cursor='bounded_dom_rescan',
                         native_cursor_verified=False, finalize_reserve_seconds=reserve,
                         phases={}, counts={}, local_errors=[])

    @contextmanager
    def phase(self, name):
        start = time.monotonic()
        try:
            yield
        finally:
            self.data['phases'].setdefault(name, {})['wall_seconds'] = round(time.monotonic() - start, 3)

    @contextmanager
    def storage(self, phase):
        start = time.monotonic()
        try:
            yield
        finally:
            block = self.data['phases'].setdefault(phase, {})
            block['storage_seconds'] = block.get('storage_seconds', 0) + time.monotonic() - start

    def count(self, name, amount=1):
        counts = self.data['counts']
        counts[name] = counts.get(name, 0) + amount

    def finish(self, cool=None):
        self.data['elapsed_seconds'] = round(time.monotonic() - self.started, 3)
        self.data['overrun_seconds'] = round(max(0, time.monotonic() - self.deadline), 3)
        self.data['rate_hits'] = cool.hits if cool else 0
        return self.data
