#!/usr/bin/env bash
# Processes intake events: the triggering one (if it is «remove») and then every open strategy issue.
# An issue is closed once processed, so an open one with the «strategy» label has not been handled yet —
# e.g. its own run was cancelled: GitHub keeps only one pending run per concurrency group.
set -euo pipefail
R="$GITHUB_REPOSITORY"

git config user.name "zemidala"   # commits under the owner, so the bot is not in Contributors
git config user.email "42742254+zemidala@users.noreply.github.com"

# one event file → base files, commit, reply, label, close
process() {
  local ev="$1" n login out label changed
  n=$(jq -r .issue.number "$ev")
  login=$(jq -r .issue.user.login "$ev")
  export ACCOUNT_CREATED="$(gh api "users/$login" --jq .created_at 2>/dev/null || true)"
  out=$(mktemp)
  GITHUB_OUTPUT="$out" python3 scripts/intake.py "$ev"
  changed=$(sed -n 's/^changed=//p' "$out")
  label=$(sed -n 's/^label=//p' "$out")
  if [ "$changed" = true ]; then
    git add -A data providers index.json STRATEGIES.md
    git commit -q -m "$([ "$label" = removed ] && echo Removed || echo Strategy) from #$n"
    for i in 1 2 3; do git pull --rebase -q && git push -q && break; sleep 3; done
  fi
  gh label create "$label" --repo "$R" --color ededed 2>/dev/null || true
  gh issue comment "$n" --repo "$R" --body-file reply.md
  gh issue edit "$n" --repo "$R" --add-label "$label"
  gh issue close "$n" --repo "$R" --reason "$([ "$label" = rejected ] && echo 'not planned' || echo completed)" 2>/dev/null || true
  echo "#$n: $label"
}

if [ "$(jq -r .action "$GITHUB_EVENT_PATH")" = labeled ] && [ "$(jq -r '.label.name // ""' "$GITHUB_EVENT_PATH")" = remove ]; then
  process "$GITHUB_EVENT_PATH"
fi

for n in $(gh issue list --repo "$R" --label strategy --state open --limit 100 --json number,labels \
           --jq '.[] | select(all(.labels[]; .name != "remove")) | .number' | sort -n); do
  gh api "repos/$R/issues/$n" | jq '{action: "opened", issue: .}' > event-sweep.json
  process event-sweep.json
done
rm -f event-sweep.json
