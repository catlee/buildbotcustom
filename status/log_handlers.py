import os
import subprocess
import tempfile

from twisted.python import log as twlog
from twisted.python import failure
from twisted.internet import defer, reactor

from buildbot.status import base
from buildbot.util import json

class QueuedCommandLogHandler(base.StatusReceiverMultiService):
    compare_attrs = ['command', 'categories', 'builders']
    def __init__(self, command, queuedir, categories=None, builders=None):
        base.StatusReceiverMultiService.__init__(self)

        self.command = command
        self.queuedir = queuedir
        self.categories = categories
        self.builders = builders

        # you should either limit on builders or categories, not both
        if self.builders != None and self.categories != None:
            twlog.err("Please specify only builders to ignore or categories to include")
            raise ValueError("Please specify only builders or categories")

        self.watched = []

    def setServiceParent(self, parent):
        base.StatusReceiverMultiService.setServiceParent(self, parent)
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

        return self.handleLogs(builder, build, results)

    def handleLogs(self, builder, build, results):
        if isinstance(self.command, str):
            cmd = [self.command]
        else:
            cmd = self.command[:]
        cmd = build.getProperties().render(cmd)
        cmd.extend([
               os.path.join(self.master_status.basedir, builder.basedir),
               str(build.number)])
        self.queuedir.add(json.dumps(cmd))
