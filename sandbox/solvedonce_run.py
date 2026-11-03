#!/usr/bin/env python3
"""
SolvedOnce reference sandbox runner (roadmap step 4).

Takes a fix packet, rebuilds its environment from the pinned dependencies,
runs the minimal reproduction BEFORE and AFTER applying the patch, compares
the results with what the packet claims, and writes a result record that can
be signed (Ed25519) and later verified by anyone with the public key.

Backends:
  docker        Isolated. Dependencies are installed while building the image
                (the only step with network); every test run then happens with
                --network none, a read-only root filesystem, no capabilities,
                no-new-privileges, an unprivileged user and CPU/memory/PID
                limits. This is the backend meant for real use.
  local-unsafe  NO ISOLATION. A throwaway virtualenv on the host. For
                developing and testing the runner itself, never for packets
                you did not write.

Commands:
  run     python sandbox/solvedonce_run.py run PACKET [options]
  verify  python sandbox/solvedonce_run.py verify RESULT.json --public-key KEY.pem
  keygen  python sandbox/solvedonce_run.py keygen --out-dir DIR

Example (runs the repository's example against its fixture package):
  python sandbox/solvedonce_run.py run examples/example-packet.json \\
      --package-dir tinycsv=examples/fixtures/tinycsv --out result.json

`--package-dir NAME=PATH` installs NAME from a local source tree instead of the
package index. It stands in for the pre-fetched package mirror described in
stage (G); in the example it supplies the fictional `tinycsv`.

Not implemented here (provider-side by design): checking `session_ref` against
session records, and the novelty re-check with the provider's own model.

Exit codes: 0 verified · 1 not verified · 2 usage / environment error.
License: MIT
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "validator"))
sys.path.insert(0, str(HERE))

import apply_patch  # noqa: E402
import solvedonce_validate as validator  # noqa: E402

RUNNER_VERSION = "0.1.0"
OUTPUT_EXCERPT = 2000
DEFAULT_TIMEOUT = 120
SAFE_PKG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")
SAFE_FILE_NAME = re.compile(r"^[A-Za-z0-9_\-]+\.py$")


class RunnerError(Exception):
    pass


# --------------------------------------------------------------------------
# Result record
# --------------------------------------------------------------------------


@dataclass
class RunResult:
    exit_code: int | None
    outcome: str  # "pass" | "fail" | "timeout" | "error" | "not_applicable"
    duration_s: float
    output_excerpt: str


@dataclass
class Result:
    runner_version: str
    result_id: str
    packet_id: str
    packet_sha256: str
    backend: str
    isolated: bool
    environment: dict
    started_at: str
    finished_at: str = ""
    before_fix: RunResult | None = None
    after_fix: RunResult | None = None
    patch_applied: bool | None = None
    claims_match: bool = False
    verdict: str = "not_verified"
    reasons: list[str] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=lambda: [
        "session_ref against provider session records (provider-side)",
        "novelty re-check with the provider's current model (provider-side)",
    ])
    signature: dict | None = None


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical(record: dict) -> bytes:
    """Bytes that get signed: the record without its signature, canonical JSON."""
    body = {k: v for k, v in record.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def excerpt(text: str) -> str:
    text = text.replace("\r\n", "\n")
    return text if len(text) <= OUTPUT_EXCERPT else "..." + text[-(OUTPUT_EXCERPT - 3):]


# --------------------------------------------------------------------------
# Packet preparation
# --------------------------------------------------------------------------


def python_version(packet: dict) -> str:
    m = re.search(r"(\d+)\.(\d+)", packet.get("environment", {}).get("runtime", ""))
    if not m:
        return "3.12"
    major, minor = int(m.group(1)), int(m.group(2))
    if major != 3 or not 9 <= minor <= 14:
        raise RunnerError(f"unsupported Python runtime {m.group(0)!r}")
    return f"{major}.{minor}"


def test_file_name(argv: list[str]) -> str:
    for tok in argv:
        if tok.endswith(".py"):
            if not SAFE_FILE_NAME.match(tok):
                raise RunnerError(f"unsafe test file name in run_command: {tok!r}")
            return tok
    return "test_repro.py"


def run_argv(packet: dict) -> list[str]:
    cmd = packet["reproduction"]["run_command"]
    argv = shlex.split(cmd)
    if not argv:
        raise RunnerError("empty run_command")
    allowed = {"pytest", "python", "python3"}
    if argv[0] not in allowed:
        raise RunnerError(f"run_command must start with one of {sorted(allowed)}, got {argv[0]!r}")
    if argv[0] == "pytest":
        argv = ["python", "-m", "pytest"] + argv[1:]
    elif argv[0] == "python3":
        argv = ["python"] + argv[1:]
    return argv


def parse_package_dirs(items: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for item in items:
        if "=" not in item:
            raise RunnerError(f"--package-dir expects NAME=PATH, got {item!r}")
        name, path = item.split("=", 1)
        p = Path(path).resolve()
        if not SAFE_PKG_NAME.match(name) or not p.is_dir():
            raise RunnerError(f"invalid --package-dir {item!r}")
        out[name] = p
    return out


def install_specs(packet: dict, local: dict[str, Path], local_prefix: str) -> list[str]:
    specs = []
    for name, version in packet["reproduction"]["dependencies"].items():
        if not SAFE_PKG_NAME.match(name):
            raise RunnerError(f"unsafe dependency name {name!r}")
        specs.append(f"{local_prefix}/{name}" if name in local else f"{name}=={version}")
    return specs


def compare_claims(packet: dict, result: Result) -> None:
    claimed_before = packet["evidence"]["before_fix"]["outcome"]
    claimed_after = packet["evidence"]["after_fix"]["outcome"]
    ok = True
    if result.before_fix is None or result.before_fix.outcome != claimed_before:
        ok = False
        got = result.before_fix.outcome if result.before_fix else "not run"
        result.reasons.append(f"before fix: packet claims {claimed_before!r}, sandbox observed {got!r}")
    if packet["fix"].get("patch"):
        if result.after_fix is None or result.after_fix.outcome != claimed_after:
            ok = False
            got = result.after_fix.outcome if result.after_fix else "not run"
            result.reasons.append(f"after fix: packet claims {claimed_after!r}, sandbox observed {got!r}")
    result.claims_match = ok


def decide(packet: dict, result: Result) -> None:
    compare_claims(packet, result)
    before_failed = result.before_fix is not None and result.before_fix.outcome == "fail"
    if not packet["fix"].get("patch"):
        result.verdict = "reproduced_only" if before_failed and result.claims_match else "not_verified"
        if result.verdict == "reproduced_only":
            result.reasons.append("fix is not a patch; only the failure could be reproduced automatically")
        return
    after_passed = result.after_fix is not None and result.after_fix.outcome == "pass"
    if before_failed and after_passed and result.patch_applied and result.claims_match:
        result.verdict = "verified"
    else:
        result.verdict = "not_verified"
        if not before_failed:
            result.reasons.append("the reproduction did not fail before the fix")
        if result.patch_applied is False:
            result.reasons.append("the patch did not apply cleanly")
        if not after_passed:
            result.reasons.append("the reproduction did not pass after the fix")


def classify(exit_code: int | None, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if exit_code == 0:
        return "pass"
    # pytest: 1 = tests failed. Plain python: non-zero after an assertion = fail.
    # 2-5 are pytest usage/collection/internal errors: the test never really ran.
    if exit_code in (2, 3, 4, 5):
        return "error"
    return "fail"


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


def _run(argv: list[str], timeout: int, cwd: Path | None = None, env: dict | None = None,
         on_timeout=None) -> tuple[int | None, str, float, bool]:
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or ""), time.monotonic() - t0, False
    except subprocess.TimeoutExpired as exc:
        if on_timeout:
            on_timeout()
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return None, out + "\n[timed out]", time.monotonic() - t0, True


class DockerBackend:
    name = "docker"
    isolated = True

    def __init__(self, timeout: int, docker: str = "docker"):
        self.timeout = timeout
        self.docker = docker
        if not shutil.which(docker):
            raise RunnerError("docker not found; install Docker or use --backend local-unsafe for development")
        self.tag = f"solvedonce-run-{uuid.uuid4().hex[:12]}"

    def _build(self, ctx: Path, dockerfile: str, tag: str) -> None:
        (ctx / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        code, out, _, timed_out = _run([self.docker, "build", "-q", "-t", tag, str(ctx)], timeout=900)
        if code != 0 or timed_out:
            raise RunnerError(f"image build failed:\n{excerpt(out)}")

    def _run_test(self, image: str, argv: list[str]) -> RunResult:
        name = f"{image}-{uuid.uuid4().hex[:6]}"
        cmd = [self.docker, "run", "--rm", "--name", name,
               "--network", "none", "--read-only",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--user", "65534:65534",
               "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "256",
               "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTEST_ADDOPTS=-p no:cacheprovider",
               "-e", "HOME=/tmp", "-w", "/work", image] + argv
        code, out, secs, timed_out = _run(
            cmd, self.timeout,
            on_timeout=lambda: subprocess.run([self.docker, "kill", name], capture_output=True))
        return RunResult(code, classify(code, timed_out), round(secs, 3), excerpt(out))

    def execute(self, packet: dict, local: dict[str, Path], result: Result) -> None:
        pyver = python_version(packet)
        argv = run_argv(packet)
        test_name = test_file_name(argv)
        base_image = f"python:{pyver}-slim"
        result.environment.update({"image": base_image, "python": pyver, "network_during_test": "none"})
        with tempfile.TemporaryDirectory(prefix="solvedonce-") as tmp:
            ctx = Path(tmp)
            (ctx / "pkgs").mkdir()
            for name, src in local.items():
                shutil.copytree(src, ctx / "pkgs" / name, ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", "build", "dist", "*.egg-info", ".git"))
            (ctx / test_name).write_text(packet["reproduction"]["test_code"], encoding="utf-8")
            shutil.copy(HERE / "apply_patch.py", ctx / "apply_patch.py")
            specs = " ".join(shlex.quote(s) for s in install_specs(packet, local, "/pkgs"))
            pins = json.dumps(packet["reproduction"]["dependencies"])
            # Base image: dependencies installed (network allowed only here), versions verified.
            self._build(ctx, f"""FROM {base_image}
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY pkgs /pkgs
RUN pip install {specs}
RUN python -c "import importlib.metadata as m, json, sys; pins = json.loads(sys.argv[1]); bad = {{k: m.version(k) for k in pins if m.version(k) != pins[k]}}; sys.exit('installed versions differ from pins: %s' % bad if bad else 0)" '{pins}'
COPY apply_patch.py /opt/solvedonce/apply_patch.py
WORKDIR /work
COPY {test_name} /work/{test_name}
""", self.tag)
            result.environment["image_id"] = self._image_id(self.tag)
            result.before_fix = self._run_test(self.tag, argv)

            patch = packet["fix"].get("patch")
            if not patch:
                result.after_fix = RunResult(None, "not_applicable", 0.0, "fix is not a patch")
                return
            (ctx / "fix.patch").write_text(patch, encoding="utf-8")
            patched = self.tag + "-patched"
            try:
                self._build(ctx, f"""FROM {self.tag}
COPY fix.patch /opt/solvedonce/fix.patch
RUN python /opt/solvedonce/apply_patch.py "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" /opt/solvedonce/fix.patch
""", patched)
                result.patch_applied = True
            except RunnerError as exc:
                result.patch_applied = False
                result.after_fix = RunResult(None, "error", 0.0, excerpt(str(exc)))
                return
            result.after_fix = self._run_test(patched, argv)

    def _image_id(self, tag: str) -> str:
        code, out, _, _ = _run([self.docker, "image", "inspect", "--format", "{{.Id}}", tag], timeout=30)
        return out.strip() if code == 0 else ""

    def cleanup(self) -> None:
        for tag in (self.tag + "-patched", self.tag):
            subprocess.run([self.docker, "image", "rm", "-f", tag], capture_output=True)


class LocalUnsafeBackend:
    """No isolation. For developing the runner only."""
    name = "local-unsafe"
    isolated = False

    def __init__(self, timeout: int):
        self.timeout = timeout

    def execute(self, packet: dict, local: dict[str, Path], result: Result) -> None:
        argv = run_argv(packet)
        test_name = test_file_name(argv)
        result.environment.update({
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "requested_python": python_version(packet),
            "network_during_test": "not restricted (local-unsafe)",
        })
        with tempfile.TemporaryDirectory(prefix="solvedonce-local-") as tmp:
            tmpp = Path(tmp)
            venv = tmpp / "venv"
            code, out, _, _ = _run([sys.executable, "-m", "venv", str(venv)], timeout=300)
            if code != 0:
                raise RunnerError(f"venv creation failed:\n{excerpt(out)}")
            py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            pkgs = tmpp / "pkgs"
            pkgs.mkdir()
            for name, src in local.items():
                shutil.copytree(src, pkgs / name, ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", "build", "dist", "*.egg-info", ".git"))
            specs = install_specs(packet, local, str(pkgs))
            code, out, _, _ = _run([str(py), "-m", "pip", "install", "-q",
                                    "--disable-pip-version-check", *specs], timeout=900)
            if code != 0:
                raise RunnerError(f"dependency install failed:\n{excerpt(out)}")
            work = tmpp / "work"
            work.mkdir()
            (work / test_name).write_text(packet["reproduction"]["test_code"], encoding="utf-8")
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTEST_ADDOPTS="-p no:cacheprovider")
            cmd = [str(py)] + argv[1:]

            code, out, secs, to = _run(cmd, self.timeout, cwd=work, env=env)
            result.before_fix = RunResult(code, classify(code, to), round(secs, 3), excerpt(out))

            patch = packet["fix"].get("patch")
            if not patch:
                result.after_fix = RunResult(None, "not_applicable", 0.0, "fix is not a patch")
                return
            code, purelib, _, _ = _run([str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                                       timeout=60)
            try:
                apply_patch.apply(Path(purelib.strip()), patch)
                result.patch_applied = True
            except apply_patch.PatchError as exc:
                result.patch_applied = False
                result.after_fix = RunResult(None, "error", 0.0, f"patch rejected: {exc}")
                return
            code, out, secs, to = _run(cmd, self.timeout, cwd=work, env=env)
            result.after_fix = RunResult(code, classify(code, to), round(secs, 3), excerpt(out))

    def cleanup(self) -> None:
        pass


# --------------------------------------------------------------------------
# Signing (Ed25519, optional dependency: cryptography)
# --------------------------------------------------------------------------


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
        return serialization, Ed25519PrivateKey, Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover
        raise RunnerError("signing needs the 'cryptography' package (pip install -r requirements.txt)") from exc


def key_fingerprint(public_raw: bytes) -> str:
    return "ed25519:" + hashlib.sha256(public_raw).hexdigest()[:32]


def sign_record(record: dict, private_key_pem: bytes) -> dict:
    serialization, _, _ = _crypto()
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    pub_raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    sig = key.sign(canonical(record))
    return {"algorithm": "ed25519", "key_id": key_fingerprint(pub_raw),
            "value": base64.b64encode(sig).decode("ascii"),
            "signed": "canonical JSON of the record without 'signature' (sorted keys, no whitespace)"}


def verify_record(record: dict, public_key_pem: bytes) -> tuple[bool, str]:
    serialization, _, Ed25519PublicKey = _crypto()
    sig = record.get("signature")
    if not sig:
        return False, "record is not signed"
    key = serialization.load_pem_public_key(public_key_pem)
    if not isinstance(key, Ed25519PublicKey):
        return False, "public key is not Ed25519"
    pub_raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if sig.get("key_id") != key_fingerprint(pub_raw):
        return False, "signature was made with a different key"
    try:
        key.verify(base64.b64decode(sig["value"]), canonical(record))
    except Exception:  # cryptography raises InvalidSignature
        return False, "signature does not match the record (record was modified or forged)"
    return True, f"valid signature by {sig['key_id']}"


def keygen(out_dir: Path) -> tuple[Path, Path]:
    serialization, Ed25519PrivateKey, _ = _crypto()
    out_dir.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    priv = out_dir / "runner_ed25519.pem"
    pub = out_dir / "runner_ed25519.pub.pem"
    priv.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    pub.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo))
    if os.name != "nt":
        os.chmod(priv, 0o600)
    return priv, pub


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def run_packet(packet_path: Path, backend_name: str, package_dirs: list[str], timeout: int,
               sign_key: Path | None) -> dict:
    raw = packet_path.read_bytes()
    schema = validator.load_schema(validator.DEFAULT_SCHEMA)
    report = validator.validate_bytes(raw, schema, str(packet_path))
    if report.errors:
        lines = "\n".join(f"  {f.code} {f.path}: {f.message}" for f in report.errors)
        raise RunnerError(f"packet failed validation; refusing to run it:\n{lines}")
    packet = json.loads(raw)
    local = parse_package_dirs(package_dirs)

    if backend_name == "docker":
        backend = DockerBackend(timeout)
    elif backend_name == "local-unsafe":
        backend = LocalUnsafeBackend(timeout)
        sys.stderr.write("WARNING: local-unsafe backend has NO isolation. Only run packets you trust.\n")
    else:
        raise RunnerError(f"unknown backend {backend_name!r}")

    result = Result(runner_version=RUNNER_VERSION, result_id=uuid.uuid4().hex,
                    packet_id=packet["packet_id"], packet_sha256=hashlib.sha256(raw).hexdigest(),
                    backend=backend.name, isolated=backend.isolated,
                    environment={"local_packages": sorted(local)}, started_at=now())
    try:
        backend.execute(packet, local, result)
    finally:
        backend.cleanup()
    result.finished_at = now()
    decide(packet, result)

    record = asdict(result)
    if sign_key:
        record["signature"] = sign_record(record, sign_key.read_bytes())
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SolvedOnce reference sandbox runner.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {RUNNER_VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a packet before and after its fix")
    r.add_argument("packet")
    r.add_argument("--backend", choices=["docker", "local-unsafe"], default="docker")
    r.add_argument("--package-dir", action="append", default=[], metavar="NAME=PATH",
                   help="install NAME from a local source tree (stands in for a package mirror)")
    r.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per test run")
    r.add_argument("--sign-key", type=Path, help="Ed25519 private key (PEM) to sign the result")
    r.add_argument("--out", type=Path, help="write the result JSON here (default: stdout)")

    v = sub.add_parser("verify", help="verify a signed result")
    v.add_argument("result")
    v.add_argument("--public-key", type=Path, required=True)

    k = sub.add_parser("keygen", help="create an Ed25519 key pair for signing results")
    k.add_argument("--out-dir", type=Path, required=True)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "run":
            record = run_packet(Path(args.packet), args.backend, args.package_dir, args.timeout, args.sign_key)
            text = json.dumps(record, indent=2, ensure_ascii=False)
            if args.out:
                args.out.write_text(text + "\n", encoding="utf-8")
            else:
                print(text)
            sys.stderr.write(f"verdict: {record['verdict']}"
                             + (f" ({'; '.join(record['reasons'])})" if record["reasons"] else "") + "\n")
            return 0 if record["verdict"] == "verified" else 1
        if args.cmd == "verify":
            record = json.loads(Path(args.result).read_text(encoding="utf-8"))
            ok, msg = verify_record(record, args.public_key.read_bytes())
            print(("OK  " if ok else "FAIL  ") + msg)
            return 0 if ok else 1
        if args.cmd == "keygen":
            priv, pub = keygen(args.out_dir)
            print(f"private key: {priv}  (keep secret)\npublic key:  {pub}")
            return 0
    except (RunnerError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
