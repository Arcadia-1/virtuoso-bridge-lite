# Fork Monitoring Procedure

How to periodically check the `virtuoso-bridge-lite` fork network for work worth
pulling back, and how to decide what (if anything) to take. This is a triage
procedure, not an auto-merge — every candidate is judged by a human.

Upstream (`Arcadia-1/virtuoso-bridge-lite`) is **actively maintained**. The single
most important rule when reading any fork: **check whether the change is already
upstream before taking it.** In the 2026-07-01 sweep, essentially every broadly
useful change in the network had already been absorbed upstream (digital_import
env-vars, Windows-tmp portability, SRAM auto-detect, the SKILL `q` helper,
version-aware daemon selection, ADE Explorer, jump-host, `127.0.0.1` tunnel,
ControlPath shortening). What remained was personal design workspaces, doc
translations, PDK-specific code, and fork-maintenance automation — none of it a
clean cherry-pick.

## Why forks diverge here

Divergence is mostly **EDA-environment adaptation**, not new features: SSH tunnel /
jump-host handling, ports, remote hostnames, PDK paths, daemon deployment,
OS-specific branches. A hardcoded adaptation to someone else's lab is **not**
something to copy verbatim — generalize it into config (`.env` / a flag) or skip
it.

## Cadence

- **Every 4–6 weeks**, or before any consolidation effort.
- Also worth a run after upstream ships a notable release (forks may have already
  solved something upstream just changed, or vice-versa).

## Run it

```bash
# Quick scan: rank forks, list ahead-commits, auto-classify by files touched.
scripts/monitor_forks.sh

# Deep scan: additionally flag commits whose content is ALREADY UPSTREAM
# (patch-id match against the last 800 upstream commits). Slower; fetches each
# ahead fork as a temp remote and cleans them up afterward.
scripts/monitor_forks.sh --deep
```

Output is a dated Markdown report under `fork-reports/` (git-ignored). Requires an
authenticated `gh` and an `upstream` remote (or `UP=owner/repo`).

## How the report is built (and why)

1. **Rank by ahead/behind, not `pushed_at`.** Fork-sync bumps `pushed_at`, so it
   lies about activity. The script uses the compare API
   (`compare/main...owner:branch`) and keeps only forks with `ahead > 0`. It
   checks each fork's **default branch**; if a promising fork shows 0 ahead,
   check its other branches manually with
   `gh api repos/OWNER/virtuoso-bridge-lite/branches`.
2. **List each fork's ahead-commits** with an auto-class based on the files each
   commit touches.
3. **`--deep`: flag already-upstream commits** by patch-id. This is the check
   that saves the most time — most "ahead" commits in old forks are just
   upstream's own history that never synced.

## Reading the auto-classes

The class is a **hint that routes attention**, not a verdict.

| Class | Signal | Default action |
|---|---|---|
| **CORE** | modifies existing `core/*.il` or `src/virtuoso_bridge/*.py` | Inspect + verify not upstream; if wanted, sequence & `pytest` after each |
| **ENV** | touches SSH/tunnel/host/port/path/daemon/PDK/OS handling | **Generalize to config, or ask** — never port a site's hardcode |
| **ADD** | only new files (examples/, skills/, tools/) | Likely safe additive — still confirm it isn't personal/site-specific |
| **DOCS** | `docs/`, `arxiv/`, `README`, `assets/`, `*.md` | Skip unless specifically wanted |
| **JUNK** | `.DS_Store`, `.idea/`, `*.iml`, raw sim dumps (`*.tran.tran`, `logFile`), wip/merge noise | Drop |
| **MIXED** | spans several categories (typical of `"update"`/`"wip"` blob commits) | Read the diff; usually a personal-workspace commit that can't be cleanly cherry-picked |

`[ALREADY UPSTREAM]` (deep mode) → **skip**, the content is already merged.

## Verify-before-take checklist

For any commit you're tempted to take:

1. **Already upstream?** Deep-mode tag, or manually:
   `git show <sha> | git patch-id --stable` and compare, or read the current
   upstream version of the file. Upstream often has a *better* version — taking
   the fork's would regress (this happened with Olderdriver's daemon-selection
   commit).
2. **Hardcoded to someone's site?** Grep the diff for hostnames, absolute paths
   (`/home/...`), PDK names (`smic`, `12sf`), ports. If present → generalize to
   `.env`/flag or skip. Do not commit another lab's config.
3. **Cleanly cherry-pickable?** Work buried in `"update"/"1".."6"/"merge"` blob
   commits (personal workspaces) usually is not — re-implement the wanted slice
   as a fresh, focused commit instead.

## If you decide to take something

Follow the consolidation rules (cherry-pick only, never `git merge` a donor
branch, never overwrite upstream to resolve a conflict, never squash/rewrite
authorship, `pytest -q` after every core-touching pick, all work on a
`consolidate` branch, `main` untouched). When re-implementing a generalized
version of someone's change, credit them:

```
Co-authored-by: Original Author <email-or-noreply>
```

## Contributing back

For any change that's broadly useful (not site-specific), prefer opening a PR to
`Arcadia-1/virtuoso-bridge-lite` over only keeping it in a private fork — upstream
is alive and most such changes belong there.

## Snapshot — 2026-07-01 baseline

18 forks ahead of upstream. Outcome: **nothing taken** (empty pick-list).

- **11-fork "~535 behind" cluster** (`0xcdef`, `uestcxyx`, `pli512`, `hxj382125921`,
  `chros098`, `tabooes`, `SherseaHe`, `fishtank666`, `easonchengyy`, `rxu1993`,
  `forkkkkk`) — snapshots of upstream's own early-2026 dev chain (authors
  `TokenZhang`/`Fabulous_Arcadia`). All already upstream.
- **muziyangyu** (28/21) — personal design workspace; clean commits already
  upstream; only non-upstream source is SMIC12sf-PDK-specific (`layout/pdk.py`,
  `layout/layers.py`) + a semi-generic `self_check.py`, all buried in wip commits.
- **DavidQyz** (12/5) — personal OTA/comparator tree with raw sim dumps + IDE
  files (junk). One reusable-ish `render_schematic.py`.
- **Pyrojewel-zard** (11/5) — docs + TB1/receiver-specific ADE examples.
- **Jureka-Shiyi** (5) — Chinese translations + per-file `*.py.md` study notes.
- **pingyi** (4/0) — only `.github/workflows/sync-fork.yml` (fork plumbing; another
  fork had removed the same file).
- **Fans-Lee** (3) / **Olderdriver** (2) — wip that nets to nothing / a
  daemon-selection change already superseded upstream.
