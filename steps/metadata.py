# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from buildbot.steps.transfer import _TransferBuildStep, StatusRemoteCommand
from buildbot.process.buildstep import SUCCESS, FAILURE, BuildStep
from buildbot.util import json

from twisted.spread import pb
from twisted.python import log
from twisted.python.failure import Failure

import tempfile


class _Writer(pb.Referenceable):
    "Helper class that writes to a file object"
    def __init__(self, fileobj, maxsize):
        self.fp = fileobj
        self.remaining = maxsize

    def remote_write(self, data):
        "Called from remote slave to write data to fp, subject to maxsize"
        if self.remaining is not None:
            if len(data) > self.remaining:
                data = data[:self.remaining]
            self.fp.write(data)
            self.remaining = self.remaining - len(data)
        else:
            self.fp.write(data)

    def remote_close(self):
        "Called by remote slave to state that no more data will be transfered"
        self.fp.flush()


class GetInstanceMetadata(_TransferBuildStep):
    """
    Get instance metadata from the slave, and set it as properties on the build
    """
    name = 'get_instance_metadata'
    maxsize = 1024
    blocksize = 1024
    workdir = "build"
    flunkOnFailure = False

    def __init__(self, metadata_path=None, **kwargs):
        _TransferBuildStep.__init__(self, **kwargs)
        self.addFactoryArguments(metadata_path=metadata_path)
        self.metadata_path = metadata_path
        self.fp = None

    def guess_path(self):
        return "/etc/instance_metadata.json"

    def start(self):
        # Create a temporary file we can write to
        self.fp = tempfile.TemporaryFile()

        # Where on the slave should we read from?
        srcpath = self.metadata_path or self.guess_path()
        log.msg("reading %s to %s" % (srcpath, self.fp))

        # Create write object that will receive remote_write calls from the
        # slave
        writer = _Writer(self.fp, self.maxsize)
        args = {
            'slavesrc': srcpath,
            'workdir': self._getWorkdir(),
            'writer': writer,
            'maxsize': self.maxsize,
            'blocksize': self.blocksize,
        }

        # Start the upload!
        self.cmd = StatusRemoteCommand('uploadFile', args)
        d = self.runCommand(self.cmd)
        d.addCallback(self.finishedUpload).addErrback(self.failed)

    def finishedUpload(self, result):
        # Called when the upload is finished
        # If we've finished with some non-zero result (failure!), call our base
        # class's finished() method which will add error logging
        if result.rc != 0:
            return self.finished(result)

        try:
            # Read the data, and set properties
            self.fp.seek(0)
            metadata = json.load(self.fp)
            self.addCompleteLog('output', json.dumps(metadata, indent=2))
            for k, v in metadata.items():
                self.build.setProperty(k, v, "get_instance_metadata")
        except ValueError:
            # Couldn't decode the json
            log.msg("error decoding json")
            self.addCompleteLog("errors", str(Failure()))
            return BuildStep.finished(self, FAILURE)
        finally:
            # Let our temporary file be garbage collected
            self.fp = None
        return BuildStep.finished(self, SUCCESS)

    def interrupt(self, reason):
        BuildStep.interrupt(self, reason)
        if self.cmd:
            d = self.cmd.interrupt(reason)
            return d
