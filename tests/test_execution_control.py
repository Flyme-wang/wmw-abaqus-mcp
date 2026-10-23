"""Offline control regressions. Fake GUI/kernel boundaries; no live endpoints."""

import asyncio
import importlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import threading
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_plugin():
    """Import the real GUI adapter with an entirely fake abaqusGui module."""
    gui = types.ModuleType("abaqusGui")
    class Form:
        ID_LAST = 100
        def __init__(self, owner):
            pass
    gui.AFXForm = Form
    gui.AFXMode = types.SimpleNamespace(ID_ACTIVATE=1)
    gui.FXMAPFUNC = mock.Mock()
    gui.SEL_COMMAND = 1
    gui.SEL_TIMEOUT = 2
    gui.sendCommand = mock.Mock()
    gui.showAFXErrorDialog = mock.Mock()
    gui.getAFXApp = mock.Mock(return_value=mock.MagicMock())
    spec = importlib.util.spec_from_file_location("offline_plugin", ROOT / "abaqus_mcp_plugin.py")
    plugin = importlib.util.module_from_spec(spec)
    # Menu registration is harmless but auto-start must never run in tests.
    with mock.patch.dict(sys.modules, {"abaqusGui": gui}), mock.patch.dict("os.environ", {"ABAQUS_MCP_AUTOSTART": "0"}):
        spec.loader.exec_module(plugin)
    return plugin, gui


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.runtime = importlib.import_module("bridge_runtime")
        self.now = 100.0
        self.registry = self.runtime.OperationRegistry(history_limit=2, clock=lambda: self.now)

    def admit(self, request_id="a", code="result = 1", **kw):
        return self.registry.admit(request_id, "execute", {"code": code}, **kw)

    def test_single_slot_duplicate_and_conflict(self):
        first = self.admit()
        self.assertIs(self.admit(), first)
        with self.assertRaises(self.runtime.BridgeError) as caught:
            self.admit("b")
        self.assertEqual(caught.exception.code, "BUSY")
        self.assertEqual(caught.exception.details["active_request_id"], "a")
        with self.assertRaises(self.runtime.BridgeError) as caught:
            self.admit("a", "result = 2")
        self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")

    def test_expired_queued_request_never_dispatches(self):
        self.admit(queue_timeout=2, execution_timeout=10)
        self.now += 3
        self.assertIsNone(self.registry.claim_next())
        status = self.registry.status("a")
        self.assertEqual(status["state"], "expired")
        self.assertFalse(status["started"])
        self.assertTrue(status["completed"])
        self.admit("b")

    def test_execution_deadline_starts_at_dispatch_and_late_response_releases(self):
        op = self.admit(queue_timeout=10, execution_timeout=2)
        self.now += 9
        self.assertIs(self.registry.claim_next(), op)
        self.assertEqual(self.registry.status("a")["state"], "running")
        self.now += 3
        self.assertEqual(self.registry.status("a")["state"], "timeout_pending")
        with self.assertRaises(self.runtime.BridgeError):
            self.admit("b")
        self.registry.complete("a", {"ok": True, "return_value": 9})
        self.assertEqual(self.registry.status("a")["result"]["return_value"], 9)
        self.assertFalse(self.registry.status("a")["outcome_pending"])
        self.admit("b")

    def test_uncertain_dispatch_keeps_active_slot_and_diagnostic(self):
        self.admit()
        self.registry.claim_next()
        self.registry.note_pending("a", "native dispatch raised")
        self.assertEqual(self.registry.status("a")["state"], "timeout_pending")
        self.assertIn("native dispatch raised", self.registry.status("a")["diagnostics"])
        self.assertIsNone(self.registry.claim_next())

    def test_terminal_history_bounded_active_never_evicted(self):
        for name in ("a", "b", "c"):
            self.admit(name)
            self.registry.claim_next()
            self.registry.complete(name, {"ok": name != "c"})
        self.admit("active")
        self.assertEqual(self.registry.status("a")["state"], "unknown")
        self.assertEqual(self.registry.status("c")["state"], "failed")
        self.assertEqual(self.registry.status("active")["state"], "queued")
        self.assertEqual(len(self.registry.snapshots()), 3)

    def test_bridge_stop_expires_queue_preserves_dispatched_operation(self):
        self.admit()
        self.registry.stop_admission()
        self.assertEqual(self.registry.status("a")["state"], "expired")
        self.registry.start_admission()
        self.assertIsNone(self.registry.claim_next())
        self.admit("b")
        self.registry.claim_next()
        self.registry.stop_admission()
        self.registry.start_admission()
        self.assertEqual(self.registry.status("b")["state"], "running")
        self.assertIsNone(self.registry.claim_next())

    def test_simultaneous_admission_has_one_winner(self):
        barrier = threading.Barrier(8)
        outcomes = []
        def attempt(i):
            barrier.wait()
            try:
                self.admit(str(i))
                outcomes.append("admitted")
            except self.runtime.BridgeError as exc:
                outcomes.append(exc.code)
        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual(outcomes.count("admitted"), 1)
        self.assertEqual(outcomes.count("BUSY"), 7)

    def test_wait_returns_expired_without_dispatch(self):
        registry = self.runtime.OperationRegistry()
        op = registry.admit("deadline", "execute", {"code": "result = 1"}, queue_timeout=0.01)
        with self.assertRaises(self.runtime.BridgeError) as caught:
            registry.wait(op)
        self.assertEqual(caught.exception.code, "EXPIRED")
        self.assertFalse(caught.exception.details["started"])
        self.assertIsNone(registry.claim_next())

    def test_wait_timeout_retains_slot_and_completed_duplicate_reuses_result(self):
        registry = self.runtime.OperationRegistry()
        op = registry.admit("deadline", "execute", {"code": "result = 1"}, execution_timeout=0.01)
        registry.claim_next()
        with self.assertRaises(self.runtime.BridgeError) as caught:
            registry.wait(op)
        self.assertEqual(caught.exception.code, "PENDING")
        registry.complete("deadline", {"ok": True, "return_value": 3})
        duplicate = registry.admit("deadline", "execute", {"code": "result = 1"})
        self.assertIs(duplicate, op)
        self.assertEqual(registry.wait(duplicate)["return_value"], 3)
        self.assertIsNone(registry.claim_next())

    def test_evicted_id_cannot_be_executed_again(self):
        for name in ("a", "b", "c"):
            self.admit(name)
            self.registry.claim_next()
            self.registry.complete(name, {"ok": True})
        with self.assertRaises(self.runtime.BridgeError) as caught:
            self.admit("a")
        self.assertEqual(caught.exception.code, "HISTORY_EVICTED")
        with self.assertRaises(self.runtime.BridgeError) as caught:
            self.admit("a", "result = 99")
        self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")

    def test_result_byte_budget_reclaims_payloads_and_terminal_source(self):
        registry = self.runtime.OperationRegistry(history_bytes=160)
        first = registry.admit("a", "execute", {"code": "sensitive source"})
        registry.claim_next()
        registry.complete("a", {"ok": True, "return_value": "x" * 80})
        self.assertEqual(first.params, {})
        registry.admit("b", "execute", {"code": "result = 2"})
        registry.claim_next()
        registry.complete("b", {"ok": True, "return_value": "y" * 80})
        self.assertEqual(registry.status("a")["state"], "unknown")
        self.assertEqual(registry.status("b")["state"], "completed")
        with self.assertRaises(self.runtime.BridgeError):
            registry.admit("a", "execute", {"code": "sensitive source"})

    def test_dedup_capacity_rejects_new_ids_without_forgetting_old(self):
        registry = self.runtime.OperationRegistry(history_limit=1, dedup_limit=2)
        for name in ("a", "b"):
            registry.admit(name, "execute", {"code": "1"})
            registry.claim_next()
            registry.complete(name, {"ok": True})
        with self.assertRaises(self.runtime.BridgeError) as caught:
            registry.admit("c", "execute", {"code": "1"})
        self.assertEqual(caught.exception.code, "HISTORY_CAPACITY")
        with self.assertRaises(self.runtime.BridgeError) as caught:
            registry.admit("a", "execute", {"code": "1"})
        self.assertEqual(caught.exception.code, "HISTORY_EVICTED")


class GuiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.plugin, self.gui = load_plugin()
        self.runtime = importlib.import_module("bridge_runtime")
        self.plugin._REGISTRY = self.runtime.OperationRegistry()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin.LOG_PATH = str(Path(self.temp.name) / "bridge.log")
        self.form = self.plugin._DISPATCHER
        self.plugin._SERVER = object()

    def admit(self, request_id="a", timeout=1):
        return self.plugin._REGISTRY.admit(request_id, "execute", {"code": "result = 1"}, execution_timeout=timeout)

    def tick(self):
        return self.form.onTimeout(None, None, None)

    def start_fake_transport(self):
        bridge = self.plugin.McpGuiServer(("127.0.0.1", 0), self.plugin.McpGuiHandler)
        port = bridge.server_address[1]
        self.fake_endpoint = ("127.0.0.1", port)
        self.assertNotIn(port, range(48152, 48165))
        worker = threading.Thread(target=bridge.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(bridge.server_close)
        self.addCleanup(bridge.shutdown)
        def request(method, params=None, request_id="wire"):
            with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
                sock.settimeout(1)
                self.plugin._send(sock, {"id": request_id, "method": method, "params": params or {}})
                return self.plugin._recv(sock, io_timeout=1)
        return request

    def test_tick_dispatches_once_then_returns_and_never_spins(self):
        self.admit()
        with mock.patch.object(self.plugin.time, "sleep", side_effect=AssertionError("GUI spun")):
            self.tick()
            self.tick()
        self.gui.sendCommand.assert_called_once()
        self.assertEqual(self.plugin._REGISTRY.status("a")["state"], "running")

    def test_reentrant_send_does_not_dispatch_twice(self):
        self.admit()
        self.gui.sendCommand.side_effect = lambda *args: self.tick()
        self.tick()
        self.gui.sendCommand.assert_called_once()

    def test_reentrant_timer_consumption_does_not_strand_future_poll(self):
        self.admit()
        self.gui.getAFXApp().addTimeout.reset_mock()
        def reenter(*args):
            # A GUI action schedules a timer while native dispatch is active;
            # native event processing then delivers that timer reentrantly.
            self.form.schedule_poll()
            self.tick()
        self.gui.sendCommand.side_effect = reenter
        self.tick()
        self.assertEqual(self.gui.getAFXApp().addTimeout.call_count, 2)
        self.gui.sendCommand.assert_called_once()

    def test_response_read_error_retains_busy_and_late_result_wins(self):
        op = self.admit(timeout=0.001)
        self.tick()
        path = Path(op.response_path)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        path.write_text('{"ok":', encoding="utf-8")
        self.tick()
        self.assertFalse(self.plugin._REGISTRY.status("a")["completed"])
        path.write_text(json.dumps({"ok": True, "return_value": 4}), encoding="utf-8")
        self.tick()
        self.assertEqual(self.plugin._REGISTRY.status("a")["result"]["return_value"], 4)

    def test_native_exception_is_uncertain_and_never_redispatched(self):
        self.admit()
        self.gui.sendCommand.side_effect = RuntimeError("native unknown")
        self.tick()
        self.tick()
        self.gui.sendCommand.assert_called_once()
        self.assertEqual(self.plugin._REGISTRY.status("a")["state"], "timeout_pending")

    def test_single_dispatcher_and_single_scheduled_timer(self):
        other = self.plugin.McpGuiActionForm(None, "status")
        self.assertIs(self.plugin._DISPATCHER, self.form)
        self.gui.getAFXApp().addTimeout.reset_mock()
        self.form._timer_scheduled = False
        self.form.schedule_poll()
        other.schedule_poll()
        self.form.schedule_poll()
        self.gui.getAFXApp().addTimeout.assert_called_once()

    def test_wrapper_publishes_atomically_and_resets_result(self):
        path = Path(self.temp.name) / "response.json"
        namespace = {}
        with mock.patch.dict(sys.modules, {"abaqus": types.SimpleNamespace(mdb=object(), session=object())}):
            exec(self.plugin._kernel_wrapper("result = 'first'", str(path)), namespace)
            self.assertEqual(json.loads(path.read_text())["return_value"], "first")
            path.unlink()
            with mock.patch("os.replace", wraps=__import__("os").replace) as replace:
                exec(self.plugin._kernel_wrapper("x = 2\nprint(x)", str(path)), namespace)
            replace.assert_called_once()
            self.assertIsNone(json.loads(path.read_text())["return_value"])

    def test_wrapper_script_error_publishes_known_failure(self):
        path = Path(self.temp.name) / "error.json"
        with mock.patch.dict(sys.modules, {"abaqus": types.SimpleNamespace(mdb=object(), session=object())}):
            exec(self.plugin._kernel_wrapper("raise ValueError('known failure')", str(path)), {})
        payload = json.loads(path.read_text())
        self.assertFalse(payload["ok"])
        self.assertIn("known failure", payload["core_error"])

    def test_complete_published_result_is_observed_after_wait_deadline(self):
        now = [0.0]
        self.plugin._REGISTRY = self.runtime.OperationRegistry(clock=lambda: now[0])
        op = self.admit(timeout=1)
        self.tick()
        path = Path(op.response_path)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        path.write_text(json.dumps({"ok": True, "return_value": 42}), encoding="utf-8")
        now[0] = 10.0
        self.tick()
        self.assertEqual(self.plugin._REGISTRY.status("a")["state"], "completed")

    def test_final_newline_chunk_cannot_bypass_receive_limit(self):
        fake = mock.Mock()
        fake.recv.return_value = b'{"oversized":"abc"}\n'
        with mock.patch.dict("os.environ", {"ABAQUS_MCP_MAX_MESSAGE_BYTES": "8"}):
            with self.assertRaisesRegex(RuntimeError, "exceeded"):
                self.plugin._recv(fake)

    def test_receive_has_overall_deadline_for_slow_partial_peer(self):
        fake = mock.Mock()
        fake.recv.side_effect = [b'{"x":', b'1}\n']
        with mock.patch.object(self.plugin.time, "monotonic", side_effect=[0.0, 0.0, 11.0]):
            with self.assertRaises(TimeoutError):
                self.plugin._recv(fake)

    def test_real_handler_expiration_never_dispatches(self):
        request = self.start_fake_transport()
        response = request("execute", {"code": "result = 1", "queue_timeout": 0.01}, "expires")
        self.assertEqual(response["error"]["code"], "EXPIRED")
        self.tick()
        self.gui.sendCommand.assert_not_called()
        status = request("request_status", {"request_id": "expires"})["result"]
        self.assertEqual(status["state"], "expired")
        self.assertFalse(status["started"])

    def test_real_handler_default_queue_deadline_uses_requested_timeout(self):
        request = self.start_fake_transport()
        response = request("execute", {"code": "result = 1", "timeout": 0.01}, "short-queue")
        self.assertEqual(response["error"]["code"], "EXPIRED")
        self.tick()
        self.gui.sendCommand.assert_not_called()
        status = request("request_status", {"request_id": "short-queue"})["result"]
        self.assertEqual(status["state"], "expired")
        self.assertFalse(status["started"])

    def test_heartbeat_separates_observed_response_from_current_admission_readiness(self):
        initial = self.plugin._heartbeat()
        self.assertIsNone(initial["kernel_ready"])
        self.assertFalse(initial["kernel_response_observed"])
        self.admit()
        self.tick()
        self.plugin._REGISTRY.complete("a", {"ok": True})
        idle = self.plugin._heartbeat()
        self.assertTrue(idle["kernel_ready"])
        self.assertTrue(idle["kernel_response_observed"])
        self.admit("b")
        busy = self.plugin._heartbeat()
        self.assertTrue(busy["busy"])
        self.assertFalse(busy["kernel_ready"])
        self.assertTrue(busy["kernel_response_observed"])

    def test_real_handler_duplicate_timeout_and_late_completion_never_reexecutes(self):
        request = self.start_fake_transport()
        admitted = threading.Event()
        original_admit = self.plugin._REGISTRY.admit
        def notify_admit(*args, **kw):
            op = original_admit(*args, **kw)
            admitted.set()
            return op
        replies = []
        with mock.patch.object(self.plugin._REGISTRY, "admit", side_effect=notify_admit):
            caller = threading.Thread(target=lambda: replies.append(request("execute", {"code": "result = 1", "timeout": 0.03}, "stable")))
            caller.start()
            self.addCleanup(caller.join, 2)
            self.assertTrue(admitted.wait(1))
            self.tick()
            caller.join(1)
        self.assertEqual(replies[0]["error"]["code"], "PENDING")
        duplicate = request("execute", {"code": "result = 1"}, "stable")
        self.assertEqual(duplicate["error"]["code"], "PENDING")
        conflict = request("execute", {"code": "result = 999"}, "stable")
        self.assertEqual(conflict["error"]["code"], "REQUEST_ID_CONFLICT")
        self.assertEqual(request("request_status", {"request_id": "stable"})["result"]["state"], "timeout_pending")
        op = self.plugin._REGISTRY.active_operation()
        path = Path(op.response_path)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        path.write_text(json.dumps({"ok": True, "return_value": "late"}), encoding="utf-8")
        self.tick()
        duplicate = request("execute", {"code": "result = 1"}, "stable")
        self.assertEqual(duplicate["result"]["return_value"], "late")
        status = request("request_status", {"request_id": "stable"})["result"]
        self.assertEqual(status["state"], "completed")
        self.gui.sendCommand.assert_called_once()

    def test_transport_disconnect_does_not_release_dispatched_slot(self):
        request = self.start_fake_transport()
        admitted = threading.Event()
        original_admit = self.plugin._REGISTRY.admit
        def notify_admit(*args, **kw):
            op = original_admit(*args, **kw)
            admitted.set()
            return op
        with mock.patch.object(self.plugin._REGISTRY, "admit", side_effect=notify_admit):
            with socket.create_connection(self.fake_endpoint, timeout=1) as sock:
                self.plugin._send(sock, {"id": "a", "method": "execute", "params": {"code": "result = 1", "timeout": 0.03}})
                self.assertTrue(admitted.wait(1))
                # Close before dispatch and before reading a response.
        op = self.plugin._REGISTRY.active_operation()
        self.tick()
        with self.assertRaises(self.runtime.BridgeError):
            self.plugin._REGISTRY.wait(op)
        self.assertEqual(request("request_status", {"request_id": "a"})["result"]["state"], "timeout_pending")
        busy = request("execute", {"code": "result = 2"}, "b")
        self.assertEqual(busy["error"]["code"], "BUSY")
        self.gui.sendCommand.assert_called_once()

    def test_dispatch_remains_on_tick_calling_thread(self):
        self.admit()
        threads = []
        self.gui.sendCommand.side_effect = lambda *args: threads.append(threading.get_ident())
        self.tick()
        self.assertEqual(threads, [threading.get_ident()])

    def test_explicit_transport_stop_keeps_running_operation_tracked(self):
        request = self.start_fake_transport()
        self.admit()
        self.tick()
        stopped = threading.Event()
        with mock.patch.object(self.plugin, "stop_gui_agent", side_effect=stopped.set):
            response = request("stop")
            self.assertTrue(response["ok"])
            self.assertTrue(stopped.wait(1))
        self.assertEqual(self.plugin._REGISTRY.status("a")["state"], "running")
        self.assertEqual(request("request_status", {"request_id": "a"})["result"]["state"], "running")
        self.gui.sendCommand.assert_called_once()

    def test_status_and_busy_network_paths_work_during_blocked_gui_dispatch(self):
        bridge = self.plugin.McpGuiServer(("127.0.0.1", 0), self.plugin.McpGuiHandler)
        port = bridge.server_address[1]
        self.assertNotIn(port, range(48152, 48165))
        worker = threading.Thread(target=bridge.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        self.addCleanup(bridge.server_close)
        self.addCleanup(bridge.shutdown)
        def request(method, params=None, request_id="wire"):
            with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
                sock.settimeout(1)
                self.plugin._send(sock, {"id": request_id, "method": method, "params": params or {}})
                return self.plugin._recv(sock)
        self.admit(timeout=5)
        entered, release = threading.Event(), threading.Event()
        def blocking_native(*args):
            entered.set()
            release.wait(2)
        self.gui.sendCommand.side_effect = blocking_native
        # This fake GUI worker is used only to simulate a native call blocking.
        fake_gui = threading.Thread(target=self.tick)
        fake_gui.start()
        self.addCleanup(fake_gui.join, 2)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(1))
        status = request("request_status", {"request_id": "a"})
        self.assertEqual(status["result"]["state"], "running")
        ping = request("ping")["result"]
        self.assertTrue(ping["busy"])
        busy = request("execute", {"code": "result = 2"}, "b")
        self.assertEqual(busy["error"]["code"], "BUSY")
        self.assertEqual(busy["error"]["active_request_id"], "a")
        release.set()
        fake_gui.join(2)


if __name__ == "__main__":
    unittest.main()
