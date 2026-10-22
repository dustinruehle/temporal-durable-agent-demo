#!/usr/bin/env bash
#
# Generic and public-safe gate. A word-bounded, case-insensitive grep
# over the tracked working tree AND the commit log. Fails the build on any hit.
#
# The forbidden term list is supplied out of band in a gitignored
# .check-generic-terms. If that file is missing the gate fails loudly rather than
# passing vacuously.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TERMS_FILE="$REPO_ROOT/.check-generic-terms"
if [ ! -f "$TERMS_FILE" ]; then
  echo "ERROR: .check-generic-terms not found."
  echo "It is supplied out of band by a human and is gitignored on purpose."
  echo "Restore it before running the generic gate."
  exit 2
fi

fail=0

while IFS= read -r raw; do
  # Skip comments and blank lines.
  case "$raw" in
    \#*|"") continue ;;
  esac
  # Trim trailing whitespace.
  term="$(printf '%s' "$raw" | sed 's/[[:space:]]*$//')"
  [ -z "$term" ] && continue

  # Tracked working tree. git grep respects .gitignore, so .check-generic-terms
  # itself is never scanned. -w is word-bounded, -i case-insensitive. LICENSE is
  # skipped: its copyright line names the author on purpose.
  if git grep -I -i -w -n -e "$term" -- . ':(exclude)LICENSE' >/tmp/_cg_tree 2>/dev/null; then
    echo "FORBIDDEN TERM in tracked files: '$term'"
    sed 's/^/  /' /tmp/_cg_tree
    fail=1
  fi

  # Commit log (all branches).
  if git log --all --pretty=format:'%H %s%n%b' 2>/dev/null | grep -I -i -w -n -e "$term" >/tmp/_cg_log 2>/dev/null; then
    echo "FORBIDDEN TERM in commit log: '$term'"
    sed 's/^/  /' /tmp/_cg_log
    fail=1
  fi
done < "$TERMS_FILE"

rm -f /tmp/_cg_tree /tmp/_cg_log

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "Generic gate FAILED. Remove the terms above from the tree and history."
  exit 1
fi

echo "Generic gate passed: no forbidden terms in the tracked tree or commit log."
