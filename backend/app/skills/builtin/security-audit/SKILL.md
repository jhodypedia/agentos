---
slug: security-audit
name: Security Audit
description: Audit code or infrastructure for exploitable vulnerabilities within an authorised scope.
version: 1.0.0
tags: [security, audit]
---

# Security audit

Only ever audit systems you have explicit written authorisation to test. If the
scope is ambiguous, stop and report the ambiguity instead of guessing.

## 1. Establish the scope

Write down: the target(s), the allowed test types, and what is out of scope.
Reference the authorisation (program, engagement letter, or ticket). Anything
outside it is read-only analysis only.

## 2. Map the attack surface

- Entry points: HTTP routes, queue consumers, CLI arguments, file imports.
- Trust boundaries: where does untrusted data cross into a privileged context?
- Secrets: how are credentials loaded, and can they reach logs or error pages?

## 3. Test the classic classes in order of likely impact

1. Injection — SQL, command, template, LDAP, header.
2. Broken authorisation — missing object-level checks, IDOR, role confusion.
3. Authentication — session handling, token lifetime, password reset flows.
4. Deserialisation and parser abuse.
5. SSRF and unrestricted outbound requests.
6. Path traversal in upload or archive handling.
7. Secret exposure in responses, logs, or repository history.

## 4. Validate before reporting

A finding is only a finding if you can state the exact request or input, the
observed response, and why that is a security consequence rather than intended
behaviour. Reproduce it a second time. If you cannot reproduce it, report it as
a hypothesis, clearly labelled.

## 5. Report

Per finding: title, severity with justification, affected component, reproduction
steps, impact, and remediation. Include negative results — "checked IDOR on
/orders/{id}, authorisation is enforced" is valuable evidence of coverage.
