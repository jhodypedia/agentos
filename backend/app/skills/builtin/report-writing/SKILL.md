---
slug: report-writing
name: Report Writing
description: Turn technical work into a report an operator can act on.
version: 1.0.0
tags: [writing, reporting]
---

# Writing the report

## 1. Lead with what the reader needs

The first paragraph answers: what was asked, what was done, what was the result.
Someone who reads only that paragraph must not be misled.

## 2. Separate fact from judgement

- Fact: "The endpoint returned 500 for 3 of 40 requests."
- Judgement: "This suggests a race condition."

Label inferences as inferences. Never present an untested guess as a measurement.

## 3. Be specific

Name files and lines, give the command that was run, quote the actual output.
"The tests pass" is weaker than "`pytest` → 159 passed in 18s".

## 4. Report negative results

What you checked and found clean is coverage, and it stops the next person
repeating the work. Say what was not checked and why.

## 5. State limitations plainly

Environment, versions, what was mocked, what was not exercised. If something is
unverified, write "unverified" rather than implying it works.

## 6. Structure

1. Summary
2. What was done
3. Evidence (commands, outputs, measurements)
4. What remains / limitations
5. Recommended next step

Keep it as short as it can be while staying complete. No filler, no restating the
question twice, no claiming success that the evidence does not support.
