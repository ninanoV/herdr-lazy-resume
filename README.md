# herdr-lazy-resume

A [herdr](https://herdr.dev) plugin that restores your Claude Code sessions
after a herdr server restart lazily: a pane's session is resumed when you
first look at that pane, one pane at a time, never two `claude` processes
starting at once.

## Why

herdr can resume agent sessions natively (`[session] resume_agents_on_restore`),
but it restarts every agent pane together as soon as a client attaches. With
many Claude Code panes that means many `claude` processes starting at the same
moment. On macOS this can make them race on keychain reads, which in turn can
re-register a "trusted device" and send you a security email (seen with
Remote Control at startup enabled). It also spends startup work on panes you
may not open for hours.

This plugin replaces that with:

- **Lazy:** a pane is resumed on its first focus after the restart. The pane
  that is focused at startup counts as focused.
- **Serialised:** the next resume waits until the previous `claude` is up,
  and at least `min_interval_seconds` (default 5) have passed. The lock is
  machine-wide, across herdr sessions.
- **Careful:** the command is typed only into an idle, empty shell prompt.
  Panes where something is running, or where you started an agent yourself,
  are left alone.

## How it works

1. **Recording.** On herdr events (`pane.focused`, `pane.agent_detected`,
   `pane.agent_status_changed`, `pane.closed`, `pane.moved`) the plugin reads
   `herdr pane list`. For every pane running Claude Code it stores
   `pane → session id`, using the id reported by herdr's Claude integration.
   The record is dropped when claude exits in that pane (herdr's `released`
   event) or the pane is closed. herdr emits no events while the server shuts
   down, so the records at that point are exactly the panes where claude was
   running.
2. **After a restart** those panes, now plain shells again, become pending
   for this server start.
3. **On focus** of a pending pane (or for the focused pane at startup) the
   plugin runs `claude --resume <id>` in it, at most once per server start.
   It does this only if all of these hold:
   - the shell is the foreground process in the pane,
   - the prompt line matches a reference that was captured while the pane was
     unfocused and couldn't receive keystrokes, so nothing has been typed,
   - no other resume is still starting, and the minimum interval has passed.
   If you leave a pane while it waits its turn, it stays pending until you
   come back.

A pending pane you never visit stays pending across further restarts. Panes
moved to another workspace are followed by their terminal id.

## Requirements

- herdr 0.9.5 or newer
- `python3` 3.9+ on the herdr server's `PATH` (stdlib only, no dependencies)
- herdr's Claude Code integration installed (`herdr integration status`;
  install with `herdr integration install claude`). It reports the session ids.

Tested on macOS with herdr 0.9.5 and Claude Code 2.1. Linux should work (it
uses `flock` and the herdr CLI only) but is untested.

## Install

Turn off herdr's built-in agent restore in `~/.config/herdr/config.toml`:

```toml
[session]
resume_agents_on_restore = false
```

Then install the plugin:

```sh
herdr plugin install ninanoV/herdr-lazy-resume
```

Or, from a clone:

```sh
git clone https://github.com/ninanoV/herdr-lazy-resume
herdr plugin link ./herdr-lazy-resume
```

Plugins are global to all your herdr sessions. Recording starts with the next
event after installing: any focus change, or a claude state change, picks up
every open Claude Code pane. Resuming starts with the next server restart.

**oh-my-zsh users:** set `zstyle ':omz:update' mode reminder` (or `auto`). In
the default `prompt` mode a restored shell may be waiting on the
`[Y/n] update?` question. That looks like an idle shell, and the question
would swallow the first character of the resume command.

## Configuration

Optional. Create `$(herdr plugin config-dir local.lazy-resume)/config.json`:

```json
{"min_interval_seconds": 5, "command": "claude --resume {id}"}
```

| Key | Default | Meaning |
|---|---|---|
| `min_interval_seconds` | `5` | Minimum gap between two resume starts |
| `command` | `claude --resume {id}` | Typed into the pane. `{id}` is the session id; only UUIDs are substituted |

## Logs and state

```
~/.local/state/herdr/plugins/local.lazy-resume/<session-hash>/lazy-resume.log
~/.local/state/herdr/plugins/local.lazy-resume/<session-hash>/state.json
```

The log shows each boot's pending panes, every resume sent, how long claude
took to become ready, and every skip with its reason and session id. If a
pane is skipped, you can resume it by hand with `claude --resume <id>` from
the log.

Example after a restart:

```
boot …: pending ['w1:p1', 'w2:p1', 'w3:p1']
w1:p1: sent resume for 1f0e6b2a-…
w1:p1: claude ready (idle) after 3.9s
w2:p1: sent resume for 7c4d9e10-…
w2:p1: claude ready (idle) after 3.6s
```

## Limitations

- A shell builtin waiting for input (`read`, `select`, or the oh-my-zsh update
  question above) can't be told apart from a prompt.
- `/clear` changes the session id without a herdr event. The new id is picked
  up at the next event of any kind. If the server stops before that, the
  session from before the `/clear` is resumed.
- The startup-focused pane, and a pane focused before its reference was
  captured, use the first idle line seen on focus as the reference. Text typed
  in the first few hundred milliseconds there can go unnoticed.
- A prompt that redraws differently more than a minute after startup (it wraps
  at a new width, or contains a clock) makes that pane skip one server start.
  It is retried at the next one.
- herdr ignores a `startup` session report while it still holds a session for
  the pane. So a fresh `claude` started by hand in a restored, never-resumed
  pane keeps the old id until `/clear` or until it exits.
- If the server starts with no client attached, the focused pane is still
  resumed, without anyone watching.
- herdr runs the command in the pane's restored working directory.

## Development

```sh
python3 test_lazy_resume.py   # pure-logic checks (record/pending/move rules, config, id validation)
```

The behaviour was tested end-to-end in an isolated herdr session, with a TUI
client attached and panes switched by keyboard. Covered cases: resume on
focus, no re-run on refocus, panes the user quit, busy panes, typing while
queued, rapid switching, cross-workspace moves, carry-over across restarts,
and a real two-pane Claude Code restart.

## Uninstall

```sh
herdr plugin uninstall local.lazy-resume   # or: herdr plugin unlink local.lazy-resume
```
