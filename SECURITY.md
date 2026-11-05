# Security

`sandbox-tool` is a guard against a model's mistakes and tricks. It is **not** a container,
not a security sandbox, and not a boundary against a determined attacker. **It does not
guarantee safety.** For untrusted models or sensitive data, run it inside a container or VM too.

## Known limits

- `os.mkfifo`, `os.mknod` and `os.chroot` raise no audit event.
- `run_python` is POSIX only and is switched off on Windows. macOS is untested.
- Python 3.10 is allowed by `pyproject.toml` and is in the CI test matrix, but the author has not
  run the tests on it yet (the CI workflow has not run at the time of writing).
- The model server you configure can see everything the agent sees.

See "What it does not protect against" in the [README](README.md) for the full list.

## Reporting a vulnerability

If you find a way for an agent to read, write, delete or run something outside the sandbox
folder, please report it privately using GitHub's **"Report a vulnerability"** button on the
repository's Security tab (the maintainer must enable private vulnerability reporting under
Settings -> Code security). Include the command or script, what you expected, and what
happened. Please do not open a public issue for a working escape until it is fixed.
