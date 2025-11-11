#!/bin/bash
# Remove and reinstall dependencies (after updates to them)

# Remove the dependency. This will change pyproject.toml and poetry.lock
poetry remove campus-api-python

# Re-add the dependency and update poetry.lock
git checkout -- pyproject.toml
poetry lock

# Reinstall dependencies
poetry install
