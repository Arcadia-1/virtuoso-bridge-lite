#!/bin/bash
# fix-symlinks.sh — Fix git symlinks on Windows (creates NTFS junctions)
#
# On Windows, git clone with core.symlinks=false stores symlinks as plain
# text files containing the target path. This script replaces them with
# NTFS junctions, which need no admin rights and no Developer Mode.
#
# Usage:  bash scripts/fix-symlinks.sh

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

is_windows() {
    [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]]
}

if ! is_windows; then
    echo "Not on Windows — symlinks should work natively, nothing to do."
    exit 0
fi

fixed=0

# Scan the entire repo for broken symlinks (text files created by git)
while IFS= read -r rel_entry; do
    entry="$REPO_ROOT/$rel_entry"
    [[ ! -L "$entry" && -f "$entry" ]] || continue  # only plain files (broken symlinks)
    target="$(cat "$entry")"
    target="${target%$'\r'}"

    # Skip if content doesn't look like a relative path
    [[ "$target" == ../* || "$target" == ./* ]] || continue

    # Resolve the target relative to the symlink's parent directory
    link_dir="$(dirname "$entry")"
    target_parent="$(cd "$link_dir" && cd "$(dirname "$target")" 2>/dev/null && pwd)" || continue
    abs_target="$target_parent/$(basename "$target")"

    if [[ ! -d "$abs_target" ]]; then
        echo "SKIP: $entry -> $target (target not a directory)"
        continue
    fi

    win_link="$(cygpath -w "$entry")"
    win_target="$(cygpath -w "$abs_target")"

    backup="$(mktemp "${entry}.vb-bak.XXXXXX")"
    if ! cp -p -- "$entry" "$backup"; then
        rm -f -- "$backup"
        exit 1
    fi
    rm -- "$entry"
    if MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' cmd /c mklink /J "$win_link" "$win_target" > /dev/null; then
        rm -f -- "$backup"
        echo "  OK: $entry -> $target"
        ((fixed++)) || true
    else
        rc=$?
        mv -f -- "$backup" "$entry"
        echo "FAIL: $entry -> $target (mklink failed; restored original file)" >&2
        exit "$rc"
    fi
done < <(git -C "$REPO_ROOT" -c core.quotepath=false ls-files -s | awk -F'\t' '$1 ~ /^120000 / {print $2}')

if [[ $fixed -eq 0 ]]; then
    echo "Nothing to fix — all symlinks are already resolved."
else
    echo "Fixed $fixed symlink(s)."
fi
