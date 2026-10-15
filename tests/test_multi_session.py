"""Offline regression checks; all TCP traffic is restricted to two fake bridges.

Run with the project's Python environment:
    python -B -m unittest discover -s tests -v

No Abaqus modules are imported, no code is evaluated by the fake bridges, and
the GUI port selector is extracted with AST rather than importing the plugin.
"""

import ast
import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import socketserver
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("abaqus_mcp_under_test", ROOT / "mcp_server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class FakeBridge(socketserver.ThreadingTCPServer):
    """A protocol peer that echoes its identity without executing Python."""

    daemon_threads = True

    def __init__(self, label):
        super().__init__(("127.0.0.1", 0), FakeBridgeHandler)
        self.label = label
        self.requests = []
        self.request_lock = threading.Lock()
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class FakeBridgeHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        request = json.loads(self.rfile.readline())
        with self.server.request_lock:
            self.server.requests.append(request)
        if request["method"] == "ping":
            result = {"instance": self.server.label, "abaqus_version": "fake-only"}
        else:
            result = {
                "ok": True,
                "return_value": {"instance": self.server.label, "code": request["params"].get("code")},
                "stdout": "",
            }
        response = {"id": request["id"], "ok": True, "result": result}
        self.wfile.write(json.dumps(response).encode("utf-8") + b"\n")


class MultiSessionTests(unittest.TestCase):
    def setUp(self):
        self.left = FakeBridge("left")
        self.addCleanup(self.left.stop)
        self.right = FakeBridge("right")
        self.addCleanup(self.right.stop)
        self.manager = server.SessionManager()
        patcher = mock.patch.object(server, "_session_manager", self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Even an accidental production fallback cannot contact a live bridge.
        original_connect = socket.create_connection
        permitted = {("127.0.0.1", self.left.port), ("127.0.0.1", self.right.port)}

        def connect_fake_only(address, *args, **kwargs):
            if address not in permitted:
                raise AssertionError("Test attempted a non-fake endpoint: %r" % (address,))
            return original_connect(address, *args, **kwargs)

        guard = mock.patch.object(server.socket, "create_connection", side_effect=connect_fake_only)
        guard.start()
        self.addCleanup(guard.stop)

    def discover_pair(self):
        found = []
        # Dynamic ports need not be adjacent. Restrict each discovery scan to
        # its one fake listener, leaving no intervening real ports to probe.
        for bridge in (self.left, self.right):
            with mock.patch.multiple(server, DEFAULT_HOST="127.0.0.1", PORT_RANGE_START=bridge.port, PORT_RANGE_END=bridge.port):
                found.extend(self.manager.discover())
        return found

    def test_discover_registers_distinct_endpoints_and_preserves_first_active(self):
        found = self.discover_pair()
        self.assertEqual([entry["info"]["instance"] for entry in found], ["left", "right"])
        self.assertEqual(len({entry["session_id"] for entry in found}), 2)
        self.assertEqual(self.manager.get_active_id(), found[0]["session_id"])
        self.assertEqual(self.manager.resolve_endpoint(found[1]["session_id"]), ("127.0.0.1", self.right.port))
        self.discover_pair()
        self.assertEqual(len(self.manager.list_all()), 2)
        self.assertEqual(self.manager.get_active_id(), found[0]["session_id"])

    def test_bridge_request_respects_explicit_target_and_active_switch(self):
        left_id, right_id = [entry["session_id"] for entry in self.discover_pair()]
        explicit = asyncio.run(server._bridge_request("identify", session_id=right_id, timeout=2))
        self.assertEqual(explicit["return_value"]["instance"], "right")
        self.assertEqual(self.manager.get_active_id(), left_id)
        active = asyncio.run(server._bridge_request("identify", timeout=2))
        self.assertEqual(active["return_value"]["instance"], "left")
        asyncio.run(server.set_active_session(right_id))
        active = asyncio.run(server._bridge_request("identify", timeout=2))
        self.assertEqual(active["return_value"]["instance"], "right")
        self.assertEqual(self.manager.get_active_id(), right_id)

    def test_run_python_keeps_concurrent_scripts_isolated(self):
        left_id, right_id = [entry["session_id"] for entry in self.discover_pair()]

        async def run_pair():
            return await asyncio.gather(
                server.run_python("result = 'left script'", timeout=2, session_id=left_id),
                server.run_python("result = 'right script'", timeout=2, session_id=right_id),
            )

        results = asyncio.run(run_pair())
        self.assertEqual([item["return_value"]["instance"] for item in results], ["left", "right"])
        self.assertEqual(self.manager.get_active_id(), left_id)
        for bridge in (self.left, self.right):
            executions = [request for request in bridge.requests if request["method"] == "execute"]
            self.assertEqual([request["params"]["code"] for request in executions], ["result = '%s script'" % bridge.label])
        asyncio.run(server.set_active_session(right_id))
        result = asyncio.run(server.run_python("result = 'active script'", timeout=2))
        self.assertEqual(result["return_value"]["instance"], "right")
        result = asyncio.run(server.run_python("result = 'explicit left'", timeout=2, session_id=left_id))
        self.assertEqual(result["return_value"]["instance"], "left")
        self.assertEqual(self.manager.get_active_id(), right_id)

    def test_unknown_session_fails_before_any_network_request(self):
        self.discover_pair()
        before = (len(self.left.requests), len(self.right.requests))
        with self.assertRaisesRegex(ValueError, "Unknown session_id"):
            self.manager.resolve_endpoint("missing")
        with self.assertRaisesRegex(ValueError, "Unknown session_id"):
            asyncio.run(server._bridge_request("identify", session_id="missing"))
        with self.assertRaisesRegex(ValueError, "Unknown session_id"):
            asyncio.run(server.run_python("result = 1", session_id="missing"))
        with self.assertRaisesRegex(ValueError, "not found"):
            asyncio.run(server.set_active_session("missing"))
        self.assertEqual((len(self.left.requests), len(self.right.requests)), before)


class PortSelectionTests(unittest.TestCase):
    def setUp(self):
        source_path = ROOT / "abaqus_mcp_plugin.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        selector = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_find_available_port")
        self.available = mock.Mock()
        namespace = {
            "HOST": "127.0.0.1", "PORT_RANGE_START": 48152, "PORT_RANGE_END": 48154,
            "_is_port_available": self.available,
        }
        exec(compile(ast.Module(body=[selector], type_ignores=[]), str(source_path), "exec"), namespace)
        self.select = namespace["_find_available_port"]

    def test_auto_selects_first_available_port(self):
        self.available.side_effect = [False, True]
        self.assertEqual(self.select(), 48153)
        self.assertEqual(self.available.call_args_list, [mock.call(48152), mock.call(48153)])

    def test_explicit_available_port_does_not_scan(self):
        self.available.return_value = True
        self.assertEqual(self.select(48154), 48154)
        self.available.assert_called_once_with(48154)

    def test_explicit_occupied_port_errors_without_fallback(self):
        self.available.return_value = False
        with self.assertRaisesRegex(RuntimeError, "Requested port 48154 is not available"):
            self.select(48154)
        self.available.assert_called_once_with(48154)

    def test_auto_errors_when_range_is_exhausted(self):
        self.available.return_value = False
        with self.assertRaisesRegex(RuntimeError, "No available port in range 48152-48154"):
            self.select()
        self.assertEqual(self.available.call_args_list, [mock.call(48152), mock.call(48153), mock.call(48154)])


if __name__ == "__main__":
    unittest.main()
