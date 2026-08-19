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
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
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
        first = await asyncio.to_thread(self._run_once, code, inputs, output_dir, limits, 1)
        if not first.ok:
            return first
        # A second, independent run: the outputs must agree, or nothing is trusted.
        second_dir = output_dir.parent / (output_dir.name + "_check")
        second = await asyncio.to_thread(self._run_once, code, inputs, second_dir, limits, 2)
        shutil.rmtree(second_dir, ignore_errors=True)
        if not second.ok:
            return second
        if second.output_hash != first.output_hash:
            first.ok = False
            first.error_code = "TOOL_FAILED"
            first.error = "the two runs did not produce the same output — non-deterministic code is not trusted"
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
            output_dir.mkdir(parents=True, exist_ok=True)
            for old in output_dir.iterdir():  # one EMPTY writable directory
                if old.is_dir():
                    shutil.rmtree(old, ignore_errors=True)
                else:
                    old.unlink(missing_ok=True)
            cmd = [*_split(self.runner), str(code_path), str(in_dir), str(output_dir)]
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                   "CLAIMS_SANDBOX_INPUT": str(in_dir), "CLAIMS_SANDBOX_OUTPUT": str(output_dir),
                   "LANG": "C.UTF-8"}
            preexec = _rlimits(limits) if os.name == "posix" else None
            popen_kw: dict = {"cwd": str(output_dir), "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                              "stdin": subprocess.DEVNULL}
            if os.name == "posix":
                popen_kw["preexec_fn"] = preexec
                popen_kw["start_new_session"] = True  # its own process group: killable as a tree
            else:
                popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            proc = subprocess.Popen(cmd, **popen_kw)
            killed, limit_hit = False, ""
            try:
                out, err = proc.communicate(timeout=limits.wall_seconds)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                out, err = proc.communicate()
                killed, limit_hit = True, "wall"
            elapsed = int((time.monotonic() - started) * 1000)
            out_text = redact(out.decode("utf-8", "replace"))
            err_text = redact(err.decode("utf-8", "replace"))
            if len(out) > limits.max_output_bytes:
                _wipe(output_dir)
                return SandboxResult(ok=False, killed=killed, limit_hit="output", error_code="SANDBOX_LIMIT",
                                     stdout=out_text[:2000], stderr=err_text[-2000:], exit_status=proc.returncode,
                                     elapsed_ms=elapsed, error=f"stdout over the {limits.max_output_bytes}-byte limit; nothing kept",
                                     versions=self._versions())
            files: dict[str, str] = {}
            total_out = 0
            for p in sorted(output_dir.rglob("*")):
                if p.is_file():
                    data = p.read_bytes()
                    total_out += len(data)
                    files[str(p.relative_to(output_dir))] = _sha(data)
            if total_out > limits.max_output_bytes:
                _wipe(output_dir)
                return SandboxResult(ok=False, killed=killed, limit_hit="output", error_code="SANDBOX_LIMIT",
                                     exit_status=proc.returncode, elapsed_ms=elapsed,
                                     error=f"output files total {total_out} bytes, over the limit; nothing kept",
                                     versions=self._versions())
            if killed:
                _wipe(output_dir)
                return SandboxResult(ok=False, killed=True, limit_hit=limit_hit, error_code="SANDBOX_LIMIT",
                                     stdout=out_text[-2000:], stderr=err_text[-2000:], exit_status=proc.returncode,
                                     elapsed_ms=elapsed, error=f"killed at the {limits.wall_seconds}s wall-time limit; nothing kept",
                                     versions=self._versions())
            if proc.returncode != 0:
                hit = _limit_from_signal(proc.returncode)
                _wipe(output_dir)
                return SandboxResult(ok=False, killed=hit != "", limit_hit=hit, exit_status=proc.returncode,
                                     error_code="SANDBOX_LIMIT" if hit else "TOOL_FAILED",
                                     stdout=out_text[-2000:], stderr=err_text[-2000:], elapsed_ms=elapsed,
                                     error=(f"stopped at the {hit} limit" if hit else f"exit status {proc.returncode}: {err_text.strip()[-300:] or 'no message'}"),
                                     versions=self._versions())
            # hashed BEFORE redaction, so two runs whose raw output differs
            # only in a redacted value are still told apart
            output_hash = _sha(("\n".join(f"{k}:{v}" for k, v in sorted(files.items())).encode() + b"\n" + out))
            return SandboxResult(ok=True, stdout=out_text[-20000:], stderr=err_text[-2000:], exit_status=0,
                                 output_files=files, output_hash=output_hash, elapsed_ms=elapsed,
                                 versions=self._versions())
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _versions(self) -> dict[str, str]:
        return {"python": sys.version.split()[0], "runner": self.runner[:80]}


def _split(cmd: str) -> list[str]:
    import shlex

    return shlex.split(cmd, posix=os.name == "posix")


def _rlimits(limits: SandboxLimits):
    def apply():
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 1))
            mem = limits.memory_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            except (ValueError, OSError):
                pass  # some kernels refuse; the runner's own limit applies
            resource.setrlimit(resource.RLIMIT_NOFILE, (limits.max_open_files, limits.max_open_files))
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (limits.max_processes, limits.max_processes))
            except (ValueError, OSError):
                pass
        except Exception:
            pass
    return apply


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


def _limit_from_signal(returncode: int) -> str:
    if os.name != "posix" or returncode >= 0:
        return ""
    sig = -returncode
    if sig == getattr(signal, "SIGXCPU", -1):
        return "cpu"
    if sig == signal.SIGKILL:
        return "memory"  # the OOM killer / rlimit AS ends in a kill
    return ""


def _wipe(output_dir: Path) -> None:
    """Nothing produced by a run that failed or hit a limit is kept."""
    for p in output_dir.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def production_sandbox():
    """The adapter the harness gets when CLAIMS_PYTHON_SANDBOX is on."""
    from ... import config

    if not (config.CLAIMS_SANDBOX_RUNNER or "").strip():
        return UnavailableSandbox()
    return RunnerSandbox(config.CLAIMS_SANDBOX_RUNNER, isolated=config.CLAIMS_SANDBOX_ISOLATED)
