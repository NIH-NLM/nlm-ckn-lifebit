# nlm-ckn-lifebit

NLM-CKN tooling for working with sc-nsforest-qc-nf pipeline outputs on the
Lifebit platform through [cloudos-cli](https://github.com/lifebit-ai/cloudos-cli).

The current workflow finds every filtered h5ad file
(`adata_filtered_*.h5ad`) produced by the pipeline, picks one copy per
dataset, and links them into a single project folder,
**Data → filtered_h5ad**, so they can be found and downloaded in the web
interface. Links are platform records that point at the original S3 objects;
no data is copied.

All access goes through the Lifebit API with your personal API key. No AWS
credentials are needed.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.9.

```bash
git clone git@github.com:NIH-NLM/nlm-ckn-lifebit.git
cd nlm-ckn-lifebit
make setup                    # or: uv sync
uv run cloudos configure      # creates the "default" profile
```

`cloudos configure` stores settings in `~/.cloudos/config` and your API key in
`~/.cloudos/credentials` (outside the repository). Restrict the key file with
`chmod 600 ~/.cloudos/credentials`.

| Prompt | Value |
|---|---|
| API key | Lifebit web UI → your account settings → API key |
| Platform URL | `https://cloudos.lifebit.ai` — **include `https://`**, or every command fails with "No scheme supplied" |
| Workspace ID | Ask a teammate, or copy it from the workspace settings |
| Project name | `nlm-ckn-sc-nsforest-qc-nf production run` — must match exactly, spaces included |
| Execution platform | `aws` |
| Repository platform | `github` |

The `cell-kn sc-nsforest-qc datasets` project is superseded; don't use it.

Check the connection:

```bash
uv run cloudos project list
```

To use a profile other than `default`, add `PROFILE=<name>` to any `make`
command.

## Workflow

```bash
make collect   # scan job folders; writes out/filtered_h5ad_locations.csv
make preview   # choose one copy per dataset; writes selection + restore list
make link      # preview, then link the selection into Data/filtered_h5ad
make recheck   # re-check archive state only (fast; e.g. after a restore)
```

| Step | Script | Output (in `out/`, not committed) |
|---|---|---|
| collect | `scripts/collect_filtered_h5ad.py` | `filtered_h5ad_locations.csv`: every copy, with job, size, platform path, S3 path and `access_state` |
| preview / link | `scripts/link_filtered_h5ad.py` | `filtered_h5ad_selected.csv`: one copy per dataset; `filtered_h5ad_restore_list.txt`: selected files that are archived |

**Selection rules.** Only copies from *completed* jobs are used (failed and
aborted runs, including archived jobs, are skipped). For each dataset a
readable copy is preferred over an archived one, then the most recent
submission. File names that differ only by a float year (`_2026.0_` vs
`_2026_`) are treated as the same dataset.

**Linking** is safe to repeat: files already in the destination folder are
skipped. If a newer run produces a file with the *same name* as an existing
link, delete the old link in the web UI first, and test with one file before
deleting in bulk.

## Downloads fail silently: archived files

Older job outputs are moved by S3 Intelligent-Tiering to the `ARCHIVE_ACCESS`
tier. The web UI then shows a download as *Failed* with no explanation: the
platform issues a signed URL, but S3 answers `403 InvalidObjectState`. This
is not a permissions problem, and it also affects reading these files from
interactive sessions.

`collect` records each file's state (`readable` or `archived (…)`). To get
archived files back:

1. Run `make preview` and send `out/filtered_h5ad_restore_list.txt` to Lifebit
   support (or the workspace admin), asking them to restore those objects.
   Restores typically take hours.
2. Run `make recheck && make preview` until the restore list is empty.
3. Download soon afterwards; restored objects return to the archive tier
   after a while.

## Notes

- `cloudos-cli` is pinned to 2.95.1 in `pyproject.toml`; `uv.lock` pins the
  rest. Upgrade deliberately and re-test `make preview`.
- The archive check uses the platform endpoint
  `/api/v1/data-access/s3/signed-object-url`, which is not part of
  `cloudos-cli` and could change.
- `cloudos-cli` cannot download files, so the web UI is the only download
  route.
