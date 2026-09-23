import asyncio, sys
sys.path.insert(0, "main-system/src-core"); sys.path.insert(0, "shared-layer/src")

from core_system.rag.service.service_api import RagApplicationService
from core_system.rag.service.commands import RagReconcileCommand
from core_system.rag.service.authorization import StaticPolicySovereign
from core_system.rag.dag import RagDagPlanner
from core_system import rag_runtime_integration as rti


class FakeWorker:
    def run(self, coro, timeout=None):
        return asyncio.run(coro)

class FakePG:
    async def fetch_resource_by_locator(self, mid, loc):
        if loc == "loc-1":
            return {"resource_id": "res-1", "resource_type": "document",
                    "content_hash": "h1", "version": 3, "metadata": {"title": "T"}}
        return None
    async def fetch_resource_chunks(self, mid, rid):
        if rid == "res-1":
            return [{"content": "alpha"}, {"content": "beta"}]
        return []

class FakePipeline:
    def __init__(self, sweep_report, health):
        self.postgresql = FakePG()
        self._sweep = sweep_report
        self._health = health
    async def run_parity_sweep(self, *, module_id=None, drain=True):
        return dict(self._sweep)
    async def run_reconciliation(self, *, batch=25):
        return {"leased": 0, "reconciled": 0, "retried": 0, "dead_lettered": 0}
    async def health_check(self):
        return dict(self._health)

class FakeOrch:  # service ctor only stores it
    pass

# --- repair executor: drifted then clean ---------------------------------
integ = rti.RagRuntimeIntegration(app=None)
integ._loop_worker = FakeWorker()
integ._pipeline = FakePipeline(
    {"checked": 5, "drifted": 1, "enqueued": 1, "unverifiable": 0,
     "drain": {"leased": 1, "reconciled": 1, "retried": 0, "dead_lettered": 0}},
    {"queue_complete": True, "queue_pending": 0},
)
# verify sweep must be clean -> make _dag_repair_verify see drifted=0
class SeqPipeline(FakePipeline):
    calls = 0
    async def run_parity_sweep(self, *, module_id=None, drain=True):
        SeqPipeline.calls += 1
        if drain:
            return dict(self._sweep)
        return {"checked": 5, "drifted": 0, "enqueued": 0, "unverifiable": 0,
                "drifts": [], "duration_ms": 1.0}
integ._pipeline = SeqPipeline(
    {"checked": 5, "drifted": 1, "enqueued": 1, "unverifiable": 0,
     "drain": {"leased": 1, "reconciled": 1, "retried": 0, "dead_lettered": 0}},
    {"queue_complete": True, "queue_pending": 0},
)

records = []
svc = RagApplicationService(
    orchestrator=FakeOrch(),
    sovereign=StaticPolicySovereign(),
    actor_role_of=lambda a: "admin",
    audit_sink=records.append,
    dag_planner=RagDagPlanner(),
    repair_executor_factory=integ._repair_executor_factory,
)
rec = svc.reconcile(RagReconcileCommand(
    request_id="r1", actor_id="ops", module_id="mod-a"))
print("reconcile result:", rec.result)
assert rec.result == "reconciled", rec.result

# --- residual drift -> verify fails closed -------------------------------
class DirtyPipeline(FakePipeline):
    async def run_parity_sweep(self, *, module_id=None, drain=True):
        if drain:
            return dict(self._sweep)
        return {"checked": 5, "drifted": 2, "enqueued": 0, "unverifiable": 0,
                "drifts": [], "duration_ms": 1.0}
integ._pipeline = DirtyPipeline(
    {"checked": 5, "drifted": 1, "enqueued": 1,
     "drain": {"reconciled": 1}}, {})
rec2 = svc.reconcile(RagReconcileCommand(
    request_id="r2", actor_id="ops", module_id="mod-a"))
print("dirty reconcile:", rec2.result)
assert rec2.result.startswith("failed:"), rec2.result

# --- no pipeline -> fail closed ------------------------------------------
integ._pipeline = None
rec3 = svc.reconcile(RagReconcileCommand(
    request_id="r3", actor_id="ops", module_id="mod-a"))
print("no pipeline:", rec3.result)
assert rec3.result.startswith("failed:"), rec3.result

# --- no factory -> legacy accepted ----------------------------------------
svc2 = RagApplicationService(
    orchestrator=FakeOrch(), sovereign=StaticPolicySovereign(),
    actor_role_of=lambda a: "admin", audit_sink=records.append)
rec4 = svc2.reconcile(RagReconcileCommand(
    request_id="r4", actor_id="ops", module_id="mod-a"))
print("no factory:", rec4.result)
assert rec4.result == "accepted"

# --- PostgresContentResolver ---------------------------------------------
integ._pipeline = FakePipeline({}, {})
integ._loop_worker = FakeWorker()
resolver = rti._PostgresContentResolver(integ)
res = resolver.resolve("agent-1", "mod-a", "loc-1", 0)
print("resolved:", res.resolver, "|", res.content, "| v", res.version,
      "| prov:", res.provenance["resource_id"])
assert res.authorized and "alpha" in res.content and "beta" in res.content

try:
    resolver.resolve("agent-1", "mod-a", "loc-missing", 0)
    raise SystemExit("expected PermissionError")
except PermissionError as e:
    print("missing locator fail-closed:", e)

integ._pipeline = None
try:
    resolver.resolve("agent-1", "mod-a", "loc-1", 0)
    raise SystemExit("expected PermissionError")
except PermissionError as e:
    print("no pipeline fail-closed:", e)

# registry routing: document registered, memory denied
from core_system.rag.service.content_resolver import (
    ContentResolverRegistry)
integ._pipeline = FakePipeline({}, {})
reg = ContentResolverRegistry()
reg.register(resolver)
out = reg.resolve("a", "mod-a", "document", "loc-1", 0)
assert out.authorized
try:
    reg.resolve("a", "mod-a", "memory", "loc-1", 0)
    raise SystemExit("expected PermissionError")
except PermissionError as e:
    print("unregistered type fail-closed:", e)

print("\nALL CHECKS PASSED")
