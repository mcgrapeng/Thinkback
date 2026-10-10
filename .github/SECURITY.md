# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Thinkback, please report it responsibly.

**Do not** open a public GitHub issue for security vulnerabilities.

Instead, please email: **security@thinkback.dev**

Include in your report:
- Description of the vulnerability
- Steps to reproduce
- Affected versions
- Any known mitigations

We aim to acknowledge reports within **48 hours** and provide a fix timeline within **7 days**.

## Supported Versions

| Version | Supported |
| --- | --- |
| 1.x | Yes |
| < 1.0 | No |

## Scope

This policy covers:
- Authentication / authorization bypass
- Data leakage (API keys, tokens, memory content)
- Remote code execution
- Denial of service via unauthenticated endpoints

## Not in Scope

- Social engineering / phishing
- DoS via authenticated endpoints (use rate limits)
- Vulnerabilities in third-party dependencies without a working exploit in Thinkback
