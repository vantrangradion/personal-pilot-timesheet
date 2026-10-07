#!/usr/bin/env bash
# Runs the deterministic [Script] steps of workflow.md, grouped so a runner needs
# only two tool calls around the single [AI] step.
#
#   run.sh prepare [DATE]                     steps 1-4: harvest, slice, print the AI payload
#   run.sh save [DATE] [save_entry.py args]   steps 6-7: merge the AI JSON from stdin, upsert, render
#
# DATE is YYYY-MM-DD (default: today). Intermediate files go to
# $TIMESHEET_RUN_ROOT/<DATE>/ (default: tmp/timesheet/, gitignored).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

usage() {
    echo "Usage: $0 prepare [YYYY-MM-DD] | save [YYYY-MM-DD] [save_entry.py args] < ai.json" >&2
    exit 2
}

stage="${1:-}"
[ $# -gt 0 ] && shift
DATE="$(date +%F)"
if [[ "${1:-}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    DATE="$1"
    shift
fi
RUN="${TIMESHEET_RUN_ROOT:-tmp/timesheet}/$DATE"

# Steps 1-3: run one harvester; on failure keep `[]` and mark the source failed.
harvest() {
    local source="$1"
    rm -f "$RUN/$source.failed"
    if ! "$PY" "scripts/fetch_$source.py" --date "$DATE" >"$RUN/$source.json" 2>"$RUN/$source.err"; then
        echo "[]" >"$RUN/$source.json"
        touch "$RUN/$source.failed"
    fi
}

prepare() {
    mkdir -p "$RUN" || exit 1
    harvest commits &
    harvest prs &
    harvest calendar &
    wait

    local failed=() source
    for source in commits prs calendar; do
        if [ -e "$RUN/$source.failed" ]; then
            failed+=(--failed-source "$source")
            echo "Warning: fetch_$source.py failed ($RUN/$source.err):" >&2
            sed 's/^/  /' "$RUN/$source.err" >&2
        fi
    done

    # Step 4: slice into blocks; stdout is the compact AI payload.
    "$PY" scripts/prepare_payload.py --date "$DATE" \
        --commits "$RUN/commits.json" --prs "$RUN/prs.json" --calendar "$RUN/calendar.json" \
        --blocks-out "$RUN/blocks.json" ${failed[@]+"${failed[@]}"}
}

save() {
    if [ ! -f "$RUN/blocks.json" ]; then
        echo "Error: $RUN/blocks.json not found; run '$0 prepare $DATE' first." >&2
        exit 1
    fi
    # Keep the AI output for auditing, then steps 6-7.
    cat >"$RUN/ai.json" || exit 1
    "$PY" scripts/merge_descriptions.py --blocks "$RUN/blocks.json" --descriptions "$RUN/ai.json" \
        >"$RUN/entries.json" || exit 1
    "$PY" scripts/save_entry.py --blocks "$RUN/blocks.json" --entries "$RUN/entries.json" "$@"
}

case "$stage" in
    prepare) [ $# -eq 0 ] || usage; prepare ;;
    save) save "$@" ;;
    *) usage ;;
esac
