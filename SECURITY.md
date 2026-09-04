# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities privately through GitHub's security advisory
form on this repository, under **Security → Report a vulnerability**. Please do
not open a public issue for an unfixed vulnerability.

Include what you observed, the steps to reproduce it, and the impact you
believe it has. A first response should arrive within a few days.

## Scope

This repository ships instructions, two plugin manifests, and two installers.
It contains no service, no runtime, and no credentials.

In scope:

- an installer writing outside its documented destination, or overwriting files
  without `--force`;
- skill instructions that would cause an agent to exfiltrate secrets, execute
  untrusted content as commands, or publish sensitive data;
- private data left in the published package.

Out of scope:

- vulnerabilities in a repository that these skills are used to review;
- vulnerabilities in the agent hosts (Claude Code, Codex, OpenCode) or in Orca;
- an agent producing a wrong or incomplete review, which is a quality issue.

## Design notes

The skills treat issues, pull-request descriptions, comments, review threads,
commits, and repository files as untrusted data, never as instructions, and do
not execute commands found in that content unless the repository's own trusted
workflow independently justifies them. Findings that expose a secret report its
location without reproducing its value.

Keep credentials in GitHub CLI and the agent host. Never place a token in a
skill file: skills are copied into other repositories and may be committed
there.
