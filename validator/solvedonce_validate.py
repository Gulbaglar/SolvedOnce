#!/usr/bin/env python3
"""
SolvedOnce reference validator.

Checks a fix packet against the JSON schema, then runs static semantic,
consistency and security checks. It NEVER executes anything from the packet:
no test code, no patch, no commands. Packets are untrusted input; running
them is the job of the sandbox runner (roadmap step 4), not of this tool.

Usage:
    python validator/solvedonce_validate.py PACKET.json [PACKET.json ...]
    python validator/solvedonce_validate.py --json PACKET.json
    python validator/solvedonce_validate.py --strict PACKET.json   # warnings fail too

Exit codes:
    0  all packets valid
    1  at least one packet has errors (or warnings, with --strict)
    2  usage error, unreadable file, or invalid JSON

License: MIT
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

try:
    import jsonschema
except ImportError:  # pragma: no cover
    sys.stderr.write("error: the 'jsonschema' package is required (pip install -r requirements.txt)\n")
    sys.exit(2)

try:
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover
    SpecifierSet = None  # version-range checks are skipped without 'packaging'

VALIDATOR_VERSION = "0.3.0"
DEFAULT_SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "fix-packet.schema.json"

# Hard cap on raw packet size. Anything bigger is rejected before parsing.
MAX_PACKET_BYTES = 256 * 1024

# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@dataclass
class Finding:
    level: str  # "error" or "warning"
    code: str  # stable identifier, e.g. "SO201"
    path: str  # JSON pointer-ish location in the packet
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "path": self.path, "message": self.message}


@dataclass
class Report:
    file: str
    findings: list[Finding] = field(default_factory=list)

    def error(self, code: str, path: str, message: str) -> None:
        self.findings.append(Finding("error", code, path, message))

    def warn(self, code: str, path: str, message: str) -> None:
        self.findings.append(Finding("warning", code, path, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    def ok(self, strict: bool = False) -> bool:
        return not self.errors and not (strict and self.warnings)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def iter_strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield (path, value) for every string anywhere in the packet."""
    if isinstance(node, str):
        yield path or "/", node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from iter_strings(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from iter_strings(v, f"{path}/{i}")


def get(packet: dict, *keys: str, default: Any = None) -> Any:
    node: Any = packet
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


# --------------------------------------------------------------------------
# Stage 0: schema
# --------------------------------------------------------------------------


def check_schema(packet: Any, schema: dict, report: Report) -> bool:
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(packet), key=lambda e: list(e.absolute_path))
    for err in errors:
        loc = "/" + "/".join(str(p) for p in err.absolute_path)
        report.error("SO001", loc, f"schema: {err.message}")
    return not errors


# --------------------------------------------------------------------------
# Stage 1: evidence consistency
# --------------------------------------------------------------------------


def check_evidence(packet: dict, report: Report) -> None:
    before = get(packet, "evidence", "before_fix", default={})
    after = get(packet, "evidence", "after_fix", default={})

    if before.get("outcome") != "fail":
        report.error("SO101", "/evidence/before_fix/outcome",
                     "the reproduction must FAIL before the fix; otherwise there is nothing to fix")
    if after.get("outcome") != "pass":
        report.error("SO102", "/evidence/after_fix/outcome",
                     "the reproduction must PASS after the fix")
    if before.get("outcome") == "fail" and before.get("exit_code") == 0:
        report.error("SO103", "/evidence/before_fix/exit_code",
                     "outcome is 'fail' but exit_code is 0; the two must agree")
    if after.get("outcome") == "pass" and after.get("exit_code", 0) != 0:
        report.error("SO104", "/evidence/after_fix/exit_code",
                     "outcome is 'pass' but exit_code is non-zero; the two must agree")
    if get(packet, "evidence", "existing_test_suite_passed") is False:
        report.error("SO105", "/evidence/existing_test_suite_passed",
                     "the fix breaks the target's existing test suite")
    if get(packet, "evidence", "existing_test_suite_passed") is None:
        report.warn("SO106", "/evidence/existing_test_suite_passed",
                    "existing test suite was not run; the fix may have side effects")


# --------------------------------------------------------------------------
# Stage 2: novelty evidence
# --------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def check_novelty(packet: dict, report: Report) -> None:
    attempts = get(packet, "novelty_check", "failed_attempts", default=[]) or []
    seen: set[str] = set()
    for i, att in enumerate(attempts):
        approach = _norm(att.get("approach", ""))
        result = _norm(att.get("result", ""))
        if len(approach) < 10:
            report.error("SO201", f"/novelty_check/failed_attempts/{i}/approach",
                         "failed attempt is too vague to be evidence (under 10 characters)")
        if len(result) < 5:
            report.error("SO202", f"/novelty_check/failed_attempts/{i}/result",
                         "failed attempt has no meaningful result")
        if approach in seen:
            report.error("SO203", f"/novelty_check/failed_attempts/{i}/approach",
                         "duplicate failed attempt; each entry must be a distinct approach")
        seen.add(approach)
        if re.search(r"\b(pass(ed|es)?|fixed|works?|resolved)\b", result) and not re.search(
                r"\b(still|not|no|fails?|failed|ignored|missing|unchanged)\b", result):
            report.warn("SO204", f"/novelty_check/failed_attempts/{i}/result",
                        "result reads like a success; a failed attempt should not have fixed the test")


# --------------------------------------------------------------------------
# Stage 3: reproduction
# --------------------------------------------------------------------------

EXACT_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+_\-]*$")
RANGE_CHARS = re.compile(r"[<>=!~^*,|\s]")


def check_reproduction(packet: dict, report: Report) -> None:
    deps = get(packet, "reproduction", "dependencies", default={}) or {}
    for name, ver in deps.items():
        if RANGE_CHARS.search(ver) or not EXACT_VERSION.match(ver):
            report.error("SO301", f"/reproduction/dependencies/{name}",
                         f"dependency must be pinned to one exact version, got {ver!r}")

    lang = (get(packet, "reproduction", "language", default="") or "").lower()
    code = get(packet, "reproduction", "test_code", default="") or ""
    if lang == "python":
        try:
            ast.parse(code)
        except SyntaxError as exc:
            report.error("SO302", "/reproduction/test_code",
                         f"test_code is not valid Python: line {exc.lineno}: {exc.msg}")
        else:
            if not re.search(r"^\s*(def test_|class Test)", code, re.MULTILINE) and "assert" not in code:
                report.warn("SO303", "/reproduction/test_code",
                            "no test function or assert found; the reproduction may not check anything")
    else:
        report.warn("SO304", "/reproduction/language",
                    f"no syntax check available for language {lang!r} in this validator version")

    if len(code.splitlines()) > 200:
        report.warn("SO305", "/reproduction/test_code",
                    "reproduction is longer than 200 lines; it should be minimal")

    # The target itself should be one of the pinned dependencies.
    target = get(packet, "target", "name")
    if target and deps and target not in deps:
        report.warn("SO306", "/reproduction/dependencies",
                    f"target {target!r} is not pinned in reproduction.dependencies")


# --------------------------------------------------------------------------
# Stage 4: target and version range
# --------------------------------------------------------------------------


def check_target(packet: dict, report: Report) -> None:
    ecosystem = get(packet, "target", "ecosystem", default="")
    name = get(packet, "target", "name")
    rng = get(packet, "target", "affected_versions", default="")
    fixed = get(packet, "target", "fixed_in_version")
    pinned = (get(packet, "reproduction", "dependencies", default={}) or {}).get(name)

    if ecosystem != "PyPI" or SpecifierSet is None:
        report.warn("SO401", "/target/affected_versions",
                    f"version-range checks are only implemented for PyPI in this version (got {ecosystem!r})")
        return
    try:
        spec = SpecifierSet(rng)
    except InvalidSpecifier:
        report.error("SO402", "/target/affected_versions", f"not a valid version range: {rng!r}")
        return
    if pinned:
        try:
            if Version(pinned) not in spec:
                report.error("SO403", f"/reproduction/dependencies/{name}",
                             f"reproduction pins {name}=={pinned}, which is outside affected_versions {rng!r}")
        except InvalidVersion:
            report.error("SO404", f"/reproduction/dependencies/{name}", f"invalid version {pinned!r}")
    if fixed:
        try:
            if Version(fixed) in spec:
                report.error("SO405", "/target/fixed_in_version",
                             f"fixed_in_version {fixed} lies inside affected_versions {rng!r}")
        except InvalidVersion:
            report.error("SO406", "/target/fixed_in_version", f"invalid version {fixed!r}")


# --------------------------------------------------------------------------
# Stage 5: fix and patch
# --------------------------------------------------------------------------

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def check_patch_structure(patch: str) -> list[str]:
    """Return a list of problems with a unified diff, without applying it."""
    problems: list[str] = []
    lines = patch.splitlines()
    if not any(l.startswith("--- ") for l in lines) or not any(l.startswith("+++ ") for l in lines):
        problems.append("missing '---' / '+++' file headers")
    i = 0
    hunks = 0
    while i < len(lines):
        m = HUNK.match(lines[i])
        if not m:
            i += 1
            continue
        hunks += 1
        old_n = int(m.group(2)) if m.group(2) is not None else 1
        new_n = int(m.group(4)) if m.group(4) is not None else 1
        old_seen = new_seen = 0
        i += 1
        while i < len(lines) and not lines[i].startswith(("@@", "--- ", "diff ")):
            line = lines[i]
            if line.startswith("\\"):  # "\ No newline at end of file"
                i += 1
                continue
            tag = line[:1]
            if tag == " " or line == "":
                old_seen += 1
                new_seen += 1
            elif tag == "-":
                old_seen += 1
            elif tag == "+":
                new_seen += 1
            else:
                problems.append(f"hunk {hunks}: unexpected line {line[:40]!r}")
            i += 1
        if (old_seen, new_seen) != (old_n, new_n):
            problems.append(f"hunk {hunks}: header says -{old_n} +{new_n} lines, body has -{old_seen} +{new_seen}")
    if hunks == 0:
        problems.append("no hunks found")
    return problems


def check_fix(packet: dict, report: Report) -> None:
    kind = get(packet, "fix", "kind")
    patch = get(packet, "fix", "patch")
    if kind == "upstream_patch" and not patch:
        report.error("SO501", "/fix/patch", "fix.kind is 'upstream_patch' but no patch is given")
    if patch:
        for problem in check_patch_structure(patch):
            report.error("SO502", "/fix/patch", f"malformed unified diff: {problem}")
        if re.search(r"^\+\+\+ (b/)?(\.\./|/)", patch, re.MULTILINE):
            report.error("SO503", "/fix/patch", "patch writes outside the target tree (absolute or '../' path)")

    license_ = (get(packet, "target", "license", default="") or "").lower()
    if patch and license_ == "proprietary" and get(packet, "consent", "allow_public_release"):
        report.warn("SO504", "/consent/allow_public_release",
                    "patch to a proprietary target: only description and workaround may be published, not the patch")
    if kind == "upstream_patch" and get(packet, "consent", "allow_upstream_report") and not get(
            packet, "target", "upstream_repository"):
        report.warn("SO505", "/target/upstream_repository",
                    "upstream report allowed but no upstream_repository given")


# --------------------------------------------------------------------------
# Stage 6: security scan of all text (static, heuristic)
# --------------------------------------------------------------------------

SECRET_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("SO601", "private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("SO602", "AWS access key id", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("SO603", "GitHub token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("SO604", "Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("SO605", "API key (sk- style)", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("SO606", "Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("SO607", "JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("SO608", "credentials in URL", re.compile(r"[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)),
]

PII_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("SO611", "email address", re.compile(r"\b[A-Za-z0-9._%+\-]+@(?!example\.(com|org|net)\b)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("SO612", "private IP address", re.compile(r"\b(10\.\d{1,3}|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
    ("SO613", "user home path", re.compile(r"(/home/|/Users/|C:\\\\Users\\\\|C:\\Users\\)[A-Za-z0-9._\-]+", re.IGNORECASE)),
    ("SO614", "internal hostname", re.compile(r"\b[a-z0-9\-]+\.(internal|corp|lan|local|intranet)\b", re.IGNORECASE)),
]

INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore (all |any )?(the )?(previous|prior|above) (instructions|rules|prompts?)", re.IGNORECASE),
    re.compile(r"disregard (all |any )?(the )?(previous|prior|above|system)", re.IGNORECASE),
    re.compile(r"\byou are now\b", re.IGNORECASE),
    re.compile(r"\b(system|developer) prompt\b", re.IGNORECASE),
    re.compile(r"</?(system|assistant|user|instructions?)>", re.IGNORECASE),
    re.compile(r"\bdo not (tell|inform|mention to) the (user|reviewer)\b", re.IGNORECASE),
]

HIDDEN_CHARS = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u202a-\u202e\u2066-\u2069]")

DANGEROUS_CODE: list[tuple[str, re.Pattern[str]]] = [
    ("network access", re.compile(r"\b(requests|urllib|httpx|socket|aiohttp)\b|\bcurl\b|\bwget\b")),
    ("subprocess / shell", re.compile(r"\b(subprocess|os\.system|os\.popen|pty\.spawn)\b")),
    ("dynamic code execution", re.compile(r"\b(eval|exec|compile|__import__)\s*\(")),
    ("encoded payload", re.compile(r"\b(base64|codecs)\.(b64decode|decode)\b|[A-Za-z0-9+/]{120,}={0,2}")),
    ("environment / credential access", re.compile(r"os\.environ|getenv\(|\.ssh|\.aws|\.netrc|keyring")),
    ("destructive file operation", re.compile(r"\b(shutil\.rmtree|os\.remove|os\.unlink|rm\s+-rf)\b")),
]


def check_security(packet: dict, report: Report) -> None:
    for path, text in iter_strings(packet):
        for code, label, pat in SECRET_PATTERNS:
            if pat.search(text):
                report.error(code, path, f"possible secret ({label}) must be scrubbed before submission")
        for code, label, pat in PII_PATTERNS:
            if pat.search(text):
                report.warn(code, path, f"possible personal or internal data ({label}); check scrubbing")
        for pat in INJECTION_PATTERNS:
            if pat.search(text):
                report.error("SO621", path, "text looks like an instruction aimed at a model (prompt injection)")
                break
        if HIDDEN_CHARS.search(text):
            report.error("SO622", path, "invisible or bidirectional control characters are not allowed")

    code = get(packet, "reproduction", "test_code", default="") or ""
    cmd = get(packet, "reproduction", "run_command", default="") or ""
    for label, pat in DANGEROUS_CODE:
        if pat.search(code) or pat.search(cmd):
            report.warn("SO631", "/reproduction",
                        f"reproduction uses {label}; a minimal test rarely needs this, review before running")
    if re.search(r"[;&|`$]|\$\(", cmd):
        report.error("SO632", "/reproduction/run_command",
                     "run_command contains shell metacharacters; it must be a single plain command")


# --------------------------------------------------------------------------
# Stage 7: privacy / scope / consent sanity
# --------------------------------------------------------------------------


def check_privacy_and_scope(packet: dict, report: Report) -> None:
    attribution = get(packet, "consent", "attribution")
    if attribution is not None and not get(packet, "consent", "allow_public_release"):
        report.warn("SO701", "/consent/attribution",
                    "attribution given but public release not allowed; attribution will not be shown")
    sess = get(packet, "provenance", "session_ref", default="") or ""
    if re.search(r"@|\buser\b|\bacct\b|\baccount\b", sess, re.IGNORECASE):
        report.error("SO702", "/provenance/session_ref", "session_ref must be opaque and must not encode identity")
    pid = get(packet, "packet_id", default="") or ""
    if sess and pid and sess.split("_")[-1] == pid.split("_")[-1]:
        report.warn("SO703", "/packet_id", "packet_id and session_ref share a value; packet_id should be random")


# --------------------------------------------------------------------------
# Stage 8: formats (explicit, so results don't depend on optional packages)
# --------------------------------------------------------------------------


def check_formats(packet: dict, report: Report) -> None:
    from datetime import datetime
    from urllib.parse import urlparse

    created = get(packet, "created_at", default="") or ""
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            report.error("SO801", "/created_at", "created_at must include a timezone (e.g. 'Z')")
    except ValueError:
        report.error("SO801", "/created_at", f"created_at is not an RFC 3339 date-time: {created!r}")

    urls = [("/target/upstream_repository", get(packet, "target", "upstream_repository"))]
    urls += [(f"/novelty_check/closest_known_references/{i}", u)
             for i, u in enumerate(get(packet, "novelty_check", "closest_known_references", default=[]) or [])]
    for path, url in urls:
        if url is None:
            continue
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            report.error("SO802", path, f"must be an https URL, got {url!r}")


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

CHECKS = [check_evidence, check_novelty, check_reproduction, check_target, check_fix,
          check_security, check_privacy_and_scope, check_formats]


def validate_bytes(raw: bytes, schema: dict, name: str = "<packet>") -> Report:
    report = Report(file=name)
    if len(raw) > MAX_PACKET_BYTES:
        report.error("SO000", "/", f"packet is {len(raw)} bytes; limit is {MAX_PACKET_BYTES}")
        return report
    try:
        packet = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        report.error("SO000", "/", f"not valid UTF-8 JSON: {exc}")
        return report
    if not check_schema(packet, schema, report):
        # Semantic checks assume a schema-valid structure; stop here.
        return report
    for check in CHECKS:
        check(packet, report)
    return report


def load_schema(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def print_human(report: Report, strict: bool) -> None:
    status = "OK" if report.ok(strict) else "FAIL"
    print(f"{status}  {report.file}  ({len(report.errors)} errors, {len(report.warnings)} warnings)")
    for f in report.findings:
        print(f"  {f.level.upper():7} {f.code}  {f.path}: {f.message}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate SolvedOnce fix packets (static checks only).")
    ap.add_argument("packets", nargs="+", help="packet JSON files")
    ap.add_argument("--schema", default=str(DEFAULT_SCHEMA), help="path to fix-packet.schema.json")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true", help="treat warnings as failures")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VALIDATOR_VERSION}")
    args = ap.parse_args(argv)

    try:
        schema = load_schema(Path(args.schema))
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        sys.stderr.write(f"error: cannot load schema {args.schema}: {exc}\n")
        return 2

    reports: list[Report] = []
    for p in args.packets:
        try:
            raw = Path(p).read_bytes()
        except OSError as exc:
            sys.stderr.write(f"error: cannot read {p}: {exc}\n")
            return 2
        reports.append(validate_bytes(raw, schema, p))

    if args.json:
        print(json.dumps({
            "validator_version": VALIDATOR_VERSION,
            "results": [{"file": r.file, "ok": r.ok(args.strict),
                         "findings": [f.as_dict() for f in r.findings]} for r in reports],
        }, indent=2))
    else:
        for r in reports:
            print_human(r, args.strict)

    if any(r.findings and r.findings[0].code == "SO000" for r in reports):
        return 2
    return 0 if all(r.ok(args.strict) for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
