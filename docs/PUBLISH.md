# Publication and contribution

The destination is `cagyirey/gyms-of-qud`, under the personal account, not `geodesa-ai`. The repository was created by its owner as public. Its visibility is not changed by the publishing workflow.

The initial handoff archive used the provisional repository name QudGym. The source package remains `qudgym`, while repository references now use `gyms-of-qud`. Its original local foundation commit was `749b736`; publication uses the new remote bootstrap as its parent rather than rewriting main with the archive's unrelated Git history.

## Existing-repository workflow

Clone the repository, switch to a feature branch, review and commit changes, and authenticate the GitHub CLI as `cagyirey`. Then:

```bash
bash scripts/publish-github.sh
```

The helper checks the origin, login, branch and clean worktree, performs an ordinary non-force feature-branch push, and opens an unmerged PR if one is not already open. It never creates a repository, changes visibility, replaces origin, pushes main, or merges.

For manual publication:

```bash
git remote -v
git push --set-upstream origin feat/qudgym-foundation
gh pr create --repo cagyirey/gyms-of-qud --base main --head feat/qudgym-foundation \
  --title 'QudGym foundation' --body-file docs/PR.md
```

Inspect the remote and existing PR after an ambiguous network failure rather than force-pushing or creating duplicate PRs. Never commit game binaries/assets, raw saves, credentials, or private diagnostic output. Keep install reports in ignored `local/` and share only reviewed metadata.
