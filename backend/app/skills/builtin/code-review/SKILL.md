---
slug: code-review
name: Code Review
description: Review a diff or file for correctness, clarity and risk before it is merged.
version: 1.0.0
tags: [review, quality]
---

# Code review

Work through the change in this order. Stop and report as soon as you find a
blocking defect; do not keep reviewing to produce a longer list.

## 1. Understand the intent

State in one sentence what the change is trying to do. If that cannot be
determined from the diff, say so — an unclear intent is itself a review finding.

## 2. Correctness

- Trace the new code with a concrete input, including the empty, negative and
  maximum cases.
- Check every changed branch for an unhandled path.
- Check error paths: does a failure leave state half-written or a resource open?
- Look for off-by-one, mutation of a collection being iterated, and equality
  where identity was meant (or vice versa).

## 3. Contracts and compatibility

- Did a public signature, response shape or database column change? Name every
  caller that must change with it.
- Is a migration reversible, and does it lock a large table?

## 4. Tests

- Does the change come with a test that fails without it?
- Are the tests asserting behaviour, or just exercising lines?

## 5. Report

For each finding give: file and line, severity (blocking / should-fix / nit),
the concrete input or condition that triggers it, and the specific change you
recommend. Never report a finding you have not verified by reading the code.
