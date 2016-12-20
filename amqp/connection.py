"""AMQP Connections."""
# Copyright (C) 2007-2008 Barry Pederson <bp@barryp.org>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301
import asyncio
import logging
import socket
import uuid
import warnings

from array import array
from io import BytesIO
from ssl import SSLError
from time import monotonic
from typing import Any, ByteString, Callable, List, Mapping, Tuple

from vine import Thenable, ensure_promise

from . import __version__
from . import spec
from . import abstract
from .abstract_channel import ChannelBase
from .channel import Channel
from .exceptions import (
    AMQPDeprecationWarning, ChannelError, ResourceError,
    ConnectionForced, ConnectionError, error_for_code,
    RecoverableConnectionError, RecoverableChannelError,
)
from .method_framing import frame_handler, frame_writer
from .serialization import _write_table
from .transport import Transport
from .types import SSLArg

W_FORCE_CONNECT = """\
The .{attr} attribute on the connection was accessed before
the connection was established.  This is supported for now, but will
be deprecated in amqp 2.2.0.

Since amqp 2.0 you have to explicitly call Connection.connect()
before using the connection.
"""

START_DEBUG_FMT = """
Start from server, version: %d.%d, properties: %s, mechanisms: %s, locales: %s
""".strip()

__all__ = ['Connection']

AMQP_LOGGER: logging.Logger = logging.getLogger('amqp')

#: Default map for :attr:`Connection.library_properties`
LIBRARY_PROPERTIES: Mapping[str, Any] = {
    'product': 'py-amqp',
    'product_version': __version__,
}

#: Default map for :attr:`Connection.negotiate_capabilities`
NEGOTIATE_CAPABILITIES: Mapping[str, Any] = {
    'consumer_cancel_notify': True,
    'connection.blocked': True,
    'authentication_failure_close': True,
}

ConnectionBlockedCallback = Callable[[str], None]
ConnectionUnblockedCallback = Callable[[], None]
ConnectionInboundMethodHandler = Callable[
    [int, Tuple[Any], ByteString, ByteString], Any,
]
ConnectionFrameHandler = Callable[
    ['Connection', ConnectionInboundMethodHandler],
    Callable,
]
ConnectionFrameWriter = Callable[
    ['Connection', Transport],
    Callable,
]
MethodSigMethodMapping = Mapping[Tuple[Any], Tuple[Any]]


class Connection(ChannelBase):
    """AMQP Connection.

    The connection class provides methods for a client to establish a
    network connection to a server, and for both peers to operate the
    connection thereafter.

    GRAMMAR::

        connection          = open-connection *use-connection close-connection
        open-connection     = C:protocol-header
                              S:START C:START-OK
                              *challenge
                              S:TUNE C:TUNE-OK
                              C:OPEN S:OPEN-OK
        challenge           = S:SECURE C:SECURE-OK
        use-connection      = *channel
        close-connection    = C:CLOSE S:CLOSE-OK
                            / S:CLOSE C:CLOSE-OK
    Create a connection to the specified host, which should be
    a 'host[:port]', such as 'localhost', or '1.2.3.4:5672'
    (defaults to 'localhost', if a port is not specified then
    5672 is used)

    If login_response is not specified, one is built up for you from
    userid and password if they are present.

    The 'ssl' parameter may be simply True/False, or for Python >= 2.6
    a dictionary of options to pass to ssl.wrap_socket() such as
    requiring certain certificates.

    The "socket_settings" parameter is a dictionary defining tcp
    settings which will be applied as socket options.
    """

    Channel: Channel = Channel
    Transport: Transport = Transport

    #: Mapping of protocol extensions to enable.
    #: The server will report these in server_properties[capabilities],
    #: and if a key in this map is present the client will tell the
    #: server to either enable or disable the capability depending
    #: on the value set in this map.
    #: For example with:
    #:     negotiate_capabilities = {
    #:         'consumer_cancel_notify': True,
    #:     }
    #: The client will enable this capability if the server reports
    #: support for it, but if the value is False the client will
    #: disable the capability.
    negotiate_capabilities = NEGOTIATE_CAPABILITIES

    #: These are sent to the server to announce what features
    #: we support, type of client etc.
    library_properties = LIBRARY_PROPERTIES

    #: Final heartbeat interval value (in float seconds) after negotiation
    heartbeat: float = None

    #: Original heartbeat interval value proposed by client.
    client_heartbeat: float = None

    #: Original heartbeat interval proposed by server.
    server_heartbeat: float = None

    #: Time of last heartbeat sent (in monotonic time, if available).
    last_heartbeat_sent: float = 0.0

    #: Time of last heartbeat received (in monotonic time, if available).
    last_heartbeat_received: float = 0.0

    #: Number of successful writes to socket.
    bytes_sent: int = 0

    #: Number of successful reads from socket.
    bytes_recv: int = 0

    #: Number of bytes sent to socket at the last heartbeat check.
    prev_sent: int = None

    #: Number of bytes received from socket at the last heartbeat check.
    prev_recv: int = None

    _METHODSET: List[spec.method_sig_t] = [
        spec.method(spec.Connection.Start, 'ooFSS'),
        spec.method(spec.Connection.OpenOk),
        spec.method(spec.Connection.Secure, 's'),
        spec.method(spec.Connection.Tune, 'BlB'),
        spec.method(spec.Connection.Close, 'BsBB'),
        spec.method(spec.Connection.Blocked),
        spec.method(spec.Connection.Unblocked),
        spec.method(spec.Connection.CloseOk),
    ]

    _METHODS: MethodSigMethodMapping = {
        m.method_sig: m for m in _METHODSET
    }

    connection_errors: Tuple[Any, ...] = (
        ConnectionError,
        socket.error,
        IOError,
        OSError,
    )
    channel_errors: Tuple[Any, ...] = (ChannelError,)
    recoverable_connection_errors: Tuple[Any, ...] = (
        RecoverableConnectionError,
        socket.error,
        IOError,
        OSError,
    )
    recoverable_channel_errors: Tuple[Any, ...] = (
        RecoverableChannelError,
    )

    def __init__(self,
                 host: str = 'localhost:5672',
                 userid: str = 'guest',
                 password: str = 'guest',
                 login_method: str = 'AMQPLAIN',
                 login_response: Any = None,
                 virtual_host: str = '/',
                 locale: str = 'en_US',
                 client_properties: Mapping = None,
                 ssl: SSLArg = False,
                 connect_timeout: float = None,
                 channel_max: int = None,
                 frame_max: int = None,
                 heartbeat: float = 0.0,
                 on_open: Thenable = None,
                 on_blocked: ConnectionBlockedCallback = None,
                 on_unblocked: ConnectionUnblockedCallback = None,
                 confirm_publish: bool = False,
                 on_tune_ok: Callable = None,
                 read_timeout: float = None,
                 write_timeout: float = None,
                 socket_settings: Mapping = None,
                 frame_handler: ConnectionFrameHandler = frame_handler,
                 frame_writer: ConnectionFrameWriter = frame_writer,
                 loop: Any = None,
                 **kwargs):
        self.loop = loop or asyncio.get_event_loop()
        self._connection_id: str = uuid.uuid4().hex
        channel_max: int = channel_max or 65535
        frame_max: int = frame_max or 131072
        if (login_response is None) \
                and (userid is not None) \
                and (password is not None):
            login_response: ByteString = BytesIO()
            _write_table({'LOGIN': userid, 'PASSWORD': password},
                         login_response.write, [])
            # Skip the length at the beginning
            login_response = login_response.getvalue()[4:]

        self.client_properties = dict(
            self.library_properties, **client_properties or {}
        )
        self.login_method: str = login_method
        self.login_response: str = login_response
        self.locale: str = locale
        self.host: str = host
        self.virtual_host: str = virtual_host
        self.on_tune_ok: Thenable = ensure_promise(on_tune_ok)

        self.frame_handler_cls: ConnectionFrameHandler = frame_handler
        self.frame_writer_cls: ConnectionFrameWriter = frame_writer

        self._handshake_complete: bool = False

        self.channels: Mapping[int, Channel] = {}
        # The connection object itself is treated as channel 0
        super().__init__(self, 0)

        self._frame_writer: Callable = None
        self._on_inbound_frame: Any = None
        self._transport: Transport = None         # type: Transport

        # Properties set in the Tune method
        self.channel_max: int = channel_max
        self.frame_max: int = frame_max
        self.client_heartbeat: float = heartbeat

        self.confirm_publish: bool = confirm_publish
        self.ssl: SSLArg = ssl
        self.read_timeout: float = read_timeout
        self.write_timeout: float = write_timeout
        self.socket_settings: Mapping = socket_settings

        # Callbacks
        self.on_blocked: ConnectionBlockedCallback = on_blocked
        self.on_unblocked: ConnectionUnblockedCallback = on_unblocked
        self.on_open: Thenable = ensure_promise(on_open)

        # type: Sequence[int]
        self._avail_channel_ids: Sequence[int] = array(
            'H', range(self.channel_max, 0, -1))

        # Properties set in the Start method
        self.version_major: int = 0
        self.version_minor: int = 0
        self.server_properties: Mapping[str, Any] = {}
        self.mechanisms: List[Any] = []
        self.locales: List[str] = []

        self.connect_timeout: float = connect_timeout

    def __enter__(self) -> Any:
        self.connect()
        return self

    def __exit__(self, *eargs) -> None:
        self.close()

    def then(self, on_success: Thenable, on_error: Thenable=None) -> Thenable:
        return self.on_open.then(on_success, on_error)

    def _setup_listeners(self) -> None:
        self._callbacks.update({
            spec.Connection.Start: self._on_start,
            spec.Connection.OpenOk: self._on_open_ok,
            spec.Connection.Secure: self._on_secure,
            spec.Connection.Tune: self._on_tune,
            spec.Connection.Close: self._on_close,
            spec.Connection.Blocked: self._on_blocked,
            spec.Connection.Unblocked: self._on_unblocked,
            spec.Connection.CloseOk: self._on_close_ok,
        })

    async def connect(self, callback: Callable[[], None] = None) -> None:
        # Let the transport.py module setup the actual
        # socket connection to the broker.
        #
        if self.connected:
            await callback() if callback else None
        else:
            self.transport = self.Transport(
                self.host, self.connect_timeout,
                self.read_timeout, self.write_timeout,
                socket_settings=self.socket_settings,
                ssl=self.ssl,
            )
            await self.transport.connect()
            self.on_inbound_frame = self.frame_handler_cls(
                self, self.on_inbound_method)
            self.frame_writer = self.frame_writer_cls(self, self.transport)

            while not self._handshake_complete:
                await self.drain_events(timeout=self.connect_timeout)

    def _warn_force_connect(self, attr: str) -> None:
        warnings.warn(AMQPDeprecationWarning(
            W_FORCE_CONNECT.format(attr=attr)))

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._warn_force_connect('transport')
            self.connect()
        return self._transport

    @transport.setter
    def transport(self, transport):
        self._transport = transport

    @property
    def on_inbound_frame(self) -> Any:
        if self._on_inbound_frame is None:
            self._warn_force_connect('on_inbound_frame')
            self.connect()
        return self._on_inbound_frame

    @on_inbound_frame.setter
    def on_inbound_frame(self, on_inbound_frame):
        self._on_inbound_frame = on_inbound_frame

    @property
    def frame_writer(self) -> Callable:
        if self._frame_writer is None:
            self._warn_force_connect('frame_writer')
            self.connect()
        return self._frame_writer

    @frame_writer.setter
    def frame_writer(self, frame_writer):
        self._frame_writer = frame_writer

    async def _on_start(self,
                        version_major: int,
                        version_minor: int,
                        server_properties: Mapping[Any, Any],
                        mechanisms: str,
                        locales: str,
                        argsig: str='FsSs') -> None:
        # type: (int, int, Mapping[Any, Any], str, str, str) -> None
        client_properties = self.client_properties
        self.version_major = version_major
        self.version_minor = version_minor
        self.server_properties = server_properties
        self.mechanisms = mechanisms.split(' ')
        self.locales = locales.split(' ')
        AMQP_LOGGER.debug(
            START_DEBUG_FMT,
            self.version_major, self.version_minor,
            self.server_properties, self.mechanisms, self.locales,
        )

        # Negotiate protocol extensions (capabilities)
        scap = server_properties.get('capabilities') or {}
        cap = client_properties.setdefault('capabilities', {})
        cap.update({
            wanted_cap: enable_cap
            for wanted_cap, enable_cap in self.negotiate_capabilities.items()
            if scap.get(wanted_cap)
        })
        if not cap:
            # no capabilities, server may not react well to having
            # this key present in client_properties, so we remove it.
            client_properties.pop('capabilities', None)

        return await self.send_method(
            spec.Connection.StartOk, argsig,
            (client_properties, self.login_method,
             self.login_response, self.locale),
        )

    async def _on_secure(self, challenge: str) -> None:
        ...

    async def _on_tune(self,
                       channel_max: int,
                       frame_max: int,
                       server_heartbeat: float,
                       argsig: str='BlB'):
        client_heartbeat = self.client_heartbeat or 0
        self.channel_max = channel_max or self.channel_max
        self.frame_max = frame_max or self.frame_max
        self.server_heartbeat = server_heartbeat or 0

        # negotiate the heartbeat interval to the smaller of the
        # specified values
        if self.server_heartbeat == 0 or client_heartbeat == 0:
            self.heartbeat = max(self.server_heartbeat, client_heartbeat)
        else:
            self.heartbeat = min(self.server_heartbeat, client_heartbeat)

        # Ignore server heartbeat if client_heartbeat is disabled
        if not self.client_heartbeat:
            self.heartbeat = 0

        return await self.send_method(
            spec.Connection.TuneOk, argsig,
            (self.channel_max, self.frame_max, self.heartbeat),
            callback=self._on_tune_sent,
        )

    async def _on_tune_sent(self, argsig: str='ssb'):
        return await self.send_method(
            spec.Connection.Open, argsig, (self.virtual_host, '', False),
        )

    async def _on_open_ok(self) -> None:
        self._handshake_complete = True
        return self.on_open(self)

    @property
    def connected(self) -> bool:
        return self._transport and self._transport.connected

    def collect(self) -> None:
        try:
            self.transport.close()

            temp_list = [x for x in self.channels.values() if x is not self]
            for ch in temp_list:
                ch.collect()
        except socket.error:
            pass  # connection already closed on the other end
        finally:
            self._transport = self.connection = self.channels = None

    def _get_free_channel_id(self) -> int:
        try:
            return self._avail_channel_ids.pop()
        except IndexError:
            raise ResourceError(
                'No free channel ids, current={0}, channel_max={1}'.format(
                    len(self.channels), self.channel_max), spec.Channel.Open)

    def _claim_channel_id(self, channel_id) -> None:
        try:
            self._avail_channel_ids.remove(channel_id)
        except ValueError:
            raise ConnectionError('Channel %r already open' % (channel_id,))

    async def channel(self,
                      channel_id: int = None,
                      callback: Callable = None) -> Channel:
        """Create new channel.

        Fetch a Channel object identified by the numeric channel_id, or
        create that object if it doesn't already exist.
        """
        if self.channels is not None:
            try:
                return self.channels[channel_id]
            except KeyError:
                channel = self.Channel(self, channel_id, on_open=callback)
                await channel.open()
                return channel
        raise RecoverableConnectionError('Connection already closed.')

    def is_alive(self) -> bool:
        raise NotImplementedError('Use AMQP heartbeats')

    async def drain_events(self, timeout: float=None) -> None:
        return await self.blocking_read(timeout)

    async def blocking_read(self, timeout: float=None) -> Any:
        with self.transport.having_timeout(timeout):
            frame = await self.transport.read_frame()
        return await self.on_inbound_frame(frame)

    async def on_inbound_method(self, channel_id, method_sig, payload, content):
        return await self.channels[channel_id].dispatch_method(
            method_sig, payload, content,
        )

    async def close(self,
                    reply_code: int = 0,
                    reply_text: str = '',
                    method_sig: Tuple[int, int] = (0, 0),
                    argsig: str = 'BsBB'):
        """Request a connection close.

        This method indicates that the sender wants to close the
        connection. This may be due to internal conditions (e.g. a
        forced shut-down) or due to an error handling a specific
        method, i.e. an exception.  When a close is due to an
        exception, the sender provides the class and method id of the
        method which caused the exception.

        RULE:

            After sending this method any received method except the
            Close-OK method MUST be discarded.

        RULE:

            The peer sending this method MAY use a counter or timeout
            to detect failure of the other peer to respond correctly
            with the Close-OK method.

        RULE:

            When a server receives the Close method from a client it
            MUST delete all server-side resources associated with the
            client's context.  A client CANNOT reconnect to a context
            after sending or receiving a Close method.

        PARAMETERS:
            reply_code: short

                The reply code. The AMQ reply codes are defined in AMQ
                RFC 011.

            reply_text: shortstr

                The localised reply text.  This text can be logged as an
                aid to resolving issues.

            class_id: short

                failing method class

                When the close is provoked by a method exception, this
                is the class of the method.

            method_id: short

                failing method ID

                When the close is provoked by a method exception, this
                is the ID of the method.
        """
        if self._transport is None:
            # already closed
            return

        return await self.send_method(
            spec.Connection.Close, argsig,
            (reply_code, reply_text, method_sig[0], method_sig[1]),
            wait=spec.Connection.CloseOk,
        )

    async def _on_close(self, reply_code, reply_text, class_id, method_id):
        """Request a connection close.

        This method indicates that the sender wants to close the
        connection. This may be due to internal conditions (e.g. a
        forced shut-down) or due to an error handling a specific
        method, i.e. an exception.  When a close is due to an
        exception, the sender provides the class and method id of the
        method which caused the exception.

        RULE:

            After sending this method any received method except the
            Close-OK method MUST be discarded.

        RULE:

            The peer sending this method MAY use a counter or timeout
            to detect failure of the other peer to respond correctly
            with the Close-OK method.

        RULE:

            When a server receives the Close method from a client it
            MUST delete all server-side resources associated with the
            client's context.  A client CANNOT reconnect to a context
            after sending or receiving a Close method.

        PARAMETERS:
            reply_code: short

                The reply code. The AMQ reply codes are defined in AMQ
                RFC 011.

            reply_text: shortstr

                The localised reply text.  This text can be logged as an
                aid to resolving issues.

            class_id: short

                failing method class

                When the close is provoked by a method exception, this
                is the class of the method.

            method_id: short

                failing method ID

                When the close is provoked by a method exception, this
                is the ID of the method.
        """
        await self._x_close_ok()
        raise error_for_code(reply_code, reply_text,
                             (class_id, method_id), ConnectionError)

    async def _x_close_ok(self):
        """Confirm a connection close.

        This method confirms a Connection.Close method and tells the
        recipient that it is safe to release resources for the
        connection and close the socket.

        RULE:
            A peer that detects a socket closure without having
            received a Close-Ok handshake method SHOULD log the error.
        """
        return await self.send_method(
            spec.Connection.CloseOk, callback=self._on_close_ok)

    async def _on_close_ok(self):
        """Confirm a connection close.

        This method confirms a Connection.Close method and tells the
        recipient that it is safe to release resources for the
        connection and close the socket.

        RULE:

            A peer that detects a socket closure without having
            received a Close-Ok handshake method SHOULD log the error.
        """
        self.collect()

    async def _on_blocked(self):
        """Callback called when connection blocked.

        Notes:
            This is an RabbitMQ Extension.
        """
        reason = 'connection blocked, see broker logs'
        if self.on_blocked:
            return await self.on_blocked(reason)

    async def _on_unblocked(self):
        if self.on_unblocked:
            return await self.on_unblocked()

    async def send_heartbeat(self):
        return await self.frame_writer(8, 0, None, None, None)

    async def heartbeat_tick(self, rate=2):
        """Send heartbeat packets if necessary.

        Raises:
            ~amqp.exceptions.ConnectionForvced: if none have been
                received recently.

        Note:
            This should be called frequently, on the order of
            once per second.

        Keyword Arguments:
            rate (int): Previously used, but ignored now.
        """
        AMQP_LOGGER.debug('heartbeat_tick : for connection %s',
                          self._connection_id)
        if not self.heartbeat:
            return

        # treat actual data exchange in either direction as a heartbeat
        sent_now = self.bytes_sent
        recv_now = self.bytes_recv
        if self.prev_sent is None or self.prev_sent != sent_now:
            self.last_heartbeat_sent = monotonic()
        if self.prev_recv is None or self.prev_recv != recv_now:
            self.last_heartbeat_received = monotonic()

        now = monotonic()
        AMQP_LOGGER.debug(
            'heartbeat_tick : Prev sent/recv: %s/%s, '
            'now - %s/%s, monotonic - %s, '
            'last_heartbeat_sent - %s, heartbeat int. - %s '
            'for connection %s',
            self.prev_sent, self.prev_recv,
            sent_now, recv_now, now,
            self.last_heartbeat_sent,
            self.heartbeat,
            self._connection_id,
        )

        self.prev_sent, self.prev_recv = sent_now, recv_now

        # send a heartbeat if it's time to do so
        if now > self.last_heartbeat_sent + self.heartbeat:
            AMQP_LOGGER.debug(
                'heartbeat_tick: sending heartbeat for connection %s',
                self._connection_id)
            await self.send_heartbeat()
            self.last_heartbeat_sent = monotonic()

        # if we've missed two intervals' heartbeats, fail; this gives the
        # server enough time to send heartbeats a little late
        if (self.last_heartbeat_received and
                self.last_heartbeat_received + 2 *
                self.heartbeat < monotonic()):
            raise ConnectionForced('Too many heartbeats missed')

    @property
    def sock(self):
        return self.transport.sock

    @property
    def server_capabilities(self):
        return self.server_properties.get('capabilities') or {}
