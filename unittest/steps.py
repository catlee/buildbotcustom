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
# ***** END LICENSE BLOCK *****

import re
import os

from buildbot.steps.shell import ShellCommand
from buildbot.status.builder import SUCCESS, WARNINGS, FAILURE, SKIPPED, EXCEPTION, HEADER

cvsCoLog = "cvsco.log"
tboxClobberCvsCoLog = "tbox-CLOBBER-cvsco.log"
buildbotClobberCvsCoLog = "buildbot-CLOBBER-cvsco.log"

def emphasizeFailureText(text):
    return '<em class="testfail">%s</em>' % text

# Some test suites (like TUnit) may not (yet) have the |knownFailCount| feature.
# Some test suites (like TUnit and Reftest) may not (yet) have the |leaked| feature.
# Expected values for |leaked|: False, no leak; True, leaked; None, report failure.
def summaryText(passCount, failCount, knownFailCount = None, leaked = False):
    # Format the tests counts.
    summary = "%d/%d" % (passCount, failCount)
    if knownFailCount != None:
        summary += "/%d" % knownFailCount
    # Check whether all tests succeeded.
    if failCount > 0:
        summary = emphasizeFailureText(summary)
    # Format the leak status.
    if leaked != False:
        summary += "&nbsp;%s" % emphasizeFailureText((leaked and "LEAK") or "FAIL")
    return summary

class ShellCommandReportTimeout(ShellCommand):
    """We subclass ShellCommand so that we can bubble up the timeout errors
    to tinderbox that normally only get appended to the buildbot slave logs.
    """
    def __init__(self, **kwargs):
	self.my_shellcommand = ShellCommand
	ShellCommand.__init__(self, **kwargs)

    def evaluateCommand(self, cmd):
        superResult = self.my_shellcommand.evaluateCommand(self, cmd)
        for line in cmd.logs['stdio'].readlines(channel=HEADER):
            if "command timed out" in line:
                self.addCompleteLog('timeout',
                                    'buildbot.slave.commands.TimeoutError: ' +
                                    line +
                                    "TinderboxPrint: " + self.name + "<br/>" +
                                    emphasizeFailureText("timeout") + "\n")
                # We don't need to print a second error if we timed out
                return WARNINGS

        if cmd.rc != 0:
            self.addCompleteLog('error',
              'Unknown Error: command finished with exit code: %d' % cmd.rc)
            return WARNINGS

        return superResult

class MozillaCheckoutClientMk(ShellCommandReportTimeout):
    haltOnFailure = True
    cvsroot = ":pserver:anonymous@cvs-mirror.mozilla.org:/cvsroot"
    
    def __init__(self, **kwargs):
        if 'cvsroot' in kwargs:
            self.cvsroot = kwargs['cvsroot']
        if not 'command' in kwargs:
            kwargs['command'] = ["cvs", "-d", self.cvsroot, "co", "mozilla/client.mk"]
        ShellCommandReportTimeout.__init__(self, **kwargs)
    
    def describe(self, done=False):
        return ["client.mk update"]
    
 
class MozillaClientMkPull(ShellCommandReportTimeout):
    haltOnFailure = True
    def __init__(self, **kwargs):
        if not 'project' in kwargs or kwargs['project'] is None:
            self.project = "browser"
        else:
            self.project = kwargs['project']
            del kwargs['project']
        if not 'workdir' in kwargs:
            kwargs['workdir'] = "mozilla"
        if not 'command' in kwargs:
            kwargs['command'] = ["make", "-f", "client.mk", "pull_all"]
        env = {}
        if 'env' in kwargs:
            env = kwargs['env'].copy()
        env['MOZ_CO_PROJECT'] = self.project
        kwargs['env'] = env
        ShellCommandReportTimeout.__init__(self, **kwargs)
    
    def describe(self, done=False):
        if not done:
            return ["pulling (" + self.project + ")"]
        return ["pull (" + self.project + ")"]
    

class MozillaPackage(ShellCommandReportTimeout):
    name = "package"
    warnOnFailure = True
    description = ["packaging"]
    descriptionDone = ["package"]
    command = ["make"]

class UpdateClobberFiles(ShellCommandReportTimeout):
    name = "update clobber files"
    warnOnFailure = True
    description = "updating clobber files"
    descriptionDone = "clobber files updated"
    clobberFilePath = "clobber_files/"
    logDir = '../logs/'

    def __init__(self, **kwargs):
        if not 'platform' in kwargs:
            return FAILURE
        self.platform = kwargs['platform']
        if 'clobberFilePath' in kwargs:
            self.clobberFilePath = kwargs['clobberFilePath']
        if 'logDir' in kwargs:
            self.logDir = kwargs['logDir']
        if self.platform.startswith('win'):
            self.tboxClobberModule = 'mozilla/tools/tinderbox-configs/firefox/win32'
        else:
            self.tboxClobberModule = 'mozilla/tools/tinderbox-configs/firefox/' + self.platform
        if 'cvsroot' in kwargs:
            self.cvsroot = kwargs['cvsroot']
        if 'branch' in kwargs:
            self.branchString = ' -r ' + kwargs['branch']
            self.buildbotClobberModule = 'mozilla/tools/buildbot-configs/testing/unittest/CLOBBER/firefox/' + kwargs['branch'] + '/' + self.platform
        else:
            self.branchString = ''
            self.buildbotClobberModule = 'mozilla/tools/buildbot-configs/testing/unittest/CLOBBER/firefox/TRUNK/' + self.platform 
            
        if not 'command' in kwargs:
            self.command = r'cd ' + self.clobberFilePath + r' && cvs -d ' + self.cvsroot + r' checkout' + self.branchString + r' -d tinderbox-configs ' + self.tboxClobberModule + r'>' + self.logDir + tboxClobberCvsCoLog + r' && cvs -d ' + self.cvsroot + r' checkout -d buildbot-configs ' + self.buildbotClobberModule + r'>' + self.logDir + buildbotClobberCvsCoLog
        ShellCommandReportTimeout.__init__(self, **kwargs)

class MozillaClobber(ShellCommandReportTimeout):
    name = "clobber"
    description = "checking clobber file"
    descriptionDone = "clobber checked"
    clobberFilePath = "clobber_files/"
    logDir = 'logs/'
    
    def __init__(self, **kwargs):
        if 'platform' in kwargs:
            self.platform = kwargs['platform']
        if 'logDir' in kwargs:
            self.logDir = kwargs['logDir']
        if 'clobberFilePath' in kwargs:
            self.clobberFilePath = kwargs['clobberFilePath']
        if not 'command' in kwargs:
            tboxGrepCommand = r"grep -q '^U tinderbox-configs.CLOBBER' " + self.logDir + tboxClobberCvsCoLog
            tboxPrintHeader = "echo Tinderbox clobber file updated"
            tboxCatCommand = "cat %s/tinderbox-configs/CLOBBER" % self.clobberFilePath
            buildbotGrepCommand = r"grep -q '^U buildbot-configs.CLOBBER' " + self.logDir + buildbotClobberCvsCoLog
            buildbotPrintHeader = "echo Buildbot clobber file updated"
            buildbotCatCommand = "cat %s/buildbot-configs/CLOBBER" % self.clobberFilePath
            rmCommand = "rm -rf mozilla"
            printExitStatus = "echo No clobber required"
            self.command = tboxGrepCommand + r' && ' + tboxPrintHeader + r' && ' + tboxCatCommand + r' && ' + rmCommand + r'; if [ $? -gt 0 ]; then ' + buildbotGrepCommand + r' && ' + buildbotPrintHeader + r' && ' + buildbotCatCommand + r' && ' + rmCommand + r'; fi; if [ $? -gt 0 ]; then ' + printExitStatus + r'; fi'
        ShellCommandReportTimeout.__init__(self, **kwargs)

class MozillaClobberWin(ShellCommandReportTimeout):
    name = "clobber win"
    description = "checking clobber file"
    descriptionDone = "clobber finished"
    
    def __init__(self, **kwargs):
        platformFlag = ""
        slaveNameFlag = ""
        branchFlag = ""
        if 'platform' in kwargs:
            platformFlag = " --platform=" + kwargs['platform']
        if 'slaveName' in kwargs:
            slaveNameFlag = " --slaveName=" + kwargs['slaveName']
        if 'branch' in kwargs:
            branchFlag = " --branch=" + kwargs['branch']
        if not 'command' in kwargs:
            self.command = 'python C:\\Utilities\\killAndClobberWin.py' + platformFlag + slaveNameFlag + branchFlag
        ShellCommandReportTimeout.__init__(self, **kwargs)

class MozillaCheck(ShellCommandReportTimeout):
    name = "TUnit"
    warnOnFailure = True
    command = ["make", "-k", "check"]

    def __init__(self, **kwargs):
        self.description = [self.name + " test"]
        self.descriptionDone = [self.description[0] + " complete"]
	self.super_class = ShellCommandReportTimeout
	ShellCommandReportTimeout.__init__(self, **kwargs)
   
    def createSummary(self, log):
        passCount = 0
        failCount = 0
        for line in log.readlines():
            if "TEST-PASS" in line:
                passCount = passCount + 1
            if "TEST-UNEXPECTED-" in line:
                failCount = failCount + 1
        summary = "TinderboxPrint: %s<br/>" % self.name
        summary += "%s\n" % summaryText(passCount, failCount)
        self.addCompleteLog('summary', summary)
    
    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        if SUCCESS != superResult:
            return WARNINGS
        if None != re.search('TEST-UNEXPECTED-', cmd.logs['stdio'].getText()):
            return WARNINGS
        return SUCCESS
    
class MozillaReftest(ShellCommandReportTimeout):
    warnOnFailure = True

    def __init__(self, test_name, **kwargs):
        self.name = test_name
        self.command = ["make", test_name]
        self.description = [test_name + " test"]
        self.descriptionDone = [self.description[0] + " complete"]
	self.super_class = ShellCommandReportTimeout
	ShellCommandReportTimeout.__init__(self, **kwargs)
   
    def createSummary(self, log):
        # Counts.
        successfulCount = -1
        unexpectedCount = -1
        knownProblemsCount = -1
        # Regular expression for result summary details.
        infoRe = re.compile(r"REFTEST INFO \| (Successful|Unexpected|Known problems): (\d+) \(")
        # Process the log.
        for line in log.readlines():
            m = infoRe.match(line)
            # Skip non-matching lines.
            if m == None:
                continue
            # Set the counts.
            r = m.group(1)
            if r == "Successful":
                successfulCount = int(m.group(2))
            elif r == "Unexpected":
                unexpectedCount = int(m.group(2))
            elif r == "Known problems":
                knownProblemsCount = int(m.group(2))
        # Add the summary.
        summary = "TinderboxPrint: %s<br/>" % self.name
        if successfulCount < 0 or unexpectedCount < 0 or knownProblemsCount < 0:
            summary += "%s\n" % emphasizeFailureText("FAIL")
        else:
            summary += "%s\n" % summaryText(successfulCount, unexpectedCount, knownProblemsCount)
        self.addCompleteLog('summary', summary)

    def evaluateCommand(self, cmd):
        superResult = self.super_class.evaluateCommand(self, cmd)
        # Assume that having the "Unexpected: 0" line means the tests run completed.
        if SUCCESS != superResult or \
           not re.search(r"^REFTEST INFO \| Unexpected: 0 \(", cmd.logs["stdio"].getText(), re.MULTILINE):
            return WARNINGS
        return SUCCESS

class MozillaMochitest(ShellCommandReportTimeout):
    warnOnFailure = True
    
    def __init__(self, test_name, leakThreshold=None, **kwargs):
        self.name = test_name
        self.command = ["make", test_name]
        self.description = [test_name + " test"]
        self.descriptionDone = [self.description[0] + " complete"]
        if leakThreshold:
            self.command.append("--leak-threshold=" + str(leakThreshold))
        ShellCommandReportTimeout.__init__(self, **kwargs)    
	self.super_class = ShellCommandReportTimeout
    
    def createSummary(self, log):
        # Support browser-chrome result summary format which differs from MozillaMochitest's.
        if self.name == 'mochitest-browser-chrome':
            passCount = 0
            failCount = 0
            todoCount = 0
            leaked = False
            for line in log.readlines():
                if "Pass:" in line:
                    passCount = int(line.split()[-1])
                if "Fail:" in line:
                    failCount = int(line.split()[-1])
                if "Todo:" in line:
                    todoCount = int(line.split()[-1])
                if "during test execution" in line and \
                  "runtests-leaks" in line and \
                  "TEST-UNEXPECTED-FAIL" in line:
                    match = re.search(r"leaked (\d+) bytes during test execution", line)
                    assert match is not None
                    leaked = int(match.group(1)) != 0
            summary = "TinderboxPrint: browser<br/>"
            if not (passCount + failCount + todoCount):
                summary += "%s\n" % emphasizeFailureText("FAIL")
            else:
                summary += summaryText(passCount, failCount, todoCount, leaked) + "\n"
            self.addCompleteLog('summary', summary)
        else:
            passCount = 0
            failCount = 0
            todoCount = 0
            leaked = False
            for line in log.readlines():
                if "INFO Passed:" in line:
                    passCount = int(line.split()[-1])
                if "INFO Failed:" in line:
                    failCount = int(line.split()[-1])
                if "INFO Todo:" in line:
                    todoCount = int(line.split()[-1])
                if "during test execution" in line and \
                  "runtests-leaks" in line and \
                  "TEST-UNEXPECTED-FAIL" in line:
                    match = re.search(r"leaked (\d+) bytes during test execution", line)
                    assert match is not None
                    leaked = int(match.group(1)) != 0
            summary = "TinderboxPrint: %s<br/>" % self.name
            if not (passCount + failCount + todoCount):
                summary += "%s\n" % emphasizeFailureText("FAIL")
            else:
                summary += summaryText(passCount, failCount, todoCount, leaked) + "\n"
            self.addCompleteLog('summary', summary)

    def evaluateCommand(self, cmd):
        # Support browser-chrome result summary format which differs from MozillaMochitest's.
        if self.name == 'mochitest-browser-chrome':
            superResult = self.super_class.evaluateCommand(self, cmd)
            if SUCCESS != superResult:
                return WARNINGS
            if re.search('TEST-UNEXPECTED-', cmd.logs['stdio'].getText()):
                return WARNINGS
            if re.search('FAIL Exited', cmd.logs['stdio'].getText()):
                return WARNINGS
            return SUCCESS
        else:
            superResult = self.super_class.evaluateCommand(self, cmd)
            if SUCCESS != superResult:
                return WARNINGS
            if re.search('TEST-UNEXPECTED-', cmd.logs['stdio'].getText()):
                return WARNINGS
            if re.search('FAIL Exited', cmd.logs['stdio'].getText()):
                return WARNINGS
            if not re.search('TEST-PASS', cmd.logs['stdio'].getText()):
                return WARNINGS
            return SUCCESS
                
class CreateProfile(ShellCommandReportTimeout):
    name = "create profile"
    warnOnFailure = True
    description = ["create profile"]
    descriptionDone = ["create profile complete"]
    command = r'python mozilla/testing/tools/profiles/createTestingProfile.py --binary mozilla/objdir/dist/bin/firefox'

class CreateProfileWin(CreateProfile):
    command = r'python mozilla\testing\tools\profiles\createTestingProfile.py --binary mozilla\objdir\dist\bin\firefox.exe'
