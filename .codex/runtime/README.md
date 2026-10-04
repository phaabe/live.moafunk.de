# Protected Codex home

The foundation validator needs Python 3.11 or newer. These files are operator
templates, not an installer. Unreplaced templates
fail preflight. The completion leaf supplies the install and smoke interfaces.
Do not point `CODEX_HOME` at the checkout or at a personal Codex home.

`EPIC_CODEX_PROTECTED_CONFIG` names a canonical absolute JSON file. It and the
dedicated home, all configured hook code and every replacement-capable parent
must be outside model-writable roots. The operator owns the files; shared
write permissions are refused. The JSON object has these fields:

- `schema`: `1`.
- `runner_root`, `code_root`, `codex_home`: canonical absolute paths. Legacy
  code uses the runner checkout; pinned code uses the selected runtime.
- `allowed_origin_urls`: approved effective fetch and push origin URLs.
  Multiple effective fetch or push URLs are refused. Git URL rewrites apply.
- `writable_roots`: exactly `git_metadata` (the checkout's common Git directory)
  and `gitnexus` (the approved state directory). These exact paths appear in
  the protected `config.toml` too.
- `temporary_parent`: an allocation parent, never a writable grant. After
  admission, the tick creates `codex-tick-<unique>` directly below it and
  passes only that exact directory through `--add-dir`.
- `review_parent`: an allocation parent, never a writable grant. Review
  evidence grants are exactly `<review_parent>/<PR>/<40-character SHA>`.
- `worktree_parents`: approved feature/review worktree locations. The model
  cwd must be a linked worktree of the bound repository, or the tick's temp
  directory. The runner and runtime directories are never model cwd.
- `files`: absolute protected file path to SHA-256. Include `config.toml`,
  `hooks.json`, both named rules, the hook scripts and their protected code.
  The bound code root's `.codex/protected_home.py`, `epic-guard.sh` and
  `epic_guard.py` entries are mandatory. A pull that changes these foundation
  components refuses until the operator reviews and updates their binding;
  selector updates do not require resealing those hooks.
  Auth is separate operator setup and never belongs in this object.
- `hook_trust`: the exact `hooks.state` object, keyed by the canonical native
  hook key. Obtain `trusted_hash` from native `hooks/list` after reviewing the
  final hook definition; do not use a trust bypass.
- `gitnexus_mcp`: the exact protected `[mcp_servers.gitnexus]` object. No
  personal MCP configuration or other MCP server is inherited.
- `network_access`: the approved boolean network policy. Production keeps
  the legacy `true` setting. Isolated controls use `false`.

The config uses `workspace-write`, approval `never`, the bound network policy,
both automatic temp grants disabled, and the main checkout untrusted. Every
project trust entry must be untrusted. Native session directories remain
host-writable without granting model tools access to `CODEX_HOME`.

Preflight scans the runner for untracked and ignored `.codex/config.toml`
layers, including nested layers, and refuses aliases. It verifies protected
files, roots, hook trust entries and config before importing shared runtime
code. The tick repeats preflight after a legacy pull and before model start.
Pinned validation additionally checks the manifest and foundation settings;
the foundation still blocks pinned model start until the child-lock leaf.

Before merge, Anton confirms explicit legacy mode, this dedicated home and
separate auth setup. Removing the live untracked runner config and enabling
these protected roots happen together. This repository does not perform that
operator action.
