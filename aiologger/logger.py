import asyncio
import logging
import sys
from asyncio.streams import StreamWriter
from asyncio.unix_events import _set_nonblocking
from io import TextIOBase
from typing import Type, Union

from aiologger.filters import StdoutFilter
from aiologger.protocols import AiologgerProtocol


class AsyncStreamHandler(logging.StreamHandler):
    @classmethod
    def make(cls,
             level: Union[int, str],
             stream: StreamWriter,
             formatter: logging.Formatter,
             filter: logging.Filter=None) -> 'AsyncStreamHandler':
        self = cls(stream)
        self.setLevel(level)
        self.setFormatter(formatter)

        if filter:
            self.addFilter(filter)

        return self

    async def handleError(self, record: logging.LogRecord):
        """
        Handle errors which occur during an emit() call.

        This method should be called from handlers when an exception is
        encountered during an emit() call. If raiseExceptions is false,
        exceptions get silently ignored. This is what is mostly wanted
        for a logging system - most users will not care about errors in
        the logging system, they are more interested in application errors.
        You could, however, replace this with a custom handler if you wish.
        The record which was being processed is passed in to this method.
        """
        pass  # pragma: no cover

    async def handle(self, record: logging.LogRecord) -> bool:
        """
        Conditionally emit the specified logging record.
        Emission depends on filters which may have been added to the handler.
        """
        rv = self.filter(record)
        if rv:
            await self.emit(record)
        return rv

    async def emit(self, record: logging.LogRecord):
        """
        Actually log the specified logging record to the stream.
        """
        try:
            msg = self.format(record) + self.terminator

            await self.stream.write(msg.encode())
            await self.stream.drain()
        except Exception:
            await self.handleError(record)


class Logger(logging.Logger):
    def __init__(self, *,
                 loop=None,
                 stdout_writer: StreamWriter,
                 stderr_writer: StreamWriter,
                 name='json_logger',
                 level=logging.DEBUG,
                 formatter: logging.Formatter=None):
        super(Logger, self).__init__(name, level)
        self.loop = loop
        self.stdout_writer = stdout_writer
        self.stderr_writer = stderr_writer
        if formatter is None:
            formatter = logging.Formatter()
        self.formatter = formatter

        stdout_handler = AsyncStreamHandler.make(level=logging.DEBUG,
                                                 stream=self.stdout_writer,
                                                 formatter=self.formatter,
                                                 filter=StdoutFilter())
        self.addHandler(stdout_handler)

        stderr_handler = AsyncStreamHandler.make(level=logging.WARNING,
                                                 stream=self.stderr_writer,
                                                 formatter=self.formatter)
        self.addHandler(stderr_handler)

    @classmethod
    async def make_stream_writer(cls,
                                 protocol_factory: Type[asyncio.Protocol],
                                 pipe: TextIOBase,
                                 pipe_fileno: int=None,
                                 loop=None) -> StreamWriter:
        """
        The traditional UNIX system calls are blocking.
        """
        loop = loop or asyncio.get_event_loop()
        _set_nonblocking(pipe_fileno or pipe.fileno())
        transport, protocol = await loop.connect_write_pipe(protocol_factory,
                                                            pipe)
        return StreamWriter(transport=transport,
                            protocol=protocol,
                            reader=None,
                            loop=loop)

    @classmethod
    async def init_async(cls, *,
                         loop=None,
                         name='default',
                         level=logging.DEBUG):
        loop = loop or asyncio.get_event_loop()

        stdout_writer = await cls.make_stream_writer(
            protocol_factory=AiologgerProtocol,
            pipe=sys.stdout,
            loop=loop
        )

        stderr_writer = await cls.make_stream_writer(
            protocol_factory=AiologgerProtocol,
            pipe=sys.stderr,
            loop=loop
        )

        return cls(
            loop=loop,
            stdout_writer=stdout_writer,
            stderr_writer=stderr_writer,
            name=name,
            level=level
        )

    async def callHandlers(self, record):
        """
        Pass a record to all relevant handlers.

        Loop through all handlers for this logger and its parents in the
        logger hierarchy. If no handler was found, raises an error. Stop
        searching up the hierarchy whenever a logger with the "propagate"
        attribute set to zero is found - that will be the last logger
        whose handlers are called.
        """
        c = self
        found = 0
        while c:
            for handler in c.handlers:
                found = found + 1
                if record.levelno >= handler.level:
                    await handler.handle(record)
            if not c.propagate:
                c = None  # break out
            else:
                c = c.parent
        if found == 0:
            raise Exception("No handlers could be found for logger")

    async def handle(self, record):
        """
        Call the handlers for the specified record.

        This method is used for unpickled records received from a socket, as
        well as those created locally. Logger-level filtering is applied.
        """
        if (not self.disabled) and self.filter(record):
            await self.callHandlers(record)
