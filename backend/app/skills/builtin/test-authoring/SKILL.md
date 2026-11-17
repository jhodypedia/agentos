---
slug: test-authoring
name: Test Authoring
description: Write tests that fail without the fix and pass with it.
version: 1.0.0
tags: [testing, quality]
---

# Writing a test that is worth keeping

## 1. Find the smallest failing case

Reduce the bug to the shortest input that still triggers it. A test built on a
long scenario hides which condition actually mattered.

## 2. Prove it fails first

Run the test against the unfixed code. If it passes, it is not testing the bug —
either the assertion is too weak or you are testing the wrong thing. Fix that
before writing the implementation.

## 3. Assert on behaviour, not implementation

- Good: `assert response.status_code == 409`, `assert retries == 3`.
- Bad: `assert repo.save_called_once_with(session)` when the caller only cares
  that the row exists.

Mock at the boundary you do not control — the network, the clock, another
service. Do not mock the thing under test.

## 4. Cover the edges that actually broke

For each bug, add the case for the input class that caused it, plus the adjacent
boundary: empty, one, many, negative, maximum, unicode, concurrent.

## 5. Keep the suite honest

- No test may depend on another test's side effects.
- No sleeps for timing; use a clock you can advance or an explicit condition.
- A test that can never fail is worse than no test. Delete it or make it assert.

## 6. Report

State the command you ran, the number of tests before and after, and confirm the
new test fails without the change.
