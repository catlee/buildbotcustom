#!/usr/bin/env python
"""
Runs commands from a queue!
"""
import subprocess, os
import time
from buildbotcustom.status.queue import QueueDir
from buildbot.util import json
import logging
log = logging.getLogger(__name__)

class CommandRunner(object):
    # TODO: Configure these
    max_retries = 5
    retry_time = 60

    def __init__(self, queuedir, concurrency=1):
        self.queuedir = queuedir
        self.q = QueueDir('commands', queuedir)
        self.concurrency = concurrency
        self.active = []

        # List of (requeue_time, item_id)
        self.to_requeue = []

    def run(self, cmd, item_id):
        if self.q.getcount(item_id) > self.max_retries:
            log.info("Giving up on %s", item_id)
            self.q.murder(item_id)
            return

        log.info("Running %s", cmd)
        output = self.q.getlog(item_id)
        output.write("Running %s\n" % cmd)
        output.flush()
        devnull = open(os.devnull, 'r')
        try:
            p = subprocess.Popen(cmd, close_fds=True, stdin=devnull, stdout=output, stderr=output)
            self.active.append((p, item_id, output))
        except OSError:
            output.write("\nFailed with OSError; requeuing in %i seconds\n", self.retry_time)
            # Wait to requeue it
            # If we die, then it's still in cur, and will be moved back into 'new' eventually
            self.to_requeue.append( (time.time() + self.retry_time, item_id) )

    def requeue(self):
        now = time.time()
        for t, item_id in self.to_requeue:
            if now > t:
                log.info("Requeuing %s", item_id)
                self.q.requeue(item_id)
                self.to_requeue.remove( (t, item_id) )
            else:
                self.q.touch(item_id)

    def monitor(self):
        # TODO: Impose a maximum time on jobs
        for p, item_id, output in self.active[:]:
            self.q.touch(item_id)
            result = p.poll()
            if result is not None:
                output.write("\nResult: %s\n" % result)
                output.close()
                self.active.remove((p, item_id, output))
                if result == 0:
                    self.q.remove(item_id)
                else:
                    log.warn("%s failed; requeuing", item_id)
                    # Requeue it!
                    self.q.requeue(item_id)

    def loop(self):
        """
        Main processing loop. Read new items from the queue and run them!
        """
        while True:
            self.requeue()
            self.monitor()
            if len(self.active) >= self.concurrency:
                # Wait!
                time.sleep(1)
                continue

            while len(self.active) < self.concurrency:
                item = self.q.pop()
                if not item:
                    self.q.wait(1000)
                    break

                item_id, fp = item
                try:
                    command = json.load(fp)
                    self.run(command, item_id)
                except ValueError:
                    # Couldn't parse it as json
                    # There's no hope!
                    self.q.log(item_id, "Couldn't load json; murdering")
                    self.q.murder(item_id)
                finally:
                    fp.close()

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

    # TODO: Log to another file? Use RotatingFileHandler?
    logging.basicConfig(level=logging.INFO)

    options, args = parser.parse_args()
    if not options.queuedir:
        parser.error("-q/--queuedir is required")

    runner = CommandRunner(options.queuedir, options.concurrency)
    runner.loop()

if __name__ == '__main__':
    main()
