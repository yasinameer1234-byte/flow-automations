#!/usr/bin/env bash
# Cheap local guard: fail if anything that looks like a credential is staged.
# Install as a hook: ln -s ../../scripts/check_no_secrets.sh .git/hooks/pre-commit
set -euo pipefail
pattern='(sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[A-Za-z0-9-]{10,}|ghp_[A-Za-z0-9]{30,})'
files=$(git diff --cached --name-only --diff-filter=ACM || true)
[ -z "$files" ] && exit 0
if echo "$files" | grep -E '(^|/)\.env$' >/dev/null; then
  echo "refusing to commit a .env file"; exit 1
fi
if git diff --cached -U0 | grep -E "^\+" | grep -Eq "$pattern"; then
  echo "possible secret in staged changes; commit blocked"; exit 1
fi
