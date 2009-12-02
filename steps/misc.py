# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 1.1
#
# The contents of this file are subject to the Mozilla Public License Version
# 1.1 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
# http://www.mozilla.org/MPL/
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License
# for the specific language governing rights and limitations under the
# License.
#
# The Original Code is Mozilla-specific Buildbot steps.
#
# The Initial Developer of the Original Code is
# Mozilla Corporation.
# Portions created by the Initial Developer are Copyright (C) 2007
# the Initial Developer. All Rights Reserved.
#
# Contributor(s):
#   Ben Hearsum <bhearsum@mozilla.com>
#   Rob Campbell <rcampbell@mozilla.com>
#   Chris Cooper <ccooper@mozilla.com>
# ***** END LICENSE BLOCK *****

from twisted.python.failure import Failure, DefaultException
from twisted.internet import reactor
from twisted.spread.pb import PBConnectionLost
from twisted.python import log
from twisted.internet.defer import DeferredList, Deferred, TimeoutError

import os
import buildbot
import re

from buildbot.process.buildstep import LoggedRemoteCommand, LoggingBuildStep, \
  BuildStep
from buildbot.steps.shell import ShellCommand, WithProperties
from buildbot.status.builder import FAILURE, SUCCESS
from buildbot.clients.sendchange import Sender

def errbackAfter(wrapped_d, timeout):
    # Thanks to Dustin!
    """Calls wrapped_d's errback after timeout seconds"""
    wrapper_d = Deferred()
    already_fired = [False]
    def cb(*args, **kwargs):
        if not already_fired[0]:
            already_fired[0] = True
            wrapper_d.callback(*args, **kwargs)
        else:
            log.msg("callback called again: %s %s" % (args, kwargs))
    def eb(*args, **kwargs):
        if not already_fired[0]:
            already_fired[0] = True
            wrapper_d.errback(*args, **kwargs)
        else:
            log.msg("errback called again: %s %s" % (args, kwargs))
    def to():
        if not already_fired[0]:
            already_fired[0] = True
            wrapper_d.errback(TimeoutError("More than %i seconds elapsed" % timeout))
    reactor.callLater(timeout, to)
    wrapped_d.addCallbacks(cb, eb)
    return wrapper_d

class InterruptableDeferred(Deferred):
    def __init__(self, wrapped_d):
        Deferred.__init__(self)

        self.already_fired = False

        def callback(*args, **kwargs):
            if not self.already_fired:
                self.already_fired = True
                self.callback(*args, **kwargs)
            else:
                log.msg("callback called again: %s %s" % (args, kwargs))

        def errback(*args, **kwargs):
            if not self.already_fired:
                self.already_fired = True
                self.errback(*args, **kwargs)
            else:
                log.msg("errback called again: %s %s" % (args, kwargs))

        wrapped_d.addCallbacks(callback, errback)

    def interrupt(self, reason="Interrupted"):
        if not self.already_fired:
            self.already_fired = True
            self.errback(DefaultException(reason))

class EvaluatingShellCommand(ShellCommand):
     """Like a basic ShellCommand but can pass in a custom eval_fn to determine if the
        results of the ShellCommand were successful or not
     """
     def __init__(self, eval_fn=None, **kwargs):
         self.super_class = ShellCommand
         self.super_class.__init__(self, **kwargs)
         self.addFactoryArguments(eval_fn=eval_fn)
         self.eval_fn = eval_fn

     def evaluateCommand(self, cmd):
         if self.eval_fn:
             return self.eval_fn(self, cmd)
         else:
             return self.super_class.evaluateCommand(cmd)


class CreateDir(ShellCommand):
    name = "create dir"
    haltOnFailure = False
    warnOnFailure = True

    def __init__(self, platform, dir=None, **kwargs):
        ShellCommand.__init__(self, **kwargs)
        self.addFactoryArguments(platform=platform, dir=dir)
        self.platform = platform
        if dir:
            self.dir = dir
        else:
            if self.platform.startswith('win'):
                self.command = r'if not exist ' + self.dir + r' mkdir ' + \
                               self.dir
            else:
                self.command = ['mkdir', '-p', self.dir]

class TinderboxShellCommand(ShellCommand):
    haltOnFailure = False
    
    """This step is really just a 'do not care' buildstep for executing a
       slave command and ignoring the results. If ignoreCodes is passed,
       only exit codes listed in it will be ignored. If ignoreCodes is not
       passed, all exit codes will be ignored.
    """
    def __init__(self, ignoreCodes=None, **kwargs):
       ShellCommand.__init__(self, **kwargs)
       self.addFactoryArguments(ignoreCodes=ignoreCodes)
       self.ignoreCodes = ignoreCodes
    
    def evaluateCommand(self, cmd):
       # Ignore all return codes
       if not self.ignoreCodes:
          return SUCCESS
       else:
          # Ignore any of the return codes we're told to
          if cmd.rc in self.ignoreCodes:
             return SUCCESS
          # If the return code is something else, fail
          else:
             return FAILURE

class GetHgRevision(ShellCommand):
    """Retrieves the revision from a Mercurial repository. Builds based on
    comm-central use this to query the revision from mozilla-central which is
    pulled in via client.py, so the revision of the platform can be displayed
    in addition to the comm-central revision we get through got_revision.
    """
    name = "get hg revision"
    command = ["hg", "identify", "-i"]

    def commandComplete(self, cmd):
        rev = ""
        try:
            rev = cmd.logs['stdio'].getText().strip().rstrip()
            # Locally modified ?
            mod = rev.find('+')
            if mod != -1:
                rev = rev[:mod]
                self.setProperty('hg_modified', True)
            self.setProperty('hg_revision', rev)
        except:
            log.msg("Could not find hg revision")
            log.msg("Output: %s" % rev)
            return FAILURE
        return SUCCESS

class GetBuildID(ShellCommand):
    """Retrieves the BuildID from a Mozilla tree (using platform.ini) and sets
    it as a build property ('buildid'). If defined, uses objdir as it's base.
    """
    description=['getting buildid']
    descriptionDone=['get buildid']
    haltOnFailure=True

    def __init__(self, objdir="", inifile="application.ini", section="App",
            **kwargs):
        ShellCommand.__init__(self, **kwargs)
        self.addFactoryArguments(objdir=objdir,
                                 inifile=inifile,
                                 section=section)

        self.objdir = objdir
        self.command = ['python', 'config/printconfigsetting.py',
                        '%s/dist/bin/%s' % (self.objdir, inifile),
                        section, 'BuildID']

    def commandComplete(self, cmd):
        buildid = ""
        try:
            buildid = cmd.logs['stdio'].getText().strip().rstrip()
            self.setProperty('buildid', buildid)
        except:
            log.msg("Could not find BuildID or BuildID invalid")
            log.msg("Found: %s" % buildid)
            return FAILURE
        return SUCCESS


class SetMozillaBuildProperties(LoggingBuildStep):
    """Gathers and sets build properties for the following data:
      buildid - BuildID of the build (from application.ini, falling back on
       platform.ini)
      appVersion - The version of the application (from application.ini, falling
       back on platform.ini)
      packageFilename - The filename of the application package
      packageSize - The size (in bytes) of the application package
      packageHash - The sha1 hash of the application package
      installerFilename - The filename of the installer (win32 only)
      installerSize - The size (in bytes) of the installer (win32 only)
      installerHash - The sha1 hash of the installer (win32 only)
      completeMarFilename - The filename of the complete update
      completeMarSize - The size (in bytes) of the complete update
      completeMarHash - The sha1 hash of the complete update

      All of these will be set as build properties -- even if no data is found
      for them. When no data is found, the value of the property will be None.

      This function requires an argument of 'objdir', which is the path to the
      objdir relative to the builddir. ie, 'mozilla/fx-objdir'.
    """

    def __init__(self, objdir="", **kwargs):
        LoggingBuildStep.__init__(self, **kwargs)
        self.addFactoryArguments(objdir=objdir)
        self.objdir = objdir

    def describe(self, done=False):
        if done:
            return ["gather", "build", "properties"]
        else:
            return ["gathering", "build", "properties"]

    def start(self):
        args = {'objdir': self.objdir, 'timeout': 60}
        cmd = LoggedRemoteCommand("setMozillaBuildProperties", args)
        self.startCommand(cmd)

    def evaluateCommand(self, cmd):
        # set all of the data as build properties
        # some of this may come in with the value 'UNKNOWN' - these will still
        # be set as build properties but 'UNKNOWN' will be substituted with None
        try:
            log = cmd.logs['stdio'].getText()
            for property in log.split("\n"):
                name, value = property.split(": ")
                if value == "UNKNOWN":
                    value = None
                self.setProperty(name, value)
        except:
            return FAILURE
        return SUCCESS

class SendChangeStep(BuildStep):
    warnOnFailure = True
    def __init__(self, master, branch, files, revision=None, user=None,
            comments="", timeout=60, retries=5, **kwargs):
        BuildStep.__init__(self, **kwargs)
        self.addFactoryArguments(master=master, branch=branch, files=files,
                revision=revision, user=user, comments=comments, timeout=timeout)
        self.master = master
        self.branch = branch
        self.files = files
        self.revision = revision
        self.user = user
        self.comments = comments
        self.timeout = timeout
        self.retries = retries

        self.name = 'sendchange'
        self.warnings = None

        self.sender = Sender(master)
        self.sleepTime = 5

        self._interrupt = None

    def start(self):
        master = self.master
        try:
            properties = self.build.getProperties()
            self.branch = properties.render(self.branch)
            self.revision = properties.render(self.revision)
            self.comments = properties.render(self.comments)
            self.files = properties.render(self.files)
            self.user = properties.render(self.user)

            self.addCompleteLog("sendchange", """\
    master: %s
    branch: %s
    revision: %s
    comments: %s
    user: %s
    files: %s""" % (self.master, self.branch, self.revision, self.comments, self.user, self.files))
            return self.sendChange()
        except KeyError:
            return self.finished(Failure())

    def sendChange(self):
        d = self.sender.send(self.branch, self.revision, self.comments, self.files, self.user)
        if self.timeout:
            d = errbackAfter(d, self.timeout)
        d = InterruptableDeferred(d)
        self._interrupt = d.interrupt
        d.addCallback(self.sendChangeSuccess)
        d.addErrback(self.sendChangeFailed)
        return d

    def sendChangeFailed(self, res):
        # Go to sleep and then try again!
        try:
            if self.retries > 0:
                if not self.warnings:
                    self.warnings = self.addLog('warnings')
                self.warnings.addStderr('\nWarning: error when trying to sendchange to %s, trying again in %i seconds (%i tries left)\n' % \
                        (self.master, self.sleepTime, self.retries))
                log.msg("Error when trying to sendchange to %s: %s.  %i tries left.  Trying again in %i seconds" % (self.master, str(res), self.retries, self.sleepTime))
                self.retries -= 1
                self.sleepTime *= 2
                d = Deferred()
                d.addCallback(lambda res: self.sendChange())
                d.addErrback(lambda res: log.msg("delayed call interrupted"))
                delayed = reactor.callLater(self.sleepTime, d.callback, None)
                def interrupt(reason):
                    delayed.cancel()
                    self.retries = 0
                    return self.sendChangeFailed(reason)
                self._interrupt = interrupt
                return d

            self.step_status.setText(['sendchange to', self.master, 'failed'])
            if self.warnOnFailure:
                self.step_status.setText2(['sendchange'])
            self.addCompleteLog("errors", str(res))
            return BuildStep.finished(self, FAILURE)
        except:
            log.msg("Error processing sendchange failure")
            log.err()
            return BuildStep.finished(self, FAILURE)

    def sendChangeSuccess(self, results):
        self.step_status.setText(['sendchange to', self.master, 'ok'])
        return BuildStep.finished(self, SUCCESS)

    def interrupt(self, reason):
        self.retries = 0
        if self._interrupt:
            self._interrupt("Cancelled sendchange")
            self._interrupt = None

class DownloadFile(ShellCommand):
    haltOnFailure = True
    name = "download"
    
    def __init__(self, url_fn, url_property=None, filename_property=None,
            ignore_certs=False, **kwargs):
        self.url_fn = url_fn
        self.url_property = url_property
        self.filename_property = filename_property
        self.ignore_certs = ignore_certs
        ShellCommand.__init__(self, **kwargs)
        self.addFactoryArguments(url_fn=url_fn, url_property=url_property,
                filename_property=filename_property, ignore_certs=ignore_certs)
        self._url = None

    def setBuild(self, build):
        ShellCommand.setBuild(self, build)
        self._url = self.url_fn(build)
        if self.url_property:
            self.setProperty(self.url_property, self._url)
        if self.filename_property:
            self.setProperty(self.filename_property, os.path.basename(self._url))

    def start(self):
        if self.ignore_certs:
            self.setCommand(["wget", "-nv", "-N", "--no-check-certificate", self._url])
        else:
            self.setCommand(["wget", "-nv", "-N", self._url])
        ShellCommand.start(self)
    
    def evaluateCommand(self, cmd):
        superResult = ShellCommand.evaluateCommand(self, cmd)
        if SUCCESS != superResult:
            return FAILURE
        if None != re.search('ERROR', cmd.logs['stdio'].getText()):
            return FAILURE
        return SUCCESS

class UnpackFile(ShellCommand):
    def __init__(self, filename, scripts_dir=".", **kwargs):
        self.filename = filename
        self.scripts_dir = scripts_dir
        ShellCommand.__init__(self, **kwargs)
        self.addFactoryArguments(filename=filename, scripts_dir=scripts_dir)

    def start(self):
        filename = self.build.getProperties().render(self.filename)
        self.filename = filename
        if filename.endswith(".zip"):
            self.setCommand(['unzip', '-o', filename])
        elif filename.endswith(".tar.gz"):
            self.setCommand(['tar', '-zxvf', filename])
        elif filename.endswith(".tar.bz2"):
            self.setCommand(['tar', '-jxvf', filename])
        elif filename.endswith(".dmg"):
            self.setCommand(['bash',
             '%s/installdmg.sh' % self.scripts_dir,
             filename]
            )
        else:
            raise ValueError("Don't know how to handle %s" % filename)
        ShellCommand.start(self)

    def evaluateCommand(self, cmd):
        superResult = ShellCommand.evaluateCommand(self, cmd)
        if superResult != SUCCESS:
            return superResult

        if self.filename.endswith(".zip"):
            if None != re.search('ERROR', cmd.logs['stdio'].getText()):
                return FAILURE
        if None != re.search('^Usage:', cmd.logs['stdio'].getText()):
            return FAILURE

        return SUCCESS

class FindFile(ShellCommand):
    def __init__(self, filename, directory, max_depth, property_name, filetype=None, **kwargs):
        ShellCommand.__init__(self, **kwargs)

        self.addFactoryArguments(filename=filename, directory=directory,
                max_depth=max_depth, property_name=property_name,
                filetype=filetype)

        self.property_name = property_name

        if filetype == "file":
            filetype = "-type f"
        elif filetype == "dir":
            filetype = "-type d"
        else:
            filetype = ""

        self.setCommand(['bash', '-c', 'find %(directory)s -maxdepth %(max_depth)s %(filetype)s -name %(filename)s' % locals()])

    def evaluateCommand(self, cmd):
        try:
            output = cmd.logs['stdio'].getText().strip()
            if output:
                self.setProperty(self.property_name, output)
                return SUCCESS
        except:
            pass
        return FAILURE

class MozillaClobberer(ShellCommand):
    flunkOnFailure = False
    description=['checking', 'clobber', 'times']

    def __init__(self, branch, clobber_url, clobberer_path, clobberTime=None,
                 timeout=3600, workdir='.', **kwargs):
        command = ['python', clobberer_path, '-s', 'tools']
        if clobberTime:
            command.extend(['-t', str(clobberTime)])
        command.extend([clobber_url, branch, WithProperties("%(buildername)s"),
                        WithProperties("%(slavename)s")])

        ShellCommand.__init__(self, command=command, timeout=timeout,
                              workdir=workdir, **kwargs)
        self.addFactoryArguments(branch=branch, clobber_url=clobber_url,
                                 clobberer_path=clobberer_path,
                                 clobberTime=clobberTime)

    def createSummary(self, log):

        # Server is forcing a clobber
        forcedClobberRe = re.compile('Server is forcing a clobber')
        # We are looking for something like :
        #  More than 7 days, 0:00:00 have passed since our last clobber
        periodicClobberRe = re.compile('More than \d+ days, [0-9:]+ have passed since our last clobber')

        # We don't have clobber data.  This usually means we've been purged before
        purgedClobberRe = re.compile("Our last clobber date:.*None")

        self.setProperty('forced_clobber', False, 'MozillaClobberer')
        self.setProperty('periodic_clobber', False, 'MozillaClobberer')
        self.setProperty('purged_clobber', False, 'MozillaClobberer')

        clobberType = None
        for line in log.readlines():
            if forcedClobberRe.search(line):
                self.setProperty('forced_clobber', True, 'MozillaClobberer')
                clobberType = "forced"
            elif periodicClobberRe.search(line):
                self.setProperty('periodic_clobber', True, 'MozillaClobberer')
                clobberType = "periodic"
            elif purgedClobberRe.search(line):
                self.setProperty('purged_clobber', True, 'MozillaClobberer')
                clobberType = "free-space"

        if clobberType != None:
            summary = "TinderboxPrint: %s clobber" % clobberType
            self.addCompleteLog('clobberer', summary)

class SetBuildProperty(BuildStep):
    name = "set build property"
    def __init__(self, property_name, value, **kwargs):
        self.property_name = property_name
        self.value = value

        BuildStep.__init__(self, **kwargs)

        self.addFactoryArguments(property_name=property_name, value=value)

    def start(self):
        if callable(self.value):
            value = self.value(self.build)
        else:
            value = self.value
        self.setProperty(self.property_name, value)
        self.step_status.setText(['set props:', self.property_name])
        self.addCompleteLog("property changes", "%s: %s" % (self.property_name, value))
        return self.finished(SUCCESS)

class OutputStep(BuildStep):
    """Simply logs some output"""
    name = "output"
    def __init__(self, data, log='output', **kwargs):
        self.data = data
        self.log = log

        BuildStep.__init__(self, **kwargs)

        self.addFactoryArguments(data=data, log=log)

    def start(self):
        properties = self.build.getProperties()
        if callable(self.data):
            data = properties.render(self.data(self.build))
        else:
            data = properties.render(self.data)
        if not isinstance(data, (str, unicode)):
            try:
                data = " ".join(data)
            except:
                data = str(data)
        self.addCompleteLog(self.log, data)
        self.step_status.setText([self.name])
        return self.finished(SUCCESS)

class DisconnectStep(ShellCommand):
    """This step is used when a command is expected to cause the slave to
    disconnect from the master.  It will handle connection lost errors as
    expected.

    Optionally it will also forcibly disconnect the slave from the master by
    calling the remote 'shutdown' command, in effect doing a graceful
    shutdown.  If force_disconnect is True, then the slave will always be
    disconnected after the command completes.  If force_disconnect is a
    function, it will be called with the command object, and the return value
    will be used to determine if the slave should be disconnected."""
    name = "disconnect"
    def __init__(self, force_disconnect=None, **kwargs):
        self.force_disconnect = force_disconnect
        ShellCommand.__init__(self, **kwargs)
        self.addFactoryArguments(force_disconnect=force_disconnect)

        self._disconnected = False

    def interrupt(self, reason):
        # Called when the slave command is interrupted, e.g. by rebooting
        # We assume this is expected
        self._disconnected = True
        return self.finished(SUCCESS)

    def checkDisconnect(self, f):
        # This is called if there's a problem executing the command because the connection was disconnected.
        # Again, we assume this is the expected behaviour
        f.trap(PBConnectionLost)
        self._disconnected = True
        return self.finished(SUCCESS)

    def commandComplete(self, cmd):
        # The command has completed normally.  If force_disconnect is set, then
        # tell the slave to shutdown
        if self.force_disconnect:
            if not callable(self.force_disconnect) or self.force_disconnect(cmd):
                try:
                    d = self.remote.callRemote('shutdown')
                    d.addErrback(self._disconnected_cb)
                    d.addCallback(self._disconnected_cb)
                    return d
                except:
                    log.err()

    def _disconnected_cb(self, res):
        # Successfully disconnected
        self._disconnected = True
        return True

    def finished(self, res):
        if self._disconnected:
            self.step_status.setText(self.describe(True) + ["slave", "lost"])
            self.step_status.setText2(['slave', 'lost'])
        return ShellCommand.finished(self, res)
