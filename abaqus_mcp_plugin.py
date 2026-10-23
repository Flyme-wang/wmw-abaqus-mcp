# -*- coding: utf-8 -*-
"""
wmw-abaqus-mcp v6.0.0 - socket GUI bridge with single-flight control.

This file runs inside Abaqus/CAE. It registers a Plug-ins menu, starts a local
TCP server, dispatches requests on the Abaqus GUI thread, and executes Python in
the Abaqus kernel through sendCommand.

v5.1: Auto-port allocation (48152-48162) enables multiple Abaqus instances
      to run simultaneously. Each instance writes a discovery file for the
      MCP server to find.
"""

from __future__ import print_function

import base64
import json
import os
import platform
try:
    import socketserver
except ImportError:
    import SocketServer as socketserver
import socket as _socket_module
import sys
import tempfile
import threading
import time
import traceback
import uuid

try:
    from bridge_runtime import BridgeError, get_process_registry, positive_timeout
except ImportError:
    # File > Run Script may execute source without adding its home to sys.path.
    # Environment home wins; exec-based loaders need not provide our __file__.
    _runtime_homes = [os.environ.get("ABAQUS_MCP_HOME")]
    if globals().get("__file__"):
        _runtime_homes.append(os.path.dirname(os.path.abspath(__file__)))
    for _runtime_home in _runtime_homes:
        if _runtime_home and os.path.isfile(os.path.join(_runtime_home, "bridge_runtime.py")):
            if _runtime_home not in sys.path:
                sys.path.insert(0, _runtime_home)
            break
    from bridge_runtime import BridgeError, get_process_registry, positive_timeout

from abaqusGui import (
    AFXForm,
    AFXMode,
    FXMAPFUNC,
    SEL_COMMAND,
    SEL_TIMEOUT,
    getAFXApp,
    sendCommand,
    showAFXErrorDialog,
)

__version__ = "6.0.0"

HOST = os.environ.get("ABAQUS_MCP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("ABAQUS_MCP_PORT", "48152"))
PORT_RANGE_START = int(os.environ.get("ABAQUS_MCP_PORT_RANGE_START", "48152"))
PORT_RANGE_END = int(os.environ.get("ABAQUS_MCP_PORT_RANGE_END", "48162"))
DEFAULT_TIMEOUT = float(os.environ.get("ABAQUS_MCP_TIMEOUT", "60"))
LOG_PATH = os.environ.get(
    "ABAQUS_MCP_LOG",
    os.path.join(tempfile.gettempdir(), "abaqus_mcp_socket_bridge.log"),
)
DISCOVERY_DIR = os.path.join(tempfile.gettempdir(), "abaqus_mcp_sessions")


def _log(message):
    try:
        with open(LOG_PATH, "a") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


def _announce(message):
    print(message)
    try:
        main_window = getAFXApp().getAFXMainWindow()
        if hasattr(main_window, "writeToMessageArea"):
            main_window.writeToMessageArea(message)
    except Exception:
        pass


def _is_port_available(port):
    """Return True if host:port can be bound by the bridge."""
    try:
        test_sock = _socket_module.socket(_socket_module.AF_INET, _socket_module.SOCK_STREAM)
        test_sock.setsockopt(_socket_module.SOL_SOCKET, _socket_module.SO_REUSEADDR, 1)
        test_sock.bind((HOST, port))
        test_sock.close()
        return True
    except (_socket_module.error, OSError):
        return False


def _find_available_port(preferred_port=None):
    """Find an available port, optionally honoring a user-selected port first."""
    if preferred_port is not None:
        if _is_port_available(preferred_port):
            return preferred_port
        raise RuntimeError("Requested port %d is not available on %s" % (preferred_port, HOST))

    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if _is_port_available(port):
            return port
    raise RuntimeError("No available port in range %d-%d" % (PORT_RANGE_START, PORT_RANGE_END))


def _write_discovery_file(port):
    """Write a discovery file so the MCP server can find this instance."""
    try:
        if not os.path.isdir(DISCOVERY_DIR):
            os.makedirs(DISCOVERY_DIR)
        discovery_path = os.path.join(DISCOVERY_DIR, "port_%d.json" % port)
        data = {
            "port": port,
            "host": HOST,
            "pid": os.getpid(),
            "version": __version__,
            "start_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(discovery_path, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        _log("discovery file written: %s" % discovery_path)
        return discovery_path
    except Exception as exc:
        _log("failed to write discovery file: %s" % exc)
        return None


def _remove_discovery_file(port):
    """Remove the discovery file for this instance."""
    try:
        discovery_path = os.path.join(DISCOVERY_DIR, "port_%d.json" % port)
        if os.path.exists(discovery_path):
            os.remove(discovery_path)
            _log("discovery file removed: %s" % discovery_path)
    except Exception as exc:
        _log("failed to remove discovery file: %s" % exc)


# Global port (set during startup, allows auto-allocation)
PORT = DEFAULT_PORT
_DISCOVERY_PATH = None


def _send(sock, payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sock.sendall(data + b"\n")


def _recv(sock, io_timeout=10.0):
    chunks = []
    total = 0
    max_bytes = int(os.environ.get("ABAQUS_MCP_MAX_MESSAGE_BYTES", str(32 * 1024 * 1024)))
    deadline = time.monotonic() + io_timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("socket message deadline elapsed")
        sock.settimeout(remaining)
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("socket closed before a complete message was received")
        newline = chunk.find(b"\n")
        total += newline if newline >= 0 else len(chunk)
        if total > max_bytes:
            raise RuntimeError("message exceeded %d bytes" % max_bytes)
        if newline >= 0:
            chunks.append(chunk[:newline])
            break
        chunks.append(chunk)
    message = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("protocol message must be a JSON object")
    return message


def _kernel_wrapper(code, response_path):
    encoded_code = base64.b64encode(code.encode("utf-8")).decode("ascii")
    encoded_path = base64.b64encode(response_path.encode("utf-8")).decode("ascii")
    template = r'''
import ast
import base64
import contextlib
import difflib
import io
import inspect
import json
import os
import re
import sys
import traceback

code = base64.b64decode("__ABAQUS_MCP_CODE__").decode("utf-8")
response_path = base64.b64decode("__ABAQUS_MCP_RESPONSE__").decode("utf-8")
MAX_OUTPUT = 4000

def _jsonable(value):
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except Exception:
        return {
            "repr": repr(value),
            "type": "%s.%s" % (type(value).__module__, type(value).__name__),
        }

def _node_source(node):
    try:
        return ast.unparse(node)
    except Exception:
        return None

def _extract_lineno(exc):
    if hasattr(exc, "lineno") and getattr(exc, "lineno") is not None:
        return getattr(exc, "lineno")
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == "<abaqus-mcp>":
            return tb.tb_lineno
        tb = tb.tb_next
    return None

def _extract_excerpt(source, lineno, radius=2):
    if lineno is None:
        return None
    lines = source.splitlines()
    idx = lineno - 1
    if idx < 0 or idx >= len(lines):
        return None
    start = max(0, idx - radius)
    end = min(len(lines), idx + radius + 1)
    out = []
    for i in range(start, end):
        prefix = ">>" if i == idx else "  "
        out.append("%s %4d | %s" % (prefix, i + 1, lines[i]))
    return "\n".join(out)

def _resolve_expr(expr, namespace):
    def _eval_node(node):
        if isinstance(node, ast.Name):
            return namespace[node.id]
        if isinstance(node, ast.Attribute):
            return getattr(_eval_node(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            base = _eval_node(node.value)
            try:
                key = ast.literal_eval(node.slice)
            except Exception:
                if hasattr(ast, "Index") and isinstance(node.slice, ast.Index):
                    key = ast.literal_eval(node.slice.value)
                else:
                    raise
            return base[key]
        raise ValueError("unsupported expression")
    return _eval_node(ast.parse(expr, mode="eval").body)

def _parent_expr_for_subscript(source, lineno):
    try:
        tree = ast.parse(source)
    except Exception:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and getattr(node, "lineno", None) == lineno:
            return _node_source(node.value)
    return None

def _parent_expr_for_attribute(source, attr_name):
    try:
        tree = ast.parse(source)
    except Exception:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == attr_name:
            return _node_source(node.value)
    return None

def _call_target(source, lineno):
    try:
        tree = ast.parse(source)
    except Exception:
        return None
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", start)
            if start is not None and end is not None and start <= lineno <= end:
                calls.append(node)
    if not calls:
        return None
    calls.sort(key=lambda n: getattr(n, "end_lineno", getattr(n, "lineno", 0)) - getattr(n, "lineno", 0))
    return _node_source(calls[0].func)

def _summarize_error(source, exc, namespace):
    tb = traceback.format_exc()
    tb_lines = [line for line in tb.strip().splitlines() if line.strip()]
    lineno = _extract_lineno(exc)
    core_error = tb_lines[-1] if tb_lines else str(exc)
    error_type = "%s.%s" % (type(exc).__module__, type(exc).__name__)
    recovery = {}

    if isinstance(exc, KeyError):
        missing = exc.args[0] if exc.args else None
        parent = _parent_expr_for_subscript(source, lineno)
        recovery = {"missing_key": _jsonable(missing), "parent_object_path": parent}
        if parent:
            try:
                obj = _resolve_expr(parent, namespace)
                keys = [str(k) for k in list(obj.keys())]
                recovery["available_keys_sample"] = keys
                recovery["possible_keys"] = difflib.get_close_matches(str(missing), keys, n=8, cutoff=0.45)
            except Exception:
                pass
    elif isinstance(exc, AttributeError):
        missing_attr = getattr(exc, "name", None)
        parent = _parent_expr_for_attribute(source, missing_attr) if missing_attr else None
        recovery = {"missing_attribute": missing_attr, "parent_object_path": parent}
        if parent:
            try:
                obj = _resolve_expr(parent, namespace)
                members = [name for name in dir(obj) if not name.startswith("_")]
                recovery["possible_members"] = difflib.get_close_matches(str(missing_attr), members, n=10, cutoff=0.45)
            except Exception:
                pass
    elif type(exc).__name__ == "NameError":
        missing = getattr(exc, "name", None)
        if not missing:
            match = re.search(r"name '(\w+)' is not defined", str(exc))
            if match:
                missing = match.group(1)
        recovery = {"missing_variable": missing}
        if missing and (missing.isupper() or missing in ("mesh", "part", "material", "assembly", "step", "interaction", "load", "section", "job")):
            recovery["import_suggestion"] = "from abaqusConstants import *"
    elif isinstance(exc, TypeError):
        target_expr = _call_target(source, lineno)
        recovery = {"call_target": target_expr}
        if target_expr:
            try:
                target = _resolve_expr(target_expr, namespace)
                try:
                    recovery["callable_signature"] = "%s%s" % (getattr(target, "__name__", target_expr), inspect.signature(target))
                except Exception:
                    recovery["callable_signature"] = getattr(target, "__doc__", None)
            except Exception:
                pass

    return {
        "ok": False,
        "core_error": core_error,
        "error_type": error_type,
        "error_line": lineno,
        "code_excerpt": _extract_excerpt(source, lineno),
        "traceback_tail": tb_lines[-8:],
        "recovery": recovery,
    }

try:
    from abaqus import mdb, session
except Exception:
    mdb = globals().get("mdb")
    session = globals().get("session")

namespace = globals().setdefault("_ABAQUS_MCP_GLOBALS", {"__name__": "__abaqus_mcp_exec__", "__doc__": None})
namespace.update({"mdb": mdb, "session": session})
namespace.pop("result", None)

stdout = io.StringIO()
stderr = io.StringIO()
returned = None
error_response = None

try:
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            try:
                parsed = ast.parse(code, mode="eval")
            except SyntaxError:
                parsed = ast.parse(code, mode="exec")
                compiled = compile(parsed, "<abaqus-mcp>", "exec")
                exec(compiled, namespace, namespace)
                returned = namespace.get("result")
            else:
                compiled = compile(parsed, "<abaqus-mcp>", "eval")
                returned = eval(compiled, namespace, namespace)
        except Exception as exc:
            error_response = _summarize_error(code, exc, namespace)
except Exception as exc:
    error_response = _summarize_error(code, exc, namespace)

captured_stdout = stdout.getvalue()
captured_stderr = stderr.getvalue()
if len(captured_stdout) > MAX_OUTPUT:
    captured_stdout = captured_stdout[:MAX_OUTPUT] + "\n... (truncated)"
if len(captured_stderr) > MAX_OUTPUT:
    captured_stderr = captured_stderr[:MAX_OUTPUT] + "\n... (truncated)"

if error_response is not None:
    error_response["stdout"] = captured_stdout
    error_response["stderr"] = captured_stderr
    payload = error_response
else:
    payload = {
        "ok": True,
        "return_value": _jsonable(returned),
        "stdout": captured_stdout,
        "stderr": captured_stderr,
    }

temporary_response_path = response_path + ".tmp"
with open(temporary_response_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False)
os.replace(temporary_response_path, response_path)
'''
    return template.replace("__ABAQUS_MCP_CODE__", encoded_code).replace("__ABAQUS_MCP_RESPONSE__", encoded_path)


# Registry survives transport stop/start. No GUI work queue exists in network
# threads: admission, status and heartbeat use only this thread-safe object.
_REGISTRY = get_process_registry()
_SERVER = globals().get("_SERVER")
_SERVER_THREAD = globals().get("_SERVER_THREAD")
_DISPATCHER = globals().get("_DISPATCHER")
_PROCESSED = globals().get("_PROCESSED", 0)
_START_TIME = globals().get("_START_TIME")


def _heartbeat():
    active = _REGISTRY.active_operation()
    observed = _REGISTRY.kernel_ready
    return {
        "connected": True,
        "transport_connected": True,
        # Admission readiness only: manual GUI/kernel work is not observed by
        # this registry and native sendCommand can still block internally.
        "kernel_ready": False if active else (True if observed else None),
        "kernel_response_observed": observed,
        "busy": active is not None,
        "active_request_id": active.request_id if active else None,
        "state": active.state if active else "idle",
        "metadata_status": "unavailable",
        "models": None,
        "viewports": None,
        "bridge": {
            "version": __version__, "host": HOST, "port": PORT,
            "transport": "socket", "processed": _PROCESSED,
            "uptime_seconds": int(time.time() - _START_TIME) if _START_TIME else 0,
            "gui_python": sys.version, "gui_platform": platform.platform(),
            "log": LOG_PATH, "discovery_dir": DISCOVERY_DIR,
        },
    }


class McpGuiHandler(socketserver.BaseRequestHandler):
    def handle(self):
        wire_id = None
        # Slow/incomplete socket peers must not block an unbounded read/write.
        self.request.settimeout(min(DEFAULT_TIMEOUT, 10.0))
        try:
            message = _recv(self.request)
            wire_id = message.get("id")
            method = message.get("method")
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            if method == "ping":
                result = _heartbeat()
            elif method == "stop":
                # Compatibility for explicit stop_mcp.py requests: this closes
                # transport only. It never stops CAE/kernel/solver work.
                _REGISTRY.stop_admission()
                stopper = threading.Thread(target=stop_gui_agent, name="AbaqusMcpTransportStop")
                stopper.daemon = True
                stopper.start()
                result = {"success": True, "message": "transport stop requested; kernel work stays tracked"}
            elif method == "request_status":
                request_id = params.get("request_id")
                if not isinstance(request_id, str) or not request_id.strip():
                    raise ValueError("params.request_id must be a non-empty string")
                result = _REGISTRY.status(request_id)
            elif method == "execute":
                if _DISPATCHER is None:
                    raise RuntimeError("Abaqus MCP GUI dispatcher is not initialized")
                code = params.get("code")
                if not isinstance(code, str) or not code.strip():
                    raise ValueError("params.code must be a non-empty string")
                request_id = params.get("request_id", wire_id)
                wait_timeout = positive_timeout(params.get("timeout", DEFAULT_TIMEOUT), "timeout")
                queue_timeout = positive_timeout(params.get("queue_timeout", wait_timeout), "queue_timeout")
                item = _REGISTRY.admit(request_id, method, params,
                                       queue_timeout=queue_timeout, execution_timeout=wait_timeout)
                result = _REGISTRY.wait(item)
            else:
                raise ValueError("unknown method: %r" % method)
            response = {"id": wire_id, "ok": True, "result": result}
        except BridgeError as exc:
            response = {"id": wire_id, "ok": False, "error": exc.as_dict()}
        except Exception as exc:
            _log("response error id=%s error=%s" % (wire_id, exc))
            response = {"id": wire_id, "ok": False,
                        "error": {"message": str(exc), "type": type(exc).__name__}}
        try:
            self.request.settimeout(min(DEFAULT_TIMEOUT, 10.0))
            _send(self.request, response)
        except Exception as exc:
            # A disconnected caller never changes the active operation.
            _log("response send failed id=%s error=%s" % (wire_id, exc))


class McpGuiServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _dispatch_one(item):
    item.response_path = os.path.join(tempfile.gettempdir(), "abaqus_mcp_%s.json" % uuid.uuid4().hex)
    command = _kernel_wrapper(item.params["code"], item.response_path)
    _log("sendCommand request_id=%s response_path=%s" % (item.request_id, item.response_path))
    # GUI thread only. Native sendCommand itself may block internally. There is
    # no documented nonblocking option; bookkeeping is already marked running.
    try:
        sendCommand(command, False, False)
    except Exception as exc:
        _REGISTRY.note_pending(item.request_id, "native dispatch raised; outcome unknown: %s" % exc)


def _poll_response_once(item):
    global _PROCESSED
    if not item.response_path or not os.path.exists(item.response_path):
        return
    try:
        with open(item.response_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            raise ValueError("invalid kernel response envelope")
    except Exception as exc:
        _REGISTRY.note_diagnostic(item.request_id, "response read failed; outcome pending: %s" % exc)
        return
    # Published responses take precedence over elapsed execution wait deadlines.
    _REGISTRY.complete(item.request_id, payload)
    _PROCESSED += 1
    try:
        os.remove(item.response_path)
    except Exception as exc:
        _log("completed response cleanup failed: %s" % exc)


def start_gui_agent(port=None):
    global _SERVER, _SERVER_THREAD, _START_TIME, PORT, _DISCOVERY_PATH
    if _SERVER is not None:
        return "Abaqus MCP socket bridge is already running on %s:%s" % (HOST, PORT)

    requested_port = int(port) if port is not None else None
    PORT = _find_available_port(requested_port)
    if requested_port is None:
        _log("starting socket bridge on %s:%s (auto-allocated)" % (HOST, PORT))
    else:
        _log("starting socket bridge on %s:%s (user-selected)" % (HOST, PORT))
    _SERVER = McpGuiServer((HOST, PORT), McpGuiHandler)
    _REGISTRY.start_admission()
    _SERVER_THREAD = threading.Thread(target=_SERVER.serve_forever, name="AbaqusMcpSocketBridge")
    _SERVER_THREAD.daemon = True
    _SERVER_THREAD.start()
    _START_TIME = time.time()

    # Write discovery file for MCP server to find
    _DISCOVERY_PATH = _write_discovery_file(PORT)
    if _DISPATCHER is not None:
        _DISPATCHER.schedule_poll()

    return "Abaqus MCP socket bridge listening on %s:%s" % (HOST, PORT)


def stop_gui_agent():
    global _SERVER, _SERVER_THREAD, _DISCOVERY_PATH
    server = _SERVER
    _REGISTRY.stop_admission()
    if server is None:
        return "Abaqus MCP socket bridge is not running."
    try:
        server.shutdown()
        server.server_close()
    finally:
        _SERVER = None
        _SERVER_THREAD = None
    # v5.1: Clean up discovery file
    if _DISCOVERY_PATH:
        _remove_discovery_file(PORT)
        _DISCOVERY_PATH = None
    _log("stopped socket bridge")
    return "Abaqus MCP socket bridge stopped."


def mcp_start(port=None):
    message = start_gui_agent(port)
    try:
        if _DISPATCHER is not None:
            _DISPATCHER.schedule_poll()
    except Exception:
        pass
    _announce(message)
    _announce("Abaqus MCP log: %s" % LOG_PATH)
    return message


def mcp_stop():
    message = stop_gui_agent()
    _announce(message)
    return message


def mcp_status():
    status = {
        "version": __version__,
        "transport": "socket",
        "endpoint": "%s:%s" % (HOST, PORT),
        "running": _SERVER is not None,
        "processed": _PROCESSED,
        "uptime_seconds": int(time.time() - _START_TIME) if _START_TIME else 0,
        "log": LOG_PATH,
        "discovery_dir": DISCOVERY_DIR,
    }
    _announce(json.dumps(status, indent=2, ensure_ascii=False))
    return status


class McpGuiActionForm(AFXForm):
    ID_START = AFXForm.ID_LAST + 1
    ID_STOP = AFXForm.ID_LAST + 2
    ID_STATUS = AFXForm.ID_LAST + 3
    ID_POLL = AFXForm.ID_LAST + 4

    def __init__(self, owner, action, port=None):
        global _DISPATCHER
        AFXForm.__init__(self, owner)
        self.action = action
        self.port = port
        FXMAPFUNC(self, SEL_COMMAND, AFXMode.ID_ACTIVATE, McpGuiActionForm.onCmdActivate)
        FXMAPFUNC(self, SEL_COMMAND, self.ID_START, McpGuiActionForm.onCmdStart)
        FXMAPFUNC(self, SEL_COMMAND, self.ID_STOP, McpGuiActionForm.onCmdStop)
        FXMAPFUNC(self, SEL_COMMAND, self.ID_STATUS, McpGuiActionForm.onCmdStatus)
        FXMAPFUNC(self, SEL_TIMEOUT, self.ID_POLL, McpGuiActionForm.onTimeout)
        self._timer_scheduled = False
        self._in_tick = False
        # Menu entries share the first form as the sole dispatcher owner.
        if _DISPATCHER is None:
            _DISPATCHER = self

    def getFirstDialog(self):
        return None

    def schedule_poll(self):
        if self is not _DISPATCHER:
            return _DISPATCHER.schedule_poll()
        if self._timer_scheduled:
            return
        try:
            self._timer_scheduled = True
            getAFXApp().addTimeout(50, self, self.ID_POLL)
        except Exception as exc:
            self._timer_scheduled = False
            _log("schedule poll failed: %s" % exc)

    def onCmdActivate(self, sender, sel, ptr):
        if self.action == "start":
            return self.onCmdStart(sender, sel, ptr)
        if self.action == "stop":
            return self.onCmdStop(sender, sel, ptr)
        return self.onCmdStatus(sender, sel, ptr)

    def onCmdStart(self, sender, sel, ptr):
        try:
            mcp_start(self.port)
            self.schedule_poll()
        except Exception as exc:
            message = "Abaqus MCP socket bridge failed: %s" % exc
            _log(message)
            showAFXErrorDialog(getAFXApp().getAFXMainWindow(), message)
        return 1

    def onCmdStop(self, sender, sel, ptr):
        try:
            mcp_stop()
        except Exception as exc:
            message = "Abaqus MCP socket bridge stop failed: %s" % exc
            _log(message)
            showAFXErrorDialog(getAFXApp().getAFXMainWindow(), message)
        return 1

    def onCmdStatus(self, sender, sel, ptr):
        try:
            mcp_status()
        except Exception as exc:
            message = "Abaqus MCP status failed: %s" % exc
            _log(message)
            showAFXErrorDialog(getAFXApp().getAFXMainWindow(), message)
        return 1

    def onTimeout(self, sender, sel, ptr):
        if self is not _DISPATCHER:
            return 1
        # A delivered timer is consumed even when native GUI event processing
        # invokes us reentrantly. The owning outer tick will schedule its heir.
        self._timer_scheduled = False
        if self._in_tick:
            return 1
        self._in_tick = True
        try:
            item = _REGISTRY.active_operation()
            if item is not None and item.started_at is not None:
                _poll_response_once(item)
            elif _SERVER is not None:
                item = _REGISTRY.claim_next()
                if item is not None:
                    _dispatch_one(item)
        except Exception as exc:
            # Never unlock or retry a possibly dispatched operation on errors.
            _log("GUI tick failed; operation retained: %s" % exc)
            item = _REGISTRY.active_operation()
            if item is not None and item.started_at is not None:
                _REGISTRY.note_pending(item.request_id, "GUI tick error: %s" % exc)
        finally:
            self._in_tick = False
            if _SERVER is not None or _REGISTRY.active_operation() is not None:
                self.schedule_poll()
        return 1


def _register_menu():
    try:
        main_window = getAFXApp().getAFXMainWindow()
        toolset = main_window.getPluginToolset()
        modules = ["Part", "Property", "Assembly", "Step", "Interaction", "Load", "Mesh", "Job", "Visualization"]
        toolset.registerGuiMenuButton(
            object=McpGuiActionForm(toolset, "start"),
            buttonText="wmw-abaqus-mcp|Start Socket Bridge|Auto Port",
            version=__version__,
            applicableModules=modules,
            description="Start the low-latency TCP bridge on the first available configured port.",
        )
        for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
            toolset.registerGuiMenuButton(
                object=McpGuiActionForm(toolset, "start", port=port),
                buttonText="wmw-abaqus-mcp|Start Socket Bridge|Port %d" % port,
                version=__version__,
                applicableModules=modules,
                description="Start the low-latency TCP bridge on port %d." % port,
            )
        toolset.registerGuiMenuButton(
            object=McpGuiActionForm(toolset, "status"),
            buttonText="wmw-abaqus-mcp|Bridge Status",
            version=__version__,
            applicableModules=modules,
            description="Print Abaqus MCP bridge status.",
        )
        toolset.registerGuiMenuButton(
            object=McpGuiActionForm(toolset, "stop"),
            buttonText="wmw-abaqus-mcp|Stop Socket Bridge",
            version=__version__,
            applicableModules=modules,
            description="Stop the low-latency TCP bridge.",
        )
        _log("registered Abaqus MCP GUI menu (v%s)" % __version__)
    except Exception as exc:
        _log("menu registration failed: %s" % exc)
        raise


if not globals().get("_ABAQUS_MCP_MENU_REGISTERED"):
    _register_menu()
    globals()["_ABAQUS_MCP_MENU_REGISTERED"] = True

_announce("Abaqus MCP Plugin v%s loaded. Use Plug-ins > wmw-abaqus-mcp > Start Socket Bridge." % __version__)

# Optional unattended startup for scripted CAE sessions. Normal interactive
# launches are unchanged unless ABAQUS_MCP_AUTOSTART is explicitly enabled.
if os.environ.get("ABAQUS_MCP_AUTOSTART", "").strip().lower() in ("1", "true", "yes", "on"):
    try:
        start_gui_agent(int(os.environ.get("ABAQUS_MCP_PORT", str(DEFAULT_PORT))))
        if _DISPATCHER is not None:
            _DISPATCHER.schedule_poll()
    except Exception as exc:
        _log("automatic bridge startup failed: %s" % exc)
