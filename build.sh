#!/bin/bash
# Builds "dist/FM TCP Bridge.app". Run from this folder:  ./build.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet rumps py2app

rm -rf build dist
.venv/bin/python setup.py py2app

# ad-hoc sign so macOS treats it as one stable app (firewall prompt, Login Items)
codesign --force --deep --sign - "dist/FM TCP Bridge.app"

echo
echo "Built: $(pwd)/dist/FM TCP Bridge.app"
