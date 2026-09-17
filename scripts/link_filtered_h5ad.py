#!/usr/bin/env python3
"""Link one filtered h5ad file per dataset into a single Lifebit Data folder.

Input: filtered_h5ad_locations.csv from scripts/collect_filtered_h5ad.py.
Job statuses are fetched with `cloudos job list` (active and archived jobs).

For each dataset, a copy from a *completed* job is selected, preferring copies
S3 can serve now (access_state "readable") and then the most recent submission.
Archived copies are still linked: the links work once Lifebit restores them.
`cloudos datasets cp` of an S3 file only creates a platform file record pointing
at the same S3 object, so no data is duplicated.

Outputs (in --dir):
  - filtered_h5ad_selected.csv      the selected copy per dataset
  - filtered_h5ad_restore_list.txt  S3 paths of selected files that are not readable

Dry run by default; pass --apply to create the folder and links.

Usage:
    python scripts/link_filtered_h5ad.py [--dir out] [--dest Data/filtered_h5ad] [--apply]
"""

import argparse
import configparser
import csv
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CLOUDOS = str(Path(sys.executable).with_name("cloudos"))


def cloudos(args, *cmd):
    """Run a cloudos command with the selected profile and project."""
    full = [CLOUDOS, *cmd, "--profile", args.profile]
    if cmd[0] == "datasets":
        full += ["--project-name", args.project_name]
    return subprocess.run(full, capture_output=True, text=True)


def fetch_jobs(args):
    """Return {job_id: row} for all active and archived jobs in the project."""
    jobs = {}
    with tempfile.TemporaryDirectory() as tmp:
        for archived in (False, True):
            base = Path(tmp) / f"jobs_{archived}"
            cmd = ["job", "list", "--filter-project", args.project_name, "--last-n-jobs", "all",
                   "--output-format", "csv", "--output-basename", str(base)]
            if archived:
                cmd.append("--archived")
            proc = cloudos(args, *cmd)
            out = base.with_suffix(".csv")
            if proc.returncode != 0 or not out.exists():
                sys.exit(f"job list failed: {proc.stdout}{proc.stderr}")
            with open(out, newline="") as f:
                for row in csv.DictReader(f):
                    jobs[row["ID"]] = row
    return jobs


def dataset_key(file_name):
    """Older runs wrote years as floats (e.g. _2026.0_); treat them as the same dataset."""
    return re.sub(r"_(\d{4})\.0_", r"_\1_", file_name)


def rank(row):
    """Prefer copies S3 can serve now (not archived), then the latest submission."""
    return (row.get("access_state") == "readable", row["submit_time"])


def select(locations, jobs):
    """Return {file_name: row} choosing the best completed-job copy per dataset."""
    selected = {}
    for row in locations:
        job = jobs.get(row["job_id"])
        if not job or job["Status"] != "completed":
            continue
        row = {**row, "job_status": job["Status"], "submit_time": job["Submit time"]}
        key = dataset_key(row["file_name"])
        current = selected.get(key)
        if current is None or rank(row) > rank(current):
            selected[key] = row
    return {row["file_name"]: row for row in selected.values()}


def list_names(args, path):
    """Names in a Data folder, or None if it does not exist."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "ls"
        proc = cloudos(args, "datasets", "ls", path, "--details",
                       "--output-format", "csv", "--output-basename", str(base))
        out = base.with_suffix(".csv")
        if proc.returncode != 0 or not out.exists():
            return None
        with open(out, newline="") as f:
            return {r["Virtual Name"] for r in csv.DictReader(f)}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=Path("out"),
                        help="Directory holding filtered_h5ad_locations.csv; outputs go here too")
    parser.add_argument("--dest", default="Data/filtered_h5ad",
                        help="Destination virtual folder (must start with Data/)")
    parser.add_argument("--profile", default="default", help="cloudos profile")
    parser.add_argument("--project-name", help="Lifebit project (default: profile's project)")
    parser.add_argument("--apply", action="store_true", help="Create folder and links")
    args = parser.parse_args()
    if not args.project_name:
        config = configparser.ConfigParser()
        config.read(Path.home() / ".cloudos" / "config")
        args.project_name = config[args.profile]["project_name"]

    with open(args.dir / "filtered_h5ad_locations.csv", newline="") as f:
        locations = list(csv.DictReader(f))
    selected = select(locations, fetch_jobs(args))
    if not selected:
        sys.exit("No files from completed jobs found.")

    selection_csv = args.dir / "filtered_h5ad_selected.csv"
    with open(selection_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(next(iter(selected.values())).keys()))
        writer.writeheader()
        writer.writerows(sorted(selected.values(), key=lambda r: r["file_name"]))
    total_gb = sum(int(r["size_bytes"] or 0) for r in selected.values()) / 1e9
    print(f"Selected {len(selected)} files ({total_gb:.1f} GB referenced) -> {selection_csv}")
    not_readable = [r for r in selected.values() if r.get("access_state") != "readable"]
    restore_list = args.dir / "filtered_h5ad_restore_list.txt"
    restore_list.write_text("".join(sorted(r["storage_path"] + "\n" for r in not_readable)))
    print(f"  {len(not_readable)} selected files are not currently readable -> {restore_list}")

    if not args.apply:
        print(f"Dry run: would link them into '{args.dest}'. Re-run with --apply.")
        return

    existing = list_names(args, args.dest)
    if existing is None:
        proc = cloudos(args, "datasets", "mkdir", args.dest)
        if proc.returncode != 0:
            sys.exit(f"mkdir failed: {proc.stdout}{proc.stderr}")
        print(f"Created {args.dest}")
        existing = set()

    failures = 0
    for i, (name, row) in enumerate(sorted(selected.items()), 1):
        if name in existing:
            print(f"[{i}/{len(selected)}] skip (exists) {name}")
            continue
        proc = cloudos(args, "datasets", "cp", row["virtual_path"], args.dest)
        ok = proc.returncode == 0 and "copied successfully" in proc.stdout
        print(f"[{i}/{len(selected)}] {'ok' if ok else 'FAILED'} {name}")
        if not ok:
            failures += 1
            print((proc.stdout + proc.stderr).strip()[-500:], file=sys.stderr)
    print(f"Done: {len(selected) - failures} linked/present, {failures} failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
