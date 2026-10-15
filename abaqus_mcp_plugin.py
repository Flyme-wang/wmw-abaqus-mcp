# -*- coding: utf-8 -*-
"""
wmw-abaqus-mcp v5.2.0 - socket GUI bridge with multi-instance support.

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
    import queue
except ImportError:
    import Queue as queue
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

__version__ = "5.2.0"

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


def _recv(sock):
    chunks = []
    total = 0
    max_bytes = int(os.environ.get("ABAQUS_MCP_MAX_MESSAGE_BYTES", str(32 * 1024 * 1024)))
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("socket closed before a complete message was received")
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.append(chunk[:newline])
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise RuntimeError("message exceeded %d bytes" % max_bytes)
    return json.loads(b"".join(chunks).decode("utf-8"))


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

with open(response_path, "w") as handle:
    json.dump(payload, handle, ensure_ascii=False)
'''
    return template.replace("__ABAQUS_MCP_CODE__", encoded_code).replace("__ABAQUS_MCP_RESPONSE__", encoded_path)


def _run_kernel_code(code, timeout):
    response_path = os.path.join(tempfile.gettempdir(), "abaqus_mcp_%s.json" % uuid.uuid4().hex)
    command = _kernel_wrapper(code, response_path)
    _log("sendCommand start response_path=%s" % response_path)
    sendCommand(command, False, False)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(response_path):
            with open(response_path, "r") as handle:
                payload = json.load(handle)
            try:
                os.remove(response_path)
            except Exception:
                pass
            _log("kernel response ok=%s" % payload.get("ok"))
            return payload
        time.sleep(0.03)

    raise TimeoutError("timed out waiting for Abaqus kernel response")


class GuiRequest(object):
    def __init__(self, method, params):
        self.method = method
        self.params = params
        self.event = threading.Event()
        self.result = None
        self.error = None


class McpGuiHandler(socketserver.BaseRequestHandler):
    def handle(self):
        request_id = None
        try:
            message = _recv(self.request)
            request_id = message.get("id")
            method = message.get("method")
            params = message.get("params") or {}
            _log("request method=%s id=%s" % (method, request_id))

            if _DISPATCHER is None:
                raise RuntimeError("Abaqus MCP GUI dispatcher is not initialized")

            wait_timeout = float(params.get("timeout") or DEFAULT_TIMEOUT) + 5.0
            item = GuiRequest(method, params)
            _REQUESTS.put(item)

            if not item.event.wait(wait_timeout):
                raise TimeoutError("timed out waiting for Abaqus GUI dispatcher")
            if item.error is not None:
                raise item.error

            _send(self.request, {"id": request_id, "ok": True, "result": item.result})
        except Exception as exc:
            _log("response error id=%s error=%s" % (request_id, exc))
            _send(
                self.request,
                {
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "message": str(exc),
                        "type": "%s.%s" % (type(exc).__module__, type(exc).__name__),
                        "traceback": traceback.format_exc(),
                    },
                },
            )


class McpGuiServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


_SERVER = None
_SERVER_THREAD = None
_DISPATCHER = None
_REQUESTS = queue.Queue()
_PROCESSED = 0
_START_TIME = None


def _handle_on_gui_thread(item):
    method = item.method
    params = item.params
    timeout = float(params.get("timeout") or DEFAULT_TIMEOUT)

    if method == "ping":
        code = (
            "import os, sys, platform\n"
            "from abaqus import mdb, session\n"
            "result = {'python': sys.version, 'executable': sys.executable, "
            "'platform': platform.platform(), 'pid': os.getpid(), "
            "'cpu_count': os.cpu_count(), 'cwd': os.getcwd(), "
            "'abaqus_version': getattr(session, 'version', None), "
            "'models': list(mdb.models.keys()), "
            "'viewports': list(session.viewports.keys())}"
        )
        payload = _run_kernel_code(code, timeout)
        result = payload.get("return_value") if payload.get("ok") else payload
        if isinstance(result, dict):
            result["bridge"] = {
                "version": __version__,
                "host": HOST,
                "port": PORT,
                "transport": "socket",
                "processed": _PROCESSED,
                "uptime_seconds": int(time.time() - _START_TIME) if _START_TIME else 0,
                "gui_python": sys.version,
                "gui_platform": platform.platform(),
                "log": LOG_PATH,
                "discovery_dir": DISCOVERY_DIR,
            }
        return result

    if method == "execute":
        code = params.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("params.code must be a non-empty string")
        return _run_kernel_code(code, timeout)

    if method == "stop":
        threading.Thread(target=stop_gui_agent, name="AbaqusMcpStopper").start()
        return {"success": True, "message": "stop requested"}

    raise ValueError("unknown method: %r" % method)


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
    _SERVER_THREAD = threading.Thread(target=_SERVER.serve_forever, name="AbaqusMcpSocketBridge")
    _SERVER_THREAD.daemon = True
    _SERVER_THREAD.start()
    _START_TIME = time.time()

    # Write discovery file for MCP server to find
    _DISCOVERY_PATH = _write_discovery_file(PORT)

    return "Abaqus MCP socket bridge listening on %s:%s" % (HOST, PORT)


def stop_gui_agent():
    global _SERVER, _SERVER_THREAD, _DISCOVERY_PATH
    server = _SERVER
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
        _DISPATCHER = self

    def getFirstDialog(self):
        return None

    def schedule_poll(self):
        try:
            getAFXApp().addTimeout(50, self, self.ID_POLL)
        except Exception as exc:
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
        global _PROCESSED
        processed = 0
        while processed < 10:
            try:
                item = _REQUESTS.get_nowait()
            except queue.Empty:
                break
            try:
                item.result = _handle_on_gui_thread(item)
                _PROCESSED += 1
            except Exception as exc:
                item.error = exc
                _log("GUI handler error method=%s error=%s" % (item.method, exc))
            finally:
                item.event.set()
            processed += 1

        if _SERVER is not None:
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
