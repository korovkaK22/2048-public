"""Runner layer: orchestrates device + vision + ai + recording on a phone.

Modules:
    live     -- LiveRunner (play for score) -- skeleton
    research -- ResearchRunner (measure spawn rules) -- skeleton
    cli      -- ``bot2048 live`` typer sub-app (wired, exits 2 until impl)
"""

from bot2048.runner.live import LiveConfig, LiveRunner
from bot2048.runner.research import ResearchConfig, ResearchRunner

__all__ = ["LiveConfig", "LiveRunner", "ResearchConfig", "ResearchRunner"]
