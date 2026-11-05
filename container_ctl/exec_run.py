"""exec_run.py — 執行面的 ExecSpec 介面（ExecSpec v1/v2）。

輸入 ExecSpec(dict)，建立一個拋棄式或工作階段型的加固容器，投影憑證到 tmpfs，執行 argv，回傳 ExecResult(dict)，最後一定銷毀容器。
不認識特定 agent 平台；不寫任何帳本；機密只經 stdin 進 tmpfs，不進 argv。
`rw` 工作區：host 目錄複製兩份（a 原樣、b 可寫），容器只掛 b；結束後以 git 管線產生 `diff --git a/x b/x` 格式 patch（`git apply -p1` 可用）。host 目錄從不被改。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable, Dict, List, Mapping, Optional, Tuple, Union

SPEC_VERSION = 1
SUPPORTED_SPEC_VERSIONS = (1, 2)
MAX_OUTPUT = 1 << 20
HOME = "/home/agent"
SECRETS_DIR = "/run/secrets"
ENV_FILE = f"{SECRETS_DIR}/env"
WORKSPACE = "/workspace"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SESSION_REF = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MEM = re.compile(r"[1-9][0-9]*[bkmg]", re.I)
_VAR = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_TOKEN = re.compile(r"[\x21-\x26\x28-\x7e]+")      # visible ASCII without ': the value is written inside single quotes in the env file
REDACTED = "[REDACTED]"
MIN_SECRET_LEN = 8
MIN_JSON_SECRET_LEN = 16
FORBIDDEN_RW_ROOTS = frozenset({"/", "/etc", "/var", "/home", "/root", "/opt", "/usr", "/srv", "/proc", "/sys", "/dev", "/boot", "/run"})
FORBIDDEN_RUNTIME_ROOTS = frozenset({"/", "/proc", "/sys", "/dev", "/etc"})
_MOUNT_UNSAFE = re.compile(r"[:,\x00-\x1f\x7f]")   # would split or extend a `-v src:dst:opts` mount string
MAX_RW_BYTES = 256 << 20
MAX_PATCH = 1 << 20

# Secret patterns for argv rejection and output masking
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-(?:oat|api)[0-9a-zA-Z_-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
]
_MASK_PATTERNS = [
    re.compile(r"sk-ant-(?:oat|api)[0-9a-zA-Z_-]{16,}"),
    re.compile(r"Bearer\s+[0-9a-zA-Z._-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
]

# runner(argv, stdin, timeout) -> (rc, stdout, stderr); injectable so tests never touch podman
Runner = Callable[[List[str], Optional[bytes], float], Tuple[int, bytes, bytes]]


class ExecSpecInvalid(ValueError):
    def __init__(self, message: str, code: str = "SPEC_INVALID"):
        super().__init__(message)
        self.code = code


def default_runner(argv: List[str], stdin: Optional[bytes], timeout: float) -> Tuple[int, bytes, bytes]:
    try:
        p = subprocess.run(["podman", *argv], input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or b"")[:MAX_OUTPUT], b"timeout"
    return p.returncode, p.stdout[:MAX_OUTPUT], p.stderr[:MAX_OUTPUT]


def _check_src(src, roots: List[str]) -> None:
    """A credential source is an absolute path without '..'; when CONTAINER_CTL_EXEC_CRED_ROOTS is set it must also resolve inside one of them."""
    if not isinstance(src, str) or not src.startswith("/") or ".." in src.split("/"):
        raise ExecSpecInvalid("credential src: absolute path without '..'")
    if roots:
        real = os.path.realpath(src)
        if not any(real == os.path.realpath(r) or real.startswith(os.path.realpath(r).rstrip("/") + "/") for r in roots):
            raise ExecSpecInvalid("credential src outside CONTAINER_CTL_EXEC_CRED_ROOTS")


def _json_strings(v, out: List[str]) -> None:
    if isinstance(v, str):
        if len(v) >= MIN_JSON_SECRET_LEN:
            out.append(v)
    elif isinstance(v, dict):
        for x in v.values():
            _json_strings(x, out)
    elif isinstance(v, list):
        for x in v:
            _json_strings(x, out)


def _secret_values(data: bytes) -> List[str]:
    """Values to scrub from a run's output for one projected credential: the whole content and, for a JSON object, its long string values."""
    import json
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return []
    found = [text] if len(text) >= MIN_SECRET_LEN else []
    try:
        doc = json.loads(text)
    except ValueError:
        return found
    if isinstance(doc, (dict, list)):
        _json_strings(doc, found)
    return found


def scrub(text: Optional[str], values: List[str]) -> Optional[str]:
    """Replace every projected secret value (longest first) with [REDACTED]. Exact match only: an encoded or split value is not caught."""
    if not text:
        return text
    for v in sorted(set(values), key=len, reverse=True):
        text = text.replace(v, REDACTED)
    return text


def _extract_binary_verification(spec: Mapping) -> Tuple[Optional[str], Optional[str]]:
    """Returns (path, expected_sha256) or (None, None)."""
    target = spec.get("herdr_binary_sha256")
    if target is None:
        target = spec.get("verify_binary")
    path, expected_sha = None, None
    if isinstance(target, Mapping):
        path = target.get("path")
        expected_sha = target.get("sha256")
    elif isinstance(target, str):
        expected_sha = target
        path = spec.get("herdr_binary_path")
    elif "herdr_sha256" in spec:
        expected_sha = spec.get("herdr_sha256")
        path = spec.get("herdr_binary_path")

    if expected_sha is not None and not path:
        argv = spec.get("argv") or []
        if argv and isinstance(argv[0], str) and argv[0].startswith("/"):
            path = argv[0]
    return path, expected_sha


def _verify_herdr_binary(spec: Mapping, read_file: Optional[Callable[[str], bytes]] = None) -> None:
    path, expected_sha = _extract_binary_verification(spec)
    if expected_sha is None:
        return
    if not path:
        raise ExecSpecInvalid("herdr binary path required for sha256 verification", code="HERDR_BINARY_SHA256_MISMATCH")
    rf = read_file or (lambda p: open(p, "rb").read())
    try:
        data = rf(path)
    except Exception as e:
        raise ExecSpecInvalid(f"herdr binary read failed for sha256 check ({path}): {e}", code="HERDR_BINARY_SHA256_MISMATCH")
    actual_sha = hashlib.sha256(data).hexdigest()
    if actual_sha.lower() != expected_sha.lower():
        raise ExecSpecInvalid(
            f"herdr binary sha256 mismatch: expected {expected_sha}, got {actual_sha}",
            code="HERDR_BINARY_SHA256_MISMATCH",
        )


def _check_argv_secrets(argv: List[str], spec: Mapping, read_file: Optional[Callable[[str], bytes]] = None) -> None:
    for a in argv:
        for pat in _SECRET_PATTERNS:
            if pat.search(a):
                raise ExecSpecInvalid(f"argv contains forbidden secret pattern: {pat.pattern}", code="SECRET_IN_ARGV")
    for s in spec.get("env_secrets", []):
        src = str(s.get("src", ""))
        if src and any(src in a for a in argv):
            raise ExecSpecInvalid("a secret path must not appear in argv", code="SECRET_IN_ARGV")
    items = [*spec.get("credential_files", []), *spec.get("env_secrets", [])]
    if items:
        rf = read_file
        if rf is None:
            rf = lambda p: open(p, "rb").read() if os.path.isfile(p) else b""
        for item in items:
            src = item.get("src")
            if not src:
                continue
            try:
                data = rf(src)
            except Exception:
                continue
            if not data:
                continue
            try:
                tok = data.decode("utf-8").strip()
                if len(tok) >= MIN_SECRET_LEN and any(tok in a for a in argv):
                    raise ExecSpecInvalid("argv contains projected secret value", code="SECRET_IN_ARGV")
            except Exception:
                pass
            for sec in _secret_values(data):
                if any(sec in a for a in argv):
                    raise ExecSpecInvalid("argv contains projected secret value", code="SECRET_IN_ARGV")


def validate(spec: Mapping, read_file: Optional[Callable[[str], bytes]] = None) -> None:
    if not isinstance(spec, Mapping):
        raise ExecSpecInvalid("spec must be a mapping")
    v = spec.get("v")
    if v not in SUPPORTED_SPEC_VERSIONS:
        raise ExecSpecInvalid(f"v must be 1 or 2 (got {v})")
    mode = spec.get("mode", "oneshot")
    if mode not in ("oneshot", "session"):
        raise ExecSpecInvalid("mode must be oneshot or session")
    if v == 1 and spec.get("mode") not in (None, "oneshot"):
        raise ExecSpecInvalid("v=1 only supports mode=oneshot")

    exec_id = spec.get("exec_id")
    if not isinstance(exec_id, str) or not _ID.fullmatch(exec_id):
        raise ExecSpecInvalid("exec_id: [A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

    if mode == "oneshot" or v == 1:
        run_id = spec.get("run_id")
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            raise ExecSpecInvalid("run_id: [A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
    else:
        run_id = spec.get("run_id")
        if run_id is not None and (not isinstance(run_id, str) or not _ID.fullmatch(run_id)):
            raise ExecSpecInvalid("run_id: [A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

    image = spec.get("image")
    if not isinstance(image, str) or not image or image.endswith(":latest") or ("@" not in image and ":" not in image.rsplit("/", 1)[-1]):
        raise ExecSpecInvalid("image must be pinned (tag or digest, not :latest)")

    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise ExecSpecInvalid("argv: non-empty list of non-empty strings")

    if mode == "session":
        session_ref = spec.get("session_ref")
        if not isinstance(session_ref, str) or not _SESSION_REF.fullmatch(session_ref):
            raise ExecSpecInvalid("session_ref: ^[a-z0-9][a-z0-9_-]{0,63}$")
        _check_argv_secrets(argv, spec, read_file=read_file)

        if "runtime_dirs" in spec:
            rdirs = spec.get("runtime_dirs")
            if not isinstance(rdirs, list):
                raise ExecSpecInvalid("runtime_dirs must be a list")
            seen_purposes = set()
            for item in rdirs:
                if not isinstance(item, Mapping):
                    raise ExecSpecInvalid("runtime_dirs entry must be a mapping")
                purpose = item.get("purpose")
                if purpose not in ("control_socket", "transcript"):
                    raise ExecSpecInvalid(f"runtime_dirs purpose must be control_socket or transcript (got {purpose!r})")
                if purpose in seen_purposes:
                    raise ExecSpecInvalid(f"duplicate runtime_dirs purpose: {purpose}")
                seen_purposes.add(purpose)
                if "host_path" in item or "src" in item:
                    raise ExecSpecInvalid("runtime_dirs host path must not be specified by caller")
                dst = item.get("dst")
                if not isinstance(dst, str) or not dst.startswith("/"):
                    raise ExecSpecInvalid("runtime_dirs dst must be an absolute path")
                clean_dst = os.path.normpath(dst)
                if _MOUNT_UNSAFE.search(dst):
                    raise ExecSpecInvalid("runtime_dirs dst must not contain ':', ',' or control characters")
                if clean_dst in FORBIDDEN_RUNTIME_ROOTS or any(
                    clean_dst == r or clean_dst.startswith(f"{r}/") for r in ("/proc", "/sys", "/dev", "/etc")
                ):
                    raise ExecSpecInvalid(f"runtime_dirs dst forbidden system root: {dst}")

    ws = spec.get("workspace")
    if mode == "session" and ws is None:
        pass
    else:
        ws = ws or {}
        if ws.get("mode") not in ("scratch", "ro", "rw"):
            raise ExecSpecInvalid("workspace.mode")
        if (ws["mode"] == "scratch") == bool(ws.get("host_path")):
            raise ExecSpecInvalid("host_path is required for ro/rw and forbidden for scratch")
        if ws["mode"] == "ro":
            hp = ws["host_path"]
            if not isinstance(hp, str) or not hp.startswith("/") or _MOUNT_UNSAFE.search(hp) \
                    or ".." in hp.split("/") or os.path.realpath(hp) in FORBIDDEN_RW_ROOTS:
                raise ExecSpecInvalid("ro host_path must be an absolute path outside system roots")

    lim = spec.get("limits") or {}
    if not isinstance(lim.get("memory_max"), str) or not _MEM.fullmatch(lim["memory_max"]):
        raise ExecSpecInvalid("limits.memory_max")
    if not isinstance(lim.get("timeout_s"), (int, float)) or isinstance(lim["timeout_s"], bool) or lim["timeout_s"] <= 0:
        raise ExecSpecInvalid("limits.timeout_s")
    if "idle_timeout_s" in lim:
        idle = lim["idle_timeout_s"]
        if isinstance(idle, bool) or not isinstance(idle, int) or idle <= 0:
            raise ExecSpecInvalid("limits.idle_timeout_s must be a positive integer")

    for m in spec.get("mounts_ro", []):
        src = str(m.get("src", ""))
        dst = str(m.get("dst", ""))
        if not dst.startswith("/opt/ext/") or ".." in dst.split("/") or not src.startswith("/") or ".." in src.split("/"):
            raise ExecSpecInvalid("mounts_ro: absolute src, dst under /opt/ext/, no '..'")
        if _MOUNT_UNSAFE.search(src) or _MOUNT_UNSAFE.search(dst) or os.path.realpath(src) in FORBIDDEN_RW_ROOTS:
            raise ExecSpecInvalid("mounts_ro: src/dst must not contain ':', ',' or control characters or system roots")
    roots = [r for r in os.environ.get("CONTAINER_CTL_EXEC_CRED_ROOTS", "").split(":") if r]
    for f in [*spec.get("credential_files", []), *spec.get("env_secrets", [])]:
        _check_src(f.get("src"), roots)
    for f in spec.get("credential_files", []):
        dest = f.get("dest")                          # joined under $HOME in the container and under the host staging dir
        if not isinstance(dest, str) or not dest.strip("/") or ".." in dest.split("/") or _MOUNT_UNSAFE.search(dest):
            raise ExecSpecInvalid("credential dest: relative path under $HOME without '..', ':', ',' or control characters")
    for s in spec.get("env_secrets", []):
        if any(str(s.get("src", "")) in a for a in argv):
            raise ExecSpecInvalid("a secret path must not appear in argv")
        for _prefix, var in s.get("env_by_prefix", []):
            if not _VAR.fullmatch(var):
                raise ExecSpecInvalid("env_by_prefix variable name")
    for k in (spec.get("env") or {}):
        if not _VAR.fullmatch(k):
            raise ExecSpecInvalid("env variable name")

    if "checks" in spec:                              # Post-run checks: commands the executor runs after the agent, without credentials
        if mode == "session":
            raise ExecSpecInvalid("checks are oneshot only")
        _validate_checks(spec["checks"], spec, read_file)

    _path, expected_sha = _extract_binary_verification(spec)
    if expected_sha is not None and not isinstance(expected_sha, str):
        raise ExecSpecInvalid("sha256 must be a string", code="HERDR_BINARY_SHA256_MISMATCH")


_CHECK_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
CHECKS_MAX, CHECK_ARGV_MAX, CHECK_ARG_BYTES, CHECK_TIMEOUT_MAX, CHECK_TAIL_BYTES = 8, 64, 1024, 1800, 1024


def _utf8_len(a: str) -> int:
    """Strict UTF-8 length; a lone surrogate (not encodable) counts as too long, as the gateway's parse_checks rejects it."""
    try:
        return len(a.encode("utf-8"))
    except UnicodeEncodeError:
        return CHECK_ARG_BYTES + 1


def _validate_checks(checks, spec: Mapping, read_file) -> None:
    if not isinstance(checks, list) or not 1 <= len(checks) <= CHECKS_MAX:
        raise ExecSpecInvalid("checks: 1..8 items")
    names = set()
    for c in checks:
        if not isinstance(c, Mapping) or set(c) != {"name", "argv", "timeout_s"}:
            raise ExecSpecInvalid("checks: keys name, argv, timeout_s")
        if not isinstance(c["name"], str) or not _CHECK_NAME.fullmatch(c["name"]) or c["name"] in names:
            raise ExecSpecInvalid("checks: name")
        names.add(c["name"])
        argv = c["argv"]
        if (not isinstance(argv, list) or not 1 <= len(argv) <= CHECK_ARGV_MAX
                or any(not isinstance(a, str) or not a or "\x00" in a or _utf8_len(a) > CHECK_ARG_BYTES for a in argv)):
            raise ExecSpecInvalid("checks: argv")
        t = c["timeout_s"]
        if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= CHECK_TIMEOUT_MAX:
            raise ExecSpecInvalid("checks: timeout_s 1..1800")
        _check_argv_secrets(argv, spec, read_file)


def checks_budget_s(checks) -> float:
    """Time budget of a run's declared checks: the sum of their `timeout_s` (they run one after another after the main command).
    Added to the run's `--timeout` fuse."""
    return float(sum(c["timeout_s"] for c in checks or ()))


def check_spec(spec: Mapping, index: int, check: Mapping, rw_dir: Optional[str] = None) -> Dict:
    """The ExecSpec of one check container: same image and read-only mounts; no network, no credentials, no env. The workspace is
    read-only: a check observes the agent's result (for rw, the isolated copy) and must not change it."""
    ws = {"mode": "ro", "host_path": rw_dir} if spec["workspace"]["mode"] == "rw" and rw_dir else spec["workspace"]
    return {**{k: v for k, v in dict(spec).items() if k != "checks"}, "workspace": ws,
            "exec_id": f"{spec['exec_id'][:120]}.c{index}", "egress": "none", "env": {}, "env_secrets": [],
            "credential_files": [], "limits": {**dict(spec["limits"]), "timeout_s": check["timeout_s"]}}


def _run_checks(spec, runner, nets, rw_dir, secrets: List[str]) -> List[Dict]:
    """Run each declared check in a fresh container on the post-run workspace. Never raises for a check's own failure."""
    results = []
    for i, c in enumerate(spec["checks"]):
        cs = check_spec(spec, i, c, rw_dir)
        name = container_name(cs)
        rc, out, err = 125, b"", b""
        timed_out = False
        try:
            start_rc, _, start_err = runner(render_run_argv(cs, nets, rw_dir), None, 60.0)
            if start_rc != 0:
                out = b"CHECK_CONTAINER_START_FAILED\n" + start_err[:2000]
            else:
                rc, out, err = runner(["exec", "-w", WORKSPACE, name] + list(c["argv"]), None, float(c["timeout_s"]))
                timed_out = rc == 124
        except ExecSpecInvalid:
            out = b"CHECK_EGRESS_UNKNOWN"
        finally:
            _remove(name, runner)
        blob = out + (b"\n" + err[:4000] if err else b"")
        tail = scrub(blob[-CHECK_TAIL_BYTES:].decode("utf-8", "replace"), secrets) or ""
        while len(tail.encode("utf-8")) > CHECK_TAIL_BYTES:
            tail = tail[1:]
        results.append({"name": c["name"], "exit_code": int(rc), "timed_out": timed_out,
                        "output_sha256": hashlib.sha256(blob).hexdigest(), "output_bytes": len(blob), "tail": tail})
    return results


def _known_secrets(spec: Mapping, read: Callable[[str], bytes]) -> List[str]:
    """The agent's projected secret values, re-read only to scrub check output (the agent may have copied one into the workspace)."""
    out: List[str] = []
    for f in [*spec.get("credential_files", []), *spec.get("env_secrets", [])]:
        try:
            data = read(f["src"])
        except OSError:
            continue
        out.extend(_secret_values(data))
        token = data.decode("utf-8", "replace").strip()
        if len(token) >= MIN_SECRET_LEN:
            out.append(token)
    return out


def container_name(spec: Mapping) -> str:
    return f"cctl-exec-{spec['exec_id']}"


EGRESS_SAMPLE_S = 2.0
FUSE_MARGIN_S = 120   # the exec's own timeout fires first; this only catches a run whose controller is gone


def render_run_argv(spec: Mapping, networks: Mapping[str, Tuple[str, Mapping[str, str]]], rw_dir: Optional[str] = None) -> List[str]:
    """The `podman run` argv. No secret and no prompt is ever rendered here beyond the idle command."""
    net, extra_env = networks.get(spec.get("egress", "default"), (None, None))
    if net is None:
        raise ExecSpecInvalid("egress profile unknown")
    ws, a = spec["workspace"], ["run", "-d", "--pull=never", "--name", container_name(spec)]
    if net != "default":
        a += ["--network", net]
    # host-side fuse: even if the worker dies mid-run, podman itself kills the container after the run's limit plus a margin
    a += ["--timeout", str(int(spec["limits"]["timeout_s"] + checks_budget_s(spec.get("checks"))) + FUSE_MARGIN_S)]
    a += ["--label", f"container-ctl.run={spec['run_id']}", "--label", f"container-ctl.exec={spec['exec_id']}",
          "--memory", spec["limits"]["memory_max"], "--pids-limit", "512", "--init", "--cap-drop", "all",
          "--security-opt", "no-new-privileges", "--read-only",
          "--tmpfs", f"{HOME}:rw,mode=0700,size=64m", "--tmpfs", "/tmp:rw,size=64m", "--tmpfs", f"{SECRETS_DIR}:rw,mode=0700,size=1m"]
    if ws["mode"] == "scratch":
        a += ["--tmpfs", f"{WORKSPACE}:rw,size=256m"]
    elif ws["mode"] == "rw":
        if not rw_dir:
            raise ExecSpecInvalid("rw needs an isolated copy")
        a += ["-v", f"{rw_dir}:{WORKSPACE}:rw"]
    else:
        a += ["-v", f"{ws['host_path']}:{WORKSPACE}:ro"]
    for k, v in {**dict(spec.get("env") or {}), **dict(extra_env or {}), "HOME": HOME}.items():
        a += ["-e", f"{k}={v}"]
    for m in spec.get("mounts_ro", []):
        a += ["-v", f"{m['src']}:{m['dst']}:ro"]
    return a + [spec["image"], "sleep", "infinity"]


def _project(name: str, spec: Mapping, run: Runner, read: Callable[[str], bytes], timeout: float, sink: Optional[List[str]] = None) -> Optional[str]:
    """Copy credentials into the container tmpfs via stdin. Returns an error code or None. Projected values are appended to `sink`
    so the caller can scrub them from the run's output; they are never stored anywhere else."""
    sink = sink if sink is not None else []
    for f in spec.get("credential_files", []):
        try:
            data = read(f["src"])
        except OSError:
            if f.get("required", True):
                return "CREDENTIAL_MISSING"
            continue
        sink.extend(_secret_values(data))
        dest = f"{HOME}/{f['dest'].lstrip('/')}"
        rc, _, _ = run(["exec", "-i", name, "sh", "-c", 'umask 077 && mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", dest], data, timeout)
        if rc != 0:
            return "CREDENTIAL_PROJECT_FAILED"
    lines = []
    for s in spec.get("env_secrets", []):
        try:
            token = read(s["src"]).decode().strip()
        except (OSError, UnicodeDecodeError):
            if s.get("required", True):
                return "CREDENTIAL_MISSING"
            continue
        var = next((v for p, v in s.get("env_by_prefix", []) if token.startswith(p)), None)
        if var is None or not _TOKEN.fullmatch(token):
            return "CREDENTIAL_UNRECOGNIZED"
        if len(token) >= MIN_SECRET_LEN:
            sink.append(token)
        lines.append(f"export {var}='{token}'")
    if lines:
        rc, _, _ = run(["exec", "-i", name, "sh", "-c", f"umask 077 && cat > {ENV_FILE}"], ("\n".join(lines) + "\n").encode(), timeout)
        if rc != 0:
            return "CREDENTIAL_PROJECT_FAILED"
    return None


def _tree_bytes(root: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(root, followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(base, f)).st_size
            except OSError:
                pass
        if total > MAX_RW_BYTES:
            break
    return total


def prepare_rw(host_path: str, scratch_root: Optional[str] = None) -> Tuple[str, str, str]:
    """-> (tmp, a, b): a = pristine copy, b = writable copy. Raises ExecSpecInvalid for unsafe or oversized sources."""
    real = os.path.realpath(host_path)
    if not os.path.isabs(host_path) or os.path.islink(host_path) or not os.path.isdir(real) or real in FORBIDDEN_RW_ROOTS:
        raise ExecSpecInvalid("rw host_path must be a real directory outside system roots")
    if _tree_bytes(real) > MAX_RW_BYTES:
        raise ExecSpecInvalid("rw workspace too large")
    tmp = tempfile.mkdtemp(prefix="cctl-rw-", dir=scratch_root)
    a, b = os.path.join(tmp, "a"), os.path.join(tmp, "b")
    try:
        shutil.copytree(real, a, symlinks=True)
        shutil.copytree(real, b, symlinks=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp, a, b


_GIT_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1", "LC_ALL": "C"}


def make_patch(tmp: str) -> Optional[str]:
    """Git-format diff of a -> b (`diff --git a/x b/x`, same path both sides, `git apply -p1` and patch_gate both read it).
    '' when nothing changed; None when it is too large to return whole. Symlinks stay links (never followed). Pure git
    plumbing in a throwaway repo inside tmp: no config, no hooks, no filters, so nothing in the workspace can run code here."""
    gd = os.path.join(tmp, "g")

    def git(*args: str, work: Optional[str] = None) -> bytes:
        env = dict(_GIT_ENV, GIT_DIR=gd, **({"GIT_WORK_TREE": work} if work else {}))
        p = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.quotepath=false",
                            "-c", "core.autocrlf=false", "-c", "core.symlinks=true", *args],
                           cwd=work or tmp, env=env, capture_output=True)
        if p.returncode not in (0, 1):
            raise RuntimeError("git failed")
        return p.stdout

    git("init", "-q", "--bare", gd)
    git("add", "-A", "-f", ".", work=os.path.join(tmp, "a"))        # index := a
    tree = git("write-tree").decode().strip()
    git("add", "-A", "-f", ".", work=os.path.join(tmp, "b"))        # index := b (adds, changes and, with -A, removals)
    out = git("diff", "--cached", "--binary", "--no-color", "--no-ext-diff", "--no-renames", tree)
    return None if len(out) > MAX_PATCH else out.decode("utf-8", "replace")


def _result(spec: Mapping, state: str, code: str = "", out: bytes = b"", seconds: float = 0.0) -> Dict:
    v = spec.get("v") if isinstance(spec, Mapping) and spec.get("v") in SUPPORTED_SPEC_VERSIONS else SPEC_VERSION
    return {"v": v, "exec_id": spec.get("exec_id", ""), "run_id": spec.get("run_id", ""), "state": state,
            "error_code": code, "output": out.decode("utf-8", "replace"), "usage": None, "patch": None, "seconds": round(seconds, 3)}


def run_exec(spec: Mapping, *, networks: Optional[Mapping] = None, runner: Runner = default_runner,
             read_file: Callable[[str], bytes] = lambda p: open(p, "rb").read(), clock: Callable[[], float] = time.monotonic,
             scratch_root: Optional[str] = None) -> Dict:
    """ExecSpec -> ExecResult. A normal return means the container is removed; if it cannot be, ContainerLeft is raised."""
    try:
        validate(spec, read_file=read_file)
        if isinstance(spec, Mapping) and spec.get("mode") == "session":
            raise ExecSpecInvalid("mode=session must use session_open")
    except ExecSpecInvalid as e:
        return _result(spec if isinstance(spec, Mapping) else {}, "failed", "SPEC_INVALID", str(e).encode())
    nets = networks if networks is not None else {"default": ("default", {}), "none": ("none", {})}
    tmp = rw_dir = None
    try:
        if spec["workspace"]["mode"] == "rw":
            try:
                tmp, _a, rw_dir = prepare_rw(spec["workspace"]["host_path"], scratch_root)
            except (ExecSpecInvalid, OSError) as e:
                return _result(spec, "failed", "RW_SOURCE_REJECTED", str(e).encode())
        try:
            run_argv = render_run_argv(spec, nets, rw_dir)
        except ExecSpecInvalid:
            return _result(spec, "failed", "EGRESS_UNKNOWN")
        res = _run_prepared(spec, run_argv, runner, read_file, clock, tmp)
        if spec.get("checks") and res.get("state") == "succeeded":   # after the patch was sealed and the agent container removed
            res["checks"] = _run_checks(spec, runner, nets, rw_dir, _known_secrets(spec, read_file))
        return res
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


# --- egress observation (phase 1 of network control: watch, do not block) -------------------------------------------------------
# The container's own view of its connections (/proc/net/*, same netns as the agent). Sampled while the agent runs, so a connection that
# opens and closes between two samples is missed: this is evidence for deciding the phase-2 allow-list, not a guarantee.
_PROC_NET = ("/proc/net/tcp", "/proc/net/tcp6", "/proc/net/udp", "/proc/net/udp6")


def _hex_ip(h: str) -> str:
    import ipaddress
    raw = bytes.fromhex(h)
    if len(raw) == 4:
        return str(ipaddress.IPv4Address(raw[::-1]))
    words = [raw[i:i + 4][::-1] for i in range(0, 16, 4)]
    ip = ipaddress.IPv6Address(b"".join(words))
    return str(ip.ipv4_mapped or ip)


def parse_remotes(text: str) -> set:
    """Remote `ip:port` of non-loopback, non-listening sockets from concatenated /proc/net/{tcp,tcp6,udp,udp6} text. Never raises."""
    import ipaddress
    found = set()
    for line in text.splitlines():
        f = line.split()
        if len(f) < 4 or not f[1].count(":") == 1 or f[0] == "sl":
            continue
        try:
            rhex, rport = f[2].split(":")
            if f[3] == "0A":                                  # TCP LISTEN
                continue
            ip = _hex_ip(rhex)
            addr = ipaddress.ip_address(ip)
            if addr.is_unspecified or addr.is_loopback:
                continue
            found.add(f"{ip}:{int(rport, 16)}")
        except (ValueError, IndexError):
            continue
    return found


def _sample_egress(name, runner, seen: set) -> None:
    rc, out, _ = runner(["exec", name, "cat", *_PROC_NET], None, 10.0)
    if rc == 0:
        seen |= parse_remotes(out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out or ""))


def _record_egress(path: str, spec: Mapping, seen: set) -> None:
    """Append one JSON line (metadata only: ids, profile, remote ip:port). Never raises, never blocks the run."""
    import json
    try:
        line = json.dumps({"ts": time.time(), "run_id": spec.get("run_id"), "exec_id": spec.get("exec_id"),
                           "profile": spec.get("egress", "default"), "remotes": sorted(seen)}, ensure_ascii=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


class ContainerLeft(RuntimeError):
    """`podman rm` failed and the container still exists. Raised instead of returning, so no caller reads a normal return as
    "the container is gone" (the caller releases a workspace lease only on that guarantee)."""


def _remove(name: str, runner: Runner) -> None:
    if runner(["rm", "-f", "-t", "2", name], None, 30.0)[0] == 0:
        return
    if runner(["container", "exists", name], None, 10.0)[0] != 1:      # 1 = confirmed missing; anything else = may still run
        raise ContainerLeft(name)


def _run_prepared(spec, run_argv, runner, read_file, clock, tmp) -> Dict:
    name, timeout, t0 = container_name(spec), float(spec["limits"]["timeout_s"]), clock()
    rc, _, err = runner(["container", "exists", name], None, 10.0)
    if rc == 0:
        return _result(spec, "failed", "EXEC_ID_IN_USE")         # idempotent resend: never a second container
    rc, _, err = runner(run_argv, None, 60.0)
    if rc != 0:
        return _result(spec, "failed", "CONTAINER_START_FAILED", err[:2000])
    secrets: List[str] = []
    try:
        code = _project(name, spec, runner, read_file, 30.0, secrets)
        if code:
            return _result(spec, "failed", code, seconds=clock() - t0)
        cmd = ["exec", "-w", WORKSPACE, name]
        if spec.get("env_secrets"):
            cmd += ["sh", "-c", f'set -a; . {ENV_FILE}; exec "$@"', "sh"]
        log_path = os.environ.get("CONTAINER_CTL_EXEC_EGRESS_LOG") or ""
        seen: set = set()
        stop_sampling = threading.Event()
        if log_path:
            def _loop():
                while not stop_sampling.wait(EGRESS_SAMPLE_S):
                    _sample_egress(name, runner, seen)
            threading.Thread(target=_loop, daemon=True, name="egress-sampler").start()
        try:
            rc, out, err = runner(cmd + list(spec["argv"]), None, timeout)
        finally:
            stop_sampling.set()
        if log_path:                                  # last look, while the container still exists
            _sample_egress(name, runner, seen)
            _record_egress(log_path, spec, seen)
        secs = clock() - t0
        if rc == 124:
            res = _result(spec, "timeout", "TIMEOUT", out, secs)
            res["output"] = scrub(res["output"], secrets)
            if tmp:                                   # a late submission is still a submission: hand back whatever is in the workspace
                runner(["rm", "-f", "-t", "2", name], None, 30.0)       # stop the agent first so the snapshot is quiet
                try:
                    res["patch"] = scrub(make_patch(tmp), secrets)    # None (too large) stays None: no patch, never a truncated one
                except (OSError, RuntimeError):
                    pass
            return res
        res = _result(spec, "succeeded", "", out, secs) if rc == 0 else _result(spec, "failed", "EXIT_NONZERO", out + b"\n" + err[:4000], secs)
        res["output"] = scrub(res["output"], secrets)
        if tmp:
            try:
                patch = make_patch(tmp)
            except (OSError, RuntimeError):
                return _result(spec, "failed", "PATCH_FAILED", seconds=secs)
            if patch is None:
                return _result(spec, "failed", "PATCH_TOO_LARGE", seconds=secs)
            res["patch"] = scrub(patch, secrets)
        return res
    finally:
        _remove(name, runner)


_EXEC_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def exec_alive(exec_id: str, runner: Runner = None) -> bool:
    """Does any container of this exec (the main container, or one of its post-run check containers) still exist? A pure state
    query for workspace lease reclaim: it knows no project and decides nothing. Unsure (podman error) = alive (fail safe)."""
    if type(exec_id) is not str or not _EXEC_ID.fullmatch(exec_id):
        return False
    run = runner or default_runner
    for name in [container_name({"exec_id": exec_id})] + [container_name({"exec_id": f"{exec_id[:120]}.c{i}"}) for i in range(CHECKS_MAX)]:
        rc = run(["container", "exists", name], None, 10.0)[0]
        if rc != 1:
            return True                     # 0 = exists; anything else is not a confirmed "missing"
    return False


def stop_exec(exec_id: str, runner: Runner = None) -> bool:
    """Remove the exec's container (`podman rm -f -t 2`). The running `exec_run` then sees its command end, snapshots the workspace
    (rw: the patch of what the agent wrote so far) and returns normally; the caller decides the outcome. Unknown id = nothing to stop."""
    if type(exec_id) is not str or not _EXEC_ID.fullmatch(exec_id):
        return False
    run, name = runner or default_runner, container_name({"exec_id": exec_id})
    if run(["container", "exists", name], None, 30.0)[0] != 0:
        return False          # no such container (yet): `rm -f` of a missing one exits 0, which must not count as "stopped"
    return run(["rm", "-f", "-t", "2", name], None, 30.0)[0] == 0


def sweep(scratch_root: Optional[str], runner: Runner = None) -> Dict:
    """Worker start-up cleanup: whatever a dead worker left behind. Removes every container labelled `container-ctl.run` (this account's rootless
    podman only) and the `cctl-rw-*` isolated copies under `scratch_root`. Safe because the one worker has just started: nothing it owns is
    running yet. Never raises."""
    import shutil
    run = runner or default_runner
    names, removed, dirs = [], 0, 0
    ok = True
    rc, out, _ = run(["ps", "-a", "--filter", "label=container-ctl.run", "--format", "{{.Names}}"], None, 30.0)
    ok = ok and rc == 0                          # cannot even list: nothing is confirmed
    if rc == 0:
        text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else (out or "")     # the real runner returns bytes
        names = [n for n in text.split() if n.startswith("cctl-exec-")]
    for n in names:
        if run(["rm", "-f", "-t", "2", n], None, 30.0)[0] == 0:
            removed += 1
        else:
            ok = False
    if scratch_root and os.path.isdir(scratch_root):
        for d in os.listdir(scratch_root):
            if d.startswith("cctl-rw-"):
                path = os.path.join(scratch_root, d)
                shutil.rmtree(path, ignore_errors=True)
                if os.path.lexists(path):
                    ok = False                   # e.g. subuid-owned files a plain rmtree cannot remove
                else:
                    dirs += 1
    return {"ok": ok, "containers_seen": len(names), "containers_removed": removed, "scratch_removed": dirs}


def _default_state_root() -> str:
    root = os.environ.get("CONTAINER_CTL_EXEC_STATE_ROOT") or os.environ.get("CONTAINER_CTL_EXEC_SCRATCH")
    if not root:
        home = os.path.expanduser("~")
        root = os.path.join(home, ".local", "state", "container-ctl", "exec-sessions")
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root


def _is_container_alive(runner: Runner, name: str) -> bool:
    rc, out, _ = runner(["container", "inspect", "--format", "{{.State.Running}}", name], None, 10.0)
    if rc == 0 and out:
        text = out.decode("utf-8", "replace").strip().lower() if isinstance(out, (bytes, bytearray)) else str(out).strip().lower()
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
    rc_exists, _, _ = runner(["container", "exists", name], None, 10.0)
    return rc_exists == 0


def render_session_run_argv(
    spec: Mapping,
    networks: Mapping[str, Tuple[str, Mapping[str, str]]],
    runtime_dirs_map: Mapping[str, str],
    staged_creds: List[Tuple[str, str]],
    staged_env: Optional[str],
    rw_dir: Optional[str] = None,
) -> List[str]:
    net, extra_env = networks.get(spec.get("egress", "default"), (None, None))
    if net is None:
        raise ExecSpecInvalid("egress profile unknown")
    ws = spec.get("workspace") or {"mode": "scratch", "host_path": ""}
    a = ["run", "-d", "--pull=never", "--name", container_name(spec)]
    if net != "default":
        a += ["--network", net]

    # timeout_s: hard fuse killing container when timeout expires (T-S-5)
    a += ["--timeout", str(int(spec["limits"]["timeout_s"]))]

    # Labels (T-C-2, T-CC-2)
    session_ref = spec.get("session_ref", "")
    a += ["--label", f"container-ctl.session={session_ref}", "--label", f"container-ctl.exec_id={spec['exec_id']}"]
    if spec.get("run_id"):
        a += ["--label", f"container-ctl.run={spec['run_id']}"]
    a += ["--label", f"container-ctl.exec={spec['exec_id']}"]

    # Hardening & Limits
    a += [
        "--memory", spec["limits"]["memory_max"],
        "--pids-limit", "512",
        "--init",
        "--cap-drop", "all",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", f"{HOME}:rw,mode=0700,size=64m",
        "--tmpfs", "/tmp:rw,size=64m",
        "--tmpfs", f"{SECRETS_DIR}:rw,mode=0700,size=1m",
    ]

    # Workspace
    if ws.get("mode") == "scratch":
        a += ["--tmpfs", f"{WORKSPACE}:rw,size=256m"]
    elif ws.get("mode") == "rw":
        if not rw_dir:
            raise ExecSpecInvalid("rw needs an isolated copy")
        a += ["-v", f"{rw_dir}:{WORKSPACE}:rw"]
    else:
        a += ["-v", f"{ws['host_path']}:{WORKSPACE}:ro"]

    # Runtime dirs
    for item in spec.get("runtime_dirs", []):
        purpose = item["purpose"]
        hpath = runtime_dirs_map[purpose]
        dst = item["dst"]
        a += ["-v", f"{hpath}:{dst}:rw,Z"]

    # Staged credentials
    for src_p, dst_p in staged_creds:
        a += ["-v", f"{src_p}:{dst_p}:ro,Z"]
    if staged_env:
        a += ["-v", f"{staged_env}:{ENV_FILE}:ro,Z"]

    # Environment
    for k, v in {**dict(spec.get("env") or {}), **dict(extra_env or {}), "HOME": HOME}.items():
        a += ["-e", f"{k}={v}"]

    # mounts_ro
    for m in spec.get("mounts_ro", []):
        a += ["-v", f"{m['src']}:{m['dst']}:ro"]

    # Entrypoint / argv
    return a + [spec["image"], *spec["argv"]]


def session_open(
    spec: Mapping,
    *,
    runner: Optional[Runner] = None,
    state_root: Optional[str] = None,
    read_file: Optional[Callable[[str], bytes]] = None,
    clock: Callable[[], float] = time.monotonic,
    networks: Optional[Mapping] = None,
) -> Dict:
    run = runner or default_runner
    read_fn = read_file or (lambda p: open(p, "rb").read())
    try:
        validate(spec, read_file=read_fn)
        _verify_herdr_binary(spec, read_file=read_fn)
    except ExecSpecInvalid as e:
        code = getattr(e, "code", "SPEC_INVALID")
        if code == "SPEC_INVALID" and "sha256" in str(e).lower():
            code = "HERDR_BINARY_SHA256_MISMATCH"
        return {"ok": False, "error_code": code, "error": str(e)}

    root = state_root or _default_state_root()
    exec_id = spec["exec_id"]
    sess_dir = os.path.join(root, exec_id)
    state_file = os.path.join(sess_dir, "state.json")
    cname = container_name(spec)

    # 1. Idempotent check (T-CC-1)
    if os.path.isfile(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            saved_cname = saved.get("container_name") or cname
            if _is_container_alive(run, saved_cname):
                return {
                    "ok": True,
                    "exec_id": exec_id,
                    "container_id": saved.get("container_id", saved_cname),
                    "session_ref": saved.get("session_ref", spec.get("session_ref")),
                    "runtime_dirs": saved.get("runtime_dirs", {}),
                }
        except Exception:
            pass

    # 2. Setup session directories (0700)
    os.makedirs(sess_dir, mode=0o700, exist_ok=True)
    runtime_root = os.path.join(sess_dir, "runtime")
    os.makedirs(runtime_root, mode=0o700, exist_ok=True)
    runtime_dirs_map: Dict[str, str] = {}
    for item in spec.get("runtime_dirs", []):
        purpose = item["purpose"]
        hpath = os.path.join(runtime_root, purpose)
        os.makedirs(hpath, mode=0o700, exist_ok=True)
        runtime_dirs_map[purpose] = hpath

    # 3. Setup staging directory for secrets (0700)
    staging_dir = os.path.join(sess_dir, "staging")
    os.makedirs(staging_dir, mode=0o700, exist_ok=True)
    secrets: List[str] = []
    staged_creds: List[Tuple[str, str]] = []

    for f in spec.get("credential_files", []):
        try:
            data = read_fn(f["src"])
        except OSError:
            if f.get("required", True):
                shutil.rmtree(sess_dir, ignore_errors=True)
                return {"ok": False, "error_code": "CREDENTIAL_MISSING", "error": f"missing {f['src']}"}
            continue
        secrets.extend(_secret_values(data))
        dst_rel = f["dest"].lstrip("/")
        staged_path = os.path.join(staging_dir, "creds", dst_rel)
        os.makedirs(os.path.dirname(staged_path), mode=0o700, exist_ok=True)
        with open(staged_path, "wb") as cf:
            cf.write(data)
        os.chmod(staged_path, 0o600)
        staged_creds.append((staged_path, f"{HOME}/{dst_rel}"))

    staged_env = None
    env_lines = []
    for s in spec.get("env_secrets", []):
        try:
            token_bytes = read_fn(s["src"])
            token = token_bytes.decode("utf-8").strip()
        except (OSError, UnicodeDecodeError):
            if s.get("required", True):
                shutil.rmtree(sess_dir, ignore_errors=True)
                return {"ok": False, "error_code": "CREDENTIAL_MISSING", "error": f"missing {s['src']}"}
            continue
        var = next((v for p, v in s.get("env_by_prefix", []) if token.startswith(p)), None)
        if var is None or not _TOKEN.fullmatch(token):
            shutil.rmtree(sess_dir, ignore_errors=True)
            return {"ok": False, "error_code": "CREDENTIAL_UNRECOGNIZED", "error": "unrecognized token"}
        if len(token) >= MIN_SECRET_LEN:
            secrets.append(token)
        env_lines.append(f"export {var}='{token}'")

    if env_lines:
        secrets_subdir = os.path.join(staging_dir, "secrets")
        os.makedirs(secrets_subdir, mode=0o700, exist_ok=True)
        staged_env = os.path.join(secrets_subdir, "env")
        with open(staged_env, "w", encoding="utf-8") as ef:
            ef.write("\n".join(env_lines) + "\n")
        os.chmod(staged_env, 0o600)

    with open(os.path.join(staging_dir, "secrets.json"), "w", encoding="utf-8") as sf:
        json.dump({"secrets": sorted(set(secrets))}, sf)
    os.chmod(os.path.join(staging_dir, "secrets.json"), 0o600)

    # 4. Workspace setup if rw
    ws = spec.get("workspace") or {"mode": "scratch", "host_path": ""}
    rw_tmp = rw_dir = None
    if ws.get("mode") == "rw":
        try:
            rw_tmp, _a, rw_dir = prepare_rw(ws["host_path"], scratch_root=root)
        except (ExecSpecInvalid, OSError) as e:
            shutil.rmtree(sess_dir, ignore_errors=True)
            return {"ok": False, "error_code": "RW_SOURCE_REJECTED", "error": str(e)}

    # 5. Render run argv
    nets = networks if networks is not None else {"default": ("default", {}), "none": ("none", {})}
    try:
        run_argv = render_session_run_argv(spec, nets, runtime_dirs_map, staged_creds, staged_env, rw_dir=rw_dir)
    except ExecSpecInvalid as e:
        shutil.rmtree(sess_dir, ignore_errors=True)
        if rw_tmp:
            shutil.rmtree(rw_tmp, ignore_errors=True)
        return {"ok": False, "error_code": "EGRESS_UNKNOWN", "error": str(e)}

    run(["rm", "-f", "-t", "2", cname], None, 30.0)

    # 6. Start container (podman run -d ...) (T-S-5)
    rc, out, err = run(run_argv, None, 60.0)
    if rc != 0:
        shutil.rmtree(sess_dir, ignore_errors=True)
        if rw_tmp:
            shutil.rmtree(rw_tmp, ignore_errors=True)
        return {"ok": False, "error_code": "CONTAINER_START_FAILED", "error": err.decode("utf-8", "replace")[:2000]}

    container_id = out.decode("utf-8", "replace").strip() if out else cname
    started_at = clock()

    state = {
        "v": 2,
        "exec_id": exec_id,
        "run_id": spec.get("run_id", ""),
        "session_ref": spec.get("session_ref", ""),
        "container_name": cname,
        "container_id": container_id,
        "started_at": started_at,
        "timeout_s": int(spec["limits"]["timeout_s"]),
        "runtime_dirs": runtime_dirs_map,
        "secrets": sorted(set(secrets)),
        "egress": spec.get("egress", "default"),
        "workspace": {
            "mode": ws.get("mode"),
            "host_path": ws.get("host_path", ""),
            "tmp": rw_tmp,
        },
    }
    with open(state_file, "w", encoding="utf-8") as sf:
        json.dump(state, sf)
    os.chmod(state_file, 0o600)

    return {
        "ok": True,
        "exec_id": exec_id,
        "container_id": container_id,
        "session_ref": spec.get("session_ref", ""),
        "runtime_dirs": runtime_dirs_map,
    }


def session_status(
    exec_id: str,
    *,
    runner: Optional[Runner] = None,
    state_root: Optional[str] = None,
) -> Dict:
    run = runner or default_runner
    root = state_root or _default_state_root()
    sess_dir = os.path.join(root, exec_id)
    state_file = os.path.join(sess_dir, "state.json")
    saved = {}
    if os.path.isfile(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except Exception:
            pass

    cname = saved.get("container_name") or container_name({"exec_id": exec_id})
    alive = _is_container_alive(run, cname)
    started_at = saved.get("started_at")
    rss = None
    if alive:
        rc, out, _ = run(["container", "stats", "--no-stream", "--format", "{{.MemUsage}}", cname], None, 10.0)
        if rc == 0 and out:
            text = out.decode("utf-8", "replace").strip() if isinstance(out, (bytes, bytearray)) else str(out).strip()
            rss = text.split("/")[0].strip() if "/" in text else text
        if not rss:
            rc, out, _ = run(["container", "inspect", "--format", "{{.HostConfig.Memory}}", cname], None, 10.0)
            if rc == 0 and out:
                rss = out.decode("utf-8", "replace").strip() if isinstance(out, (bytes, bytearray)) else str(out).strip()

    return {
        "ok": True,
        "exec_id": exec_id,
        "alive": alive,
        "started_at": started_at,
        "rss": rss,
    }


def session_close(
    exec_id: str,
    *,
    collect_patch: bool = False,
    runner: Optional[Runner] = None,
    state_root: Optional[str] = None,
) -> Dict:
    if not isinstance(exec_id, str) or not _EXEC_ID.fullmatch(exec_id):
        return {"ok": False, "error": f"invalid exec_id: {exec_id!r}"}
    run = runner or default_runner
    root = state_root or _default_state_root()
    sess_dir = os.path.join(root, exec_id)
    state_file = os.path.join(sess_dir, "state.json")
    saved = {}
    if os.path.isfile(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except Exception:
            pass

    session_ref = saved.get("session_ref", "")
    cname = saved.get("container_name") or container_name({"exec_id": exec_id})
    secrets = saved.get("secrets", [])
    ws = saved.get("workspace") or {}
    rw_tmp = ws.get("tmp")

    # Egress observation (phase 1, watch only): one last look at the container's own connection table
    # before it is removed. Same contract as the one-shot path: never raises, never blocks the close.
    log_path = os.environ.get("CONTAINER_CTL_EXEC_EGRESS_LOG") or ""
    if log_path:
        seen: set = set()
        try:
            _sample_egress(cname, run, seen)
            _record_egress(log_path, {"exec_id": exec_id, "run_id": saved.get("run_id", ""), "egress": saved.get("egress", "default")}, seen)
        except Exception:
            pass

    patch = None
    if collect_patch and ws.get("mode") == "rw" and rw_tmp and os.path.isdir(rw_tmp):
        try:
            raw_patch = make_patch(rw_tmp)
            if raw_patch is not None:
                patch = scrub(raw_patch, secrets)
        except Exception:
            patch = None
        shutil.rmtree(rw_tmp, ignore_errors=True)

    # Force remove container
    run(["rm", "-f", "-v", cname], None, 30.0)

    # Filter by label container-ctl.session (T-CC-2)
    if session_ref:
        run(["rm", "-f", "-v", "--filter", f"label=container-ctl.session={session_ref}"], None, 30.0)
        rc, out, _ = run(["ps", "-a", "--filter", f"label=container-ctl.session={session_ref}", "--format", "{{.Names}}"], None, 30.0)
        if rc == 0 and out:
            text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
            for n in text.split():
                if n:
                    run(["rm", "-f", "-v", n], None, 30.0)

    # Remove host runtime_dirs and staging directories (T-CC-2)
    shutil.rmtree(sess_dir, ignore_errors=True)

    evidence = {
        "container_removed": True,
        "session_ref": session_ref,
        "runtime_dirs_cleaned": not os.path.exists(sess_dir),
    }

    return {
        "ok": True,
        "exec_id": exec_id,
        "patch": patch,
        "evidence": evidence,
    }


def session_sample(
    exec_id: str,
    *,
    runner: Optional[Runner] = None,
    state_root: Optional[str] = None,
    log_path: Optional[str] = None,
) -> Dict:
    """One watch-only egress sample of a live session container.

    Meant to be called on a timer by the worker while the session lives. Appends one line to the egress log in the
    same format as the close observation. Never changes the container and never raises to the caller."""
    run = runner or default_runner
    root = state_root or _default_state_root()
    log = log_path or os.environ.get("CONTAINER_CTL_EXEC_EGRESS_LOG") or ""
    state_file = os.path.join(root, exec_id, "state.json")
    if not log:
        return {"ok": False, "exec_id": exec_id, "error_code": "EGRESS_LOG_UNSET"}
    if not os.path.isfile(state_file):
        return {"ok": False, "exec_id": exec_id, "error_code": "SESSION_NOT_FOUND"}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        return {"ok": False, "exec_id": exec_id, "error_code": "STATE_UNREADABLE"}

    cname = saved.get("container_name") or container_name({"exec_id": exec_id})
    seen: set = set()
    _sample_egress(cname, run, seen)
    _record_egress(log, {"exec_id": exec_id, "run_id": saved.get("run_id", ""), "egress": saved.get("egress", "default")}, seen)
    return {"ok": True, "exec_id": exec_id, "remotes": len(seen)}


def session_mask(
    exec_id: str,
    text_or_bytes: Union[str, bytes, bytearray, None],
    *,
    state_root: Optional[str] = None,
) -> Union[str, bytes, None]:
    if text_or_bytes is None:
        return None
    root = state_root or _default_state_root()
    sess_dir = os.path.join(root, exec_id)
    secrets: List[str] = []

    staging_secrets = os.path.join(sess_dir, "staging", "secrets.json")
    if os.path.isfile(staging_secrets):
        try:
            with open(staging_secrets, "r", encoding="utf-8") as f:
                secrets.extend(json.load(f).get("secrets", []))
        except Exception:
            pass

    state_file = os.path.join(sess_dir, "state.json")
    if os.path.isfile(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                secrets.extend(json.load(f).get("secrets", []))
        except Exception:
            pass

    is_bytes = isinstance(text_or_bytes, (bytes, bytearray))
    text = text_or_bytes.decode("utf-8", "replace") if is_bytes else str(text_or_bytes)

    # 1. Scrub literal secrets
    text = scrub(text, secrets) or ""

    # 2. Scrub token formats via regex
    for pat in _MASK_PATTERNS:
        text = pat.sub(REDACTED, text)

    return text.encode("utf-8") if is_bytes else text


def main(argv=None, stdin=None, stdout=None) -> int:
    """`python -m container_ctl.exec_run`: ExecSpec JSON on stdin -> ExecResult JSON on stdout. The boundary other repos call (CP-2)."""
    import json
    import sys
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    args = list(argv) if argv else []

    if args and args[0] == "--stop":          # `--stop EXEC_ID`: the owner asked to stop a running exec (control API v1.5 `stop`)
        ok = len(args) == 2 and stop_exec(args[1])
        stdout.write(json.dumps({"stopped": bool(ok)}) + "\n")
        return 0 if ok else 1
    if args and args[0] == "--alive":         # `--alive EXEC_ID`: a state query only; exit 0 whenever answered
        if len(args) != 2:
            return 2
        stdout.write(json.dumps({"alive": bool(exec_alive(args[1]))}) + "\n")
        return 0
    if args and args[0] == "--sweep":         # worker start-up: remove what a dead worker left behind
        res = sweep(os.environ.get("CONTAINER_CTL_EXEC_SCRATCH") or None)
        stdout.write(json.dumps(res) + "\n")
        return 0 if res["ok"] else 1             # non-zero = not confirmed: the worker must not tell the control plane the host is empty

    # Session subcommands
    subcmd = args[0] if args else None
    if subcmd in ("session_sample", "--session-sample"):   # watch-only; the worker calls this on a timer while the session lives
        exec_id = args[1] if len(args) > 1 else ""
        res = session_sample(exec_id)
        stdout.write(json.dumps(res, ensure_ascii=True) + "\n")
        return 0 if res.get("ok") else 1
    if subcmd in ("session_open", "--session-open"):
        if len(args) > 1 and args[1].startswith("{"):
            try:
                spec = json.loads(args[1])
            except ValueError:
                spec = None
        elif len(args) > 1 and os.path.isfile(args[1]):
            try:
                with open(args[1], "r", encoding="utf-8") as f:
                    spec = json.load(f)
            except Exception:
                spec = None
        else:
            try:
                spec = json.loads(stdin.read())
            except ValueError:
                spec = None
        res = session_open(spec if isinstance(spec, dict) else {})
        stdout.write(json.dumps(res, ensure_ascii=True) + "\n")
        return 0 if res.get("ok") else 1

    if subcmd in ("session_status", "--session-status"):
        exec_id = args[1] if len(args) > 1 else ""
        if not exec_id:
            try:
                payload = json.loads(stdin.read())
                exec_id = payload.get("exec_id", "")
            except Exception:
                pass
        res = session_status(exec_id)
        stdout.write(json.dumps(res, ensure_ascii=True) + "\n")
        return 0 if res.get("ok") else 1

    if subcmd in ("session_close", "--session-close"):
        collect_patch = "--patch" in args
        non_flags = [a for a in args[1:] if a != "--patch"]
        exec_id = non_flags[0] if non_flags else ""
        if not exec_id:
            try:
                payload = json.loads(stdin.read())
                exec_id = payload.get("exec_id", "")
                if "collect_patch" in payload:
                    collect_patch = payload["collect_patch"]
            except Exception:
                pass
        res = session_close(exec_id, collect_patch=collect_patch)
        stdout.write(json.dumps(res, ensure_ascii=True) + "\n")
        return 0 if res.get("ok") else 1

    if subcmd in ("session_mask", "--session-mask"):
        exec_id = args[1] if len(args) > 1 else ""
        content = stdin.read()
        masked = session_mask(exec_id, content)
        stdout.write(masked if isinstance(masked, str) else masked.decode("utf-8", "replace"))
        return 0

    try:
        spec = json.loads(stdin.read())
    except ValueError:
        spec = None

    if isinstance(spec, dict) and spec.get("mode") == "session":
        res = session_open(spec)
        stdout.write(json.dumps(res, ensure_ascii=True) + "\n")
        return 0 if res.get("ok") else 1

    # rw copies live here. Under a unit with PrivateTmp the default /tmp is invisible to podman ("statfs …: no such file"), so the
    # caller may name a directory both sides can see. Unset = the system default, as before.
    scratch = os.environ.get("CONTAINER_CTL_EXEC_SCRATCH") or None
    if scratch:
        os.makedirs(scratch, mode=0o700, exist_ok=True)
    result = run_exec(spec if isinstance(spec, dict) else {}, scratch_root=scratch)
    stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
