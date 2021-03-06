"""Provides stubs for required callable DBus objects.
https://git.kernel.org/cgit/bluetooth/bluez.git/tree/doc
"""

import logging
import os
import socket

from dbus import Array, Byte, UInt16
import dbus.service

from pytooth.bluez5.helpers import Bluez5Utils, dbus_to_py
from pytooth.errors import InvalidOperationError

logger = logging.getLogger(__name__)


class Media:
    """Encapsulates a Media bluez5 object.
    """

    def __init__(self, system_bus, adapter_path):
        self._media_proxy = Bluez5Utils.get_media(
            bus=system_bus,
            adapter_path=adapter_path)

    def register(self, dbus_path, uuid, codec, capabilities):
        """Registers our capabilities with bluez5.
        """
        self._media_proxy.proxy.RegisterEndpoint(
            dbus_path,
            {
                "UUID": uuid,
                "Codec": Byte(codec),
                "Capabilities": Array(capabilities, signature="y")
            })

    def unregister(self, dbus_path):
        """Unregisters our capabilities with bluez5.
        """
        self._media_proxy.proxy.UnregisterEndpoint(dbus_path)

class MediaEndpoint(dbus.service.Object):
    """Encapsulates a MediaEndpoint bluez5 object.
    """

    def __init__(self, system_bus, dbus_path, configuration):
        dbus.service.Object.__init__(self, system_bus, dbus_path)

        self._configuration = configuration # desired
        self._transport = None
        self._system_bus = system_bus

        self.on_release = None
        self.on_transport_setup_error = None
        self.on_transport_state_changed = None

    @dbus.service.method(dbus_interface=Bluez5Utils.MEDIA_ENDPOINT_INTERFACE,
                         in_signature="oa{sv}", out_signature=None)
    def SetConfiguration(self, transport, properties):
        """Invoked by bluez5 when the transport configuration has been set.
        """
        transport = dbus_to_py(transport)
        properties = dbus_to_py(properties)

        logger.debug("Media endpoint config set - {}".format(properties))
        logger.debug("Media transport is available - {}".format(transport))

        # build media transport
        try:
            self._transport = MediaTransport(
                system_bus=self._system_bus,
                dbus_path=transport)
        except Exception as ex:
            logger.exception("Error fetching media transport.")
            if self.on_transport_setup_error:
                self.on_transport_setup_error(ex)
            return

        # hand out
        if self.on_transport_state_changed:
            self.on_transport_state_changed(
                transport=self._transport,
                available=True)

    @dbus.service.method(dbus_interface=Bluez5Utils.MEDIA_ENDPOINT_INTERFACE,
                         in_signature="ab", out_signature="ab")
    def SelectConfiguration(self, capabilities):
        """Invoked by bluez5 when negotiating transport configuration with us.
        """
        capabilities = dbus_to_py(capabilities)

        logger.debug("Media endpoint capabilities - {}".format(capabilities))
        return self._configuration

    @dbus.service.method(dbus_interface=Bluez5Utils.MEDIA_ENDPOINT_INTERFACE,
                         in_signature="o", out_signature=None)
    def ClearConfiguration(self, transport):
        """Invoked by bluez5 when it is forgetting configuration because the
        transport was stopped.
        """
        transport = dbus_to_py(transport)

        logger.debug("Bluez5 has cleared the configuration for transport - {}"
            "".format(transport))

        if self.on_transport_state_changed:
            self.on_transport_state_changed(
                transport=self._transport,
                available=False)
            self._transport = None

    @dbus.service.method(dbus_interface=Bluez5Utils.MEDIA_ENDPOINT_INTERFACE,
                         in_signature=None, out_signature=None)
    def Release(self):
        """Invoked when bluez5 shuts down.
        """
        if self.on_release:
            self.on_release()

class MediaTransport:
    """Encapsulates a bluez5 MediaTransport object.
    """

    def __init__(self, system_bus, dbus_path):
        self._system_bus = system_bus
        self._proxy = Bluez5Utils.get_media_transport(
            bus=self._system_bus,
            transport_path=dbus_path)
        
        # other state
        self._acquired = False
        self._fd = None
        self._socket = None
        self._read_mtu = None
        self._write_mtu = None

    @property
    def acquired(self):
        return self._acquired

    @property
    def fd(self):
        return self._fd

    @property
    def read_mtu(self):
        return self._read_mtu

    @property
    def write_mtu(self):
        return self._write_mtu

    @property
    def socket(self):
        return self._socket

    @property
    def proxy(self):
        """Returns the underlying DBusProxy object. Should only be used for
        property access.
        """
        return self._proxy

    def acquire(self):
        """Acquires the transport OS file descriptor from bluez5.
        """
        if self._acquired:
            return

        logger.debug("Acquiring OS file descriptor for transport - {}".format(
            self._proxy.path))
        self._fd, self._read_mtu, self._write_mtu = \
            self._proxy.proxy.TryAcquire()
        self._fd = self._fd.take()
        self._socket = socket.socket(fileno=self._fd)
        logger.debug("Successfully acquired OS file descriptor - fd={}, "
            "readMTU={}, writeMTU={}".format(
                self._fd, self._read_mtu, self._write_mtu))
        self._acquired = True

    def release(self):
        """Manually releases the media transport.
        """
        if not self._acquired:
            return

        logger.debug("Releasing the media transport.")
        self._proxy.proxy.Release()
        self._acquired = False

    def __repr__(self):
        return "<MediaTransport: "+self._proxy.path+">"

    def __str__(self):
        return "<MediaTransport: "+self._proxy.path+">"

    def __unicode__(self):
        return "<MediaTransport: "+self._proxy.path+">"

class ObexSessionFactory:
    """Uses an Obex.Client1 bluez5 object to create and destroy Obex sessions.
    This does not do any session tracking; it is up to the caller to invoke
    destroy_session for every active session.
    """

    def __init__(self, session_bus):
        self._obex_client_proxy = Bluez5Utils.get_obex_client(
            bus=session_bus)
        self._session_bus = session_bus

    def create_session(self, destination, target):
        """Creates and returns a new Obex client session, encapsulated in a
        pytooth.bluez5.helpers.DBusProxy object.
        """
        session_path = self._obex_client_proxy.proxy.CreateSession(
            destination,
            {
                "Target": target
            })
        return Bluez5Utils.get_obex_session(
            bus=self._session_bus,
            session_path=session_path)

    def destroy_session(self, session):
        """Closes an existing Obex session.
        """
        self._obex_client_proxy.proxy.RemoveSession(session.path)

class PhonebookClient:
    """Wrapper that provides access to PBAP client methods. This class only
    permits one transfer of phonebook data at a time. As per PBAP spec, this
    also provides access to call history datasets.

    https://github.com/r10r/bluez/blob/master/doc/obex-api.txt
    """
    def __init__(self, session_bus, session):
        self._client = Bluez5Utils.get_phonebook_client(
            bus=session_bus,
            session_path=session.path)
        self._destination = session.get("Destination")
        self._session = session
        self._session_bus = session_bus
        self._transfer = None
        self._transfer_file = None

        # for some reason we can't access the properties via the DBusProxy
        # object. quite strange...
        session_bus.add_signal_receiver(
            self._properties_changed,
            dbus_interface=Bluez5Utils.PROPERTIES_INTERFACE,
            signal_name="PropertiesChanged",
            path_keyword="path")

        # public events
        self.on_transfer_complete = None
        self.on_transfer_error = None

    @property
    def destination(self):
        return self._destination

    @property
    def session(self):
        return self._session

    def _properties_changed(self, interface, properties, invalidated, path):
        """DBus callback that we use for checking the status of an existing
        transfer.
        """
        if self._transfer is None:
            return
        if self._transfer.path != path:
            return
        if "Status" not in properties:
            return

        status = properties["Status"]
        
        # still going?
        if status in ["queued", "active"]:
            return

        # store and cleanup before anything can blow up
        self._transfer = None
        fname = self._transfer_file
        self._transfer_file = None

        # Bluez doesn't elaborate on the error :(
        if status == "error":
            logger.info("Obex session transfer from '{}' failed.".format(
                self._destination))
            if self.on_transfer_error:
                self.on_transfer_error(
                    client=self)

        # Bluez writes the data to a temp file so we need
        # to return all data in that file and delete it
        # NOTE: parsing is the initiators responsibility
        if status == "complete":
            logger.info("Obex session transfer from '{}' completed.".format(
                self._destination))
            data = None
            try:
                with open(fname, 'r') as f:
                    data = f.read()
            except Exception:
                logger.exception("Error reading transferred data in "
                    "temporary file '{}' from '{}'.".format(
                        fname,
                        self._destination))
                if self.on_transfer_error:
                    self.on_transfer_error(
                        client=self)
            else:
                if self.on_transfer_complete:
                    self.on_transfer_complete(
                        client=self,
                        data=data)

            # delete the temporary file
            try:
                os.remove(fname)
                logger.debug("Temporary destination file '{}' for transfer from"
                    " '{}' has been deleted.".format(
                        fname,
                        self._destination))
            except Exception as e:
                logger.warning("Error deleting temporary destination file '{}' "
                    "for transfer from '{}' - {}".format(
                        fname,
                        self._destination,
                        e))

    def select(self, location, name):
        """Selects a phonebook for further operations. Location can be ['int',
        'sim1', 'sim2'...] and name can be ['pb', 'ich', 'och', 'mch', 'cch'].
        This does nothing if a transfer is in progress.
        """
        if self._transfer is not None:
            return

        self._client.proxy.Select(location, name)

    def get_all(self, fmt=None, order=None, offset=None, maxcount=None, \
        fields=None):
        """Fetches the entire selected phonebook. Actual data is returned via
        the `on_transfer_complete` event, if the transfer is successful. This
        does nothing if a transfer is in progress.
        """
        if self._transfer is not None:
            return

        # all filters are optional
        filters = {}
        if fmt is not None:
            filters.update({"Format": fmt})
        if order is not None:
            filters.update({"Order": order})
        if offset is not None:
            filters.update({"Offset": UInt16(offset)})
        if maxcount is not None:
            filters.update({"MaxCount": UInt16(maxcount)})
        if fields is not None:
            filters.update({"Fields": Array(fields, signature="s")})

        # start the transfer
        tx_path, tx_props = self._client.proxy.PullAll("", filters)
        self._transfer = Bluez5Utils.get_transfer(
            bus=self._session_bus,
            transfer_path=tx_path)
        self._transfer_file = tx_props["Filename"]

    def abort(self):
        """Abort the active transfer, if any. The underlying Obex session is
        left as-is. If there is no active transfer, this does nothing.
        """
        if self._transfer is not None:
            self._transfer = None
            self._transfer_file = None

class Profile(dbus.service.Object):
    """Encapsulates a Profile bluez5 object.
    """

    def __init__(self, system_bus, dbus_path):
        dbus.service.Object.__init__(self, system_bus, dbus_path)

        self._fds = {} # device: [fd]

        self.on_connect = None
        self.on_disconnect = None
        self.on_release = None

    @dbus.service.method(dbus_interface=Bluez5Utils.PROFILE_INTERFACE,
                         in_signature=None, out_signature=None)
    def Release(self):
        """Called when bluez5 unregisters the profile.
        """
        logger.debug("Bluez5 has unregistered the profile.")
        
        if self.on_release:
            self.on_release()

    @dbus.service.method(dbus_interface=Bluez5Utils.PROFILE_INTERFACE,
                         in_signature="oha{sv}", out_signature=None)
    def NewConnection(self, device, fd, fd_properties):
        """Called when a new service-level connection has been established.
        """
        device = dbus_to_py(device)
        fd_properties = dbus_to_py(fd_properties)

        logger.debug("New RFCOMM service-level connection - device={}, fd={}, "
            "fd_properties={}".format(device, fd, fd_properties))
        fd = fd.take()
        logger.debug("OS-level fd = {}".format(fd))

        # track new socket for later cleanup
        fds = self._fds.get(device, [])
        fds.append(fd)
        self._fds.update({device: fds})
        
        if self.on_connect:
            self.on_connect(
                device=device,
                socket=socket.socket(fileno=fd),
                fd_properties=fd_properties)

    @dbus.service.method(dbus_interface=Bluez5Utils.PROFILE_INTERFACE,
                         in_signature=None, out_signature="o")
    def RequestDisconnection(self, device):
        """Called when profile is disconnected from device.
        """
        device = dbus_to_py(device)

        logger.debug("Profile connections to device {} are now closed.".format(
            device))
        
        # need to close each socket to the device
        for fd in self._fds.pop(device, []):
            try:
                os.close(fd)
            except Exception:
                logger.exception("Unable to close fd {}.".format(fd))
        
        if self.on_disconnect:
            self.on_disconnect(
                device=device)
