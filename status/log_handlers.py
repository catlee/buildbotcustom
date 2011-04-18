import os
import subprocess
import tempfile

from twisted.python import log as twlog
from twisted.python import failure
from twisted.internet import defer, reactor

from buildbot.status import base

class ThreadedLogHandler(base.StatusReceiverMultiService):
    # TODO: 'size' isn't needed, but due to
    # http://trac.buildbot.net/ticket/1791 we can't change compare_attrs on a
    # running master.
    compare_attrs = ['categories', 'builders', 'size']
    def __init__(self, categories=None, builders=None):
        base.StatusReceiverMultiService.__init__(self)

        self.categories = categories
        self.builders = builders

        # you should either limit on builders or categories, not both
        if self.builders != None and self.categories != None:
            twlog.err("Please specify only builders to ignore or categories to include")
            raise ValueError("Please specify only builders or categories")

        self.watched = []

        self.size = None # Unused, see TODO above

    def setServiceParent(self, parent):
        base.StatusReceiverMultiService.setServiceParent(self, parent)
        self.setup()

    def setup(self):
        self.master_status = self.parent.getStatus()
        self.master_status.subscribe(self)

    def disownServiceParent(self):
        self.master_status.unsubscribe(self)
        for w in self.watched:
            w.unsubscribe(self)
        return base.StatusReceiverMultiService.disownServiceParent(self)

    def stopService(self):
        base.StatusReceiverMultiService.stopService(self)

    def builderAdded(self, name, builder):
        # only subscribe to builders we are interested in
        if self.categories != None and builder.category not in self.categories:
            return None

        self.watched.append(builder)
        return self # subscribe to this builder

    def buildStarted(self, builderName, build):
        pass

    def buildFinished(self, builderName, build, results):
        builder = build.getBuilder()
        if self.builders is not None and builderName not in self.builders:
            return # ignore this build
        if self.categories is not None and \
               builder.category not in self.categories:
            return # ignore this build

        reactor.callInThread(self.handleLogs, builder, build, results)

    def handleLogs(self, builder, build, results):
        pass

class SubprocessLogHandler(ThreadedLogHandler):
    compare_attrs = ['command', 'categories', 'builders', 'size']
    def __init__(self, command, categories=None, builders=None):
        ThreadedLogHandler.__init__(self, categories, builders)
        self.command = command

    def handleLogs(self, builder, build, results):
        if isinstance(self.command, str):
            cmd = [self.command]
        else:
            cmd = self.command[:]
        cmd.extend([
               os.path.join(self.master_status.basedir, builder.basedir),
               str(build.number)])

        properties = build.getProperties()
        cmd = properties.render(cmd)
        output = tempfile.TemporaryFile()

        try:
            twlog.msg("Running %s" % cmd)
            subprocess.check_call(cmd, stdout=output, stderr=subprocess.STDOUT)
            output.seek(0)
            twlog.msg("Log output: %s" % output.read())
        except:
            twlog.msg("Error running %s" % cmd)
            output.seek(0)
            twlog.msg("Log output: %s" % output.read())
            twlog.err()
