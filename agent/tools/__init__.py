"""Register all agent tools on import."""

from agent.tools.analytics import register_analytics_tools
from agent.tools.guardrails import register_guardrail_tools
from agent.tools.ordering import register_ordering_tools
from agent.tools.prediction import register_prediction_tools
from agent.tools.routing import register_routing_tools
from agent.tools.tracking import register_tracking_tools

register_ordering_tools()
register_tracking_tools()
register_routing_tools()
register_analytics_tools()
register_prediction_tools()
register_guardrail_tools()
