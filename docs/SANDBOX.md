# Claims Python Sandbox (hardening H8)

**Status:** shipped DISABLED by default. `run_python` is absent from the
agent's tool allowlist unless three things hold at once:

1. `CLAIMS_PYTHON_SANDBOX=1` (feature switch — the tool exists);
2. `CLAIMS_SANDBOX_RUNNER` names an OS-level isolation runner (see the contract
   below);
3. `CLAIMS_SANDBOX_ISOLATED=1` — the operator's explicit assertion, after the
   feasibility spike, that the runner meets the production requirements.

Without all three the harness answers `TOOL_UNAVAILABLE`, the investigator
raises the `TOOL_UNAVAILABLE` Flag only when the investigation genuinely did
not converge without it, and every deterministic tool (workbook, document,
search, calculator, table comparison) keeps working. Nothing else in H0–H12
depends on this feature.

## Why a runner, not a Python filter

Python AST filtering, import allowlists and `resource` limits are not a
security boundary. The adapter (`backend/app/claims/tools/sandbox.py`,
`RunnerSandbox`) therefore hands execution to a runner the operator provides
— a container, `bubblewrap`/`firejail`, or a Windows AppContainer / Job-Object
wrapper — and only ADDS belt-and-braces controls of its own:

- environment cleared (no API keys, no SharePoint tokens, no proxy secrets);
- inputs copied read-only into a scratch `in/` directory — the copies AND the
  directories holding them (`0555`), so a script cannot add, rename or delete
  anything beside them; one EMPTY writable `out/` directory, which must hold
  nothing but regular files (a symlink, a special file or a path escaping
  `out/` refuses the whole run, so the host never follows a link into a file
  of its own and hands the model its hash); the snapshot itself is never
  mounted writable;
- wall-time kill of the whole process group; CPU / address-space / open-file /
  process / file-size rlimits where the OS has them; stdout and output-file
  byte caps — stdout is STREAMED into a capped buffer (never buffered whole
  on the host) and a child past the cap is killed at once; output files are
  sized with `stat` before any byte is read and hashed by streaming; input
  byte cap;
- a cancelled investigation kills a running child (the harness's `cancel()`
  reaches the adapter's live process handles) and no further child starts;
- every successful script is run a SECOND time and its output hash compared;
  a difference fails the call (`TOOL_FAILED`, non-deterministic);
- nothing produced by a run that failed or hit a limit is kept — including
  the FIRST run's output when the second run fails or disagrees;
- everything the adapter returns — code hash, runner/library versions,
  stdout and stderr redacted of account-number and token-looking strings,
  output-file hashes, exit status, limit hit — is redacted BEFORE it reaches
  the model or any record. What is PERSISTED of it (2026-08-19, corrected
  against the code) is the tool-execution row: the tool name, elapsed ms, the
  input hashes (the code hash and the input artifacts' hashes), the output
  hash, the error code and a short redacted note. The stdout/stderr text,
  the versions, the exit status and the limit hit are **not** columns of
  `ClaimToolExecution` — they live in the call's result (the model sees
  stdout) and, when the call failed, in the note and the Flag text. Storing
  the streams themselves would need a schema migration and is not done; a
  limit becomes the `SANDBOX_LIMIT` Flag on the run, never a run failure.

## Runner contract

`CLAIMS_SANDBOX_RUNNER` is a command line (shell-split) that is invoked as:

```text
<runner ...> <code.py> <input dir> <output dir>
```

with the working directory set to `<output dir>` and the environment reduced
to `PATH`, `PYTHONIOENCODING`, `PYTHONDONTWRITEBYTECODE`, `LANG`,
`CLAIMS_SANDBOX_INPUT` and `CLAIMS_SANDBOX_OUTPUT`. The runner must:

- execute `code.py` with an interpreter that has ONLY the allowlisted data
  libraries (openpyxl, pandas, pymupdf, Pillow, decimal — no network clients);
- mount `<input dir>` read-only and `<output dir>` as the only writable path;
- deny network, host filesystem, subprocess creation beyond its own tree,
  inherited credentials, browser sessions and environment secrets;
- give each execution an ephemeral identity (fresh container / user);
- honour or tighten the limits the adapter passes (30 s wall, 20 s CPU,
  512 MB, 1 process, 64 files, 50 MB in, 2 MB out — `SandboxLimits`);
- **die with its client.** The adapter kills the process GROUP it started
  (and, on POSIX, applies the rlimits in that group through an exec'd
  launcher rather than `preexec_fn`, which is unsafe from the server's
  worker threads). A runner that only *talks to* a daemon — `docker run`
  against the Docker daemon is the obvious one — leaves the real workload
  alive when its client is killed: the container keeps running after the
  wall-time kill and after a cancel. Such a runner MUST pass its own
  hard timeout to the engine (`--stop-timeout`, an in-image `timeout`, a
  `--rm` job with a deadline) so the workload dies on its own; the
  adapter's tree kill cannot do it for you. This is a requirement on the
  operator's wrapper, not a control the adapter provides;
- exit non-zero when it cannot provide any of the above.

Example (Linux, container):

```text
CLAIMS_SANDBOX_RUNNER="docker run --rm --network none --read-only --cap-drop ALL --pids-limit 4 --memory 512m --cpus 1 -v INPUT:/in:ro -v OUTPUT:/out sandbox-image python"
```

(the operator's wrapper script substitutes the two directories into the
mounts; the adapter passes them positionally).

## Windows enterprise feasibility spike — owner action

Not done in this rollout; H8 ships disabled. The spike must show, on the
enterprise Windows host, one runner that meets every requirement above and
passes `backend/tests/test_claims_sandbox.py` (the mechanics) plus a manual
scenario J check:

- [ ] a script that opens a socket fails (no network);
- [ ] a script that reads a path outside `<input dir>` fails (no host FS);
- [ ] a script that spawns a process fails or is contained (process limit);
- [ ] a script that prints `os.environ` shows no secrets;
- [ ] a busy loop is killed at the wall limit and leaves no output;
- [ ] a 1 GB allocation is killed at the memory limit;
- [ ] two runs of a deterministic script produce the same output hash.

Candidate mechanisms: Windows Sandbox / AppContainer via a small launcher,
Docker Desktop with WSL2 (if permitted), or a remote job runner. If none is
permitted, leave `CLAIMS_PYTHON_SANDBOX` off — that is the accepted outcome,
not a blocker.

## Tests

`backend/tests/test_claims_sandbox.py` exercises the adapter's own controls
with a plain-Python "runner" declared isolated FOR THE TEST ONLY: cleared
environment, read-only inputs, empty output dir, wall-time kill of the tree,
output/input caps, non-determinism refusal, redaction, `SANDBOX_LIMIT` on the
harness and the investigator, and the off switch. It is not evidence that a
bare interpreter isolates — it is evidence that the adapter behaves once a
real runner is behind it.
