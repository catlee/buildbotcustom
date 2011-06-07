"""
see http://hg.mozilla.org/users/clegnitto_mozilla.com/mozillapulse/ for pulse
code
"""
import time
from datetime import tzinfo, timedelta, datetime

from mozillapulse.messages.build import BuildMessage
from buildbotcustom.status.queue import QueueDir
from buildbot.util import json

import logging
log = logging.getLogger(__name__)

ZERO = timedelta(0)
HOUR = timedelta(hours=1)

# A UTC class.

class UTC(tzinfo):
    """UTC"""

    def utcoffset(self, dt):
        return ZERO

    def tzname(self, dt):
        return "UTC"

    def dst(self, dt):
        return ZERO

def transform_time(t):
    """Transform an epoch time to a string representation of the form
    YYYY-mm-ddTHH:MM:SS+0000"""
    if t is None:
        return None
    elif isinstance(t, basestring):
        return t

    dt = datetime.fromtimestamp(t, UTC())
    return dt.strftime('%Y-%m-%dT%H:%M:%S%z')

def transform_times(event):
    """Replace epoch times in event with string representations of the time"""
    if isinstance(event, dict):
        retval = {}
        for key, value in event.items():
            if key == 'times' and len(value) == 2:
                retval[key] = [transform_time(t) for t in value]
            else:
                retval[key] = transform_times(value)
    else:
        retval = event
    return retval

class PulsePusher(object):
    def __init__(self, queuedir, publisher, max_idle_time=300, max_connect_time=600):
        self.queuedir= QueueDir(queuedir)
        self.publisher = publisher
        self.max_idle_time = max_idle_time
        self.max_connect_time = max_connect_time

        self._disconnect_timer = None
        self._last_activity = None
        self._last_connection = None

    def send(self, events):
        if not self._last_connection and self.max_connect_time:
            self._last_connection = time.time()
        log.debug("Sending %i messages", len(events))
        start = time.time()
        for e in events:
            msg = BuildMessage(transform_times(e))
            self.publisher.publish(msg)
        end = time.time()
        log.debug("Sent %i messages in %.2fs", len(events), end-start)
        self._last_activity = time.time()

        # Update our timers
        t = 0
        if self.max_connect_time:
            t = self._last_connection + self.max_connect_time
        if self.max_idle_time:
            if t:
                t = min(t, self._last_activity + self.max_idle_time)
            else:
                t = self._last_activity + self.max_idle_time
        if t:
            self._disconnect_timer = t

    def maybe_disconnect(self):
        now = time.time()
        if self._disconnect_timer and now > self._disconnect_timer:
            log.info("Disconnecting")
            self.publisher.disconnect()
            self._disconnect_timer = None
            self._last_connection = None
            self._last_activity = None

    def loop(self):
        while True:
            # possibly disconnect
            self.maybe_disconnect()

            # Grab any new events
            while True:
                item = self.queuedir.pop()
                if not item:
                    break
                item_id, fp = item
                log.debug("Got %s", item)
                try:
                    events = json.load(fp)
                    self.send(events)
                    log.info("Removing %s", item_id)
                    self.queuedir.remove(item_id)
                except:
                    self.queuedir.requeue(item_id)
                    log.exception("Error loading %s", item_id)
                    raise
                finally:
                    fp.close()

            # Wait for more
            # don't wait more than our max_idle/max_connect_time
            now = time.time()
            to_wait = None
            if self._disconnect_timer:
                to_wait = self._disconnect_timer - now
                # Convert to ms
                to_wait *= 1000
                if to_wait < 0:
                    to_wait = None
            log.info("Waiting for %s", to_wait)
            self.queuedir.wait(to_wait)

def main():
    from optparse import OptionParser
    from mozillapulse.publishers import GenericPublisher
    from mozillapulse.config import PulseConfiguration
    parser = OptionParser()
    parser.add_option("--passwords", dest="passwords")
    parser.add_option("-q", "--queuedir", dest="queuedir")

    logging.basicConfig(level=logging.DEBUG)

    options, args = parser.parse_args()
    if not options.passwords:
        parser.error("--passwords is required")
    if not options.queuedir:
        parser.error("-q/--queuedir is required")

    passwords = {}
    execfile(options.passwords, passwords, passwords)

    publisher = GenericPublisher(
            PulseConfiguration(
                user=passwords['PULSE_USERNAME'],
                password=passwords['PULSE_PASSWORD'],
            ),
            exchange=passwords['PULSE_EXCHANGE'])

    pusher = PulsePusher(options.queuedir, publisher, max_connect_time=30, max_idle_time=15)
    pusher.loop()

if __name__ == '__main__':
    main()
