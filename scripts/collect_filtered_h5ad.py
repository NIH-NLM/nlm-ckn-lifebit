#!/usr/bin/env python3
"""Collect storage locations of filtered h5ad files produced by sc-nsforest-qc-nf.

Walks "Analyses Results/<job name>-<job id>/results" in a Lifebit project using
`cloudos datasets ls` (platform API only, no direct AWS access) and writes a CSV
of every file matching adata_filtered_*.h5ad.

Each file's access state is also checked: the platform issues a signed URL and a
1-byte read shows whether S3 serves the object ("readable") or refuses it
because S3 Intelligent-Tiering has moved it to an archive tier ("archived").
Archived objects must be restored by Lifebit before they can be downloaded or
read in the platform.

Usage:
    python scripts/collect_filtered_h5ad.py [--project-name NAME] [--output FILE]
                                            [--restore-list FILE]
    # Re-check access state of an existing CSV (e.g. after a restore request):
    python scripts/collect_filtered_h5ad.py --recheck --output FILE [--restore-list FILE]
"""

import argparse
import base64
import configparser
import csv
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

CLOUDOS = str(Path(sys.executable).with_name("cloudos"))
ANALYSES = "Analyses Results"
PATTERN = re.compile(r"^adata_filtered_.*\.h5ad$")
JOB_FOLDER = re.compile(r"^(?P<name>.+)-(?P<job_id>[0-9a-f]{24})$")
FIELDS = ["job_name", "job_id", "file_name", "size", "size_bytes", "last_updated",
          "virtual_path", "storage_path", "access_state"]


def ls(path, project_name, profile):
    """Return `cloudos datasets ls --details` rows for path, or None on error."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "ls"
        cmd = [CLOUDOS, "datasets", "ls", path, "--details",
               "--output-format", "csv", "--output-basename", str(base), "--profile", profile]
        if project_name:
            cmd += ["--project-name", project_name]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        out = base.with_suffix(".csv")
        if proc.returncode != 0 or not out.exists():
            print(f"  warning: could not list '{path}'", file=sys.stderr)
            return None
        with open(out, newline="") as f:
            return list(csv.DictReader(f))


def is_folder(row):
    return "folder" in row["Type"]


def walk(path, project_name, profile, depth):
    """Yield (path, row) for all files under path, descending up to depth levels."""
    for row in ls(path, project_name, profile) or []:
        child = f"{path}/{row['Virtual Name']}"
        if is_folder(row):
            if depth > 0:
                yield from walk(child, project_name, profile, depth - 1)
        else:
            yield child, row


class AccessChecker:
    """Check whether S3 will serve an object, via the platform's signed-URL API."""

    def __init__(self, profile):
        config = configparser.ConfigParser()
        config.read(Path.home() / ".cloudos" / "config")
        credentials = configparser.ConfigParser()
        credentials.read(Path.home() / ".cloudos" / "credentials")
        self.url = config[profile]["cloudos_url"].rstrip("/")
        self.workspace_id = config[profile]["workspace_id"]
        self.session = requests.Session()
        self.session.headers.update({"ApiKey": credentials[profile]["apikey"],
                                     "accept": "application/json"})

    def check(self, storage_path):
        """Return "readable", "archived (<tier>)", or "unknown (<reason>)"."""
        m = re.match(r"^s3://([^/]+)/(.+)$", storage_path)
        if not m:
            return "unknown (not an S3 path)"
        bucket, key = m.groups()
        try:
            r = self.session.get(
                f"{self.url}/api/v1/data-access/s3/signed-object-url",
                params={"bucket": bucket, "teamId": self.workspace_id,
                        "key": base64.b64encode(key.encode()).decode()},
                timeout=60)
            if not r.ok:
                return f"unknown (signed-url HTTP {r.status_code})"
            s3 = requests.get(r.json()["signedUrl"], headers={"Range": "bytes=0-0"}, timeout=60)
        except (requests.RequestException, ValueError, KeyError) as e:
            return f"unknown ({type(e).__name__})"
        if s3.ok:
            return "readable"
        code = re.search(r"<Code>(.*?)</Code>", s3.text)
        tier = re.search(r"<AccessTier>(.*?)</AccessTier>", s3.text)
        if code and code[1] == "InvalidObjectState":
            return f"archived ({tier[1] if tier else 'unknown tier'})"
        return f"unknown (S3 HTTP {s3.status_code}{' ' + code[1] if code else ''})"


def scan(args, checker):
    # Analyses Results also holds interactive-session folders; keep job outputs only.
    folders = [r["Virtual Name"] for r in ls(ANALYSES, args.project_name, args.profile) or []
               if is_folder(r) and "/jobs/" in r["Storage Path"]]
    print(f"Scanning {len(folders)} job folders...", file=sys.stderr)
    rows = []
    for i, folder in enumerate(folders, 1):
        m = JOB_FOLDER.match(folder)
        job_name, job_id = (m["name"], m["job_id"]) if m else (folder, "")
        print(f"[{i}/{len(folders)}] {folder}", file=sys.stderr)
        for path, row in walk(f"{ANALYSES}/{folder}", args.project_name, args.profile, args.max_depth):
            if not PATTERN.match(row["Virtual Name"]):
                continue
            rows.append({
                "job_name": job_name,
                "job_id": job_id,
                "file_name": row["Virtual Name"],
                "size": row["Size"],
                "size_bytes": row["Size (bytes)"],
                "last_updated": row["Last Updated"],
                "virtual_path": path,
                "storage_path": row["Storage Path"],
                "access_state": checker.check(row["Storage Path"]),
            })
    return rows


def recheck(args, checker):
    with open(args.output, newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"Re-checking access state of {len(rows)} files...", file=sys.stderr)
    for row in rows:
        row["access_state"] = checker.check(row["storage_path"])
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-name", help="Lifebit project (default: profile's project)")
    parser.add_argument("--profile", default="default", help="cloudos profile")
    parser.add_argument("--output", default="out/filtered_h5ad_locations.csv")
    parser.add_argument("--max-depth", type=int, default=3,
                        help="Subfolder depth to search within each job folder")
    parser.add_argument("--recheck", action="store_true",
                        help="Only re-check access state of the rows already in --output")
    parser.add_argument("--restore-list",
                        help="Also write S3 paths of archived files (one per line) to this file")
    args = parser.parse_args()

    checker = AccessChecker(args.profile)
    rows = recheck(args, checker) if args.recheck else scan(args, checker)

    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    archived = [r for r in rows if r["access_state"].startswith("archived")]
    unknown = [r for r in rows if r["access_state"].startswith("unknown")]
    print(f"Found {len(rows)} filtered h5ad files -> {args.output}: "
          f"{len(rows) - len(archived) - len(unknown)} readable, "
          f"{len(archived)} archived, {len(unknown)} unknown", file=sys.stderr)
    if args.restore_list:
        paths = sorted({r["storage_path"] for r in archived})
        Path(args.restore_list).write_text("".join(p + "\n" for p in paths))
        print(f"Wrote {len(paths)} archived S3 paths -> {args.restore_list}", file=sys.stderr)


if __name__ == "__main__":
    main()
