"""Thread-safe single-flight bookkeeping, independent of Abaqus and its GUI.

Only queued work may expire without a kernel response. A dispatched operation
keeps its slot even when callers disappear, a deadline passes, or dispatch raises.
History is bounded and process-local: unknown IDs are never evidence of safety
to replay. This module has no solver lifecycle or termination operations.
"""

from collections import OrderedDict
import copy
import hashlib
import json
import math
import os
import threading
import time


TERMINAL = frozenset(("completed", "failed", "expired"))


class BridgeError(RuntimeError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self):
        return dict(self.details, code=self.code, message=str(self))


def positive_timeout(value, name):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("%s must be finite and positive" % name)
    return value


class Operation(object):
    def __init__(self, request_id, method, params, fingerprint, now, queue_timeout, execution_timeout):
        self.request_id = request_id
        self.method = method
        self.params = copy.deepcopy(params)
        self.fingerprint = fingerprint
        self.state = "queued"
        self.queued_at = now
        self.queue_deadline = now + queue_timeout
        self.execution_timeout = execution_timeout
        self.started_at = None
        self.execution_deadline = None
        self.completed_at = None
        self.result = None
        self.result_bytes = 0
        self.response_path = None
        self.diagnostics = []


class OperationRegistry(object):
    def __init__(self, history_limit=128, clock=time.monotonic, history_bytes=16 * 1024 * 1024, dedup_limit=65536):
        self._condition = threading.Condition(threading.RLock())
        self._operations = OrderedDict()
        self._seen = {}
        self._active_id = None
        self._accepting = True
        self._clock = clock
        self.history_limit = max(1, int(history_limit))
        self.history_bytes = max(1, int(history_bytes))
        self.dedup_limit = max(1, int(dedup_limit))
        self.kernel_ready = False

    def _refresh(self):
        op = self._operations.get(self._active_id)
        if op is None:
            return
        now = self._clock()
        if op.state == "queued" and now >= op.queue_deadline:
            self._expire(op, "queue deadline passed before dispatch")
        elif op.state == "running" and now >= op.execution_deadline:
            op.state = "timeout_pending"
            self._diagnostic(op, "execution wait elapsed; actual outcome pending")
            self._condition.notify_all()

    def _diagnostic(self, op, message):
        message = str(message)[:1000]
        if not op.diagnostics or op.diagnostics[-1] != message:
            op.diagnostics.append(message)
            del op.diagnostics[:-10]

    def _expire(self, op, reason):
        op.state = "expired"
        op.completed_at = self._clock()
        op.params.clear()
        self._diagnostic(op, reason)
        self._active_id = None
        self._prune()
        self._condition.notify_all()

    def _prune(self):
        terminal_ids = [key for key, op in self._operations.items() if op.state in TERMINAL]
        retained_bytes = sum(self._operations[key].result_bytes for key in terminal_ids)
        while terminal_ids and (len(terminal_ids) > self.history_limit or retained_bytes > self.history_bytes):
            key = terminal_ids.pop(0)
            retained_bytes -= self._operations[key].result_bytes
            del self._operations[key]

    def admit(self, request_id, method, params, queue_timeout=60.0, execution_timeout=60.0):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ValueError("request_id must be a non-empty string of at most 200 characters")
        queue_timeout = positive_timeout(queue_timeout, "queue_timeout")
        execution_timeout = positive_timeout(execution_timeout, "timeout")
        # Wait budgets do not change the identity of an operation.
        identity = {key: value for key, value in params.items() if key not in ("timeout", "queue_timeout", "request_id")}
        fingerprint = hashlib.sha256(json.dumps([method, identity], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        id_hash = hashlib.sha256(request_id.encode("utf-8")).digest()
        with self._condition:
            self._refresh()
            previous = self._operations.get(request_id)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise BridgeError("REQUEST_ID_CONFLICT", "request_id already identifies a different operation", request_id=request_id, state=previous.state)
                return previous
            if id_hash in self._seen:
                if self._seen[id_hash] != fingerprint:
                    raise BridgeError("REQUEST_ID_CONFLICT", "request_id identifies a different evicted operation", request_id=request_id, state="unknown")
                raise BridgeError("HISTORY_EVICTED", "saved result evicted; operation will not be replayed", request_id=request_id, state="unknown")
            if not self._accepting:
                raise BridgeError("BRIDGE_STOPPED", "bridge admission is stopped", request_id=request_id)
            if self._active_id is not None:
                active = self._operations[self._active_id]
                raise BridgeError("BUSY", "CAE already has an active operation", request_id=request_id, active_request_id=active.request_id, state=active.state)
            if len(self._seen) >= self.dedup_limit:
                raise BridgeError("HISTORY_CAPACITY", "request ID history is full; no new execution admitted", request_id=request_id, state="not_admitted", limit=self.dedup_limit)
            op = Operation(request_id, method, params, fingerprint, self._clock(), queue_timeout, execution_timeout)
            self._operations[request_id] = op
            self._seen[id_hash] = fingerprint
            self._active_id = request_id
            self._condition.notify_all()
            return op

    def claim_next(self):
        """Mark started BEFORE returning work to native sendCommand."""
        with self._condition:
            self._refresh()
            op = self._operations.get(self._active_id)
            if not self._accepting or op is None or op.state != "queued":
                return None
            op.state = "running"
            op.started_at = self._clock()
            op.execution_deadline = op.started_at + op.execution_timeout
            self._condition.notify_all()
            return op

    def active_operation(self):
        with self._condition:
            self._refresh()
            return self._operations.get(self._active_id)

    def note_pending(self, request_id, message):
        with self._condition:
            op = self._operations[request_id]
            if op.state not in TERMINAL:
                op.state = "timeout_pending"
                self._diagnostic(op, message)
                self._condition.notify_all()

    def note_diagnostic(self, request_id, message):
        with self._condition:
            self._diagnostic(self._operations[request_id], message)

    def complete(self, request_id, result):
        with self._condition:
            op = self._operations[request_id]
            if op.state in TERMINAL:
                return
            if op.started_at is None:
                raise ValueError("cannot complete an operation that was never dispatched")
            op.result = copy.deepcopy(result)
            op.result_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            op.params.clear()
            op.state = "completed" if result.get("ok") else "failed"
            op.completed_at = self._clock()
            self._active_id = None
            self.kernel_ready = True
            self._prune()
            self._condition.notify_all()

    def _snapshot(self, request_id):
        op = self._operations.get(request_id)
        if op is None:
            return {"request_id": request_id, "state": "unknown", "started": None, "completed": False, "outcome_pending": True,
                    "diagnostics": ["unknown or evicted ID; do not automatically replay"]}
        status = {"request_id": request_id, "state": op.state, "started": op.started_at is not None,
                  "completed": op.state in TERMINAL, "outcome_pending": op.state in ("running", "timeout_pending"),
                  "diagnostics": list(op.diagnostics)}
        if op.result is not None:
            status["result"] = copy.deepcopy(op.result)
        return status

    def status(self, request_id):
        with self._condition:
            self._refresh()
            return self._snapshot(request_id)

    def snapshots(self):
        with self._condition:
            self._refresh()
            return [self._snapshot(key) for key in self._operations]

    def wait(self, op):
        """Network-thread wait with separate queue and execution deadlines."""
        with self._condition:
            while True:
                self._refresh()
                if op.state in TERMINAL or op.state == "timeout_pending":
                    # Use the held object even if bounded history just evicted it.
                    if op.result is not None:
                        return copy.deepcopy(op.result)
                    code = "EXPIRED" if op.state == "expired" else "PENDING"
                    raise BridgeError(code, "request %s: %s; query its status, do not replay" % (op.request_id, op.state),
                                      request_id=op.request_id, state=op.state, started=op.started_at is not None,
                                      outcome_pending=op.state == "timeout_pending")
                deadline = op.queue_deadline if op.state == "queued" else op.execution_deadline
                self._condition.wait(max(0.001, deadline - self._clock()))

    def stop_admission(self):
        """Stop transport admission only; dispatched kernel work stays tracked."""
        with self._condition:
            self._accepting = False
            op = self._operations.get(self._active_id)
            if op is not None and op.state == "queued":
                self._expire(op, "bridge stopped before dispatch")
            self._condition.notify_all()

    def start_admission(self):
        with self._condition:
            self._accepting = True


# Imported once per interpreter; menu forms/bridge restarts share this instance.
_PROCESS_REGISTRY = OperationRegistry(dedup_limit=int(os.environ.get("ABAQUS_MCP_ID_HISTORY_LIMIT", "65536")))


def get_process_registry():
    return _PROCESS_REGISTRY
