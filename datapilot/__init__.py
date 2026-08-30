"""DataPilot agent application package.

Phase 2 provides typed state, in-memory tracing, and a CLI skeleton. The
actual agent workflow is intentionally left for later phases.
"""

from datapilot.agent.state import AgentState, create_initial_state

__all__ = ["AgentState", "create_initial_state"]
__version__ = "0.1.0"
