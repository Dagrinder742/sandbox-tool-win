# Security

`sandbox-tool` is a guard against a model's mistakes and tricks. It is **not** a container,
not a security sandbox, and not a boundary against a determined attacker. **It does not
guarantee safety.** For untrusted models or sensitive data, run it inside a container or VM too.

## Known limits

- `os.mkfifo`, `os.mknod` and `os.chroot` raise no audit event.
- On Windows, `run_python` is limited by a Job Object (memory cap, one process, kill on close, CPU backstop,
  a cap on everything the script writes); there is no per-file size limit there, only that total.
- macOS has not been run on a real Mac. In CI, `run_python` has no memory cap there (no `/proc`) and `sed` fails
  (BSD `sed` has no `--sandbox`).
- The model server you configure can see everything the agent sees.

See "What it does not protect against" in the [README](README.md) for the full list.

## Reporting a vulnerability

If you find a way for an agent to read, write, delete or run something outside the sandbox
folder, please report it privately using GitHub's **"Report a vulnerability"** button on the
repository's Security tab (the maintainer must enable private vulnerability reporting under
Settings -> Code security). Include the command or script, what you expected, and what
happened. Please do not open a public issue for a working escape until it is fixed.
