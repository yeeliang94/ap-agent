"""SandboxPort adapters (hardening H8): running model-written Python
OUTSIDE the FastAPI process, read-only over the snapshot, one empty
writable output directory, no network, hard limits, killed as a tree on
timeout, audited, run twice so its output is shown to be deterministic.

Three adapters behind one port (tools/contracts.SandboxPort):

  UnavailableSandbox  the default: never runs anything and says why.
  RunnerSandbox       the production adapter. It does NOT claim to isolate
                      by itself — Python AST filtering or resource limits
                      are not a security boundary — it hands the execution
                      to an OS-level ISOLATION RUNNER the operator declares
                      (CLAIMS_SANDBOX_RUNNER: a command such as a container,
                      firejail/bubblewrap or Windows AppContainer wrapper
                      that takes <code.py> <input dir> <output dir>) and
                      adds belt-and-braces controls of its own: a cleared
                      environment (no secrets), inputs copied read-only,
                      wall/CPU/memory/file/process rlimits where the OS has
                      them, output size caps, kill of the whole process
                      group on timeout, and a second run to compare output
                      hashes. available() is True only when the runner is
                      declared AND the operator has asserted isolation
                      (CLAIMS_SANDBOX_ISOLATED=1) — see docs/SANDBOX.md.
  InMemorySandbox     the test double: scripted results, every call recorded.

If the enterprise Windows host cannot provide OS-level isolation, run_python
ships disabled (TOOL_UNAVAILABLE) and every other tool still works.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .contracts import SandboxLimits, SandboxResult

# Redaction of what leaves the sandbox record: long digit runs (account
# numbers), bearer/API-key-looking tokens, signed URLs.
_REDACT = [
    (re.compile(r"\b\d{10,}\b"), "<number>"),
    (re.compile(r"\b(sk|key|token|bearer)[-_ ]?[A-Za-z0-9\-_]{12,}", re.IGNORECASE), "<token>"),
    (re.compile(r"https?://\S*(sig|signature|token|sv=|se=)=?\S*", re.IGNORECASE), "<signed-url>"),
]


def redact(text: str) -> str:
    for pattern, sub in _REDACT:
        text = pattern.sub(sub, text or "")
    return text


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# What a child's stderr says when it died of memory: Python's MemoryError
# under RLIMIT_AS, the kernel / a container runtime's OOM message. Only
# with this evidence is a kill reported as the MEMORY limit; a bare
# SIGKILL is reported as "killed" (the runner, the operator, or a container
# OOM the runtime did not name) — never guessed to be memory.
# NOT case-insensitive over the whole pattern, and "OOM" is anchored on word
# boundaries: an ignore-case `OOM` matches the middle of "boom", "room" and
# "zoom", which would turn an ordinary error message into a memory verdict.
_MEMORY_EVIDENCE = re.compile(
    r"MemoryError"
    r"|[Cc]annot allocate memory"
    r"|[Oo]ut of [Mm]emory"
    r"|\bOOM\b"
    r"|\boom[-_ ]?kill"
    r"|std::bad_alloc")

# The rlimit launcher: a tiny Python -I -S program that applies the limits
# to ITSELF and then execs the runner command in place (same pid, same
# process group). This replaces preexec_fn — which runs between fork and
# exec inside a multi-threaded server process (the adapter runs in a
# worker thread) and is documented as unsafe there — with an ordinary
# exec: the limits are set by the child, in the child, before the runner
# starts. Errors applying a limit the kernel refuses are ignored (the
# runner's own limit applies); an exec failure exits 127 with a message.
_LAUNCHER = r"""
import json, os, sys
lim = json.loads(sys.argv[1]); argv = sys.argv[2:]
try:
    import resource
    def _set(name, soft, hard=None):
        r = getattr(resource, name, None)
        if r is None:
            return
        try:
            resource.setrlimit(r, (soft, soft if hard is None else hard))
        except (ValueError, OSError):
            pass
    _set("RLIMIT_CPU", lim["cpu"], lim["cpu"] + 1)
    _set("RLIMIT_AS", lim["mem"])
    _set("RLIMIT_NOFILE", lim["nofile"])
    _set("RLIMIT_FSIZE", lim["fsize"])
    _set("RLIMIT_NPROC", lim["nproc"])
except Exception:
    pass
try:
    os.execvp(argv[0], argv)
except OSError as exc:
    sys.stderr.write("sandbox launcher: cannot start the runner: %s\n" % exc)
    sys.exit(127)
"""


class UnavailableSandbox:
    """Never runs anything; says why."""

    def __init__(self, why: str = "no OS-level isolation runner is declared on this host "
                                  "(CLAIMS_SANDBOX_RUNNER unset or CLAIMS_SANDBOX_ISOLATED not asserted)"):
        self.why = why

    def available(self) -> tuple[bool, str]:
        return False, self.why

    async def run(self, code: str, inputs: dict[str, Path], output_dir: Path, limits: SandboxLimits) -> SandboxResult:
        return SandboxResult(ok=False, error_code="TOOL_UNAVAILABLE", error=self.why)


class InMemorySandbox:
    """The test double: answers scripted results, records every call."""

    def __init__(self, results: list[SandboxResult] | None = None, isolated: bool = True):
        self.results = list(results or [])
        self.isolated = isolated
        self.calls: list[dict] = []

    def available(self) -> tuple[bool, str]:
        return (True, "") if self.isolated else (False, "test sandbox marked not isolated")

    async def run(self, code: str, inputs: dict[str, Path], output_dir: Path, limits: SandboxLimits) -> SandboxResult:
        self.calls.append({"code": code, "inputs": dict(inputs), "output_dir": str(output_dir), "limits": limits.model_dump()})
        if self.results:
            return self.results.pop(0)
        return SandboxResult(ok=True, stdout="", exit_status=0, output_hash=_sha(code.encode()),
                             versions={"python": "fake"})


class RunnerSandbox:
    """The production adapter — see the module docstring."""

    def __init__(self, runner: str, isolated: bool, python: str | None = None):
        # runner: the command (shell-split) that receives <code.py> <input dir> <output dir>
        self.runner = runner
        self.isolated = isolated
        self.python = python or sys.executable
        self._live: set = set()  # running child processes, so a cancel can kill them
        self._cancelled = False

    def cancel(self) -> int:
        """Kill every running child tree now (the harness calls this when
        the investigation is cancelled) and refuse to start more."""
        self._cancelled = True
        n = 0
        for proc in list(self._live):
            _kill_tree(proc)
            n += 1
        return n

    def available(self) -> tuple[bool, str]:
        if not (self.runner or "").strip():
            return False, "no isolation runner declared (CLAIMS_SANDBOX_RUNNER)"
        if not self.isolated:
            return False, "the operator has not asserted OS-level isolation (CLAIMS_SANDBOX_ISOLATED=1)"
        return True, ""

    async def run(self, code: str, inputs: dict[str, Path], output_dir: Path, limits: SandboxLimits) -> SandboxResult:
        ok, why = self.available()
        if not ok:
            return SandboxResult(ok=False, error_code="TOOL_UNAVAILABLE", error=why)
        total_in = sum(p.stat().st_size for p in inputs.values() if p.is_file())
        if total_in > limits.max_input_bytes:
            return SandboxResult(ok=False, error_code="SANDBOX_LIMIT", limit_hit="input",
                                 error=f"inputs total {total_in} bytes, over the {limits.max_input_bytes} limit")
        if self._cancelled:
            return SandboxResult(ok=False, error_code="TOOL_FAILED", error="the investigation was cancelled")
        first = await asyncio.to_thread(self._run_once, code, inputs, output_dir, limits, 1)
        if not first.ok:
            return first
        # A second, independent run: the outputs must agree, or nothing is
        # trusted — and nothing of EITHER run is kept when they do not.
        second_dir = output_dir.parent / (output_dir.name + "_check")
        second = await asyncio.to_thread(self._run_once, code, inputs, second_dir, limits, 2)
        shutil.rmtree(second_dir, ignore_errors=True)
        if not second.ok:
            _wipe(output_dir)
            return second
        if second.output_hash != first.output_hash:
            _wipe(output_dir)
            first.ok = False
            first.output_files = {}
            first.error_code = "TOOL_FAILED"
            first.error = "the two runs did not produce the same output — non-deterministic code is not trusted; nothing kept"
        return first

    def _run_once(self, code: str, inputs: dict[str, Path], output_dir: Path, limits: SandboxLimits, attempt: int) -> SandboxResult:
        started = time.monotonic()
        work = Path(tempfile.mkdtemp(prefix=f"claims-sbx-{attempt}-"))
        try:
            code_path = work / "code.py"
            code_path.write_text(code, encoding="utf-8")
            in_dir = work / "in"
            in_dir.mkdir()
            for rel, src in inputs.items():
                dst = in_dir / rel.replace("\\", "/")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                dst.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # read-only copy
            # the input TREE is read-only too (0555 directories): the script
            # cannot add, rename or delete anything beside the copies
            _chmod_dirs(in_dir, 0o555)
            output_dir.mkdir(parents=True, exist_ok=True)
            _wipe(output_dir)  # one EMPTY writable directory
            runner_cmd = [*_split(self.runner), str(code_path), str(in_dir), str(output_dir)]
            cmd = _with_rlimits(self.python, runner_cmd, limits) if os.name == "posix" else runner_cmd
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                   "CLAIMS_SANDBOX_INPUT": str(in_dir), "CLAIMS_SANDBOX_OUTPUT": str(output_dir),
                   "LANG": "C.UTF-8"}
            popen_kw: dict = {"cwd": str(output_dir), "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                              "stdin": subprocess.DEVNULL}
            if os.name == "posix":
                # its own session / process group: killable as a tree, and
                # no preexec_fn (the rlimit launcher sets the limits in the child)
                popen_kw["start_new_session"] = True
            else:
                popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if self._cancelled:
                return SandboxResult(ok=False, error_code="TOOL_FAILED", error="the investigation was cancelled")
            proc = subprocess.Popen(cmd, **popen_kw)
            self._live.add(proc)
            killed, limit_hit = False, ""
            # stdout/stderr are STREAMED into capped buffers by reader
            # threads — never buffered whole: a child that floods its pipe
            # cannot fill the host's memory, and stdout past the cap kills
            # the tree at once instead of being read to the end.
            out_buf, err_buf = bytearray(), bytearray()
            out_over: list[bool] = []
            t_out = threading.Thread(target=_drain, args=(proc.stdout, limits.max_output_bytes + 1, out_buf, out_over, proc), daemon=True)
            t_err = threading.Thread(target=_drain, args=(proc.stderr, 64 * 1024, err_buf, [], None), daemon=True)
            t_out.start()
            t_err.start()
            try:
                proc.wait(timeout=limits.wall_seconds)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                proc.wait()
                killed, limit_hit = True, "wall"
            finally:
                self._live.discard(proc)
            t_out.join(timeout=5)
            t_err.join(timeout=5)
            out, err = bytes(out_buf), bytes(err_buf)
            elapsed = int((time.monotonic() - started) * 1000)
            out_text = redact(out.decode("utf-8", "replace"))
            err_text = redact(err.decode("utf-8", "replace"))
            if self._cancelled:
                _wipe(output_dir)
                return SandboxResult(ok=False, killed=True, error_code="TOOL_FAILED", exit_status=proc.returncode,
                                     elapsed_ms=elapsed, error="the investigation was cancelled; nothing kept",
                                     versions=self._versions())
            if out_over or len(out) > limits.max_output_bytes:
                _wipe(output_dir)
                return SandboxResult(ok=False, killed=True, limit_hit="output", error_code="SANDBOX_LIMIT",
                                     stdout=out_text[:2000], stderr=err_text[-2000:], exit_status=proc.returncode,
                                     elapsed_ms=elapsed, error=f"stdout over the {limits.max_output_bytes}-byte limit; nothing kept",
                                     versions=self._versions())
            # Output files: symlinks are NEVER followed (a link to a host
            # file would otherwise be stat'ed, hashed and its hash handed to
            # the model) — any symlink, special file or path that resolves
            # outside out/ refuses the whole run; sizes come from lstat
            # BEFORE any byte is read; over the cap, nothing is opened;
            # hashes are streamed through O_NOFOLLOW descriptors.
            paths, bad = _collect_outputs(output_dir)
            if bad:
                _wipe(output_dir)
                return SandboxResult(ok=False, killed=killed, exit_status=proc.returncode, error_code="TOOL_FAILED",
                                     stdout=out_text[-2000:], stderr=err_text[-2000:], elapsed_ms=elapsed,
                                     error=f"the output directory holds {bad}; only regular files are accepted; nothing kept",
                                     versions=self._versions())
            total_out = sum(os.lstat(p).st_size for p in paths)
            if total_out > limits.max_output_bytes:
                _wipe(output_dir)
                return SandboxResult(ok=False, killed=killed, limit_hit="output", error_code="SANDBOX_LIMIT",
                                     exit_status=proc.returncode, elapsed_ms=elapsed,
                                     error=f"output files total {total_out} bytes, over the limit; nothing kept",
                                     versions=self._versions())
            files: dict[str, str] = {str(p.relative_to(output_dir)): _sha_file(p) for p in paths}
            if killed:
                _wipe(output_dir)
                return SandboxResult(ok=False, killed=True, limit_hit=limit_hit, error_code="SANDBOX_LIMIT",
                                     stdout=out_text[-2000:], stderr=err_text[-2000:], exit_status=proc.returncode,
                                     elapsed_ms=elapsed, error=f"killed at the {limits.wall_seconds}s wall-time limit; nothing kept",
                                     versions=self._versions())
            if proc.returncode != 0:
                hit, was_killed = _limit_from_exit(proc.returncode, err_text)
                _wipe(output_dir)
                if hit == "killed":
                    why = f"killed (signal {abs(proc.returncode) if proc.returncode < 0 else proc.returncode - 128}) " \
                          "with no limit evidence — the runner, the host or an unnamed container limit stopped it; nothing kept"
                elif hit:
                    why = f"stopped at the {hit} limit; nothing kept"
                else:
                    why = f"exit status {proc.returncode}: {err_text.strip()[-300:] or 'no message'}"
                return SandboxResult(ok=False, killed=was_killed, limit_hit=hit if hit != "killed" else "",
                                     exit_status=proc.returncode,
                                     error_code="SANDBOX_LIMIT" if hit and hit != "killed" else "TOOL_FAILED",
                                     stdout=out_text[-2000:], stderr=err_text[-2000:], elapsed_ms=elapsed,
                                     error=why, versions=self._versions())
            # hashed BEFORE redaction, so two runs whose raw output differs
            # only in a redacted value are still told apart
            output_hash = _sha(("\n".join(f"{k}:{v}" for k, v in sorted(files.items())).encode() + b"\n" + out))
            return SandboxResult(ok=True, stdout=out_text[-20000:], stderr=err_text[-2000:], exit_status=0,
                                 output_files=files, output_hash=output_hash, elapsed_ms=elapsed,
                                 versions=self._versions())
        finally:
            _chmod_dirs(work, 0o700)  # the read-only input tree must be writable again to be removed
            shutil.rmtree(work, ignore_errors=True)

    def _versions(self) -> dict[str, str]:
        out = {"python": sys.version.split()[0], "runner": self.runner[:80]}
        for lib in ("openpyxl", "pymupdf", "PIL", "pandas"):
            try:
                out[lib] = __import__(lib).__version__
            except Exception:
                pass
        return out


def _drain(stream, cap: int, sink: bytearray, over: list, proc) -> None:
    """Read a child's pipe to EOF in chunks, keeping at most `cap` bytes.
    Past the cap: mark `over`, stop keeping, and (when `proc` is given) kill
    the tree — the pipe is still drained so the child never blocks on it."""
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            room = cap - len(sink)
            if room > 0:
                sink.extend(chunk[:room])
            if len(chunk) > room:
                if not over:
                    over.append(True)
                    if proc is not None:
                        _kill_tree(proc)
    except Exception:
        return
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _sha_file(path: Path) -> str:
    """Streamed sha256 through a descriptor opened O_NOFOLLOW (where the OS
    has it): a file swapped for a symlink between the walk and the read is
    refused by the kernel, not followed."""
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _split(cmd: str) -> list[str]:
    import shlex

    return shlex.split(cmd, posix=os.name == "posix")


def _with_rlimits(python: str, runner_cmd: list[str], limits: SandboxLimits) -> list[str]:
    """The runner command wrapped in the rlimit launcher (POSIX): the
    launcher applies CPU / address-space / open-file / file-size / process
    limits to itself, then execs the runner in place."""
    lim = {"cpu": limits.cpu_seconds, "mem": limits.memory_mb * 1024 * 1024, "nofile": limits.max_open_files,
           # no single output file grows far past the cap: the kernel stops
           # the write at cap+1 (EFBIG / SIGXFSZ), and a file of cap+1 bytes
           # then fails the total-size check — a file cut off EXACTLY at
           # the cap would pass as if complete
           "fsize": limits.max_output_bytes + 1, "nproc": limits.max_processes}
    return [python, "-I", "-S", "-c", _LAUNCHER, json.dumps(lim), *runner_cmd]


def _chmod_dirs(root: Path, mode: int) -> None:
    """Every directory under root (root included) gets `mode`; best effort."""
    dirs = [root]
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        dirs.extend(Path(dirpath) / d for d in dirnames)
    for d in reversed(dirs) if mode & 0o200 == 0 else dirs:
        try:
            os.chmod(d, mode)
        except OSError:
            pass


def _collect_outputs(output_dir: Path) -> tuple[list[Path], str]:
    """The regular files under out/, without following anything. Returns
    (paths, problem): `problem` names the first symlink / special file /
    escaping path found, and then the run is refused."""
    root = output_dir.resolve()
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(output_dir, followlinks=False):
        here = Path(dirpath)
        for d in list(dirnames):
            if (here / d).is_symlink():
                return [], f"a symbolic link ({(here / d).relative_to(output_dir)})"
        for name in filenames:
            p = here / name
            if p.is_symlink():
                return [], f"a symbolic link ({p.relative_to(output_dir)})"
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                return [], f"a special file ({p.relative_to(output_dir)})"
            if root not in p.resolve().parents:
                return [], f"a path outside the output directory ({p.relative_to(output_dir)})"
            files.append(p)
    return sorted(files), ""


def _kill_tree(proc) -> None:
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _limit_from_exit(returncode: int, stderr: str = "") -> tuple[str, bool]:
    """(limit, killed) from how the child ended. A negative code is the
    signal that ended it; 128+n is the same signal forwarded by a runner
    (a container client, a shell). SIGXCPU → cpu, SIGXFSZ → output, a
    MemoryError / OOM message in stderr → memory; SIGKILL with no such
    evidence → "killed" (reported as such, NOT assumed to be memory)."""
    if returncode == 0:
        return "", False
    sig = 0
    if returncode < 0:
        sig = -returncode
    elif returncode > 128:
        sig = returncode - 128
    if sig and sig == getattr(signal, "SIGXCPU", -1):
        return "cpu", True
    if sig and sig == getattr(signal, "SIGXFSZ", -1):
        return "output", True
    if _MEMORY_EVIDENCE.search(stderr or ""):
        return "memory", sig == getattr(signal, "SIGKILL", 9)
    if sig == getattr(signal, "SIGKILL", 9):
        return "killed", True
    return "", False


def _wipe(output_dir: Path) -> None:
    """Nothing produced by a run that failed or hit a limit is kept —
    symlinks are unlinked, never followed into."""
    for p in output_dir.iterdir():
        if p.is_symlink() or not p.is_dir():
            p.unlink(missing_ok=True)
        else:
            shutil.rmtree(p, ignore_errors=True)


def production_sandbox():
    """The adapter the harness gets when CLAIMS_PYTHON_SANDBOX is on."""
    from ... import config

    if not (config.CLAIMS_SANDBOX_RUNNER or "").strip():
        return UnavailableSandbox()
    return RunnerSandbox(config.CLAIMS_SANDBOX_RUNNER, isolated=config.CLAIMS_SANDBOX_ISOLATED)
