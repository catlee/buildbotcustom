#!/usr/bin/env python
"""
Runs commands from a queue!
"""
import subprocess
import time
from buildbotcustom.status.queue import QueueDir
from buildbot.util import json
import logging
log = logging.getLogger(__name__)

class CommandRunner(object):
    def __init__(self, queuedir, concurrency=1):
        self.queuedir = QueueDir('commands', queuedir)
        self.concurrency = concurrency
        self.active = []

    def run(self, cmd, item_id):
        log.info("Running %s", cmd)
        # TODO: Where to stdout/stderr go?
        p = subprocess.Popen(cmd, close_fds=True)
        self.active.append((p, item_id))

    def monitor(self):
        for p, item_id in self.active[:]:
            if p.poll() is not None:
                self.queuedir.remove(item_id)
                self.active.remove((p, item_id))

    def loop(self):
        """
        Main processing loop. Read new items from the queue and run them!
        """
        while True:
            self.monitor()
            if len(self.active) >= self.concurrency:
                # Wait!
                time.sleep(1)
                continue

            item = self.queuedir.pop()
            if not item:
                self.queuedir.wait(1000)
                continue

            item_id, fp = item
            command = json.load(fp)
            self.run(command, item_id)

def main():
    from optparse import OptionParser
    from mozillapulse.publishers import GenericPublisher
    from mozillapulse.config import PulseConfiguration
    parser = OptionParser()
    parser.set_defaults(
            concurrency=1,
            )
    parser.add_option("-q", "--queuedir", dest="queuedir")
    parser.add_option("-j", "--jobs", dest="concurrency", type="int")

    logging.basicConfig(level=logging.INFO)

    options, args = parser.parse_args()
    if not options.queuedir:
        parser.error("-q/--queuedir is required")

    runner = CommandRunner(options.queuedir, options.concurrency)
    runner.loop()

if __name__ == '__main__':
    main()
