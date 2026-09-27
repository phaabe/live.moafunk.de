# Codex epic guard

The tracked `hooks.json` loads the guard from the current Git root, including
linked worktrees and sessions started in subdirectories. It requires Bash 3.2
and Python 3.10+. Existing machine-global hooks still load separately.

After this branch is reviewed and merged, update each worktree to a commit
containing these files. Existing worktrees on older commits do not gain them
automatically. If a checkout already has an untracked `.codex/hooks.json`, back
it up and reconcile its local registrations before updating; do not overwrite it.

Trust the project, then review and trust the new hook using `/hooks` in Codex.
Codex skips new or changed hook definitions until they are trusted. Check
`/hooks` in each worktree; tracking a file does not grant runtime trust. See the
[Codex hook documentation](https://learn.chatgpt.com/docs/hooks).

The guard blocks:

- Claude verdicts containing a real 40-character SHA in tool input, a `gh`
  body file or a typed API field file (`-F key=@path` / `--field key=@path`).
  Codex may only write its own verdict. API field paths resolve against the
  command's working directory; unreadable files and `@-` (stdin) are refused.
- PR creation without an explicit base. Feature PRs target
  `dev/streaming-architecture`; only that branch may create a release into `main`.
  The approved setup exception is exactly `ci/312-epic-guard` → `main`.
- PR merges without a 40-character `--match-head-commit` value.
- REST PR writes through `gh api`, including implicit POSTs from field flags
  and opaque `--input` bodies. Use `gh pr` commands instead. GET requests work.
- MCP merges: use `gh pr merge --match-head-commit` instead. MCP PR creation
  follows the same base and head rules.

Use a single literal `gh pr create` or `gh pr merge` command. Put flags after
the verb. Compound commands, wrappers and shell expansion are refused for these
operations. Flags before or between subcommands are refused. Quoted values,
`--flag=value` and backslash-newline continuations work. Short option values
must be separate (`-B main`, not `-Bmain`); repeated base/head options are refused.
Known value flags accept quoted values starting with a dash, such as
`--body '- fix workflow'`. Shell command payloads must be strings; arrays and
other types are refused.
Body files must exist before the command; stdin and heredoc bodies are refused,
including heredocs passed through shell wrappers. For `gh api`, `-F` is a typed
API field, not a body-file path. There is no main-branch override.

This is a command guard, not a security boundary against arbitrary scripts,
GitHub API clients or aliases. Shell `gh api` calls must also be single literal
invocations, with flags after `api`. It does not
verify GitHub approval, lane ownership or green checks. Those remain mandatory
under the epic rules; the shared server checker is separate setup work.

## Recovery if the guard blocks every tool

The hook fails closed: missing `git` or `python3`, unreadable files and parser
errors block the tool call. The operator can disable this hook through `/hooks`
in Codex, repair the dependency or guard from a separate terminal, and run the
tests below. If `/hooks` is unavailable, back up `.codex/hooks.json` and remove
only this guard's registration from that worktree in a separate terminal, then
restart the session. Restore the registration and trust it through `/hooks`
after the repair. There is no environment-variable bypass.

While disabled, make only the repair: do not create or merge PRs or post review
verdicts through the unguarded session. Check the epic rules manually. Disabling
the guard does not grant approval for `main` PRs, merges or production actions.

Run the regression tests (no GitHub writes):

```sh
python3 -m unittest discover -s .codex/hooks/scripts -p 'test_*.py' -v
/bin/bash -n .codex/hooks/scripts/epic-guard.sh
```

The tests invoke the registered command through macOS-compatible `/bin/bash`,
including from a fresh linked worktree with a space in its path.
