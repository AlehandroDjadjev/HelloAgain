"""
ExecutorRegistry — maps Android package names to executor classes.
Executors self-register by calling ExecutorRegistry.register().
"""
from __future__ import annotations

import logging
from typing import Type

from .base import AbstractExecutor

logger = logging.getLogger(__name__)

_registry: dict[str, Type[AbstractExecutor]] = {}


class ExecutorRegistry:
    @classmethod
    def register(cls, executor_cls: Type[AbstractExecutor]) -> Type[AbstractExecutor]:
        """Decorator and explicit registration method."""
        package = executor_cls.get_package()
        if package in _registry:
            logger.warning(
                "Executor for '%s' is being overwritten by %s.",
                package, executor_cls.__name__,
            )
        _registry[package] = executor_cls
        logger.info("Executor registered: %s → %s", package, executor_cls.__name__)
        return executor_cls

    @classmethod
    def get(cls, package: str) -> Type[AbstractExecutor]:
        if package not in _registry:
            raise KeyError(
                f"No executor registered for package '{package}'. "
                f"Available: {list(_registry.keys())}"
            )
        return _registry[package]

    @classmethod
    def all_packages(cls) -> list[str]:
        return list(_registry.keys())

    @classmethod
    def is_supported(cls, package: str) -> bool:
        return package in _registry


def get_executor(package: str):
    """
    Convenience function — return an instantiated executor for *package*, or
    None if no executor is registered for that package.

    Callers should treat the return type as AppExecutor | None.
    """
    if not ExecutorRegistry.is_supported(package):
        return None
    return ExecutorRegistry.get(package)()
