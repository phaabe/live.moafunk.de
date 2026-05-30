---
name: test-strategy
description: Use whenever writing, planning, or critiquing tests. Covers the test pyramid, mocking patterns, fixture design, coverage targets, and how to choose between unit / integration / e2e for a given scenario.
---

# Test Strategy Skill

## The pyramid (target ratio)

```
       /\        e2e        ~5%   slow, brittle, expensive
      /  \       integration ~20%  medium, mostly stable
     /____\      unit       ~75%  fast, reliable, cheap
```

If your suite is inverted (most tests are e2e), the suite is slow, flaky, and expensive — push tests downward.

## What to test where

| Layer | Goal | Examples |
|-------|------|----------|
| Unit | One function / class, no I/O | pure functions, parsers, validators, utilities |
| Integration | Multiple modules together with **real** boundaries replaced by **fakes / sqlite / docker** | DB queries against testcontainer, HTTP handlers with in-mem server |
| Contract | Verify our API matches consumers' expectations | Pact, OpenAPI conformance |
| e2e | A user journey end-to-end through the real system | login → create → view → delete |

## Mocking

- **Mock the boundary, not the collaborator.** Mock the network call, not your repository class.
- Prefer **fakes** (in-memory implementations) and **stubs** (return canned data) over **mocks** (verify call shape). Mocks couple tests to implementation.
- Never mock what you don't own (third-party libs). Wrap them, then fake the wrapper.
- One mock per test if possible. Many mocks = the unit under test does too much.

## Fixtures

- **Co-locate** fixtures with tests. Avoid sprawling `conftest.py` / `setup.ts` files.
- **Build, don't share.** Use builders / factories so each test owns its data; shared mutable fixtures cause flakiness.
- **Realistic data.** Use Faker / Mimesis / fishery to get plausible values.

## Coverage

- **Targets**: line ≥ 80%, branch ≥ 70% for new code. Below 70% is a smell; chasing 100% is waste.
- Coverage measures what was *executed*, not what was *verified*. Don't confuse "covered" with "tested".
- Mutation testing (mutmut, Stryker, PIT) is a stronger signal — run it occasionally on critical paths.

## Anti-patterns to call out

- **Tests that test the mock.** If the test fails, it tells you nothing about real behaviour.
- **Time-bomb tests.** `assert datetime.now() < ...` — fix with injected clocks.
- **Sequencing dependencies.** `test_b` requires `test_a` to have run. Fix ordering, isolate state.
- **Snapshot abuse.** Snapshotting whole UI trees ⇒ every change is "approved" without review.
- **Test-only branches in production code.** `if NODE_ENV === 'test'` in business logic — refactor to inject the dependency instead.

## What "good" looks like for one test

```py
def test_<unit>_<scenario>_<expected>():
    # Arrange
    sut = build_sut(...)

    # Act
    result = sut.do_thing(...)

    # Assert
    assert result == expected
```

Three blocks, no surprises, one assertion (or one logical assertion split across lines).
