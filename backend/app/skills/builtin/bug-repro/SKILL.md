---
slug: bug-repro
name: Bug Reproduction
description: Turn a vague report into a deterministic, minimal reproduction.
version: 1.0.0
tags: [debugging]
---

# Reproducing a bug

## 1. Capture the report verbatim

Write down exactly what was observed, on which version, with which input. Do not
paraphrase — paraphrase loses the detail that matters.

## 2. Reproduce before theorising

Get the failure to happen once. If it will not reproduce, say so and list what
you tried. A bug you cannot reproduce cannot be fixed with confidence.

## 3. Shrink it

Halve the input, remove dependencies, strip configuration until either the bug
disappears (you just found the relevant factor) or you cannot remove anything
else (you have the minimal case).

## 4. Bisect the cause

- Which commit introduced it? `git bisect` between a known-good and known-bad
  revision.
- Which layer? Add one log or assertion at each boundary and find the first one
  that is already wrong. The bug is between the last correct check and the first
  incorrect one.

## 5. Separate symptom from cause

A null four frames down is a symptom. Keep asking why until the answer is a
decision in the code, not a coincidental value.

## 6. Report

Give: the minimal reproduction (exact command or input), the expected and actual
output, the layer where behaviour first diverges, and the file:line of the cause.
