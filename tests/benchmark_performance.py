"""Synthetic, read-only benchmarks: python -m tests.benchmark_performance.

No scanned files are opened or recycled. Times are medians, not test assertions.
"""

import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from duplicate_cleaner.cleanup import recycle_selected
from duplicate_cleaner.gui import MainWindow
from duplicate_cleaner.models import DuplicateGroup, FileRecord
from duplicate_cleaner.sessions import _record_json


def measure(job, repetitions=5):
    samples = []
    for _ in range(repetitions):
        started = perf_counter()
        job()
        samples.append(perf_counter() - started)
    return round(median(samples), 6)


def main():
    root = Path(__file__).resolve().parents[1] / ".verification"
    root.mkdir(exist_ok=True)
    records = [FileRecord(root / f"synthetic-{index}.txt", 1024, 1_700_000_000_000_000_000,
                          1_700_000_000_000_000_000, 1, index + 1) for index in range(6000)]
    groups = [DuplicateGroup(tuple(records[index:index + 3]), "a" * 64)
              for index in range(0, len(records), 3)]
    selected = frozenset(record.path for record in records)
    results = {"records": len(records)}
    results["serialize_records_s"] = measure(lambda: [_record_json(record, selected) for record in records])
    with patch("duplicate_cleaner.cleanup.open_checked", side_effect=PermissionError("Synthetic failure")):
        def failed_cleanup():
            result = recycle_selected(groups, selected)
            assert len(result.issues) == len(records) and not result.recycled
        results["report_failed_cleanup_s"] = measure(failed_cleanup, 3)

    app = QApplication.instance() or QApplication([])
    with patch("duplicate_cleaner.gui.QSettings", side_effect=lambda *args: QSettings(
            str(root / "benchmark-preferences.ini"), QSettings.Format.IniFormat)):
        window = MainWindow()
    try:
        results["populate_results_s"] = measure(lambda: window.set_groups(groups), 3)
        for column in (2, 1):
            window.tree.sortByColumn(column, Qt.SortOrder.AscendingOrder)
            results[f"filter_sorted_column_{column}_s"] = measure(window.filter_results)
        assert len(window.records) == len(records) and not window.selected
    finally:
        window.close()
        app.processEvents()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
