# License: MIT
# Copyright © 2023 Frequenz Energy-as-a-Service GmbH

"""A data window that moves with the latest datapoints of a data stream."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import SupportsIndex, overload

import numpy as np
from frequenz.channels import Broadcast, Receiver, Sender
from numpy.typing import ArrayLike

from .._internal._asyncio import cancel_and_await
from ._base_types import UNIX_EPOCH, Sample
from ._quantities import Quantity
from ._resampling import Resampler, ResamplerConfig
from ._ringbuffer import OrderedRingBuffer

_logger = logging.getLogger(__name__)


class MovingWindow:
    """
    A data window that moves with the latest datapoints of a data stream.

    After initialization the `MovingWindow` can be accessed by an integer
    index or a timestamp. A sub window can be accessed by using a slice of
    integers or timestamps.

    Note that a numpy ndarray is returned and thus users can use
    numpys operations directly on a window.

    The window uses a ring buffer for storage and the first element is aligned to
    a fixed defined point in time. Since the moving nature of the window, the
    date of the first and the last element are constantly changing and therefore
    the point in time that defines the alignment can be outside of the time window.
    Modulo arithmetic is used to move the `align_to` timestamp into the latest
    window.

    If for example the `align_to` parameter is set to
    `datetime(1, 1, 1, tzinfo=timezone.utc)` and the window size is bigger than
    one day then the first element will always be aligned to midnight.

    Resampling might be required to reduce the number of samples to store, and
    it can be set by specifying the resampler config parameter so that the user
    can control the granularity of the samples to be stored in the underlying
    buffer.

    If resampling is not required, the resampler config parameter can be
    set to None in which case the MovingWindow will not perform any resampling.

    Example: Calculate the mean of a time interval

        ```python
        from datetime import datetime, timedelta, timezone

        async def send_mock_data(sender: Sender[Sample]) -> None:
            while True:
                await sender.send(Sample(datetime.now(tz=timezone.utc), 10.0))
                await asyncio.sleep(1.0)

        async def run() -> None:
            resampled_data_channel = Broadcast[Sample]("sample-data")
            resampled_data_receiver = resampled_data_channel.new_receiver()
            resampled_data_sender = resampled_data_channel.new_sender()

            send_task = asyncio.create_task(send_mock_data(resampled_data_sender))

            window = MovingWindow(
                size=timedelta(seconds=5),
                resampled_data_recv=resampled_data_receiver,
                input_sampling_period=timedelta(seconds=1),
            )

            time_start = datetime.now(tz=timezone.utc)
            time_end = time_start + timedelta(seconds=5)

            # ... wait for 5 seconds until the buffer is filled
            await asyncio.sleep(5)

            # return an numpy array from the window
            array = window[time_start:time_end]
            # and use it to for example calculate the mean
            mean = array.mean()

        asyncio.run(run())
        ```

    Example: Create a polars data frame from a `MovingWindow`

        ```python
        import polars as pl
        from datetime import datetime, timedelta, timezone

        async def send_mock_data(sender: Sender[Sample]) -> None:
            while True:
                await sender.send(Sample(datetime.now(tz=timezone.utc), 10.0))
                await asyncio.sleep(1.0)

        async def run() -> None:
            resampled_data_channel = Broadcast[Sample]("sample-data")
            resampled_data_receiver = resampled_data_channel.new_receiver()
            resampled_data_sender = resampled_data_channel.new_sender()

            send_task = asyncio.create_task(send_mock_data(resampled_data_sender))

            # create a window that stores two days of data
            # starting at 1.1.23 with samplerate=1
            window = MovingWindow(
                size=timedelta(days=2),
                resampled_data_recv=resampled_data_receiver,
                input_sampling_period=timedelta(seconds=1),
            )

            # wait for one full day until the buffer is filled
            await asyncio.sleep(60*60*24)

            # create a polars series with one full day of data
            time_start = datetime(2023, 1, 1, tzinfo=timezone.utc)
            time_end = datetime(2023, 1, 2, tzinfo=timezone.utc)
            series = pl.Series("Jan_1", window[time_start:time_end])

        asyncio.run(run())
        ```
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        size: timedelta,
        resampled_data_recv: Receiver[Sample[Quantity]],
        input_sampling_period: timedelta,
        resampler_config: ResamplerConfig | None = None,
        align_to: datetime = UNIX_EPOCH,
    ) -> None:
        """
        Initialize the MovingWindow.

        This method creates the underlying ring buffer and starts a
        new task that updates the ring buffer with new incoming samples.
        The task stops running only if the channel receiver is closed.

        Args:
            size: The time span of the moving window over which samples will be stored.
            resampled_data_recv: A receiver that delivers samples with a
                given sampling period.
            input_sampling_period: The time interval between consecutive input samples.
            resampler_config: The resampler configuration in case resampling is required.
            align_to: A datetime object that defines a point in time to which
                the window is aligned to modulo window size. For further
                information, consult the class level documentation.

        Raises:
            asyncio.CancelledError: when the task gets cancelled.
        """
        assert (
            input_sampling_period.total_seconds() > 0
        ), "The input sampling period should be greater than zero."
        assert (
            input_sampling_period <= size
        ), "The input sampling period should be equal to or lower than the window size."

        self._sampling_period = input_sampling_period

        self._resampler: Resampler | None = None
        self._resampler_sender: Sender[Sample[Quantity]] | None = None
        self._resampler_task: asyncio.Task[None] | None = None

        if resampler_config:
            assert (
                resampler_config.resampling_period <= size
            ), "The resampling period should be equal to or lower than the window size."

            self._resampler = Resampler(resampler_config)
            self._sampling_period = resampler_config.resampling_period

        # Sampling period might not fit perfectly into the window size.
        num_samples = math.ceil(
            size.total_seconds() / self._sampling_period.total_seconds()
        )

        self._resampled_data_recv = resampled_data_recv
        self._buffer = OrderedRingBuffer(
            np.empty(shape=num_samples, dtype=float),
            sampling_period=self._sampling_period,
            align_to=align_to,
        )

        if self._resampler:
            self._configure_resampler()

        self._update_window_task: asyncio.Task[None] = asyncio.create_task(
            self._run_impl()
        )

    @property
    def sampling_period(self) -> timedelta:
        """
        Return the sampling period of the MovingWindow.

        Returns:
            The sampling period of the MovingWindow.
        """
        return self._sampling_period

    async def _run_impl(self) -> None:
        """Awaits samples from the receiver and updates the underlying ring buffer.

        Raises:
            asyncio.CancelledError: if the MovingWindow task is cancelled.
        """
        try:
            async for sample in self._resampled_data_recv:
                _logger.debug("Received new sample: %s", sample)
                if self._resampler and self._resampler_sender:
                    await self._resampler_sender.send(sample)
                else:
                    self._buffer.update(sample)

        except asyncio.CancelledError:
            _logger.info("MovingWindow task has been cancelled.")
            raise

        _logger.error("Channel has been closed")

    async def stop(self) -> None:
        """Cancel the running tasks and stop the MovingWindow."""
        await cancel_and_await(self._update_window_task)
        if self._resampler_task:
            await cancel_and_await(self._resampler_task)

    def _configure_resampler(self) -> None:
        """Configure the components needed to run the resampler."""
        assert self._resampler is not None

        async def sink_buffer(sample: Sample[Quantity]) -> None:
            if sample.value is not None:
                self._buffer.update(sample)

        resampler_channel = Broadcast[Sample[Quantity]]("average")
        self._resampler_sender = resampler_channel.new_sender()
        self._resampler.add_timeseries(
            "avg", resampler_channel.new_receiver(), sink_buffer
        )
        self._resampler_task = asyncio.create_task(self._resampler.resample())

    def __len__(self) -> int:
        """
        Return the size of the `MovingWindow`s underlying buffer.

        Returns:
            The size of the `MovingWindow`.
        """
        return len(self._buffer)

    @overload
    def __getitem__(self, key: SupportsIndex) -> float:
        """See the main __getitem__ method.

        # noqa: DAR101 key
        """

    @overload
    def __getitem__(self, key: datetime) -> float:
        """See the main __getitem__ method.

        # noqa: DAR101 key
        """

    @overload
    def __getitem__(self, key: slice) -> ArrayLike:
        """See the main __getitem__ method.

        # noqa: DAR101 key
        """

    def __getitem__(self, key: SupportsIndex | datetime | slice) -> float | ArrayLike:
        """
        Return a sub window of the `MovingWindow`.

        The `MovingWindow` is accessed either by timestamp or by index
        or by a slice of timestamps or integers.

        * If the key is an integer, the float value of that key
          at the given position is returned.
        * If the key is a datetime object, the float value of that key
          that corresponds to the timestamp is returned.
        * If the key is a slice of timestamps or integers, an ndarray is returned,
          where the bounds correspond to the slice bounds.
          Note that a half open interval, which is open at the end, is returned.

        Args:
            key: Either an integer or a timestamp or a slice of timestamps or integers.

        Raises:
            IndexError: when requesting an out of range timestamp or index
            TypeError: when the key is not a datetime or slice object.

        Returns:
            A float if the key is a number or a timestamp.
            an numpy array if the key is a slice.
        """
        if isinstance(key, slice):
            if isinstance(key.start, int) or isinstance(key.stop, int):
                if key.start is None or key.stop is None:
                    key = slice(slice(key.start, key.stop).indices(self.__len__()))
            elif isinstance(key.start, datetime) or isinstance(key.stop, datetime):
                if key.start is None:
                    key = slice(self._buffer.time_bound_oldest, key.stop)
                if key.stop is None:
                    key = slice(key.start, self._buffer.time_bound_newest)

            _logger.debug("Returning slice for [%s:%s].", key.start, key.stop)

            # we are doing runtime typechecks since there is no abstract slice type yet
            # see also (https://peps.python.org/pep-0696)
            if isinstance(key.start, datetime) and isinstance(key.stop, datetime):
                return self._buffer.window(key.start, key.stop)
            if isinstance(key.start, int) and isinstance(key.stop, int):
                return self._buffer[key]
        elif isinstance(key, datetime):
            _logger.debug("Returning value at time %s ", key)
            return self._buffer[self._buffer.datetime_to_index(key)]
        elif isinstance(key, SupportsIndex):
            return self._buffer[key]

        raise TypeError(
            "Key has to be either a timestamp or an integer "
            "or a slice of timestamps or integers"
        )


# We need to register the class as a subclass of Sequence like this because
# otherwise type-checking fails complaining that MovingWindow has more
# overloads of __getitem__() than Sequence (which doesn't have an overload with
# a datetime key)
Sequence.register(MovingWindow)
