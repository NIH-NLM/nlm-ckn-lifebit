# Filtered h5ad workflow for sc-nsforest-qc-nf outputs on Lifebit.
# Override the profile with: make <target> PROFILE=myprofile

PROFILE ?= default
RUN = uv run python

.PHONY: help setup collect recheck preview link

help:
	@echo "make setup    Install the environment (then run: uv run cloudos configure)"
	@echo "make collect  Scan job folders for filtered h5ad files and check archive state"
	@echo "make recheck  Re-check archive state only (e.g. after a Lifebit restore)"
	@echo "make preview  Select one copy per dataset; write selection and restore list"
	@echo "make link     Same as preview, then link files into Data/filtered_h5ad"

setup:
	uv sync

collect:
	$(RUN) scripts/collect_filtered_h5ad.py --profile $(PROFILE)

recheck:
	$(RUN) scripts/collect_filtered_h5ad.py --profile $(PROFILE) --recheck

preview:
	$(RUN) scripts/link_filtered_h5ad.py --profile $(PROFILE)

link:
	$(RUN) scripts/link_filtered_h5ad.py --profile $(PROFILE) --apply
