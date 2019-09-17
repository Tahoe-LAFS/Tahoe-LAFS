#!/usr/bin/env bash

set -x
set -eo pipefail

if [ "${CIRCLE_BRANCH}" != "master" ]; then
    echo "Declining to update dependency graph for non-master build."
    exit 0
fi

TAHOE="${PWD}"
git clone -b gh-pages git@github.com:tahoe-lafs/tahoe-depgraph.git
cd tahoe-depgraph

# Generate the maybe-changed data.
python tahoe-depgraph.py "${TAHOE}"

if git diff-index --quiet HEAD; then
  echo "Declining to commit without any changes."
  exit 0
fi

git config user.name 'Build Automation'
git config user.email 'tahoe-dev@tahoe-lafs.org'

git add tahoe-deps.json tahoe-ported.json.
git commit -m "\
Built from ${CIRCLE_REPOSITORY_URL}@${CIRCLE_SHA1}

tahoe-depgraph was $(git rev-parse HEAD)
"

# Publish it on GitHub.
git push -q origin gh-pages
