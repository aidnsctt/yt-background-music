# Instructions

## Always auto-commit and auto-push

Whenever you finish a section of work on this project, automatically commit and
push the changes — do not wait to be asked.

- As soon as a logical unit of work is complete (a fix, a feature, a refactor),
  run `git add -A`, commit with a clear message, and `git push` to the remote.
- Treat commit + push as part of "done." Work is not finished until it is pushed.
- This keeps the remote always in sync so any developer working on this project
  has the latest changes.
- The only time to hold off is if the user explicitly says not to commit/push, or
  if changes are clearly incomplete/broken (in which case, say so first).

## Always update the decision log

Keep a running decision log in `DECISIONS.md` — a chronological timeline of the
**meaningful** decisions made on this project, each with its reason.

- Whenever you finish a section of work that involved a real decision (a design
  choice, a trade-off, a behavior change, a reversal of a previous approach),
  append an entry to `DECISIONS.md` **before** you commit, so the log ships in the
  same commit as the change.
- Follow the format already in the file: `## YYYY-MM-DD — Short title`, then
  **Decision**, **Why**, and (when relevant) trade-offs or alternatives. Add
  newest entries at the bottom.
- The **Why** is the important part — write it so a developer six months later
  understands the original reasoning and can judge whether it still holds.
- If a new change reverses or contradicts an earlier decision, add a new entry
  that links back to the old one (by date + title) rather than deleting history.
  The log is append-only; we keep the timeline intact.
- Skip purely mechanical changes (typo fixes, formatting) — log decisions, not
  every diff.
