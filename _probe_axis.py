import sys, io, inspect, dis
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import governance_rule.execution.audit.audit_directories as m
print(m.__file__)
src = inspect.getsource(m.check_provision_classification)
print(src)
