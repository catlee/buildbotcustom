"""
Publisher for Pulse events.

Consumes new events being written into a queue directory by the PulseStatus
plugin

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
    """
    Publish buildbot events via pulse.

    `queuedir`         - a directory to look for incoming events being written
                         by a buildbot master

    `publisher`        - an instance of mozillapulse.GenericPublisher indicating where
                         these messages should be sent

    `max_idle_time`    - number of seconds since last activity after which we'll
                         disconnect. Set to None/0 to disable

    `max_connect_time` - number of seconds since we last connected after which
                         we'll disconnect. Set to None/0 to disable
    """
    def __init__(self, queuedir, publisher, max_idle_time=300, max_connect_time=600):
        self.queuedir= QueueDir(queuedir)
        self.publisher = publisher
        self.max_idle_time = max_idle_time
        self.max_connect_time = max_connect_time

        # When should we next disconnect
        self._disconnect_timer = None
        # When did we last have activity
        self._last_activity = None
        # When did we last connect
        self._last_connection = None

    def send(self, events):
        """
        Send events to pulse

        `events` - a list of buildbot event dicts
        """
        if not self._last_connection and self.max_connect_time:
            self._last_connection = time.time()
        log.debug("Sending %i messages", len(events))
        start = time.time()
        for e in events:
            msg = BuildMessage(transform_times(e))
            self.publisher.publish(msg)
        end = time.time()
        log.info("Sent %i messages in %.2fs", len(events), end-start)
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
        "Disconnect from pulse if our timer has expired"
        now = time.time()
        if self._disconnect_timer and now > self._disconnect_timer:
            log.info("Disconnecting")
            self.publisher.disconnect()
            self._disconnect_timer = None
            self._last_connection = None
            self._last_activity = None

    def loop(self):
        """
        Main processing loop. Read new items from the queue, push them to
        pulse, remove processed items, and then wait for more.
        """
        while True:
            self.maybe_disconnect()

            # Grab any new events
            item_ids = []
            events = []
            come_back_soon = False
            try:
                while True:
                    item = self.queuedir.pop()
                    if not item:
                        break
                    if len(events) > 50:
                        come_back_soon = True
                        break

                    try:
                        item_id, fp = item
                        item_ids.append(item_id)
                        log.debug("Loading %s", item)
                        events.extend(json.load(fp))
                    except:
                        log.exception("Error loading %s", item_id)
                        raise
                    finally:
                        fp.close()
                log.info("Loaded %i events", len(events))
                self.send(events)
                for item_id in item_ids:
                    log.info("Removing %s", item_id)
                    self.queuedir.remove(item_id)
            except:
                for item_id in item_ids:
                    self.queuedir.requeue(item_id)
                raise

            if come_back_soon:
                # Let's do more right now!
                log.info("Doing more!")
                continue

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

    logging.basicConfig(level=logging.INFO)

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

    pusher = PulsePusher(options.queuedir, publisher)
    pusher.loop()

if __name__ == '__main__':
    main()
