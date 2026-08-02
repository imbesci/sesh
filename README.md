# sesh

A terminal UI for finding and resuming Claude Code sessions.

`claude --resume` gives you a flat, undifferentiated list of the sessions in the
current directory. That works right up until you have real history: dozens of
sessions across several repos and branches, half of them titled `hello`, and the
one you want is the one where you were debugging the auth redirect three days
ago on a branch you have since switched away from.

`sesh` treats that as a search problem. It reads the transcripts Claude Code
already writes, indexes what is actually in them — every prompt you typed, every
branch and directory touched, every file edited — and gives you a picker that
starts narrow and widens on one keystroke.

```
 sesh  api  main                                              12/34 of 210
 scope:branch sort:recent                                              on main
 ❯ auth redirect
────────────────────────────────────────────────────────────────────────────────
 ● 2h  main   Fix the auth redirect loop          14   842k │  Fix the auth redirect loop
   3d  main   Session refresh returns 401          6   210k │
       ↳ …the redirect drops the returnTo param…            │  when   2026-07-29 14:02 · 2h ago
   5d  main   Add PKCE to the login flow          22   1.4M │  repo   api  ~/dev/api
                                                            │  branch main·1596  fix/auth·311
                                                            │  stats  14 prompts · 592 tools
```

## Install

Python 3.11+ and nothing else — no runtime dependencies. `ripgrep` is optional
and makes full-text search faster.

```sh
uv tool install git+https://github.com/imbesci/sesh.git
# or
pipx install git+https://github.com/imbesci/sesh.git
```

Either puts a `sesh` command on your PATH in its own isolated environment. With
`uv` you don't even need Python installed — it fetches a suitable one; `pipx`
uses your system Python, which must be 3.11+. If the tool isn't found after
install, run `uv tool update-shell` (or add pipx's bin dir to PATH) and reopen
the shell. From a local checkout, `uv tool install .` / `pipx install .` work
the same way.

For development, skip the install and point PATH at the checkout — edits take
effect immediately:

```sh
git clone https://github.com/imbesci/sesh.git ~/dev/sesh
ln -s ~/dev/sesh/bin/sesh /opt/homebrew/bin/sesh   # or any dir on your PATH
```

`git` is *not* required at runtime: repo roots, branches and worktrees are
resolved by reading `.git/HEAD` directly.

## Use

```sh
sesh                   # picker, scoped to the current branch
sesh --safe            # same, but resume with permission prompts left on
sesh auth redirect     # picker with a filter already applied
sesh --last            # resume the most recent session here, no UI
sesh is:unfinished     # sessions you left mid-action
sesh --open            # open the best match's directory in $EDITOR
sesh --list --all      # print every session
sesh --print           # print the resume command for the best match
```

Resuming runs `claude --dangerously-skip-permissions --resume <id>` — you land
back in the session ready to work, no prompts. Pass `--safe` (or set
`SESH_SAFE=1`) to resume with permission prompts on instead.

## How finding works

Two independent controls, which is the whole design.

**Scope** is where you are looking. It comes from your working directory, and
`tab` widens it one step at a time:

```
branch  →  repo  →  dir  →  all
```

You start on `branch`, because when you are on a branch the sessions you want
are almost always that branch's. If that turns up nothing, `sesh` widens on its
own rather than greeting you with an empty list.

**Query** is what you are looking for, and typing never changes your scope — so
widening the search never costs you what you typed, and refining it never
traps you in a corner.

Plain words fuzzy-match titles, prompts, repos, branches and paths. When a row
matched on something other than its title, the reason is shown beneath it:

```
   3d  main   Session refresh returns 401
       ↳ …the redirect drops the returnTo param…
```

### Query syntax

| | |
|---|---|
| `branch:main` `b:` | sessions that touched a branch |
| `repo:api` `r:` | by repository |
| `file:auth.py` `f:` | sessions that edited or read a file |
| `dir:` `tool:` `model:` `id:` | working directory, tool used, model, session id |
| `age:7d` `after:2026-07-01` `before:` | time windows |
| `turns:>5` `tokens:>100k` `records:` | numeric comparisons |
| `is:live` `is:unfinished` `is:compacted` | state; also `is:subagents`, `is:fork`, `is:empty` |
| `text:"cannot read"` | search inside full transcripts, not just prompts |
| `'exact` | literal substring instead of fuzzy |
| `!term` | exclude |

Everything combines: `branch:main file:auth.py age:14d !test`.

`text:` is the escape hatch. The index holds what you typed; `text:` greps the
raw transcripts for anything else — an error you pasted, a filename Claude
mentioned once, a command buried in tool output.

## Keys

| | |
|---|---|
| `enter` | resume, in the session's own directory |
| `ctrl+f` | fork — resume under a new session id, original untouched |
| `ctrl+o` | resume from the current directory instead |
| `tab` / `shift+tab` | widen / narrow the scope |
| `ctrl+b` `ctrl+r` | filter by branch / project |
| `alt+r` | show sessions related to this one (same task) |
| `ctrl+s` | cycle sort |
| `ctrl+g` | show sessions with no prompts |
| `ctrl+v` | open the full transcript; `/` searches inside it |
| `ctrl+t`, `alt+↑` `alt+↓` | toggle and scroll the detail pane |
| `ctrl+y` / `alt+y` | copy the resume command / session id |
| `alt+o` | open the session's directory in `$EDITOR` |
| `ctrl+x` / `alt+u` | move a session to trash / undo |
| `ctrl+l` | reload from disk |
| `alt+h` | all keys |
| `esc` | clear the filter, then quit |

## Notes on behaviour

**Resuming changes directory.** Claude Code scopes session lookup to the project
directory, so a session created in `~/dev/api` cannot be resumed from anywhere
else — it reports `No conversation found`. `sesh` reconstructs a working
directory that resolves, and tells you when it cannot: if the original directory
is gone, it says so rather than launching into a confusing failure.

**A session is not one branch or one directory.** You `cd`, you switch branches,
subagents run elsewhere. `sesh` records every branch and directory a session
touched and filters on all of them, so a session you started on `main` and
finished on `fix/auth` is findable from either. Linked worktrees are folded onto
their parent repo, so a worktree-per-branch layout still reads as one project.

**Running sessions are marked** with `●`, but they are not floated to the top:
a `claude` left open in a terminal for days is not "recent", and hoisting it
above a session you touched minutes ago makes the age column read as mis-sorted.
They rank by their real activity like everything else. Resuming one warns first:
two processes writing one transcript interleave their messages. `ctrl+f` forks
instead, which is usually what you wanted.

**Every row shows where it left off.** The detail pane carries Claude's last
message and last action alongside the prompts you typed — the prompts say what
you asked, this says what came of it, which is usually how you recognise the
session you meant. Both are searchable, so a plain query matches Claude's own
last words, not just your prompts.

**Unfinished work is findable.** A session whose last event was a tool call or
its result — rather than a closing message — was interrupted mid-action.
`is:unfinished` lists exactly those, and `ctrl+s` has a matching sort that floats
them up. It is the "what did I leave hanging?" list.

**A task is rarely one session.** You stop for the day and start fresh tomorrow,
or split work across two windows. `alt+r` pivots to the sessions that share this
one's task — same repo and branch, or the same files touched — across whatever
scope they live in. `esc` returns to the full list.

**The recent list is grouped by day.** Under the default recency sort, thin
`Today` / `Yesterday` / `Past week` headers break up the list so a date is a
glance, not arithmetic. Any other sort or an active query removes them.

**Deleting is reversible.** `ctrl+x` moves a session's files to
`~/.claude/sesh/trash`; `alt+u` puts them back. Nothing is unlinked. (To
remove *all* state for a project, Claude Code's own `claude project purge` is
the supported route.)

**Nothing is ever written to your transcripts.** `sesh` only reads them. Its own
cache lives in `~/.claude/sesh/`.

## Performance

The cost is a cold index — every transcript is parsed once. In practice that is
about 100ms for 19 sessions spanning 33MB. Results are cached against each
file's size and mtime, so subsequent launches only re-read what actually
changed, and transcripts are append-only so that check is exact. A warm launch
is ~80ms.

Every transcript line is parsed with `json.loads`. That sounds wasteful — most
of the bytes are tool output that gets discarded — but it was measured against
a hand-rolled scanner that skipped parsing, and the parser won by ~2x. It is
also the only correct approach: the record-level `type` field sits *after* the
nested message on assistant records and *before* it on user records, so no
positional heuristic finds it, and quoted transcript content can spoof any
string search.

## Development

```sh
python3 -m unittest discover -s tests -t .    # 145 tests, stdlib only
```

The picker's terminal and frame buffer are injectable (`sesh/tui/io.py`), so
tests drive the real key-handling and rendering code and assert on the painted
frame rather than poking at internal state. That is how the two nastiest bugs in
this thing were caught: fuzzy search matching a file path by scattering letters
across forty characters, and background-task notifications being counted as
prompts you had typed.
