#!/usr/bin/env bash
# Publish a feature branch to the EXISTING personal repo; never merge or push main.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
command -v gh >/dev/null || { echo 'Install the GitHub CLI and authenticate with gh auth login.' >&2; exit 1; }
[[ "$(gh api user --jq .login)" == 'cagyirey' ]] || { echo 'Authenticate as cagyirey before publishing.' >&2; exit 1; }
[[ "$(git rev-parse --show-toplevel)" == "$root" ]] || { echo 'Expected the repository root.' >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo 'Review and commit local changes before publishing.' >&2; exit 1; }
repo='cagyirey/gyms-of-qud'
branch="$(git symbolic-ref --quiet --short HEAD)"
case "$branch" in
  ''|main|master) echo 'Check out a feature branch before publishing.' >&2; exit 1 ;;
esac
origin="$(git remote get-url origin)"
case "$origin" in
  "https://github.com/$repo"|"https://github.com/$repo.git"|"git@github.com:$repo.git") ;;
  *) echo "Origin must point to $repo; refusing to replace it." >&2; exit 1 ;;
esac
git push --set-upstream origin "$branch"
existing="$(gh pr list --repo "$repo" --head "$branch" --base main --state open --json url --jq '.[0].url // empty')"
if [[ -n "$existing" ]]; then
  printf '%s\n' "$existing"
else
  gh pr create --repo "$repo" --base main --head "$branch" \
    --title 'QudGym foundation: decision protocol, mock environment, NeMo adapter and bridge seam' \
    --body-file docs/PR.md
fi
