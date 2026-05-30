---
description: Run the project's tests, parse failures, and propose fixes.
argument-hint: [optional test path / filter]
allowed-tools: Bash, Read, Grep, Glob, Edit
model: opus
---

## Pre-flight: GitNexus first

Before any Grep / Read / Glob, **always**:

1. Read `gitnexus://repo/{name}/context` to confirm the index is fresh.
2. If your task involves a concept → `gitnexus_query({query: "..."})`.
3. If your task is about a specific symbol → `gitnexus_context({name: "..."})`.
4. If your task is "what breaks if I change X" → `gitnexus_impact({name: "...", depth: 2})`.
5. If you are looking at uncommitted changes → `gitnexus_detect_changes({})`.

Only fall back to Grep / Read / Glob for things the graph cannot answer (string search, comments, formatting, raw text).


# /test

Detect the test framework and run tests.

## Workflow

1. Detect framework (in priority order):
   - Node: `package.json` → `scripts.test`. Frameworks: jest, vitest, mocha, playwright.
   - Python: `pyproject.toml` / `pytest.ini` / `setup.cfg` / `tox.ini`. Default: `pytest -q`.
   - Go: `go test ./...`.
   - Rust: `cargo test`.
   - PHP: `composer test` or `./vendor/bin/phpunit`.
   - Java/Kotlin: `mvn test` or `./gradlew test`.
2. If $ARGUMENTS is set, scope the run to it (`pytest path/to/test.py::test_x`, `vitest run path`, `go test ./pkg/x`).
3. Run. Capture output.
4. If failures: invoke the **test-runner** subagent to triage (don't fix here unless trivial — let the subagent decide).
5. Print a summary: framework, command, N passed / failed / skipped, time, link to next steps.

Args: $ARGUMENTS
