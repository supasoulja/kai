"""
time.now — returns the current date, time, and day of week.
"""
from datetime import datetime
from kai.tools.registry import registry


@registry.tool(
    name="time.now",
    description="Get the current date, time, and day of week.",
)
def get_time() -> str:
    now = datetime.now()
    return now.strftime("%A, %B %d %Y — %I:%M %p")
