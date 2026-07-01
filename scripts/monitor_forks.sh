#!/usr/bin/env bash
#
# monitor_forks.sh — periodic triage of virtuoso-bridge-lite community forks.
#
# Surfaces which forks are AHEAD of upstream, lists their unmerged commits,
# auto-classifies each by the files it touches, and (in --deep mode) flags
# commits whose content is already upstream. It never modifies your tree and
# never cherry-picks — it produces a report for a human to act on.
#
# See docs/fork-monitoring.md for the full procedure, cadence, and how to read
# the output.
#
# Usage:
#   scripts/monitor_forks.sh                 # quick scan -> report
#   scripts/monitor_forks.sh --deep          # + patch-id "already upstream" check (slower)
#   UP=Owner/repo scripts/monitor_forks.sh   # scan a different upstream
#
# Requirements: gh (authenticated), git, awk. Run from inside a clone that has
# an 'upstream' remote (or set UP=owner/repo).

set -uo pipefail

UP="${UP:-Arcadia-1/virtuoso-bridge-lite}"
DEEP=0
[ "${1:-}" = "--deep" ] && DEEP=1

REPO_SLUG="${UP##*/}"
OUTDIR="${OUTDIR:-fork-reports}"
STAMP="$(date -u +%Y-%m-%d)"
REPORT="${OUTDIR}/${STAMP}.md"
mkdir -p "$OUTDIR"

command -v gh  >/dev/null || { echo "error: gh not found" >&2; exit 1; }
command -v git >/dev/null || { echo "error: git not found" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "error: gh not authenticated (run: gh auth login)" >&2; exit 1; }

echo "Scanning forks of $UP (deep=$DEEP) ..." >&2

# ---------------------------------------------------------------------------
# Path-based auto-classifier. Judgment still belongs to the human — this only
# routes attention. Mirrors the classes in docs/fork-monitoring.md.
#   CORE  : modifies existing core/*.il or src/virtuoso_bridge/*.py
#   ENV   : touches SSH/tunnel/host/port/path/daemon/OS handling -> generalize/ask
#   DOCS  : docs/, arxiv/, README, assets/, *.md
#   JUNK  : .DS_Store, IDE files, raw sim dumps, wip/merge noise
#   ADD   : only-new files elsewhere (examples/, skills/, tools/) -> likely additive
# ---------------------------------------------------------------------------
classify_files() {
  # stdin: "<status>\t<filename>" lines for one commit
  awk -F'\t' '
    { st=$1; f=$2
      if (f ~ /\.DS_Store$|(^|\/)\.idea\/|\.iml$|\.tran\.tran$|\/logFile$|\/logStatus$|\.sweep$/) { junk++; next }
      if (f ~ /(^|\/)(core\/[^\/]*\.il$|src\/virtuoso_bridge\/.*\.py$)/) {
        if (st != "added") { core++ } else { add++ }
        if (f ~ /ssh|tunnel|daemon|transport|host|port/) env++
        next
      }
      if (f ~ /ssh|tunnel|daemon|jump|cshrc|\.env|pdk|PDK/) { env++; next }
      if (f ~ /(^|\/)(docs\/|arxiv\/|assets\/|README|.*\.md$)/) { docs++; next }
      add++
    }
    END {
      cls=""
      if (core) cls="CORE"
      else if (env) cls="ENV"
      else if (add && !docs) cls="ADD"
      else if (docs && !add) cls="DOCS"
      else if (junk && !core && !env && !add && !docs) cls="JUNK"
      else cls="MIXED"
      printf "%s (core=%d env=%d add=%d docs=%d junk=%d)", cls, core+0, env+0, add+0, docs+0, junk+0
    }'
}

# In --deep mode, precompute the set of patch-ids already in upstream/main so we
# can flag fork commits whose content is already merged (rebased/squashed SHAs).
UP_PIDS=""
if [ "$DEEP" = 1 ]; then
  git rev-parse --verify upstream/main >/dev/null 2>&1 || git fetch upstream --quiet 2>/dev/null
  echo "  building upstream patch-id set (last 800 commits)..." >&2
  UP_PIDS="$(mktemp)"
  git log upstream/main -n 800 --pretty=%H | while read -r c; do
    git show "$c" 2>/dev/null | git patch-id --stable
  done | awk '{print $1}' | sort -u > "$UP_PIDS"
fi

{
  echo "# Fork triage — $UP"
  echo
  echo "_Generated $(date -u +'%Y-%m-%d %H:%M UTC') by scripts/monitor_forks.sh (deep=$DEEP)._"
  echo
  echo "Auto-classes are hints only — verify before taking anything. See docs/fork-monitoring.md."
  echo
} > "$REPORT"

# ---------------------------------------------------------------------------
# Rank every fork by commits ahead of upstream (ahead>0 only). pushed_at is
# unreliable (fork-sync bumps it), so compare ahead/behind instead.
# ---------------------------------------------------------------------------
RANK="$(mktemp)"
gh api "repos/$UP/forks" --paginate \
  --jq '.[] | [.full_name, .default_branch] | @tsv' |
while IFS=$'\t' read -r fork branch; do
  owner=${fork%%/*}
  stats=$(gh api "repos/$UP/compare/main...${owner}:${branch}" \
            --jq '[.ahead_by,.behind_by]|@tsv' 2>/dev/null) || continue
  ahead=${stats%%$'\t'*}; behind=${stats##*$'\t'}
  [ "${ahead:-0}" -gt 0 ] 2>/dev/null \
    && printf '%d\t%d\t%s\t%s\n' "$ahead" "$behind" "$owner" "$branch"
done | sort -rn > "$RANK"

NFORKS=$(wc -l < "$RANK" | tr -d ' ')
{
  echo "## Forks ahead of upstream ($NFORKS)"
  echo
  echo "| ahead | behind | fork | branch |"
  echo "|------:|-------:|------|--------|"
  while IFS=$'\t' read -r ahead behind owner branch; do
    echo "| $ahead | $behind | $owner | $branch |"
  done < "$RANK"
  echo
} >> "$REPORT"

# ---------------------------------------------------------------------------
# Per-fork detail: list ahead-commits + files-touched auto-class.
# ---------------------------------------------------------------------------
{
  echo "## Per-fork ahead-commits"
  echo
} >> "$REPORT"

while IFS=$'\t' read -r ahead behind owner branch; do
  {
    echo "### $owner ($ahead ahead / $behind behind)"
    echo
  } >> "$REPORT"

  # In deep mode, fetch the fork so patch-ids can be computed locally.
  if [ "$DEEP" = 1 ]; then
    git remote get-url "_mon_$owner" >/dev/null 2>&1 || \
      git remote add "_mon_$owner" "https://github.com/$owner/${REPO_SLUG}.git" 2>/dev/null
    git fetch "_mon_$owner" --quiet 2>/dev/null
  fi

  gh api "repos/$UP/compare/main...${owner}:${branch}" \
    --jq '.commits[] | [.sha, (.commit.author.date[0:10]), .commit.author.name, (.commit.message | split("\n")[0])] | @tsv' 2>/dev/null |
  while IFS=$'\t' read -r sha date author subject; do
    short=${sha:0:9}
    files=$(gh api "repos/$UP/commits/$sha" \
              --jq '.files[] | [.status, .filename] | @tsv' 2>/dev/null)
    cls=$(printf '%s\n' "$files" | classify_files)

    tag=""
    if [ "$DEEP" = 1 ] && [ -n "$UP_PIDS" ]; then
      pid=$(git show "$sha" 2>/dev/null | git patch-id --stable | awk '{print $1}')
      if [ -n "$pid" ] && grep -qx "$pid" "$UP_PIDS"; then
        tag=" **[ALREADY UPSTREAM]**"
      fi
    fi
    printf -- "- \`%s\`  %s  _%s_  %s — **%s**%s\n" \
      "$short" "$date" "$author" "$subject" "$cls" "$tag" >> "$REPORT"
  done
  echo >> "$REPORT"
done < "$RANK"

# Cleanup temp remotes created in deep mode.
if [ "$DEEP" = 1 ]; then
  for r in $(git remote | grep '^_mon_'); do git remote remove "$r" 2>/dev/null; done
  [ -n "$UP_PIDS" ] && rm -f "$UP_PIDS"
fi
rm -f "$RANK"

echo "Report written to: $REPORT" >&2
echo "$REPORT"
