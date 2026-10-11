"""Telegram bot middlewares for RBAC enforcement and OIDC session synchronization."""

from src.bot.middlewares.rbac import RbacMiddleware

__all__ = ["RbacMiddleware"]
