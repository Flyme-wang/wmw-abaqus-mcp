#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wmw-abaqus-mcp Server v6.0.0 - recoverable single-flight execution.

This stdio MCP server talks to one or more live Abaqus/CAE GUI bridges over
local TCP sockets. Each Abaqus/CAE instance runs its own socket bridge on a
unique port (48152-48162), and this server manages all connections through
a SessionManager, allowing simultaneous control of multiple Abaqus windows.
"""

from __future__ import annotations

import json
import socket
import uuid
import tempfile
from typing import Any
import os
import asyncio
from collections import OrderedDict
import math
import time

from mcp.server.fastmcp import FastMCP


DEFAULT_HOST = os.environ.get("ABAQUS_MCP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("ABAQUS_MCP_PORT", "48152"))
DEFAULT_TIMEOUT = float(os.environ.get("ABAQUS_MCP_TIMEOUT", "60"))
MAX_MESSAGE_BYTES = int(os.environ.get("ABAQUS_MCP_MAX_MESSAGE_BYTES", str(32 * 1024 * 1024)))

# Port range for multi-session discovery
PORT_RANGE_START = int(os.environ.get("ABAQUS_MCP_PORT_RANGE_START", "48152"))
PORT_RANGE_END = int(os.environ.get("ABAQUS_MCP_PORT_RANGE_END", "48162"))
DISCOVERY_DIR = os.path.join(tempfile.gettempdir(), "abaqus_mcp_sessions")

INSTRUCTIONS = """You are controlling one or more live Abaqus/CAE sessions through MCP.

Use small validated Python chunks instead of one large script. Set a clean
working directory before creating jobs. Prefer named sets and named surfaces
over fragile raw-coordinate selections for loads, boundary conditions, section
assignments, and interactions. When API behavior is uncertain, inspect the live
Abaqus objects first with run_python before continuing.

Multi-Session Support (v5.1+):
- Use list_sessions() to see all connected Abaqus instances.
- Use discover_sessions() to scan for newly opened Abaqus windows.
- Use set_active_session(session_id) to switch between instances.
- Pass session_id parameter to run_python/execute_script for direct targeting
  without switching the active session.
- Each Abaqus/CAE instance uses a unique port (48152-48162).

Recoverable single-flight execution (v6.0+):
- Save the original request_id and session_id for every execution/submission.
  On BUSY, timeout_pending, or unknown, never automatically replay with a new request_id.
  Query get_request_status(original_request_id, session_id) on the same session;
  BUSY also identifies active_request_id. An unknown/evicted result is not proof
  that execution did not happen. Do not auto-unlock, restart, or terminate work.
- submit_job returns a submission acknowledgement, not solver completion.
  Read actual progress and status separately with monitor_job_status.
- ping never injects a kernel command. kernel_response_observed records only a
  past valid wrapper response. kernel_ready is false while bridge work is busy,
  null before any response was observed, and true otherwise. This is bridge
  admission information, not a guarantee about manual kernel work or native
  sendCommand blocking/GIL behavior. Always inspect busy and operation status.
"""

mcp = FastMCP("wmw-abaqus-mcp", instructions=INSTRUCTIONS)


class ProtocolError(RuntimeError):
    """Raised when the Abaqus GUI bridge returns malformed protocol data."""


class RequestError(RuntimeError):
    """Structured bridge outcome, also readable by clients that display text."""

    def __init__(self, code: str, message: str, **details: Any):
        self.code = code
        self.details = details
        suffix = ", ".join(f"{key}={value}" for key, value in details.items())
        super().__init__(f"{code}: {message}" + (f" ({suffix})" if suffix else ""))


# Routing hints only, bounded and local to this MCP process. Explicit session_id
# always wins. The bridge registry remains the source of operation outcomes.
_request_endpoints: OrderedDict[str, tuple[str, int] | None] = OrderedDict()


def _remember_endpoint(request_id: str, endpoint: tuple[str, int]) -> None:
    if request_id in _request_endpoints and _request_endpoints[request_id] != endpoint:
        _request_endpoints[request_id] = None  # same ID used on several sessions
    else:
        _request_endpoints[request_id] = endpoint
    _request_endpoints.move_to_end(request_id)
    while len(_request_endpoints) > 512:
        _request_endpoints.popitem(last=False)


# ---------------------------------------------------------------------------
# Session Manager - supports multiple concurrent Abaqus/CAE connections
# ---------------------------------------------------------------------------

class SessionManager:
    """Manages multiple Abaqus/CAE sessions, each with its own TCP endpoint."""

    def __init__(self):
        self._sessions: dict[str, dict[str, Any]] = {}
        self._active_id: str | None = None

    def add(self, host: str, port: int, info: dict | None = None) -> str:
        """Register a session. Returns session_id."""
        for sid, sess in self._sessions.items():
            if sess["host"] == host and sess["port"] == port:
                if info:
                    sess["info"].update(info)
                return sid
        session_id = f"session_{port}"
        self._sessions[session_id] = {
            "host": host,
            "port": port,
            "info": info or {},
        }
        if self._active_id is None:
            self._active_id = session_id
        return session_id

    def get(self, session_id: str) -> dict[str, Any] | None:
        return self._sessions.get(session_id)

    def get_active(self) -> dict[str, Any] | None:
        if self._active_id is None:
            return None
        return self._sessions.get(self._active_id)

    def get_active_id(self) -> str | None:
        return self._active_id

    def set_active(self, session_id: str) -> bool:
        if session_id not in self._sessions:
            return False
        self._active_id = session_id
        return True

    def remove(self, session_id: str) -> bool:
        if session_id not in self._sessions:
            return False
        del self._sessions[session_id]
        if self._active_id == session_id:
            self._active_id = next(iter(self._sessions), None)
        return True

    def list_all(self) -> list[dict[str, Any]]:
        result = []
        for sid, sess in self._sessions.items():
            result.append({
                "session_id": sid,
                "host": sess["host"],
                "port": sess["port"],
                "active": sid == self._active_id,
                "info": sess.get("info", {}),
            })
        return result

    def resolve_endpoint(self, session_id: str | None = None) -> tuple[str, int]:
        """Resolve (host, port) for a session_id, falling back to active/default."""
        if session_id:
            sess = self._sessions.get(session_id)
            if sess is None:
                raise ValueError(f"Unknown session_id: {session_id}")
            return sess["host"], sess["port"]
        sess = self.get_active()
        if sess:
            return sess["host"], sess["port"]
        return DEFAULT_HOST, DEFAULT_PORT

    def discover(self) -> list[dict[str, Any]]:
        """Scan port range for available Abaqus bridges and register them."""
        found = []
        for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
            try:
                with socket.create_connection((DEFAULT_HOST, port), timeout=2.0) as sock:
                    sock.settimeout(3.0)
                    payload = {
                        "id": str(uuid.uuid4()),
                        "method": "ping",
                        "params": {"timeout": 3.0},
                    }
                    _send_message(sock, payload)
                    resp = _read_message(sock, io_timeout=3.0)
                    if resp.get("ok") and resp.get("result"):
                        info = resp["result"]
                        sid = self.add(DEFAULT_HOST, port, info)
                        found.append({
                            "session_id": sid,
                            "host": DEFAULT_HOST,
                            "port": port,
                            "info": info,
                        })
            except (ConnectionRefusedError, TimeoutError, OSError, ProtocolError):
                continue
        return found


_session_manager = SessionManager()


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

def _send_message(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sock.sendall(data + b"\n")


def _read_message(sock: socket.socket, max_bytes: int = MAX_MESSAGE_BYTES, io_timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + io_timeout

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("socket message deadline elapsed")
        sock.settimeout(remaining)
        chunk = sock.recv(4096)
        if not chunk:
            raise ProtocolError("socket closed before a complete message was received")

        newline = chunk.find(b"\n")
        total += newline if newline >= 0 else len(chunk)
        if total > max_bytes:
            raise ProtocolError(f"message exceeded {max_bytes} bytes")
        if newline >= 0:
            chunks.append(chunk[:newline])
            break

        chunks.append(chunk)

    try:
        message = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("Abaqus bridge returned invalid UTF-8/JSON: %s" % exc) from exc
    if not isinstance(message, dict):
        raise ProtocolError("protocol message must be a JSON object")
    return message


def _request(
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
    host: str | None = None,
    port: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    effective_timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    if not math.isfinite(effective_timeout) or effective_timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    effective_host = host or DEFAULT_HOST
    effective_port = port or DEFAULT_PORT
    payload = {
        "id": request_id if request_id is not None else str(uuid.uuid4()),
        "method": method,
        "params": {**(params or {}), "timeout": effective_timeout},
    }
    with socket.create_connection((effective_host, effective_port), timeout=effective_timeout) as sock:
        sock.settimeout(effective_timeout)
        _send_message(sock, payload)
        response = _read_message(sock, io_timeout=effective_timeout)
    if response.get("id") != payload["id"]:
        raise ProtocolError("Abaqus bridge returned a mismatched response id")
    if not response.get("ok", False):
        error = response.get("error") or {}
        if isinstance(error, dict):
            details = {key: value for key, value in error.items() if key not in ("code", "message")}
            raise RequestError(error.get("code", "BRIDGE_ERROR"), error.get("message", "bridge error"), **details)
        raise RequestError("BRIDGE_ERROR", str(error))
    result = response.get("result")
    if not isinstance(result, dict):
        raise ProtocolError("Abaqus bridge returned an invalid result envelope")
    return result


async def _bridge_request(
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    # Capture once: active-session changes cannot redirect timeout recovery.
    host, port = _session_manager.resolve_endpoint(session_id)
    stable_id = request_id if request_id is not None else str(uuid.uuid4())
    if not isinstance(stable_id, str) or not stable_id.strip() or len(stable_id) > 200:
        raise ValueError("request_id must be a non-empty string of at most 200 characters")
    if method == "execute":
        _remember_endpoint(stable_id, (host, port))
    try:
        result = await asyncio.to_thread(_request, method, params, timeout, host, port, stable_id)
    except RequestError:
        raise
    except ConnectionRefusedError as exc:
        raise RuntimeError(
            f"Cannot connect to the Abaqus GUI bridge at {host}:{port} "
            f"(session_id={session_id or _session_manager.get_active_id() or 'default'}, request_id={stable_id}). "
            f"Original error: {exc}"
        ) from exc
    except (TimeoutError, OSError, ProtocolError) as exc:
        if method != "execute":
            raise RuntimeError(f"Bridge request failed at {host}:{port}: {exc}") from exc
        # Query ONLY the original operation. Never repeat execute, change target,
        # unlock the GUI, or recommend a restart to resolve an uncertain outcome.
        try:
            status = await asyncio.to_thread(_request, "request_status", {"request_id": stable_id},
                                             5.0, host, port)
        except Exception as status_exc:
            raise RequestError("PENDING", "transport failed; outcome unknown; query status, do not replay",
                               request_id=stable_id, state="unknown", endpoint=f"{host}:{port}",
                               status_error=str(status_exc)) from exc
        if status.get("completed") and isinstance(status.get("result"), dict):
            result = status["result"]
        else:
            state = status.get("state", "unknown")
            code = "EXPIRED" if state == "expired" else "PENDING"
            raise RequestError(code, "transport wait ended; query original status, do not replay",
                               request_id=stable_id, state=state, endpoint=f"{host}:{port}") from exc
    if method == "execute":
        result = dict(result, request_id=stable_id)
    return result


async def _exec(code: str, timeout: float | None = None, session_id: str | None = None,
                request_id: str | None = None) -> dict[str, Any]:
    return await _bridge_request("execute", {"code": code}, timeout, session_id=session_id, request_id=request_id)


def _json_string(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _format_error_to_markdown(result: dict[str, Any]) -> str:
    error_type = str(result.get("error_type", "Error")).rsplit(".", 1)[-1]
    core_error = result.get("core_error", "Unknown error")
    error_line = result.get("error_line")
    location = f" at line {error_line}" if error_line else ""
    parts = [f"{error_type}{location}: {core_error}"]
    if result.get("request_id"):
        parts.append(f"  request_id: {result['request_id']}")

    recovery = result.get("recovery")
    if isinstance(recovery, dict) and recovery:
        if recovery.get("parent_object_path"):
            parts.append(f"  Object: {recovery['parent_object_path']}")
        if recovery.get("available_keys_sample"):
            sample = recovery["available_keys_sample"]
            if isinstance(sample, list) and len(sample) > 20:
                sample = sample[:20] + ["..."]
            parts.append(f"  Available: {sample}")
        if recovery.get("possible_keys"):
            parts.append(f"  Similar keys: {recovery['possible_keys']}")
        if recovery.get("possible_members"):
            parts.append(f"  Similar members: {recovery['possible_members']}")
        if recovery.get("callable_signature"):
            parts.append(f"  Signature: {recovery['callable_signature']}")
        if recovery.get("possible_keywords"):
            parts.append(f"  Similar keywords: {recovery['possible_keywords']}")
        if recovery.get("import_suggestion"):
            parts.append(f"  Import suggestion: {recovery['import_suggestion']}")

    code_excerpt = result.get("code_excerpt")
    if code_excerpt:
        parts.append("  Code excerpt:")
        parts.extend("    " + line for line in str(code_excerpt).splitlines())

    stdout = str(result.get("stdout") or "").strip()
    stderr = str(result.get("stderr") or "").strip()
    if stdout:
        parts.append(f"  stdout: {stdout}")
    if stderr:
        parts.append(f"  stderr: {stderr}")
    return "\n".join(parts)


def _unwrap_execution_result(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok", False):
        raise RuntimeError(_format_error_to_markdown(result))
    return result


# ---------------------------------------------------------------------------
# Session Management Tools (new in v5.1)
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_sessions() -> str:
    """List all known Abaqus/CAE sessions.

    Returns JSON with session_id, host, port, active flag, and Abaqus metadata
    for each connected instance.
    """
    sessions = _session_manager.list_all()
    if not sessions:
        return "No sessions registered. Use discover_sessions() to scan for Abaqus instances."
    return _json_string(sessions)


@mcp.tool()
async def discover_sessions() -> str:
    """Scan ports 48152-48162 for available Abaqus/CAE socket bridges.

    Automatically registers all found instances. The first discovered session
    becomes the active session.
    """
    found = _session_manager.discover()
    if not found:
        return (
            "No Abaqus/CAE socket bridges found. Please start Abaqus/CAE and run "
            "Plug-ins > wmw-abaqus-mcp > Start Socket Bridge in each instance."
        )
    active = _session_manager.get_active_id()
    return (
        f"Discovered {len(found)} Abaqus session(s):\n"
        + _json_string(found)
        + f"\nActive session: {active}"
    )


@mcp.tool()
async def set_active_session(session_id: str) -> str:
    """Set the active Abaqus session for subsequent tool calls.

    Args:
        session_id: Session identifier (e.g. "session_48152"), as returned by
                    list_sessions() or discover_sessions().
    """
    if not session_id.strip():
        raise ValueError("session_id must not be empty")
    sid = session_id.strip()
    if _session_manager.set_active(sid):
        sess = _session_manager.get(sid)
        return (
            f"Active session set to {sid} "
            f"(endpoint: {sess['host']}:{sess['port']})"
        )
    raise ValueError(
        f"Session '{sid}' not found. Use list_sessions() to see available sessions."
    )


# ---------------------------------------------------------------------------
# Original Tools (now with optional session_id parameter)
# ---------------------------------------------------------------------------

@mcp.tool()
async def ping(timeout: float | None = None, session_id: str | None = None) -> dict[str, Any]:
    """Check whether the Abaqus-side socket bridge is reachable.

    Args:
        session_id: Target a specific session. If omitted, uses the active session.
    """
    return await _bridge_request("ping", timeout=timeout or 10.0, session_id=session_id)


@mcp.tool()
async def get_request_status(request_id: str, session_id: str | None = None) -> dict[str, Any]:
    """Read a known operation without executing it again.

    Saved terminal results are bounded and local to the CAE process. Unknown or
    evicted IDs must not trigger automatic replay. With no session_id, a recent
    execution's captured endpoint is used even after the active session changes.
    """
    if not request_id.strip():
        raise ValueError("request_id must not be empty")
    if session_id is not None:
        endpoint = _session_manager.resolve_endpoint(session_id)
    elif request_id in _request_endpoints:
        endpoint = _request_endpoints[request_id]
        if endpoint is None:
            raise ValueError("request_id was used on multiple sessions; specify session_id")
    else:
        endpoint = _session_manager.resolve_endpoint()
    return await asyncio.to_thread(_request, "request_status", {"request_id": request_id}, 5.0, *endpoint)


@mcp.tool()
async def check_abaqus_connection(timeout: float | None = None, session_id: str | None = None) -> str:
    """Return a concise human-readable bridge status.

    Args:
        session_id: Target a specific session. If omitted, uses the active session.
    """
    info = await ping(timeout=timeout or 10.0, session_id=session_id)
    models = info.get("models", [])
    viewports = info.get("viewports", [])
    version = info.get("abaqus_version") or "unknown"
    host, port = _session_manager.resolve_endpoint(session_id)
    active_id = _session_manager.get_active_id() or "default"
    return (
        f"Connected to Abaqus socket bridge at {host}:{port} (session: {active_id}).\n"
        f"Abaqus version: {version}\n"
        f"Models: {models}\n"
        f"Viewports: {viewports}"
    )


@mcp.tool()
async def run_python(code: str, timeout: float | None = None, session_id: str | None = None, request_id: str | None = None) -> dict[str, Any]:
    """Execute Python code in the active (or specified) Abaqus/CAE kernel.

    Single-line expressions are evaluated and returned. Multi-line scripts are
    executed; set a variable named ``result`` to return structured data.

    Args:
        code: Python code to execute.
        session_id: Target a specific Abaqus session. If omitted, uses the active session.
        request_id: Optional stable operation ID. Reuse only for identical code.
    """
    if not code.strip():
        raise ValueError("code must not be empty")
    return _unwrap_execution_result(await _exec(code, timeout, session_id=session_id, request_id=request_id))


@mcp.tool()
async def execute_script(script: str, timeout: float | None = None, session_id: str | None = None, request_id: str | None = None) -> str:
    """Compatibility wrapper around run_python that returns stdout/text.

    Args:
        script: Python script to execute.
        session_id: Target a specific Abaqus session. If omitted, uses the active session.
        request_id: Optional stable operation ID for duplicate detection/status recovery.
    """
    result = await run_python(script, timeout, session_id=session_id, request_id=request_id)
    stdout = str(result.get("stdout") or "")
    returned = result.get("return_value")
    if returned is not None:
        if stdout:
            return stdout + "\n" + _json_string(returned)
        return _json_string(returned)
    return stdout if stdout else "(Script executed successfully, no output)"


@mcp.tool()
async def set_workdir(path: str, timeout: float | None = None, session_id: str | None = None) -> dict[str, Any]:
    """Change the current Abaqus working directory.

    Args:
        path: New working directory path.
        session_id: Target a specific Abaqus session.
    """
    if not path.strip():
        raise ValueError("path must not be empty")
    code = r"""
import os

new_path = __PATH__
old_dir = os.getcwd()
if not os.path.isdir(new_path):
    raise OSError("Directory does not exist: " + new_path)
os.chdir(new_path)
result = {"success": True, "previous": old_dir, "current": os.getcwd()}
""".replace("__PATH__", json.dumps(path.strip()))
    return (await run_python(code, timeout, session_id=session_id)).get("return_value")


@mcp.tool()
async def get_model_info(timeout: float | None = None, session_id: str | None = None) -> str:
    """Get parts, materials, steps, loads, BCs, interactions, jobs, and viewports.

    Args:
        session_id: Target a specific Abaqus session.
    """
    code = r"""
from abaqus import mdb, session

def _keys(obj):
    try:
        return list(obj.keys())
    except Exception:
        return []

models = {}
for model_name in mdb.models.keys():
    model = mdb.models[model_name]
    model_info = {
        "parts": _keys(model.parts),
        "materials": _keys(model.materials),
        "sections": _keys(model.sections),
        "steps": _keys(model.steps),
        "loads": _keys(model.loads),
        "boundary_conditions": _keys(model.boundaryConditions),
        "interactions": _keys(model.interactions),
        "constraints": _keys(model.constraints),
        "amplitudes": _keys(model.amplitudes),
        "assembly_instances": _keys(model.rootAssembly.instances),
        "sets": _keys(model.rootAssembly.sets),
        "surfaces": _keys(model.rootAssembly.surfaces),
    }
    part_details = {}
    for part_name in model.parts.keys():
        part = model.parts[part_name]
        part_details[part_name] = {
            "cells": len(getattr(part, "cells", [])),
            "faces": len(getattr(part, "faces", [])),
            "edges": len(getattr(part, "edges", [])),
            "vertices": len(getattr(part, "vertices", [])),
            "sets": _keys(part.sets),
            "surfaces": _keys(part.surfaces),
        }
    model_info["part_details"] = part_details
    models[model_name] = model_info

jobs = []
for job_name in mdb.jobs.keys():
    job = mdb.jobs[job_name]
    item = {"name": job_name}
    for attr in ("status", "type", "model", "description", "numCpus", "numDomains", "memory"):
        try:
            value = getattr(job, attr, None)
            if value is not None:
                item[attr] = str(value)
        except Exception:
            pass
    jobs.append(item)

result = {
    "models": models,
    "jobs": jobs,
    "current_viewport": getattr(session, "currentViewportName", None),
    "viewports": list(session.viewports.keys()) if hasattr(session, "viewports") else [],
}
"""
    return _json_string((await run_python(code, timeout, session_id=session_id)).get("return_value"))


@mcp.tool()
async def list_jobs(timeout: float | None = None, session_id: str | None = None) -> str:
    """List all Abaqus jobs in the current CAE session.

    Args:
        session_id: Target a specific Abaqus session.
    """
    code = r"""
from abaqus import mdb

jobs = []
for name in mdb.jobs.keys():
    job = mdb.jobs[name]
    item = {"name": name}
    for attr in ("status", "type", "model", "description", "numCpus", "numDomains", "memory", "explicitPrecision"):
        try:
            value = getattr(job, attr, None)
            if value is not None:
                item[attr] = str(value)
        except Exception:
            pass
    jobs.append(item)
result = {"jobs": jobs}
"""
    return _json_string((await run_python(code, timeout, session_id=session_id)).get("return_value"))


@mcp.tool()
async def submit_job(job_name: str, timeout: float | None = None, session_id: str | None = None,
                     request_id: str | None = None) -> str:
    """Submit an existing job and return an acknowledgement immediately.

    Submission is not solver completion. Use monitor_job_status separately.
    request_id is an optional stable ID for this particular submission operation.
    """
    if not job_name.strip():
        raise ValueError("job_name must not be empty")
    code = r"""
import os
import weakref
from abaqus import mdb

job_name = __JOB_NAME__
if job_name not in mdb.jobs:
    raise KeyError("Job not found: " + job_name)
job = mdb.jobs[job_name]
workdir = os.path.abspath(os.getcwd())
job.submit(consistencyChecking=False)
# No strong references to old databases/jobs are kept in this bounded cache.
try:
    mdb_ref, job_ref = weakref.ref(mdb), weakref.ref(job)
    identity_error = None
except Exception as exc:
    mdb_ref, job_ref = None, None
    identity_error = "identity_unavailable: " + str(exc)
submission_records = globals().setdefault("_ABAQUS_MCP_JOB_WORKDIRS", {})
submission_records.pop(job_name, None)
while len(submission_records) >= 128:
    del submission_records[next(iter(submission_records))]
submission_records[job_name] = {"workdir": workdir, "mdb_ref": mdb_ref, "job_ref": job_ref, "identity_error": identity_error}
result = {"submitted": True, "job": job_name, "status": str(getattr(job, "status", "UNKNOWN")), "workdir": workdir}
""".replace("__JOB_NAME__", json.dumps(job_name.strip()))
    return _json_string((await run_python(code, timeout, session_id=session_id, request_id=request_id)).get("return_value"))


@mcp.tool()
async def monitor_job_status(job_name: str = "", timeout: float | None = None, session_id: str | None = None) -> dict[str, Any]:
    """Read actual job.status and bounded log tails; never control the solver.

    Named jobs use the absolute directory captured by submit_job when available.
    Missing jobs/files and read failures are explicit; logs are not proof of exit.
    """
    code = r"""
import os
import re
from abaqus import mdb

job_name = __JOB_NAME__
MAX_TAIL_BYTES = 128 * 1024
file_errors = {}

def _bounded_tail(path, key):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            offset = max(0, size - MAX_TAIL_BYTES)
            handle.seek(offset)
            lines = handle.read(MAX_TAIL_BYTES).decode("utf-8", errors="replace").splitlines()
        if offset and lines:
            lines = lines[1:]
        return lines
    except FileNotFoundError as exc:
        file_errors[key] = "missing: " + str(exc)
    except Exception as exc:
        file_errors[key] = "read failed: " + str(exc)
    return []

if not job_name:
    jobs = []
    for name in mdb.jobs.keys():
        job = mdb.jobs[name]
        item = {"name": name}
        for attr in ("status", "type", "model", "description", "numCpus", "numDomains", "memory"):
            try:
                value = getattr(job, attr, None)
                if value is not None:
                    item[attr] = str(value)
            except Exception as exc:
                item[attr + "_error"] = str(exc)
        jobs.append(item)
    result = {"jobs": jobs, "workdir": os.path.abspath(os.getcwd())}
else:
    found = job_name in mdb.jobs
    current_job = mdb.jobs[job_name] if found else None
    submission_record = globals().get("_ABAQUS_MCP_JOB_WORKDIRS", {}).get(job_name)
    saved_workdir = None
    workdir_fallback_reason = "no_confirmed_submission"
    if submission_record is not None:
        if not isinstance(submission_record, dict):
            workdir_fallback_reason = "legacy_submission_identity_unavailable"
        elif submission_record.get("identity_error") or not callable(submission_record.get("mdb_ref")) or not callable(submission_record.get("job_ref")):
            workdir_fallback_reason = "submission_identity_unavailable"
        elif submission_record["mdb_ref"]() is not mdb:
            workdir_fallback_reason = "mdb_identity_changed"
        elif submission_record["job_ref"]() is not current_job or current_job is None:
            workdir_fallback_reason = "job_identity_changed"
        else:
            saved_workdir = submission_record.get("workdir")
    workdir = saved_workdir or os.path.abspath(os.getcwd())
    sta_path = os.path.join(workdir, job_name + ".sta")
    msg_path = os.path.join(workdir, job_name + ".msg")
    status = None
    status_error = None
    if found:
        try:
            status = str(current_job.status)
        except Exception as exc:
            status_error = "job.status read failed: " + str(exc)
    progress = _bounded_tail(sta_path, "sta")
    messages = _bounded_tail(msg_path, "msg")
    rx = re.compile(r"^\*\*\*(ERROR|WARNING)")
    result = {
        "job_name": job_name, "job_found": found, "status": status,
        "workdir": workdir, "workdir_source": "submission" if saved_workdir else "current_directory_fallback",
        "sta_path": sta_path, "msg_path": msg_path,
        "progress_tail": progress[-8:],
        "diagnostics_tail": [line for line in messages if rx.search(line)][-12:],
        "file_errors": file_errors, "tail_max_bytes": MAX_TAIL_BYTES,
    }
    if not found:
        result["error"] = "Job not found: " + job_name
    if not saved_workdir:
        result["workdir_fallback_reason"] = workdir_fallback_reason
    if status_error:
        result["status_error"] = status_error
""".replace("__JOB_NAME__", json.dumps(job_name.strip()))
    return (await run_python(code, timeout, session_id=session_id)).get("return_value")


@mcp.tool()
async def inspect_odb(odb_path: str, timeout: float | None = None, session_id: str | None = None) -> dict[str, Any]:
    """Open an ODB read-only and return metadata about steps, frames, and outputs.

    Args:
        odb_path: Path to the .odb file.
        session_id: Target a specific Abaqus session.
    """
    if not odb_path.strip():
        raise ValueError("odb_path must not be empty")
    code = r"""
from odbAccess import openOdb

odb_path = __ODB_PATH__
odb = None
try:
    odb = openOdb(path=odb_path, readOnly=True)
    steps = []

    def _slice_frames(frames):
        count = len(frames)
        if count <= 5:
            return [(i, frames[i]) for i in range(count)]
        idxs = [0, int(round((count - 1) * 0.25)), int(round((count - 1) * 0.5)), int(round((count - 1) * 0.75)), count - 1]
        seen = []
        for idx in idxs:
            if idx not in seen:
                seen.append(idx)
        return [(i, frames[i]) for i in seen]

    for step_name in odb.steps.keys():
        step = odb.steps[step_name]
        frame_items = []
        for idx, frame in _slice_frames(step.frames):
            frame_items.append({
                "index": idx,
                "frameId": frame.frameId,
                "frameValue": frame.frameValue,
                "description": str(getattr(frame, "description", "")),
            })

        field_outputs = []
        history_outputs = []
        if step.frames:
            try:
                for key in step.frames[-1].fieldOutputs.keys():
                    field = step.frames[-1].fieldOutputs[key]
                    field_outputs.append({
                        "name": key,
                        "position": str(getattr(field, "position", "")),
                        "components": list(getattr(field, "componentLabels", []) or []),
                        "validInvariants": [str(x) for x in (getattr(field, "validInvariants", []) or [])],
                    })
            except Exception:
                pass
            try:
                history_outputs = list(step.historyRegions.keys())
            except Exception:
                pass

        steps.append({
            "name": step_name,
            "procedure": str(getattr(step, "procedure", "")),
            "totalTime": getattr(step, "totalTime", 0.0),
            "frame_count": len(step.frames),
            "frames": frame_items,
            "fieldOutputs": field_outputs,
            "historyRegions": history_outputs,
        })

    result = {
        "title": str(getattr(odb, "title", "")),
        "description": str(getattr(odb, "description", "")),
        "parts": list(odb.parts.keys()) if hasattr(odb, "parts") else [],
        "instances": list(odb.rootAssembly.instances.keys()) if hasattr(odb, "rootAssembly") else [],
        "steps": steps,
    }
finally:
    if odb is not None:
        odb.close()
""".replace("__ODB_PATH__", json.dumps(odb_path.strip()))
    return (await run_python(code, timeout or 120.0, session_id=session_id)).get("return_value")


@mcp.tool()
async def get_odb_info(odb_path: str, timeout: float | None = None, session_id: str | None = None) -> str:
    """Compatibility wrapper for inspect_odb returning formatted JSON.

    Args:
        odb_path: Path to the .odb file.
        session_id: Target a specific Abaqus session.
    """
    return _json_string(await inspect_odb(odb_path, timeout, session_id=session_id))


@mcp.tool()
async def capture_viewport(
    viewport_name: str = "",
    image_format: str = "PNG",
    timeout: float | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Capture an Abaqus viewport as base64 image data.

    Args:
        viewport_name: Name of the viewport to capture (empty = current).
        image_format: Image format (PNG, TIFF, SVG, EPS, PS).
        session_id: Target a specific Abaqus session.
    """
    code = r"""
import os
import tempfile
import base64
from abaqus import session
import abaqusConstants

vp_name = __VP_NAME__
fmt_name = __FORMAT__.upper()
fmt_map = {
    "PNG": abaqusConstants.PNG,
    "TIFF": abaqusConstants.TIFF,
    "SVG": abaqusConstants.SVG,
    "EPS": abaqusConstants.EPS,
    "PS": abaqusConstants.PS,
}
fmt = fmt_map.get(fmt_name, abaqusConstants.PNG)

if not vp_name or vp_name not in session.viewports.keys():
    vp_name = session.currentViewportName
vp = session.viewports[vp_name]
suffix = "." + fmt_name.lower()
handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
tmp_path = handle.name
handle.close()

try:
    session.printToFile(fileName=tmp_path, format=fmt, canvasObjects=(vp,))
    with open(tmp_path, "rb") as image_file:
        image_base64 = base64.b64encode(image_file.read()).decode("ascii")
    result = {
        "success": True,
        "viewport": vp_name,
        "format": fmt_name.lower(),
        "image_base64": image_base64,
        "size_bytes": int(len(image_base64) * 3 / 4),
    }
finally:
    try:
        os.unlink(tmp_path)
    except Exception:
        pass
""".replace("__VP_NAME__", json.dumps(viewport_name.strip())).replace("__FORMAT__", json.dumps(image_format.strip() or "PNG"))
    return (await run_python(code, timeout or 60.0, session_id=session_id)).get("return_value")


@mcp.tool()
async def get_viewport_image(
    viewport_name: str = "",
    image_format: str = "PNG",
    timeout: float | None = None,
    session_id: str | None = None,
) -> str:
    """Compatibility wrapper returning a data URI for the requested viewport.

    Args:
        viewport_name: Name of the viewport to capture (empty = current).
        image_format: Image format (PNG, TIFF, SVG, EPS, PS).
        session_id: Target a specific Abaqus session.
    """
    data = await capture_viewport(viewport_name, image_format, timeout, session_id=session_id)
    fmt = data.get("format", "png")
    b64 = data.get("image_base64", "")
    return f"data:image/{fmt};base64,{b64}"


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource("abaqus://session-telemetry")
def session_telemetry() -> str:
    """Live Abaqus/CAE session telemetry from the socket bridge."""
    try:
        return _json_string(_request("ping", timeout=5.0))
    except Exception as exc:
        return _json_string({"connected": False, "error": str(exc), "endpoint": f"{DEFAULT_HOST}:{DEFAULT_PORT}"})


@mcp.resource("abaqus://status")
def abaqus_status() -> str:
    """Compatibility status resource."""
    return session_telemetry()


@mcp.resource("abaqus://sessions")
def sessions_resource() -> str:
    """List all registered Abaqus sessions."""
    return _json_string(_session_manager.list_all())


@mcp.resource("abaqus://agent-instructions")
def agent_instructions() -> str:
    """Abaqus modeling instructions for MCP clients."""
    return INSTRUCTIONS


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
