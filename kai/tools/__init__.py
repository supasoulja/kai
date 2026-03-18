# Import all tool modules so their @registry.tool() decorators fire.
# Order doesn't matter — just needs to run before the registry is used.
from kai.tools import time_tool, system_info, notes, search  # noqa: F401
from kai.tools import weather, temps, crash_logs              # noqa: F401
from kai.tools import pc_tools, file_tools, network          # noqa: F401
from kai.tools import system_ops                             # noqa: F401
from kai.tools import memory_tools                           # noqa: F401
from kai.tools import campaign_tools                         # noqa: F401
from kai.tools import workspace_tools                        # noqa: F401
from kai.tools import rag                                    # noqa: F401
from kai.tools.registry import registry

__all__ = ["registry"]
