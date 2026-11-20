# G. Git report

| | |
|---|---|
| Repository | `https://github.com/The-Outsider-97/documaster` (read-only clone; **nothing was pushed**) |
| Base | `main` @ `7d41eed` ("Add files via upload") |
| Branch | `audit/documaster-production-readiness` (local) |
| Commits | 4 (3 code + this documentation commit) |
| Diff vs base (before docs) | 46 files, +1870 / −101 (7 of them rebuilt `dist/` output) |
| Force-push / history rewrite | none |
| Remote state | untouched. `main` on GitHub was ahead of the device clone (169d82d) and of an older copy; the audit started from GitHub `main` |

## Commits
```
e6fe941 feat(startup): run_documaster.py with --check/--build/--dev; real CI; repo hygiene
3eef98c feat(client,build): layout-only routes in the converter, accessible document picker, build record, e2e and tools
2534d0a fix(core): DOC/RTF reading, merge versioning, input and archive hardening; add XLSX/PPTX/ODP to PDF
7d41eed Add files via upload                      <- base
```
Each commit message lists what changed and why. The documentation commit follows (`docs/audit/`).

## Notes for the reviewer
- **Line endings.** The base has files with CRLF (rewritten by "Add files via upload" commits) and files with LF. Every edit preserved the file's existing ending so the diff shows only real changes; `convert.py` (restored from 25da986) was converted back to CRLF for the same reason (its diff is +50/−4, not a whole-file rewrite). No renormalisation was done (O-007).
- **`dist/`** is committed output. It was rebuilt with the global `tsc` and `node tools/build-static.mjs` (no registry access); the committed `dist/build.json` is checked against the sources by `test_committed_build_matches_its_sources`.
- **Deleted:** `.github/workflows/webpack.yml` (a Node/Webpack template; the project has no webpack). Replaced by `.github/workflows/ci.yml`. `writer-vector.yml` is unchanged.
- **Regression that predates this work:** an "Add files via upload" commit after 25da986 reverted `convert.py` features (D-002). Prefer reviewed commits over web uploads for this repo.
- **Not authorised, not done:** pushing the branch, opening a pull request, tagging a release. To publish: `git push -u origin audit/documaster-production-readiness` then open a PR against `main` (this needs write access that this session does not have).

## Delivered artefacts and how to use them
- `documaster-audit.bundle` — the four commits on top of `7d41eed`. In a clone of the repo: `git fetch documaster-audit.bundle audit/documaster-production-readiness:audit/documaster-production-readiness` (verified: fetches to the same commit).
- `documaster-audit.patch` — the same commits as one mailbox. Apply with **`git am --keep-cr documaster-audit.patch`** on `7d41eed` (the repo has CRLF files; without `--keep-cr` git strips the CRs and the patch fails). Verified on a fresh clone: the resulting tree is byte-identical to the branch.
