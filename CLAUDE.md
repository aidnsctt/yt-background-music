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

## Make the call yourself, and record it

This project has one user and one file. Waiting for approval costs more than a
wrong choice that is written down and can be reversed. So: **decide, act, and
log the reason.** Do not open a task by asking which option to take.

- Pick the option you can defend, apply it, and append the entry to
  `DECISIONS.md` in the same commit. The entry is what makes the choice
  reversible, so it is not optional.
- Ask only when the choice is not yours to make: it needs a credential, it
  costs money, it changes what the user hears in a way they did not ask for, or
  it would delete history.
- **Quote a past decision back before you reverse it.** If `DECISIONS.md`
  already covers the area, read that entry first, say what it decided and why,
  and then say why that reasoning no longer holds. Never re-litigate a
  deliberate choice without its original context.
- Do not keep project knowledge in an agent's private memory. It is invisible
  on any other machine. `DECISIONS.md` and `README.md` are the only stores.

## Investigate before you fix

A symptom report ("it pauses", "it went silent") names what the user heard, not
the cause. Every wrong fix in this repo's history came from skipping a step
here.

1. **Read `DECISIONS.md` first.** The failure you are looking at has probably
   been looked at before, and the log says what was tried and what was rejected.
2. **Read `/tmp/lofi-player.log`.** Look for `mpv exited` lines. A repeating
   automatic restart and a buffering stall sound identical to the user and have
   nothing in common.
3. **Observe the running player, do not guess.** Read `time-pos`,
   `demuxer-cache-duration` and `paused-for-cache` from the live mpv over the
   IPC socket (the one-liner is in `README.md`). Watch the mpv pid too: a pid
   that changes is a restart, not a stall.
4. **Test competing stream configs concurrently, never one after the other.**
   This stream's throughput swings minute to minute, so a sequential A/B
   measures the weather, not the config. Run both at once against the same
   stream.

## Verify the fix live before you report it

Say what you ran, not what you expect. A change that compiles is not a change
that works, and this app's failures only appear after minutes of playing.

- Restart with `./restart.sh` and **soak past the failure window** — longer than
  the interval the symptom happened at. For the 403 restart loop that meant more
  than 90 seconds; several minutes is the honest test.
- Confirm the **mpv pid did not change** over the soak, and that
  `/tmp/lofi-player.log` holds no restart lines.
- Separate what you ran from what you read. "I soaked it for 6 minutes and the
  pid held" and "the flag looks right" are different claims. Never let the
  second one be read as the first.

## Keep `README.md` current

`README.md` is the operator's page. Its Troubleshooting table is a
symptom → cause → action map, and it is part of the deliverable, not a
follow-up.

- **A new failure mode gets a new row, in the same commit that fixes it.**
  Write the row as the user would describe the symptom, not as the code
  describes it.
- Update the existing row when a fix changes the cause or the action.
- Update the Requirements or Run sections when setup changes.
- If a change hits none of these, say "no README impact" rather than staying
  silent about it.

## Never remove logging

The app restarted mpv all morning with no trace, and that turned a 30-second
diagnosis into a 45-minute one (see `DECISIONS.md`, 2026-09-17). Every automatic
recovery — an mpv exit, a stall kill, a fallback to the `/live` URL — writes a
line to stderr, which `restart.sh` sends to `/tmp/lofi-player.log`.

Do not delete or quieten those lines to reduce noise. Add to them. This app
heals itself silently, so the log is the only evidence that anything went wrong.

## Known dead ends — do not retry

Ideas that look obvious, were tried, and made things worse. Read this before
proposing one of them again. Add to the list when you burn time on a new one,
and link the `DECISIONS.md` entry.

- **ffmpeg `reconnect` / `seg_max_retry` options.** Tried twice
  (2026-08-15, 2026-09-17). HLS fetches each segment as its own short request,
  so connection-level retry has almost nothing to work with. Worse, it makes
  mpv wedge in `paused-for-cache` instead of exiting in 2s, which turns a fast
  recoverable failure into a silent one.
- **Sequential A/B tests of two mpv configs.** Produced two confident and
  opposite wrong conclusions (2026-08-15). Only concurrent runs mean anything on
  this stream.
- **Raising `--cache-secs` to fix a thin live cushion.** On a live stream mpv
  cannot read ahead of what YouTube has published, so the cushion equals
  `live_start_index`, not `cache-secs`. The flags asked for 180s and the real
  cushion was 55s (2026-09-17).
