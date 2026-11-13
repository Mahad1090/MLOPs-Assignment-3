"""Tool modules of the MLOps agent."""

from agent.tools import airflow_tools, dvc_tools, kubernetes_tools, mlflow_tools, policy_tools
from agent.tools.base import Tool, ToolRegistry

ALL_TOOL_MODULES = (dvc_tools, mlflow_tools, airflow_tools, kubernetes_tools, policy_tools)


def build_registry(default_timeout: float = 30.0) -> ToolRegistry:
    tools: list[Tool] = [tool for module in ALL_TOOL_MODULES for tool in module.TOOLS]
    return ToolRegistry(tools, default_timeout=default_timeout)
