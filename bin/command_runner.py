#!/usr/bin/env python
"""
Runs commands from a queue!
"""
import subprocess, os, signal
import time
from buildbotcustom.status.queue import QueueDir
from buildbot.util import json
import logging
log = logging.getLogger(__name__)

class Job(object):
    max_time = 30
    def __init__(self, cmd, item_id, log_fp):
        self.cmd = cmd
        self.log = log_fp
        self.item_id = item_id
        self.started = None
        self.last_signal_time = 0
        self.last_signal = None

        self.proc = None

    def start(self):
        devnull = open(os.devnull, 'r')
        self.log.write("Running %s\n" % self.cmd)
        self.log.flush()
        self.proc = subprocess.Popen(self.cmd, close_fds=True, stdin=devnull, stdout=self.log, stderr=self.log)
        self.started = time.time()

    def check(self):
        now = time.time()
        if now - self.started > self.max_time:
            log.info("Killit!")
            # Kill stuff off
            if now - self.last_signal_time > 60:
                log.info("Kill it now!")
                s = {None: signal.SIGINT, signal.SIGINT: signal.SIGTERM}.get(self.last_signal, signal.SIGKILL)
                try:
                    self.log.write("Killing with %s\n" % s)
                    os.kill(self.proc.pid, s)
                    self.last_signal = s
                    self.last_signal_time = now
                except OSError:
                    # Ok, process must have exited already
                    log.exception("Failed to kill")
                    pass

        result = self.proc.poll()
        if result is not None:
            self.log.write("\nResult: %s\n" % result)
            self.log.close()
        return result

class CommandRunner(object):
    # TODO: Make these configurable
    max_retries = 5
    retry_time = 60

    def __init__(self, queuedir, concurrency=1):
        self.queuedir = queuedir
        self.q = QueueDir('commands', queuedir)
        self.concurrency = concurrency

        self.active = []

        # List of (signal_time, level, proc)
        self.to_kill = []

    def run(self, cmd, item_id):
        log.info("Running %s", cmd)
        output = self.q.getlog(item_id)
        try:
            j = Job(cmd, item_id, output)
            j.start()
            self.active.append(j)
        except OSError:
            output.write("\nFailed with OSError; requeuing in %i seconds\n" % self.retry_time)
            # Wait to requeue it
            # If we die, then it's still in cur, and will be moved back into 'new' eventually
            self.q.requeue(item_id, self.retry_time, self.max_retries)

    def killjobs(self):
        now = time.time()
        for sigtime, level, p in self.to_kill:
            if now > sigtime:
                s = {0: signal.SIGINT, 1: signal.SIGTERM}.get(level, signal.SIGKILL)
                try:
                    os.killpg(p.pid, s)
                except OSError:
                    pass

    def monitor(self):
        for job in self.active[:]:
            self.q.touch(job.item_id)
            result = job.check()

            if result is not None:
                self.active.remove(job)
                if result == 0:
                    self.q.remove(job.item_id)
                else:
                    log.warn("%s failed; requeuing", job.item_id)
                    # Requeue it!
                    self.q.requeue(job.item_id, self.retry_time, self.max_retries)

    def loop(self):
        """
        Main processing loop. Read new items from the queue and run them!
        """
        while True:
            self.monitor()
            self.killjobs()
            if len(self.active) >= self.concurrency:
                # Wait!
                time.sleep(1)
                continue

            while len(self.active) < self.concurrency:
                item = self.q.pop()
                if not item:
                    if self.active:
                        self.q.wait(1)
                    else:
                        self.q.wait(60)
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
