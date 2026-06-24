# Import all tool modules so their @registry.tool() decorators fire.
# Grouped by domain subpackage; order doesn't matter — it just needs to run
# before the registry is used.
from kai.tools.system import (                               # noqa: F401
    pc_tools, system_info, system_ops, temps, crash_logs, self_inspect, time_tool,
)
from kai.tools.files import file_tools, workspace_tools      # noqa: F401
from kai.tools.web import network, browser, search, weather, researcher  # noqa: F401
from kai.tools.knowledge import rag, study, notes            # noqa: F401
from kai.tools.memory import memory_tools                    # noqa: F401
from kai.tools.media import audio_tools, vision              # noqa: F401
from kai.tools.compute import cluster, lxc, sandbox          # noqa: F401
from kai.tools.agent import goals                           # noqa: F401
from kai.tools.registry import registry

__all__ = ["registry"]
