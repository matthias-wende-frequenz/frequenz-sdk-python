# License: MIT
# Copyright © 2023 Frequenz Energy-as-a-Service GmbH

"""Utility functions to run and synchronize the execution of actors."""


import asyncio
import logging

from ._actor import Actor

_logger = logging.getLogger(__name__)


async def run(*actors: Actor) -> None:
    """Await the completion of all actors.

    Args:
        actors: the actors to be awaited.
    """
    _logger.info("Starting %s actor(s)...", len(actors))
    await _wait_tasks(
        set(asyncio.create_task(a.start(), name=str(a)) for a in actors),
        "starting",
        "started",
    )

    # Wait until all actors are done
    await _wait_tasks(
        set(asyncio.create_task(a.wait(), name=str(a)) for a in actors),
        "running",
        "finished",
    )

    _logger.info("All %s actor(s) finished.", len(actors))


async def _wait_tasks(
    tasks: set[asyncio.Task[None]], error_str: str, success_str: str
) -> None:
    pending_tasks = tasks
    while pending_tasks:
        done_tasks, pending_tasks = await asyncio.wait(
            pending_tasks, return_when=asyncio.FIRST_COMPLETED
        )

        # This should always be only one task, but we handle many for extra safety
        for task in done_tasks:
            # Cancellation needs to be checked first, otherwise the other methods
            # could raise a CancelledError
            if task.cancelled():
                _logger.info(
                    "Actor %s: Cancelled while %s.",
                    task.get_name(),
                    error_str,
                )
            elif exception := task.exception():
                _logger.error(
                    "Actor %s: Raised an exception while %s.",
                    task.get_name(),
                    error_str,
                    exc_info=exception,
                )
            else:
                _logger.info(
                    "Actor %s: %s normally.", task.get_name(), success_str.capitalize()
                )
