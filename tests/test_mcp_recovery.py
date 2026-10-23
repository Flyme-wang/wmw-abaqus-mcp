"""MCP-side timeout/status and submission tests, using fake peers only."""

import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("recovery_server", ROOT / "mcp_server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class RecoveryPeer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), RecoveryHandler)
        self.requests = []
        self.outcome = "completed"
        self.malformed_response = None
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def stop(self):
        self.shutdown()
        self.server_close()
        self.thread.join(2)


class RecoveryHandler(socketserver.StreamRequestHandler):
    def handle(self):
        message = json.loads(self.rfile.readline())
        self.server.requests.append(message)
        method = message["method"]
        if method == "execute":
            if self.server.malformed_response is not None:
                self.wfile.write(self.server.malformed_response)
                return
            # Let the client timeout. No script is ever evaluated here.
            threading.Event().wait(0.15)
            return
        result = {"request_id": message["params"]["request_id"], "state": self.server.outcome,
                  "started": True, "completed": self.server.outcome in ("completed", "failed"), "outcome_pending": True}
        if result["completed"]:
            result["outcome_pending"] = False
            result["result"] = {"ok": self.server.outcome == "completed", "return_value": 7,
                                "core_error": "fake script failed"}
        self.wfile.write(json.dumps({"id": message["id"], "ok": True, "result": result}).encode() + b"\n")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.peer = RecoveryPeer()
        self.addCleanup(self.peer.stop)
        self.manager = server.SessionManager()
        self.sid = self.manager.add(*self.peer.server_address)
        patcher = mock.patch.object(server, "_session_manager", self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        original = socket.create_connection
        def fake_only(address, *args, **kw):
            if address != self.peer.server_address:
                raise AssertionError("non-fake endpoint %r" % (address,))
            return original(address, *args, **kw)
        patcher = mock.patch.object(server.socket, "create_connection", side_effect=fake_only)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_timeout_recovers_original_result_without_reexecution(self):
        result = asyncio.run(server.run_python("result = 7", timeout=0.03, session_id=self.sid))
        self.assertEqual(result["return_value"], 7)
        self.assertEqual([item["method"] for item in self.peer.requests], ["execute", "request_status"])
        self.assertEqual(self.peer.requests[1]["params"]["request_id"], self.peer.requests[0]["id"])
        self.assertEqual(result["request_id"], self.peer.requests[0]["id"])

    def test_pending_reports_stable_id_and_never_replays(self):
        self.peer.outcome = "timeout_pending"
        with self.assertRaisesRegex(RuntimeError, "fixed-id.*timeout_pending"):
            asyncio.run(server.run_python("result = 7", timeout=0.03, session_id=self.sid, request_id="fixed-id"))
        self.assertEqual([item["method"] for item in self.peer.requests], ["execute", "request_status"])

    def test_unknown_status_never_replays(self):
        self.peer.outcome = "unknown"
        with self.assertRaisesRegex(RuntimeError, "lost-id.*unknown"):
            asyncio.run(server.run_python("result = 7", timeout=0.03, session_id=self.sid, request_id="lost-id"))
        self.assertEqual(len(self.peer.requests), 2)

    def test_status_uses_captured_endpoint_after_active_session_changes(self):
        self.peer.outcome = "timeout_pending"
        with self.assertRaises(RuntimeError):
            asyncio.run(server.run_python("result = 7", timeout=0.03, request_id="captured-id"))
        other = self.manager.add("127.0.0.1", 9)
        self.manager.set_active(other)
        status = asyncio.run(server.get_request_status("captured-id"))
        self.assertEqual(status["state"], "timeout_pending")

    def test_recovered_script_error_remains_a_script_error(self):
        self.peer.outcome = "failed"
        with self.assertRaisesRegex(RuntimeError, "fake script failed"):
            asyncio.run(server.run_python("result = 7", timeout=0.03, session_id=self.sid))
        self.assertEqual(len(self.peer.requests), 2)

    def test_malformed_replies_recover_original_id_without_reexecution(self):
        for request_id, reply in (("malformed-json", b'not JSON\n'), ("malformed-utf8", b'\xff\n')):
            with self.subTest(request_id=request_id):
                self.peer.requests.clear()
                self.peer.malformed_response = reply
                result = asyncio.run(server.run_python("result = 7", timeout=0.03, session_id=self.sid, request_id=request_id))
                self.assertEqual(result["return_value"], 7)
                self.assertEqual(result["request_id"], request_id)
                self.assertEqual([item["method"] for item in self.peer.requests], ["execute", "request_status"])
                self.assertEqual(self.peer.requests[1]["params"]["request_id"], request_id)

    def test_malformed_reply_pending_error_preserves_original_id(self):
        for request_id, reply in (("pending-json", b'not JSON\n'), ("pending-utf8", b'\xff\n')):
            with self.subTest(request_id=request_id):
                self.peer.requests.clear()
                self.peer.malformed_response = reply
                self.peer.outcome = "timeout_pending"
                with self.assertRaisesRegex(RuntimeError, request_id + ".*timeout_pending"):
                    asyncio.run(server.run_python("result = 7", timeout=0.03, session_id=self.sid, request_id=request_id))
                self.assertEqual([item["method"] for item in self.peer.requests], ["execute", "request_status"])


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.namespace = {}
        self.current_workdir = self.temp.name
        self.codes = []
        self.calls = []
        class IdentityObject:
            pass
        self.identity_object = IdentityObject
        self.job = IdentityObject()
        self.job.status = "SUBMITTED"
        self.job.submit = lambda **kw: self.calls.append("submit")
        self.job.waitForCompletion = lambda: self.fail("submission waited for solver completion")
        fake = types.ModuleType("abaqus")
        fake.mdb = IdentityObject()
        fake.mdb.jobs = {"Demo": self.job}
        self.fake_abaqus = fake
        patcher = mock.patch.dict(sys.modules, {"abaqus": fake})
        patcher.start()
        self.addCleanup(patcher.stop)

        async def execute_fake(code, *args, **kwargs):
            self.codes.append((code, kwargs))
            with mock.patch("os.getcwd", return_value=self.current_workdir):
                exec(code, self.namespace)
            return {"ok": True, "return_value": self.namespace["result"]}
        patcher = mock.patch.object(server, "run_python", side_effect=execute_fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_submit_returns_acknowledgement_immediately_and_forwards_id(self):
        result = json.loads(asyncio.run(server.submit_job("Demo", session_id="fake", request_id="submit-id")))
        self.assertEqual(self.calls, ["submit"])
        self.assertEqual(result, {"submitted": True, "job": "Demo", "status": "SUBMITTED", "workdir": self.temp.name})
        self.assertEqual(self.codes[-1][1]["request_id"], "submit-id")

    def test_named_monitor_uses_submission_workdir_and_actual_status(self):
        asyncio.run(server.submit_job("Demo", request_id="submit-id"))
        Path(self.temp.name, "Demo.sta").write_text("accepted step\n", encoding="utf-8")
        self.job.status = "RUNNING"
        with tempfile.TemporaryDirectory() as other:
            self.current_workdir = other
            result = asyncio.run(server.monitor_job_status("Demo"))
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(result["workdir"], self.temp.name)
        self.assertEqual(result["progress_tail"], ["accepted step"])
        self.assertIn("missing", result["file_errors"]["msg"])

    def test_failed_resubmission_preserves_confirmed_submission_directory(self):
        asyncio.run(server.submit_job("Demo"))
        Path(self.temp.name, "Demo.sta").write_text("original progress\n", encoding="utf-8")
        self.job.status = "RUNNING"
        def reject_duplicate(**kwargs):
            raise RuntimeError("Demo is already running")
        self.job.submit = reject_duplicate
        with tempfile.TemporaryDirectory() as other:
            self.current_workdir = other
            with self.assertRaisesRegex(RuntimeError, "already running"):
                asyncio.run(server.submit_job("Demo"))
            result = asyncio.run(server.monitor_job_status("Demo"))
        self.assertEqual(result["workdir"], self.temp.name)
        self.assertEqual(result["workdir_source"], "submission")
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(result["progress_tail"], ["original progress"])

    def test_replaced_same_named_job_does_not_reuse_old_submission_logs(self):
        asyncio.run(server.submit_job("Demo"))
        Path(self.temp.name, "Demo.sta").write_text("old job progress\n", encoding="utf-8")
        replacement = self.identity_object()
        replacement.status = "RUNNING_NEW_JOB"
        self.fake_abaqus.mdb.jobs["Demo"] = replacement
        with tempfile.TemporaryDirectory() as other:
            self.current_workdir = other
            Path(other, "Demo.sta").write_text("new job progress\n", encoding="utf-8")
            result = asyncio.run(server.monitor_job_status("Demo"))
            self.assertEqual(result["workdir"], other)
        self.assertEqual(result["workdir_source"], "current_directory_fallback")
        self.assertEqual(result["workdir_fallback_reason"], "job_identity_changed")
        self.assertEqual(result["status"], "RUNNING_NEW_JOB")
        self.assertEqual(result["progress_tail"], ["new job progress"])

    def test_switched_mdb_does_not_reuse_old_submission_logs(self):
        asyncio.run(server.submit_job("Demo"))
        previous_mdb = self.fake_abaqus.mdb
        replacement_mdb = self.identity_object()
        replacement_mdb.jobs = {"Demo": self.job}
        self.fake_abaqus.mdb = replacement_mdb
        with tempfile.TemporaryDirectory() as other:
            self.current_workdir = other
            result = asyncio.run(server.monitor_job_status("Demo"))
            self.assertEqual(result["workdir"], other)
        self.assertEqual(result["workdir_source"], "current_directory_fallback")
        self.assertEqual(result["workdir_fallback_reason"], "mdb_identity_changed")
        self.assertEqual(result["status"], "SUBMITTED")
        self.assertIsNot(previous_mdb, self.fake_abaqus.mdb)

    def test_unverifiable_identity_falls_back_with_explicit_reason(self):
        # SimpleNamespace does not support weak references. Real Abaqus wrapper
        # support is unknown; an unsupported boundary must safely fall back.
        self.fake_abaqus.mdb = types.SimpleNamespace(jobs={"Demo": self.job})
        asyncio.run(server.submit_job("Demo"))
        with tempfile.TemporaryDirectory() as other:
            self.current_workdir = other
            result = asyncio.run(server.monitor_job_status("Demo"))
            self.assertEqual(result["workdir"], other)
        self.assertEqual(result["workdir_source"], "current_directory_fallback")
        self.assertIn("identity_unavailable", result["workdir_fallback_reason"])

    def test_submission_identity_records_are_bounded(self):
        for index in range(129):
            name = "Job%d" % index
            job = self.identity_object()
            job.status = "SUBMITTED"
            job.submit = lambda **kwargs: None
            self.fake_abaqus.mdb.jobs[name] = job
            asyncio.run(server.submit_job(name))
        records = self.namespace["_ABAQUS_MCP_JOB_WORKDIRS"]
        self.assertEqual(len(records), 128)
        with tempfile.TemporaryDirectory() as other:
            self.current_workdir = other
            result = asyncio.run(server.monitor_job_status("Job0"))
            self.assertEqual(result["workdir"], other)
        self.assertEqual(result["workdir_source"], "current_directory_fallback")
        self.assertEqual(result["workdir_fallback_reason"], "no_confirmed_submission")

    def test_named_missing_job_is_explicit(self):
        result = asyncio.run(server.monitor_job_status("Missing"))
        self.assertFalse(result["job_found"])
        self.assertIsNone(result["status"])
        self.assertIn("not found", result["error"])

    def test_named_monitor_bounds_tail_bytes(self):
        asyncio.run(server.submit_job("Demo"))
        Path(self.temp.name, "Demo.msg").write_bytes(b"***ERROR OLD\n" + b"padding\n" * 20000 + b"***WARNING RECENT\n")
        result = asyncio.run(server.monitor_job_status("Demo"))
        self.assertEqual(result["diagnostics_tail"], ["***WARNING RECENT"])
        self.assertEqual(result["tail_max_bytes"], 128 * 1024)

    def test_monitor_reports_read_failures_without_hiding_them(self):
        asyncio.run(server.submit_job("Demo"))
        with mock.patch("builtins.open", side_effect=PermissionError("access denied")):
            result = asyncio.run(server.monitor_job_status("Demo"))
        self.assertEqual(result["status"], "SUBMITTED")
        self.assertIn("read failed", result["file_errors"]["sta"])
        self.assertIn("access denied", result["file_errors"]["msg"])
        self.assertEqual(self.calls, ["submit"])

    def test_monitor_reports_job_status_read_failure(self):
        asyncio.run(server.submit_job("Demo"))
        class BrokenJob:
            @property
            def status(self):
                raise OSError("status unavailable")
        self.namespace["mdb"].jobs["Demo"] = BrokenJob()
        result = asyncio.run(server.monitor_job_status("Demo"))
        self.assertTrue(result["job_found"])
        self.assertIsNone(result["status"])
        self.assertIn("status unavailable", result["status_error"])


class ProtocolBoundaryTests(unittest.TestCase):
    def test_invalid_json_or_utf8_is_a_protocol_error(self):
        for reply in (b'not JSON\n', b'\xff\n'):
            with self.subTest(reply=reply):
                fake = mock.Mock()
                fake.recv.return_value = reply
                with self.assertRaises(server.ProtocolError):
                    server._read_message(fake)

    def test_agent_instructions_explain_safe_status_recovery_and_submission(self):
        instructions = server.agent_instructions()
        self.assertEqual(instructions, server.INSTRUCTIONS)
        for phrase in ("BUSY", "timeout_pending", "unknown", "new request_id", "get_request_status", "same session", "not solver completion", "monitor_job_status", "kernel_response_observed"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, instructions)

    def test_receive_has_overall_deadline_for_slow_partial_peer(self):
        fake = mock.Mock()
        fake.recv.side_effect = [b'{"x":', b'1}\n']
        with mock.patch("time.monotonic", side_effect=[0.0, 0.0, 61.0]):
            with self.assertRaises(TimeoutError):
                server._read_message(fake)

    def test_final_newline_chunk_cannot_bypass_receive_limit(self):
        fake = mock.Mock()
        fake.recv.return_value = b'{"oversized":"abc"}\n'
        with self.assertRaisesRegex(server.ProtocolError, "exceeded"):
            server._read_message(fake, max_bytes=8)

    def test_script_error_preserves_request_id_in_message(self):
        with self.assertRaisesRegex(RuntimeError, "script-id"):
            server._unwrap_execution_result({"ok": False, "core_error": "failed", "request_id": "script-id"})


if __name__ == "__main__":
    unittest.main()
