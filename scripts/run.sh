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

# Submissions from routers: the relay writes one file per submission to inbox/ of the «inbox» branch. All of them
# are processed in one commit to main, then removed from the branch (the relay may add new ones meanwhile:
# removing other files rebases cleanly)
relay_inbox() {
  git ls-remote --exit-code --heads origin inbox >/dev/null 2>&1 || return 0
  git fetch -q origin inbox
  local box wt done
  box=$(mktemp -d)
  git archive origin/inbox inbox 2>/dev/null | tar -x -C "$box" || return 0
  done=$(python3 scripts/intake.py --inbox "$box/inbox")
  [ -n "$done" ] || return 0
  if [ -n "$(git status --porcelain data providers index.json STRATEGIES.md stats state log)" ]; then
    git add -A data providers index.json STRATEGIES.md stats state log
    git commit -q -m "Strategies from routers ($(echo "$done" | wc -l))"
    for i in 1 2 3; do git pull --rebase -q && git push -q && break; sleep 3; done
  fi
  wt=$(mktemp -d)
  git worktree add -q --detach "$wt" origin/inbox
  (
    cd "$wt"
    for f in $done; do git rm -q "inbox/$(basename "$f")"; done
    git commit -q -m "Processed $(echo "$done" | wc -l)"
    for i in 1 2 3 4 5; do git push -q origin HEAD:inbox && break; git pull --rebase -q origin inbox; done
  )
  git worktree remove --force "$wt"
  echo "inbox: $(echo "$done" | wc -l) processed"
}
relay_inbox

if [ "$(jq -r .action "$GITHUB_EVENT_PATH")" = labeled ] && [ "$(jq -r '.label.name // ""' "$GITHUB_EVENT_PATH")" = remove ]; then
  process "$GITHUB_EVENT_PATH"
fi

for n in $(gh issue list --repo "$R" --label strategy --state open --limit 100 --json number,labels \
           --jq '.[] | select(all(.labels[]; .name != "remove")) | .number' | sort -n); do
  gh api "repos/$R/issues/$n" | jq '{action: "opened", issue: .}' > event-sweep.json
  process event-sweep.json
done
rm -f event-sweep.json
