"""http://www.bluez.org/bluez-5-api-introduction-and-porting-guide/
"""

import logging

from pytooth.agents import NoInputNoOutputAgent
from pytooth.bluez5.helpers import Bluez5Utils
from pytooth.constants import DBUS_AGENT_PATH
from pytooth.errors import CommandError, InvalidOperationError

logger = logging.getLogger(__name__)


class BaseAdapter:
    """Provides functions to control an adapter. Requires subclasses to
    configure an agent. A GI loop is required to receieve DBus signals.
    """

    def __init__(self, system_bus, io_loop, retry_interval, \
        preferred_address=None):
        
        # subclass-accessible
        self._preferred_address = preferred_address
        self._connected = False
        self._known_adapters = []
        self._started = False
        
        # events
        self.on_connected_changed = None
        self.on_properties_changed = None
        
        # public
        self.io_loop = io_loop
        self.retry_interval = retry_interval

        # dbus / bluez objects
        self._adapter_proxy = None
        self._adapter_props_proxy = None
        self._bus = system_bus
        self._objectmgr_proxy = Bluez5Utils.get_objectmanager(bus=system_bus)

        # subscribe to property changes
        system_bus.subscribe(
            iface=Bluez5Utils.PROPERTIES_INTERFACE,
            signal="PropertiesChanged",
            arg0=Bluez5Utils.ADAPTER_INTERFACE,
            signal_fired=self._propertieschanged)

    def start(self):
        """Starts interaction with bluez. If already started, this does nothing.
        """
        if self._started:
            return

        self._started = True
        self.io_loop.add_callback(callback=self._check_adapter_available)

    def stop(self):
        """Stops interaction with bluez. If already stopped, this does nothing.
        """
        if not self._started:
            return

        self._started = False
        self._known_adapters = []
        self._connected = False
        self._adapter_proxy = None
        self._adapter_props_proxy = None

    @property
    def address(self):
        """Returns the address of the connected adapter (if any), or None if
        no adapter is connected.
        """
        if self._adapter_props_proxy:
            return self._adapter_props_proxy.Address
        return None

    @property
    def connected(self):
        """Returns True if desired adapter is connected.
        """
        return self._connected

    @property
    def path(self):
        """Returns the DBus object path of the connected adapter (if any), or
        None if no adapter is connected.
        """
        if self._adapter_props_proxy:
            return self._adapter_props_proxy.path
        return None

    def set_discoverable(self, enabled, timeout=None):
        """Toggles visibility of the BT subsystem to other searching BT devices.
        Timeout is in seconds, or pass None for no timeout.
        """

        if not self._started:
            raise InvalidOperationError("Not started.")
        if self._adapter_props_proxy is None:
            raise InvalidOperationError("No adapter available.")
            
        try:
            self._adapter_props_proxy.Discoverable = enabled
            self._adapter_props_proxy.DiscoverableTimeout = timeout or 0
        except Exception as e:
            raise CommandError(e)

    def set_pairable(self, enabled, timeout=None):
        """Makes the BT subsystem pairable with other BT devices. Timeout is
        in seconds, or pass None for no timeout.
        """

        if not self._started:
            raise InvalidOperationError("Not started.")
        if self._adapter_props_proxy is None:
            raise InvalidOperationError("No adapter available.")

        try:
            self._adapter_props_proxy.Pairable = enabled
            self._adapter_props_proxy.PairableTimeout = timeout or 0
        except Exception as e:
            raise CommandError(e)

    def _check_adapter_available(self):
        """Attempts to get the specified adapter proxy object. This is called
        repeatedly as it's the most reliable detection method.
        """

        # break out of potentially infinite check loop
        if not self._started:
            return

        try:
            logger.debug("Checking for '{}' bluetooth adapter...".format(
                self._preferred_address if self._preferred_address \
                else "first available"))

            # get first or preferred BT adapter
            if len(self._known_adapters) == 0:
                # this will kick-start the property change signals
                adapter = Bluez5Utils.find_adapter(
                    bus=self._bus,
                    address=self._preferred_address)
            else:
                adapter = Bluez5Utils.find_adapter_from_paths(
                    bus=self._bus,
                    paths=self._known_adapters,
                    address=self._preferred_address)

            # check adapter connection status
            is_found = adapter is not None and self._adapter_proxy is None
            is_lost = adapter is None and self._adapter_proxy is not None
            
            # notify
            if is_lost:
                logger.info("No suitable adapter is available.")
                self._adapter_proxy = None
                self._adapter_props_proxy = None
            elif is_found:
                logger.info("Adapter '{} - {}' is available.".format(
                    adapter.Name,
                    adapter.Address))
                self._adapter_proxy = adapter[Bluez5Utils.ADAPTER_INTERFACE]
                self._adapter_props_proxy = adapter
            else:
                logger.debug("No change in adapter status.")
            if (is_found or is_lost) and self.on_connected_changed:
                self._connected = is_found
                self.io_loop.add_callback(
                    callback=self.on_connected_changed,
                    adapter=self)

        except Exception as e:
            logger.exception("Failed to get suitable adapter.")
            
        self.io_loop.call_later(
            delay=self.retry_interval,
            callback=self._check_adapter_available)

    def _propertieschanged(self, sender, object, iface, signal, params):
        """Fired by the system bus subscription when a Bluez5 object property
        changes. 
        e.g.
            object=/org/bluez/hci0
            iface=org.freedesktop.DBus.Properties
            signal=PropertiesChanged
            params=('org.bluez.Adapter1', {'Connected': True}, [])
        """
        if not self._started:
            return

        logger.debug("SIGNAL: object={}, iface={}, signal={}, params={}".format(
            object, iface, signal, params))

        # make it known to check it later
        if object not in self._known_adapters:
            logger.debug("Remembering adapter {} for later polling.".format(
                object))
            self._known_adapters.append(object)

        # pass DBus property proxy to event handler if the adapter is the one
        # we want to use
        if object == self.path:
            if self.on_properties_changed:
                self.on_properties_changed(
                    adapter=self,
                    props=params[1])

class OpenPairableAdapter(BaseAdapter):
    """Adapter that can accept unsecured (i.e. no PIN) pairing requests.
    """

    def __init__(self, system_bus, io_loop, *args, **kwargs):
        super().__init__(system_bus, io_loop, *args, **kwargs)
        
        self._system_bus = system_bus
        self.io_loop = io_loop

        # build agent
        self._agent = NoInputNoOutputAgent()
        self._agent.on_release = self._on_agent_release
        self._register_context = self._system_bus.register_object(
            path=DBUS_AGENT_PATH,
            object=self._agent,
            node_info=None)

        # register it
        self._agentmgr_proxy = Bluez5Utils.get_agentmanager(
            bus=self._system_bus)
        self._register_agent()

    def _register_agent(self):
        """Registers the agent.
        """
        self._agentmgr_proxy.RegisterAgent(
            DBUS_AGENT_PATH,
            "NoInputNoOutput")
        logger.debug("Agent registered.")
        self._agentmgr_proxy.RequestDefaultAgent(
            DBUS_AGENT_PATH)
        logger.debug("We are now the default agent.")

    def _on_agent_release(self):
        """Called when bluez5 has unregistered the agent.
        """
        logger.debug("Agent was unregistered. Attempting to re-register in 15 "
            "seconds...")
        self.io_loop.call_later(
            delay=15,
            callback=self._register_agent)

    def __repr__(self):
        return "<OpenPairableAdapter: {}>".format(
            self.path if self.path else "N/A")

    def __str__(self):
        return "<OpenPairableAdapter: {}>".format(
            self.path if self.path else "N/A")

    def __unicode__(self):
        return "<OpenPairableAdapter: {}>".format(
            self.path if self.path else "N/A")
