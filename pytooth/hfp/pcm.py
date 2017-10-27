
from functools import partial
import logging

from tornado.gen import coroutine, sleep
from tornado.ioloop import IOLoop
from tornado.iostream import IOStream

logger = logging.getLogger(__name__)


class PCMDecoder:
    """An asynchronous PCM decoder class for the default HFP codec audio.
    This doesn't do any decoding per-se, it simply passes the raw samples
    up the chain.
    """

    def __init__(self):
        self._ioloop = IOLoop.current()
        self._started = False

        self.on_data_ready = None
        self.on_fatal_error = None
        self.on_pcm_format_ready = None

    def start(self, socket, read_mtu):
        """Starts the decoder. If already started, this does nothing.
        """
        if self._started:
            return

        # setup
        self._read_mtu = read_mtu
        self._socket = socket
        self._stream = IOStream(socket=socket)
        self._ioloop.add_callback(self._do_decode)
        self._started = True

    def stop(self):
        """Stops the decoder. If already stopped, this does nothing.
        """
        if not self._started:
            return

        self._started = False
        self._stream = None
        self._socket = None
        self._read_mtu = None

    @property
    def channels(self):
        return 1
    
    @property
    def channel_mode(self):
        return "Mono"

    @property
    def sample_rate(self):
        return 8000

    @property
    def sample_size(self):
        return 16

    @coroutine
    def _do_decode(self):
        """Runs the decoder in a try/catch just in case something goes wrong.
        """
        try:
            yield self._worker_proc()
        except Exception as e:
            logger.exception("Unhandled decode error.")
            self.stop()
            if self.on_fatal_error:
                self.on_fatal_error(error=e)

    @coroutine
    def _worker_proc(self):
        """Does the passing-through of PCM samples. Runs in an infinite but
        asynchronous-style loop until stopped.
        """

        # calculate sleep delay so we don't wait too long for more samples
        # => 8000 samples/src * 2-byte samples = 16000 bytes/sec
        # => 16000 bytes / MTU = X transmissions/sec
        # => 1000 msec / X = Y msec/transmission
        delay = int(1000 / (16000 / self._read_mtu))

        logger.debug("PCM decoder thread has started, delay = {}".format(
            delay))

        # initialise
        if self.on_pcm_format_ready:
            self.on_pcm_format_ready()

        # loop until stopped
        while self._started:

            # read more PCM data in non-blocking mode
            data = b''
            try:
                data = yield self._stream.read_bytes(
                    num_bytes=self._read_mtu,
                    partial=False)
            except Exception as e:
                logger.error("Socket read error - {}".format(e))
            if len(data) == 0:
                yield sleep(delay) # CPU busy safety
                continue

            # hand over data
            if self.on_data_ready:
                self.on_data_ready(
                    data=data)

        logger.debug("PCM decoder has gracefully stopped.")
