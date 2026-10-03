#!/usr/bin/env python3
"""Guard tests for the CPI-U staleness check in build_db.py.

    python test_build_db.py

Uses sample_ntd.csv, so no DOT fetch is needed. Report years are stamped onto the sample rows
relative to the newest year in CPI_U, so these stay correct when a new year is added.
"""
import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

import build_db

HERE = Path(__file__).resolve().parent
SAMPLE = HERE / "sample_ntd.csv"
LATEST_CPI = max(build_db.CPI_U)


def write_sample(path, years):
    """sample_ntd.csv repeated once per report year, with a Report Year column added."""
    rows = list(csv.reader(SAMPLE.read_text(encoding="utf-8").splitlines()))
    header, body = rows[0], rows[1:]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header + ["Report Year"])
        for year in years:
            w.writerows(row + [year] for row in body)


def run_build(tmp, years, *extra):
    csv_path, db_path = Path(tmp) / "ntd.csv", Path(tmp) / "ntd.duckdb"
    write_sample(csv_path, years)
    done = subprocess.run(
        [sys.executable, str(HERE / "build_db.py"), "--file", str(csv_path), "--db", str(db_path),
         *extra],
        capture_output=True, text=True, cwd=str(HERE))
    return done, db_path


def read_meta(db_path):
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute("SELECT cpi_base_year, latest_report_year, latest_cpi_year, cpi_stale "
                          "FROM build_meta").fetchone()
    finally:
        con.close()
    return dict(zip(("cpi_base_year", "latest_report_year", "latest_cpi_year", "cpi_stale"), row))


class StaleCpiGuard(unittest.TestCase):
    def test_current_cpi_builds_quietly(self):
        """CPI_U covers the newest report year: build succeeds, nothing shouts."""
        with tempfile.TemporaryDirectory() as tmp:
            done, db_path = run_build(tmp, [LATEST_CPI - 1, LATEST_CPI])
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertNotIn("stale", (done.stdout + done.stderr).lower())
            self.assertEqual(read_meta(db_path), {
                "cpi_base_year": LATEST_CPI, "latest_report_year": LATEST_CPI,
                "latest_cpi_year": LATEST_CPI, "cpi_stale": False})

    def test_stale_cpi_fails_the_build(self):
        """CPI_U behind the data: non-zero exit, both years named, no database written."""
        with tempfile.TemporaryDirectory() as tmp:
            done, db_path = run_build(tmp, [LATEST_CPI, LATEST_CPI + 2])
            self.assertNotEqual(done.returncode, 0, "stale CPI_U must fail the build")
            self.assertIn(str(LATEST_CPI + 2), done.stderr)      # newest report year in the data
            self.assertIn(str(LATEST_CPI), done.stderr)          # newest year CPI_U covers
            self.assertIn("CPI_U", done.stderr)                  # what to edit
            self.assertIn("build_db.py", done.stderr)            # and where
            self.assertFalse(db_path.exists(), "failed build must not write a database")

    def test_stale_cpi_can_be_forced(self):
        """--allow-stale-cpi builds anyway, warns loudly, and records the staleness."""
        with tempfile.TemporaryDirectory() as tmp:
            done, db_path = run_build(tmp, [LATEST_CPI, LATEST_CPI + 2], "--allow-stale-cpi")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("--allow-stale-cpi", done.stdout)
            self.assertIn("stale", done.stdout.lower())
            self.assertEqual(read_meta(db_path), {
                "cpi_base_year": LATEST_CPI, "latest_report_year": LATEST_CPI + 2,
                "latest_cpi_year": LATEST_CPI, "cpi_stale": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
