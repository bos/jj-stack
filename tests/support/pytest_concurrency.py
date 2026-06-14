from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from _pytest.terminal import TerminalReporter

_REPORT_DIR_NAME = "jj-stack-concurrency"
_PLUGIN_NAME = "jj-stack-pytest-concurrency"


@dataclass(frozen=True)
class RecordedInterval:
    nodeid: str
    worker_id: str
    start_ns: int
    end_ns: int

    @property
    def wall_time_s(self) -> float:
        return max(self.end_ns - self.start_ns, 0) / 1_000_000_000


@dataclass(frozen=True)
class Bottleneck:
    nodeid: str
    worker_id: str
    wall_time_s: float
    concurrency_debt_s: float


@dataclass(frozen=True)
class ConcurrencySummary:
    requested_slots: int
    observed_workers: tuple[str, ...]
    test_count: int
    wall_time_s: float
    avg_active: float
    max_active: int
    full_capacity_time_s: float
    concurrency_debt_s: float
    occupancy: tuple[tuple[int, float], ...]
    bottlenecks: tuple[Bottleneck, ...]

    @property
    def estimated_savings_upper_bound_s(self) -> float:
        return self.concurrency_debt_s / self.requested_slots

    @property
    def estimated_balanced_runtime_floor_s(self) -> float:
        return max(self.wall_time_s - self.estimated_savings_upper_bound_s, 0.0)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("jj-stack")
    group.addoption(
        "--concurrency-report",
        action="store_true",
        default=False,
        help="Report observed test concurrency and major bottlenecks",
    )


def pytest_configure(config: pytest.Config) -> None:
    if not config.getoption("concurrency_report"):
        return
    if config.pluginmanager.hasplugin(_PLUGIN_NAME):
        return
    plugin = _ConcurrencyReporter(config)
    config.pluginmanager.register(plugin, _PLUGIN_NAME)


def analyze_intervals(
    intervals: list[RecordedInterval],
    *,
    requested_slots: int,
) -> ConcurrencySummary:
    if requested_slots < 1:
        raise ValueError("requested_slots must be at least 1")
    if not intervals:
        return ConcurrencySummary(
            requested_slots=requested_slots,
            observed_workers=(),
            test_count=0,
            wall_time_s=0.0,
            avg_active=0.0,
            max_active=0,
            full_capacity_time_s=0.0,
            concurrency_debt_s=0.0,
            occupancy=(),
            bottlenecks=(),
        )

    normalized = sorted(intervals, key=lambda interval: (interval.start_ns, interval.end_ns))
    start_ns = min(interval.start_ns for interval in normalized)
    end_ns = max(interval.end_ns for interval in normalized)
    wall_time_ns = max(end_ns - start_ns, 0)
    if wall_time_ns == 0:
        occupancy = ((len(normalized), 0.0),)
        bottlenecks = tuple(
            Bottleneck(
                nodeid=interval.nodeid,
                worker_id=interval.worker_id,
                wall_time_s=interval.wall_time_s,
                concurrency_debt_s=0.0,
            )
            for interval in normalized
        )
        return ConcurrencySummary(
            requested_slots=requested_slots,
            observed_workers=tuple(sorted({interval.worker_id for interval in normalized})),
            test_count=len(normalized),
            wall_time_s=0.0,
            avg_active=0.0,
            max_active=len(normalized),
            full_capacity_time_s=0.0,
            concurrency_debt_s=0.0,
            occupancy=occupancy,
            bottlenecks=bottlenecks,
        )

    boundaries: list[tuple[int, int, RecordedInterval]] = []
    for interval in normalized:
        if interval.end_ns < interval.start_ns:
            raise ValueError(f"interval ended before it started: {interval.nodeid}")
        boundaries.append((interval.start_ns, 1, interval))
        boundaries.append((interval.end_ns, 0, interval))
    boundaries.sort(key=lambda boundary: (boundary[0], boundary[1]))

    active: dict[str, RecordedInterval] = {}
    occupancy_ns: dict[int, int] = {}
    concurrency_debt_ns = 0
    debt_by_nodeid_ns: dict[str, int] = {}
    prev_ns = boundaries[0][0]
    max_active = 0

    for timestamp_ns, event_kind, interval in boundaries:
        if timestamp_ns > prev_ns:
            active_count = len(active)
            segment_ns = timestamp_ns - prev_ns
            occupancy_ns[active_count] = occupancy_ns.get(active_count, 0) + segment_ns
            shortfall = max(requested_slots - active_count, 0)
            if shortfall and active_count:
                debt_segment_ns = shortfall * segment_ns
                concurrency_debt_ns += debt_segment_ns
                debt_share_ns = debt_segment_ns / active_count
                for nodeid in active:
                    debt_by_nodeid_ns[nodeid] = debt_by_nodeid_ns.get(nodeid, 0) + int(
                        debt_share_ns
                    )
            elif shortfall:
                concurrency_debt_ns += shortfall * segment_ns
        if event_kind == 0:
            active.pop(interval.nodeid, None)
        else:
            active[interval.nodeid] = interval
            max_active = max(max_active, len(active))
        prev_ns = timestamp_ns

    avg_active = sum(count * duration for count, duration in occupancy_ns.items()) / wall_time_ns
    observed_workers = tuple(sorted({interval.worker_id for interval in normalized}))
    occupancy = tuple(
        (count, duration / 1_000_000_000)
        for count, duration in sorted(occupancy_ns.items(), reverse=True)
        if duration
    )
    intervals_by_nodeid = {interval.nodeid: interval for interval in normalized}
    bottlenecks = tuple(
        sorted(
            (
                Bottleneck(
                    nodeid=nodeid,
                    worker_id=intervals_by_nodeid[nodeid].worker_id,
                    wall_time_s=intervals_by_nodeid[nodeid].wall_time_s,
                    concurrency_debt_s=debt_ns / 1_000_000_000,
                )
                for nodeid, debt_ns in debt_by_nodeid_ns.items()
                if debt_ns > 0
            ),
            key=lambda bottleneck: (-bottleneck.concurrency_debt_s, -bottleneck.wall_time_s),
        )[:5]
    )
    full_capacity_time_ns = occupancy_ns.get(requested_slots, 0)
    return ConcurrencySummary(
        requested_slots=requested_slots,
        observed_workers=observed_workers,
        test_count=len(normalized),
        wall_time_s=wall_time_ns / 1_000_000_000,
        avg_active=avg_active,
        max_active=max_active,
        full_capacity_time_s=full_capacity_time_ns / 1_000_000_000,
        concurrency_debt_s=concurrency_debt_ns / 1_000_000_000,
        occupancy=occupancy,
        bottlenecks=bottlenecks,
    )


def format_summary(summary: ConcurrencySummary) -> list[str]:
    if summary.test_count == 0:
        return ["No test intervals were captured."]

    utilization_pct = 100.0 * summary.avg_active / summary.requested_slots
    full_capacity_pct = 100.0 * summary.full_capacity_time_s / summary.wall_time_s
    visible_bottlenecks = tuple(
        bottleneck for bottleneck in summary.bottlenecks if bottleneck.concurrency_debt_s >= 0.01
    )
    lines = [
        (
            "requested slots: "
            f"{summary.requested_slots} ({len(summary.observed_workers)} workers observed)"
        ),
        f"tests measured: {summary.test_count}",
        f"measured test wall time: {summary.wall_time_s:.2f}s",
        (
            "parallelism-limited savings upper bound: "
            f"{summary.estimated_savings_upper_bound_s:.2f}s"
        ),
        (
            "estimated runtime floor with perfect balancing: "
            f"{summary.estimated_balanced_runtime_floor_s:.2f}s"
        ),
        (
            "average active tests: "
            f"{summary.avg_active:.2f} / {summary.requested_slots} "
            f"({utilization_pct:.1f}% utilization)"
        ),
        f"max active tests: {summary.max_active}",
        (
            "time at full concurrency: "
            f"{summary.full_capacity_time_s:.2f}s ({full_capacity_pct:.1f}%)"
        ),
        f"concurrency debt: {summary.concurrency_debt_s:.2f} worker-seconds",
    ]
    lines.append("concurrency distribution:")
    for active_count, duration_s in summary.occupancy[: min(5, len(summary.occupancy))]:
        percentage = 100.0 * duration_s / summary.wall_time_s
        lines.append(f"  {active_count} active: {duration_s:.2f}s ({percentage:.1f}%)")
    if visible_bottlenecks:
        lines.append("top bottlenecks:")
        for bottleneck in visible_bottlenecks:
            suite_time_share_s = bottleneck.concurrency_debt_s / summary.requested_slots
            lines.append(
                "  "
                f"{bottleneck.concurrency_debt_s:.2f} worker-s  "
                f"{suite_time_share_s:.2f}s suite time  "
                f"{bottleneck.nodeid} [{bottleneck.worker_id}] "
                f"({bottleneck.wall_time_s:.2f}s)"
            )
    return lines


def load_intervals(report_dir: Path) -> list[RecordedInterval]:
    intervals: list[RecordedInterval] = []
    for path in sorted(report_dir.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            payload = json.loads(line)
            intervals.append(
                RecordedInterval(
                    nodeid=str(payload["nodeid"]),
                    worker_id=str(payload["worker_id"]),
                    start_ns=int(payload["start_ns"]),
                    end_ns=int(payload["end_ns"]),
                )
            )
    return intervals


class _ConcurrencyReporter:
    def __init__(self, config: pytest.Config) -> None:
        self._config = config
        self._is_worker = hasattr(config, "workerinput")
        self._requested_slots = _requested_slots(config)
        self._report_root = _report_root_for(config)
        run_id = getattr(config.option, "testrunuid", None)
        if not isinstance(run_id, str) or not run_id:
            run_id = os.environ.get("PYTEST_XDIST_TESTRUNUID") or "local"
        self._report_dir = self._report_root / run_id
        if not self._is_worker:
            shutil.rmtree(self._report_root, ignore_errors=True)
        self._report_dir.mkdir(parents=True, exist_ok=True)
        self._worker_id = "main"
        if hasattr(config, "workerinput"):
            worker_id = config.workerinput.get("workerid")
            if worker_id:
                self._worker_id = str(worker_id)
        self._file_path = self._report_dir / f"{self._worker_id}.jsonl"
        self._writer = self._file_path.open("a", encoding="utf-8")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(
        self,
        item: pytest.Item,
        nextitem: pytest.Item | None,
    ):
        start_ns = time.time_ns()
        try:
            yield
        finally:
            self._append_interval(
                nodeid=item.nodeid,
                worker_id=self._worker_id,
                start_ns=start_ns,
                end_ns=time.time_ns(),
            )

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        self._writer.flush()
        self._writer.close()
        if self._is_worker:
            return
        summary = analyze_intervals(
            load_intervals(self._report_root),
            requested_slots=self._requested_slots,
        )
        terminal_reporter_plugin = self._config.pluginmanager.getplugin("terminalreporter")
        if terminal_reporter_plugin is None:
            return
        terminal_reporter = terminal_reporter_plugin
        if not isinstance(terminal_reporter, TerminalReporter):
            return
        terminal_reporter.ensure_newline()
        terminal_reporter.section("Concurrency Report", sep="-", bold=True)
        for line in format_summary(summary):
            terminal_reporter.write_line(line)

    def _append_interval(
        self,
        *,
        nodeid: str,
        worker_id: str,
        start_ns: int,
        end_ns: int,
    ) -> None:
        payload = {
            "nodeid": nodeid,
            "worker_id": worker_id,
            "start_ns": start_ns,
            "end_ns": end_ns,
        }
        self._writer.write(json.dumps(payload))
        self._writer.write("\n")
        self._writer.flush()


def _requested_slots(config: pytest.Config) -> int:
    if hasattr(config, "workerinput"):
        worker_count = config.workerinput.get("workercount")
        if isinstance(worker_count, int) and worker_count > 0:
            return worker_count
    numprocesses = getattr(config.option, "numprocesses", None)
    if isinstance(numprocesses, int) and numprocesses > 0:
        return numprocesses
    env_value = os.environ.get("PYTEST_XDIST_WORKER_COUNT")
    if env_value is not None:
        try:
            parsed = int(env_value)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return 1




def _report_root_for(config: pytest.Config) -> Path:
    return Path(config.rootpath) / ".pytest_cache" / _REPORT_DIR_NAME
