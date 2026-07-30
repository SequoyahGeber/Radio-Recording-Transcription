#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v node >/dev/null 2>&1; then
    echo "Browser tests require Node.js."
    exit 1
fi

if [ ! -x "./node_modules/.bin/playwright" ]; then
    echo "Browser test dependencies are missing. Run: npm install"
    exit 1
fi

npm run test:browser
