"""
agent.orchestrator — modular decomposition of agent/orchestrator_v2.py.

Strangler-fig migration in progress. See YANDI_ORCHESTRATOR_MODULARIZATION_MAP.md
and YANDI_ORCHESTRATOR_MODULARIZATION.md for the audit and living status.

orchestrator_v2.py remains the canonical production entry point and CLI until
the migration is complete; this package is imported *by* orchestrator_v2.py,
never the reverse.
"""
