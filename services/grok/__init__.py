"""Grok/xAI registration and warehouse integration."""

from services.grok.registration import register_grok_account
from services.grok.solver_manager import grok_solver_manager

__all__ = ["grok_solver_manager", "register_grok_account"]
