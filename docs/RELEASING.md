# 🚀 Releasing

One command does the whole checklist:

```bash
scripts/release.sh v0.9.1 --prerelease        # drop --prerelease once out of beta
```

What it does, in order:

1. **Checks**: you are on `main`, the tree is clean and in sync with GitHub, CI is green on that commit, the tag is free.
2. **Notes**: the top section of `CHANGELOG.md` becomes the release notes; its heading is renamed to the version and today's date. So: write the changelog first, release second.
3. **Baselines**: the screenshots from that green CI run become the visual-diff baselines in `uibot/baseline/`, so the next CI run compares against what you just shipped.
4. **Commit, tag, push**: a `release vX.Y.Z` commit, an annotated tag, both pushed. CI builds `ghcr.io/chrizantoz/auditarr:X.Y.Z`, `X.Y` and `latest` from that commit, and stamps the image with the version (the About tab shows it).
5. **GitHub release** with the notes.
6. **Boot check**: waits for the tag's CI run, then starts the tagged image once (locally with Docker, or over ssh with `RELEASE_VERIFY_SSH=user@host`) and checks it reports the version.

Flags: `--dry-run` shows what would change and stops before committing; `--no-baselines` keeps the current screenshots.

Identity: the release commit uses your git config, or `RELEASE_GIT_NAME` / `RELEASE_GIT_EMAIL` if set.

Afterwards, promote the new image where you run it (`docs/auditarr-deploy.sh update` stages it first and only swaps if the staged run looks sane).

Version numbers: `vMAJOR.MINOR.PATCH`. Patch for fixes, minor for features, major when something changes that needs a note before upgrading. Beta releases are marked pre-release.
