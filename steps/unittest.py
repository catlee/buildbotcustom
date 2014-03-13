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
#   Rob Campbell <rcampbell@mozilla.com>
#   Chris Cooper <ccooper@mozilla.com>
#   Ben Hearsum <bhearsum@mozilla.com>
#   Serge Gautherie <sgautherie.bz@free.fr>
# ***** END LICENSE BLOCK *****

import re

from buildbot.steps.shell import WithProperties
from buildbot.status.builder import SUCCESS, WARNINGS, FAILURE, HEADER, worst_status

from buildbotcustom.steps.base import ShellCommand


def emphasizeFailureText(text):
    return '<em class="testfail">%s</em>' % text

# Some test suites (like TUnit) may not (yet) have the knownFailCount feature.
# Some test suites (like TUnit) may not (yet) have the crashed feature.
# Expected values for leaked: False, no leak; True, leaked; None, report
# failure.


def summaryText(passCount, failCount, knownFailCount=None,
                crashed=False, leaked=False):
    # Format the tests counts.
    if passCount < 0 or failCount < 0 or \
            (knownFailCount != None and knownFailCount < 0):
        # Explicit failure case.
        summary = emphasizeFailureText("T-FAIL")
    elif passCount == 0 and failCount == 0 and \
            (knownFailCount == None or knownFailCount == 0):
        # Implicit failure case.
        summary = emphasizeFailureText("T-FAIL")
    else:
        # Handle failCount.
        failCountStr = str(failCount)
        if failCount > 0:
            failCountStr = emphasizeFailureText(failCountStr)
        # Format the counts.
        summary = "%d/%s" % (passCount, failCountStr)
        if knownFailCount != None:
            summary += "/%d" % knownFailCount

    # Format the crash status.
    if crashed:
        summary += "&nbsp;%s" % emphasizeFailureText("CRASH")

    # Format the leak status.
    if leaked != False:
        summary += "&nbsp;%s" % emphasizeFailureText(
            (leaked and "LEAK") or "L-FAIL")

    return summary

# otherIdent can be None if the test suite does not have this feature (yet).


def summarizeLog(name, log, successIdent, failureIdent, otherIdent, infoRe):
    # Counts and flags.
    successCount = -1
    failureCount = -1
    otherCount = otherIdent and -1
    crashed = False
    leaked = False

    # Regular expression for result summary details.
    # Reuse 'infoRe'.
    infoRe = re.compile(infoRe)
    # Regular expression for crash and leak detections.
    harnessErrorsRe = re.compile(r"(?:TEST-UNEXPECTED-FAIL|PROCESS-CRASH) \| .* \| (application crashed|missing output line for total leaks!|negative leaks caught!|\d+ bytes leaked)")
    # Process the log.
    for line in log.readlines():
        # Set the counts.
        m = infoRe.match(line)
        if m:
            r = m.group(1)
            if r == successIdent:
                successCount = int(m.group(2))
            elif r == failureIdent:
                failureCount = int(m.group(2))
            # If otherIdent == None, then infoRe should not match it,
            # so this test is fine as is.
            elif r == otherIdent:
                otherCount = int(m.group(2))
            continue
        # Set the error flags.
        m = harnessErrorsRe.match(line)
        if m:
            r = m.group(1)
            if r == "application crashed":
                crashed = True
            elif r == "missing output line for total leaks!":
                leaked = None
            else:
                leaked = True
            # continue

    # Return the summary.
    return "TinderboxPrint: %s<br/>%s\n" % (name,
                                            summaryText(successCount, failureCount, otherCount, crashed, leaked))


def summarizeLogMochitest(name, log):
    infoRe = r"\d+ INFO (Passed|Failed|Todo):\ +(\d+)"
    # Support browser-chrome result summary format which differs from
    # MozillaMochitest's.
    if name == 'mochitest-browser-chrome':
        infoRe = r"\t(Passed|Failed|Todo): (\d+)"

    return summarizeLog(
        name, log, "Passed", "Failed", "Todo",
        infoRe)


def summarizeLogRemoteMochitest(name, log):
    keys = ('Passed', 'Failed', 'Todo')
    d = {}
    summary = ""
    found = False

    for s in keys:
        d[s] = '0'
    for line in log.readlines():
        if found:
            s = line.strip()
            l = s.split(': ')
            if len(l) == 2 and l[0] in keys:
                if l[0] in d:
                    d[l[0]] = l[1]
        else:
            if line.startswith('Browser Chrome Test Summary'):
                found = True
    if found:
        if 'Failed' in d and str(d['Failed']) != '0':
            d['Failed'] = emphasizeFailureText(d['Failed'])
        summary = "%(Passed)s/%(Failed)s/%(Todo)s" % d
    # Return the summary.
    return "TinderboxPrint: %s<br/>%s\n" % (name, summary)


def summarizeLogReftest(name, log):
    return summarizeLog(
        name, log, "Successful", "Unexpected", "Known problems",
        r"REFTEST INFO \| (Successful|Unexpected|Known problems): (\d+) \(")


def summarizeLogXpcshelltests(name, log):
    return summarizeLog(
        name, log, "Passed", "Failed", None,
        r"INFO \| (Passed|Failed): (\d+)")


def summarizeLogJetpacktests(name, log):
    log = log.getText()
    infoRe = re.compile(r"(\d+) of (\d+) tests passed")
    successCount = 0
    failCount = 0
    totalCount = 0
    summary = ""
    for line in log.splitlines():
        m = infoRe.match(line)
        if m:
            successCount += int(m.group(1))
            totalCount += int(m.group(2))
    failCount = int(totalCount - successCount)
    # Handle failCount.
    failCountStr = str(failCount)
    if failCount > 0:
        failCountStr = emphasizeFailureText(failCountStr)
    # Format the counts
    summary = "%d/%d" % (totalCount, failCount)
    # Return the summary.
    return "TinderboxPrint:%s<br/>%s\n" % (name, summary)


def summarizeTUnit(name, log):
    # Counts and flags.
    passCount = 0
    failCount = 0
    leaked = False

    # Regular expression for crash and leak detections.
    harnessErrorsRe = re.compile(r"(?:TEST-UNEXPECTED-FAIL|PROCESS-CRASH) \| .* \| (application crashed|missing output line for total leaks!|negative leaks caught!|\d+ bytes leaked)")
    # Process the log.
    for line in log.readlines():
        if "TEST-PASS" in line:
            passCount += 1
            continue
        if "TEST-UNEXPECTED-" in line:
            # Set the error flags.
            # Or set the failure count.
            m = harnessErrorsRe.match(line)
            if m:
                r = m.group(1)
                if r == "missing output line for total leaks!":
                    leaked = None
                else:
                    leaked = True
            else:
                failCount += 1
            # continue

    # Return the summary.
    return "TinderboxPrint: %s<br/>%s\n" % (name,
                                            summaryText(passCount, failCount, leaked=leaked))


def evaluateMochitest(name, log, superResult):
    # When a unittest fails we mark it orange, indicating with the
    # WARNINGS status. Therefore, FAILURE needs to become WARNINGS
    # However, we don't want to override EXCEPTION or RETRY, so we still
    # need to use worst_status in further status decisions.
    if superResult == FAILURE:
        superResult = WARNINGS

    if superResult != SUCCESS:
        return superResult

    failIdent = r"^\d+ INFO Failed:\s+0"
    # Support browser-chrome result summary format which differs from
    # MozillaMochitest's.
    if 'browser-chrome' in name:
        failIdent = r"^\tFailed:\s+0"
    # Assume that having the 'failIdent' line
    # means the tests run completed (successfully).
    # Also check for "^TEST-UNEXPECTED-" for harness errors.
    if not re.search(failIdent, log, re.MULTILINE) or \
            re.search("^TEST-UNEXPECTED-", log, re.MULTILINE):
        return worst_status(superResult, WARNINGS)

    return worst_status(superResult, SUCCESS)


def evaluateRemoteMochitest(name, log, superResult):
    # When a unittest fails we mark it orange, indicating with the
    # WARNINGS status. Therefore, FAILURE needs to become WARNINGS
    # However, we don't want to override EXCEPTION or RETRY, so we still
    # need to use worst_status in further status decisions.
    if superResult == FAILURE:
        superResult = WARNINGS

    if superResult != SUCCESS:
        return superResult

    failIdent = r"^\d+ INFO Failed:\s+0"
    # Support browser-chrome result summary format which differs from
    # MozillaMochitest's.
    if 'browser-chrome' in name:
        failIdent = r"^\tFailed:\s+0"
    # Assume that having the 'failIdent' line
    # means the tests run completed (successfully).
    # Also check for "^TEST-UNEXPECTED-" for harness errors.
    if not re.search(failIdent, log, re.MULTILINE) or \
            re.search("^TEST-UNEXPECTED-", log, re.MULTILINE):
        return worst_status(superResult, WARNINGS)

    return worst_status(superResult, SUCCESS)


def evaluateReftest(log, superResult):
    # When a unittest fails we mark it orange, indicating with the
    # WARNINGS status. Therefore, FAILURE needs to become WARNINGS
    # However, we don't want to override EXCEPTION or RETRY, so we still
    # need to use worst_status in further status decisions.
    if superResult == FAILURE:
        superResult = WARNINGS

    if superResult != SUCCESS:
        return superResult

    # Assume that having the "Unexpected: 0" line
    # means the tests run completed (successfully).
    # Also check for "^TEST-UNEXPECTED-" for harness errors.
    if not re.search(r"^REFTEST INFO \| Unexpected: 0 \(", log, re.MULTILINE) or \
            re.search("^TEST-UNEXPECTED-", log, re.MULTILINE):
        return worst_status(superResult, WARNINGS)

    return worst_status(superResult, SUCCESS)


class MochitestMixin(object):
    warnOnFailure = True
    warnOnWarnings = True

    def getVariantOptions(self, variant):
        if variant == 'ipcplugins':
            return ['--setpref=dom.ipc.plugins.enabled=false',
                    '--setpref=dom.ipc.plugins.enabled.x86_64=false',
                    '--%s' % variant]
        elif variant == 'robocop':
            return ['--robocop=mochitest/robocop.ini']
        elif variant != 'plain':
            return ['--%s' % variant]
        else:
            return []

    def createSummary(self, log):
        self.addCompleteLog('summary', summarizeLogMochitest(self.name, log))

    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        return evaluateMochitest(self.name, cmd.logs['stdio'].getText(),
                                 superResult)


class XPCShellMixin(object):
    warnOnFailure = True
    warnOnWarnings = True

    def createSummary(self, log):
        self.addCompleteLog(
            'summary', summarizeLogXpcshelltests(self.name, log))

    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        # When a unittest fails we mark it orange, indicating with the
        # WARNINGS status. Therefore, FAILURE needs to become WARNINGS
        # However, we don't want to override EXCEPTION or RETRY, so we still
        # need to use worst_status in further status decisions.
        if superResult == FAILURE:
            superResult = WARNINGS

        if superResult != SUCCESS:
            return superResult

        # Assume that having the "Failed:\s+0" line
        # means the tests run completed (successfully).
        # Also check for "^TEST-UNEXPECTED-" for harness errors.
        if not re.search(r"^INFO \| Failed:\s+0", cmd.logs["stdio"].getText(), re.MULTILINE) or \
                re.search("^TEST-UNEXPECTED-", cmd.logs["stdio"].getText(), re.MULTILINE):
            return worst_status(superResult, WARNINGS)

        return worst_status(superResult, SUCCESS)


class ReftestMixin(object):
    warnOnFailure = True
    warnOnWarnings = True

    def getSuiteOptions(self, suite):
        if suite == 'crashtest':
            return ['reftest/tests/testing/crashtest/crashtests.list']
        elif suite == 'crashtest-ipc':
            return ['--setpref=browser.tabs.remote=true',
                    'reftest/tests/testing/crashtest/crashtests.list']
        elif suite in ('reftest', 'direct3D', 'opengl', 'reftestsmall'):
            return ['reftest/tests/layout/reftests/reftest.list']
        elif suite in ('reftest-ipc'):
            # See bug 637858 for why we are doing a subset of all reftests
            return ['--setpref=browser.tabs.remote=true',
                    'reftest/tests/layout/reftests/reftest-sanity/reftest.list']
        elif suite == 'reftest-d2d':
            return ['--setpref=gfx.font_rendering.directwrite.enabled=true',
                    '--setpref=mozilla.widget.render-mode=6',
                    'reftest/tests/layout/reftests/reftest.list']
        elif suite == 'reftest-no-d2d-d3d':
            return ['--setpref=gfx.direct2d.disabled=true',
                    '--setpref=layers.acceleration.disabled=true',
                    'reftest/tests/layout/reftests/reftest.list']
        elif suite == 'opengl-no-accel':
            return ['--setpref=layers.acceleration.force-enabled=disabled',
                    'reftest/tests/layout/reftests/reftest.list']
        elif suite == 'jsreftest':
            return ['--extra-profile-file=jsreftest/tests/user.js',
                    'jsreftest/tests/jstests.list']
        elif suite == 'reftest-sanity':
            return ['reftest/tests/layout/reftests/reftest-sanity/reftest.list']

    def createSummary(self, log):
        self.addCompleteLog('summary', summarizeLogReftest(self.name, log))

    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        return evaluateReftest(cmd.logs['stdio'].getText(), superResult)


class ChunkingMixin(object):
    def getChunkOptions(self, totalChunks, thisChunk, chunkByDir=None):
        if not totalChunks or not thisChunk:
            return []
        ret = ['--total-chunks', str(totalChunks),
               '--this-chunk', str(thisChunk)]
        if chunkByDir:
            ret.extend(['--chunk-by-dir', str(chunkByDir)])
        return ret


class ShellCommandReportTimeout(ShellCommand):
    """We subclass ShellCommand so that we can bubble up the timeout errors
    to tinderbox that normally only get appended to the buildbot slave logs.
    """
    def __init__(self, timeout=2 * 3600, maxTime=4 * 3600, **kwargs):
        self.my_shellcommand = ShellCommand
        ShellCommand.__init__(self, timeout=timeout, maxTime=maxTime, **kwargs)

    def evaluateCommand(self, cmd):
        superResult = self.my_shellcommand.evaluateCommand(self, cmd)
        for line in cmd.logs["stdio"].readlines(channel=HEADER):
            if "command timed out" in line:
                self.addCompleteLog('timeout',
                                    "TinderboxPrint: " + self.name + "<br/>" +
                                    emphasizeFailureText("timeout") + "\n")
                # We don't need to print a second error if we timed out
                return worst_status(superResult, WARNINGS)

        if cmd.rc != 0:
            self.addCompleteLog('error',
                                'Unknown Error: command finished with exit code: %d' % cmd.rc)
            return worst_status(superResult, WARNINGS)

        return superResult


class MozillaCheck(ShellCommandReportTimeout):
    warnOnFailure = True

    def __init__(self, test_name, makeCmd=["make"], **kwargs):
        self.name = test_name
        if test_name == "check":
            # Target executing recursively in all (sub)directories.
            # "-k: Keep going when some targets can't be made."
            self.command = makeCmd + ["-k", test_name]
        else:
            # Target calling a python script.
            self.command = makeCmd + [test_name]
        self.description = [test_name + " test"]
        self.descriptionDone = [self.description[0] + " complete"]
        self.super_class = ShellCommandReportTimeout
        ShellCommandReportTimeout.__init__(self, **kwargs)
        self.addFactoryArguments(test_name=test_name, makeCmd=makeCmd)

    def createSummary(self, log):
        if 'xpcshell' in self.name:
            self.addCompleteLog(
                'summary', summarizeLogXpcshelltests(self.name, log))
        else:
            self.addCompleteLog('summary', summarizeTUnit(self.name, log))

    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        # When a unittest fails we mark it orange, indicating with the
        # WARNINGS status. Therefore, FAILURE needs to become WARNINGS
        # However, we don't want to override EXCEPTION or RETRY, so we still
        # need to use worst_status in further status decisions.
        if superResult == FAILURE:
            superResult = WARNINGS

        if superResult != SUCCESS:
            return worst_status(superResult, WARNINGS)

        # Xpcshell tests (only):
        # Assume that having the "Failed:\s+0" line
        # means the tests run completed (successfully).
        if 'xpcshell' in self.name and \
           not re.search(r"^INFO \| Failed:\s+0", cmd.logs["stdio"].getText(), re.MULTILINE):
            return worst_status(superResult, WARNINGS)

        # Also check for "^TEST-UNEXPECTED-" for harness errors.
        if re.search("^TEST-UNEXPECTED-", cmd.logs["stdio"].getText(), re.MULTILINE):
            return worst_status(superResult, WARNINGS)

        return worst_status(superResult, SUCCESS)


class MozillaPackagedXPCShellTests(XPCShellMixin, ShellCommandReportTimeout):
    name = "xpcshell"

    def __init__(self, platform, symbols_path=None, **kwargs):
        self.super_class = ShellCommandReportTimeout
        ShellCommandReportTimeout.__init__(self, **kwargs)

        self.addFactoryArguments(platform=platform, symbols_path=symbols_path)

        bin_extension = ""
        if platform.startswith('win'):
            bin_extension = ".exe"
        script = " && ".join(["if [ ! -d %(exedir)s/plugins ]; then mkdir %(exedir)s/plugins; fi",
                              "if [ ! -d %(exedir)s/components ]; then mkdir %(exedir)s/components; fi",
                              "if [ ! -d %(exedir)s/extensions ]; then mkdir %(exedir)s/extensions; fi",
                              "cp bin/xpcshell" +
                              bin_extension + " %(exedir)s",
                              "cp bin/ssltunnel" +
                              bin_extension + " %(exedir)s",
                              "cp -R bin/components/* %(exedir)s/components/",
                              "cp -R bin/plugins/* %(exedir)s/plugins/",
                              "if [ -d extensions ]; then cp -R extensions/* %(exedir)s/extensions/; fi",
                              "python -u xpcshell/runxpcshelltests.py"])

        if symbols_path:
            script += " --symbols-path=%s" % symbols_path
        script += " --manifest=xpcshell/tests/all-test-dirs.list %(exedir)s/xpcshell" + bin_extension

        self.command = ['bash', '-c', WithProperties(script)]


# MochitestMixin overrides some methods that BuildStep calls
# In order to make sure its are called, instead of ShellCommandReportTimeout's,
# it needs to be listed first
class MozillaPackagedMochitests(MochitestMixin, ChunkingMixin, ShellCommandReportTimeout):
    def __init__(self, variant='plain', symbols_path=None, leakThreshold=None,
                 chunkByDir=None, totalChunks=None, thisChunk=None, testPath=None,
                 **kwargs):
        self.super_class = ShellCommandReportTimeout
        ShellCommandReportTimeout.__init__(self, **kwargs)

        if totalChunks:
            assert 1 <= thisChunk <= totalChunks

        self.addFactoryArguments(variant=variant, symbols_path=symbols_path,
                                 leakThreshold=leakThreshold, chunkByDir=chunkByDir,
                                 totalChunks=totalChunks, thisChunk=thisChunk, testPath=testPath)

        if totalChunks:
            self.name = 'mochitest-%s-%i' % (variant, thisChunk)
        else:
            self.name = 'mochitest-%s' % variant

        self.command = ['python', 'mochitest/runtests.py',
                        WithProperties(
                        '--appname=%(exepath)s'), '--utility-path=bin',
                        WithProperties('--extra-profile-file=bin/plugins'),
                        '--certificate-path=certs', '--autorun', '--close-when-done',
                        '--console-level=INFO']
        if testPath:
            self.command.append("--test-path=%s" % testPath)

        if symbols_path:
            self.command.append(
                WithProperties("--symbols-path=%s" % symbols_path))

        if leakThreshold:
            self.command.append('--leak-threshold=%d' % leakThreshold)

        self.command.extend(self.getChunkOptions(totalChunks, thisChunk,
                                                 chunkByDir))
        self.command.extend(self.getVariantOptions(variant))


class MozillaPackagedReftests(ReftestMixin, ShellCommandReportTimeout):
    def __init__(self, suite, symbols_path=None, leakThreshold=None,
                 **kwargs):
        self.super_class = ShellCommandReportTimeout
        ShellCommandReportTimeout.__init__(self, **kwargs)

        self.addFactoryArguments(suite=suite,
                                 symbols_path=symbols_path, leakThreshold=leakThreshold)
        self.name = suite
        self.command = ['python', 'reftest/runreftest.py',
                        WithProperties('--appname=%(exepath)s'),
                        '--utility-path=bin',
                        '--extra-profile-file=bin/plugins',
                        ]
        if symbols_path:
            self.command.append(
                WithProperties("--symbols-path=%s" % symbols_path))
        if leakThreshold:
            self.command.append('--leak-threshold=%d' % leakThreshold)
        self.command.extend(self.getSuiteOptions(suite))


class MozillaPackagedJetpackTests(ShellCommandReportTimeout):
    warnOnFailure = True
    warnOnWarnings = True

    def __init__(self, suite, symbols_path=None, leakThreshold=None, **kwargs):
        self.super_class = ShellCommandReportTimeout
        ShellCommandReportTimeout.__init__(self, **kwargs)

        self.addFactoryArguments(suite=suite, symbols_path=symbols_path,
                                 leakThreshold=leakThreshold)

        self.name = suite

        self.command = [
            'python', 'jetpack/bin/cfx',
            WithProperties('--binary=%(exepath)s'),
            '--parseable', suite
        ]

        # TODO: When jetpack can handle symbols path and leak testing, add those
        # until then, we skip that.

    def createSummary(self, log):
        self.addCompleteLog(
            'summary', summarizeLogJetpacktests(self.name, log))

    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        # When a unittest fails we mark it orange, indicating with the
        # WARNINGS status. Therefore, FAILURE needs to become WARNINGS
        # However, we don't want to override EXCEPTION or RETRY, so we still
        # need to use worst_status in further status decisions.
        if superResult == FAILURE:
            superResult = WARNINGS

        if superResult != SUCCESS:
            return worst_status(superResult, WARNINGS)

        return worst_status(superResult, SUCCESS)


class RemoteMochitestStep(MochitestMixin, ChunkingMixin, ShellCommandReportTimeout):
    def __init__(self, variant, symbols_path=None, testPath=None,
                 xrePath='../hostutils/xre', testManifest=None,
                 utilityPath='../hostutils/bin', certificatePath='certs',
                 consoleLevel='INFO', totalChunks=None, thisChunk=None,
                 **kwargs):
        self.super_class = ShellCommandReportTimeout
        ShellCommandReportTimeout.__init__(self, **kwargs)

        if totalChunks:
            assert 1 <= thisChunk <= totalChunks

        self.addFactoryArguments(variant=variant, symbols_path=symbols_path,
                                 testPath=testPath, xrePath=xrePath,
                                 testManifest=testManifest, utilityPath=utilityPath,
                                 certificatePath=certificatePath,
                                 consoleLevel=consoleLevel,
                                 totalChunks=totalChunks, thisChunk=thisChunk)

        self.name = 'mochitest-%s' % variant
        self.command = ['python', '-u', 'mochitest/runtestsremote.py',
                        '--deviceIP', WithProperties('%(sut_ip)s'),
                        '--xre-path', xrePath,
                        '--utility-path', utilityPath,
                        '--certificate-path', certificatePath,
                        '--app', WithProperties("%(remoteProcessName)s"),
                        '--console-level', consoleLevel,
                        '--http-port', WithProperties('%(http_port)s'),
                        '--ssl-port', WithProperties('%(ssl_port)s'),
                        '--pidfile', WithProperties(
                            '%(basedir)s/../runtestsremote.pid')
                        ]
        self.command.extend(self.getVariantOptions(variant))
        if testPath:
            self.command.extend(['--test-path', testPath])
        if testManifest:
            self.command.extend(['--run-only-tests', testManifest])
        if symbols_path:
            self.command.append(
                WithProperties("--symbols-path=%s" % symbols_path))
        self.command.extend(self.getChunkOptions(totalChunks, thisChunk))

class RemoteMochitestBrowserChromeStep(RemoteMochitestStep):
    def __init__(self, **kwargs):
        self.super_class = RemoteMochitestStep
        RemoteMochitestStep.__init__(self, **kwargs)

    def createSummary(self, log):
        self.addCompleteLog(
            'summary', summarizeLogRemoteMochitest(self.name, log))

    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        return evaluateRemoteMochitest(self.name, cmd.logs['stdio'].getText(),
                                       superResult)


class RemoteReftestStep(ReftestMixin, ChunkingMixin, ShellCommandReportTimeout):
    def __init__(self, suite, symbols_path=None, xrePath='../hostutils/xre',
                 utilityPath='../hostutils/bin', totalChunks=None,
                 thisChunk=None, cmdOptions=None, extra_args=None, **kwargs):
        self.super_class = ShellCommandReportTimeout
        ShellCommandReportTimeout.__init__(self, **kwargs)
        self.addFactoryArguments(suite=suite, xrePath=xrePath,
                                 symbols_path=symbols_path,
                                 utilityPath=utilityPath,
                                 totalChunks=totalChunks, thisChunk=thisChunk,
                                 cmdOptions=cmdOptions, extra_args=extra_args)

        self.name = suite
        if totalChunks:
            self.name += '-%i' % thisChunk
        self.command = ['python', '-u', 'reftest/remotereftest.py',
                        '--deviceIP', WithProperties('%(sut_ip)s'),
                        '--xre-path', xrePath,
                        '--utility-path', utilityPath,
                        '--app', WithProperties("%(remoteProcessName)s"),
                        '--http-port', WithProperties('%(http_port)s'),
                        '--ssl-port', WithProperties('%(ssl_port)s'),
                        '--pidfile', WithProperties(
                            '%(basedir)s/../remotereftest.pid'),
                        '--enable-privilege'
                        ]
        if suite == 'jsreftest' or suite == 'crashtest':
            self.command.append('--ignore-window-size')
        if extra_args:
            self.command.append(extra_args)

        if cmdOptions:
            self.command.extend(cmdOptions)
        self.command.extend(self.getChunkOptions(totalChunks, thisChunk))
        self.command.extend(self.getSuiteOptions(suite))

        if symbols_path:
            self.command.append(
                WithProperties("--symbols-path=%s" % symbols_path))


class RemoteXPCShellStep(XPCShellMixin, ChunkingMixin, ShellCommandReportTimeout):
    def __init__(self, suite, symbols_path=None, xrePath='../hostutils/xre',
                 totalChunks=None, thisChunk=None, cmdOptions=None, extra_args=None, **kwargs):
        self.super_class = ShellCommandReportTimeout
        ShellCommandReportTimeout.__init__(self, **kwargs)
        self.addFactoryArguments(suite=suite, xrePath=xrePath,
                                 symbols_path=symbols_path,
                                 totalChunks=totalChunks, thisChunk=thisChunk,
                                 cmdOptions=cmdOptions, extra_args=extra_args)

        self.name = suite
        if totalChunks:
            self.name += '-%i' % thisChunk

        self.command = ['python2.7', '-u', 'xpcshell/remotexpcshelltests.py',
                        '--deviceIP', WithProperties('%(sut_ip)s'),
                        '--xre-path', xrePath,
                        '--manifest', 'xpcshell/tests/xpcshell_android.ini',
                        '--build-info-json', 'xpcshell/mozinfo.json',
                        '--testing-modules-dir', 'modules',
                        '--local-lib-dir', WithProperties('../%(exedir)s'),
                        '--apk', WithProperties('../%(build_filename)s'),
                        '--no-logfiles']
        if extra_args:
            self.command.append(extra_args)

        self.command.extend(self.getChunkOptions(totalChunks, thisChunk))

        if symbols_path:
            self.command.append(
                WithProperties("--symbols-path=%s" % symbols_path))
