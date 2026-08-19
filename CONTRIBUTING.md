# Contributing

`agent-sync` is a single Bash CLI plus documentation and shell tests. Keep changes small, portable, and safe around user data.

## Checks

Run these before publishing changes:

```bash
bash -n bin/agent-sync install.sh tests/run.sh
tests/run.sh
shellcheck bin/agent-sync install.sh tests/run.sh
```

The GitHub Actions workflow runs ShellCheck and the test suite on pull requests.

## Safety Notes

- Do not commit real agent config, project memory, credentials, or local machine paths.
- Test with a temporary `HOME`; never point tests at your real `~/.claude`, `~/.tclaude`, or `~/.codex`.
- Prefer no-clobber copies and backups before replacing local files.
- Keep new synced paths explicit in the allowlist-style `.gitignore`.
