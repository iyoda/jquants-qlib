# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository instead.

Private vulnerability reporting is a repository setting. The maintainer must enable it under
**Settings → Code security → Private vulnerability reporting** before the link above accepts
reports; see GitHub's guide on
[configuring private vulnerability reporting for a repository](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository).
Until it is enabled, the "Report a vulnerability" button is absent from the Security tab. In that
case, open an issue with the
[Security report (contact only)](https://github.com/iyoda/jquants-qlib/issues/new?template=security_report.md)
template (`.github/ISSUE_TEMPLATE/security_report.md`, title prefix `[security]`). State only that
you have a security report and how to reach you; do **not** include details in the public issue.
The maintainer then opens a private channel (for example a draft security advisory) to receive them.

Whichever channel you use, we aim to acknowledge reports within 7 days, provide an initial
assessment within 14 days, and send status updates at least every 14 days while investigating.
These are best-effort targets for a volunteer-maintained project, not service guarantees.

Conduct concerns are handled through the same private channel; see the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Credentials

This project never stores J-Quants credentials in the repository. They are read
from the `JQUANTS_API_KEY` / `JQUANTS_REFRESH_TOKEN` environment variables or,
on macOS, from the Keychain. If you believe a credential has leaked, revoke it
in the J-Quants dashboard first, then report the path that leaked it.

## Supported versions

| Version | Security fixes |
| --- | --- |
| Latest released minor series (currently `0.1.x`), latest patch | Supported |
| Older minor series or superseded patches | Upgrade to the latest supported release |
| Unreleased development versions | No release support guarantee |

## Scope

Security reports include credential exposure through configuration, environment or Keychain
handling, logs, and exception output, plus unsafe dataset publication path handling such as
unintended writes or symlink replacement outside the selected destination. Include minimal
reproduction steps with synthetic inputs, affected versions, and expected versus actual behavior.
Do not attach API keys, real market data, or unredacted authenticated responses.

We coordinate disclosure and a fix or mitigation with the reporter. Ordinary data-quality bugs
can use the public bug template unless they also expose a security issue.
