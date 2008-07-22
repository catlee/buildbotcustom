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

def summaryText(passCount, failCount, knownFailCount=None):
    knownFail = ""
    if knownFailCount is not None:
        knownFail = "/%d" % knownFailCount
    summary = "%d/%d%s" % (passCount, failCount, knownFail)
    if failCount > 0:
        summary = '<em class="testfail">%s</em>' % summary
    return summary

class ShellCommandReportTimeout(ShellCommand):
    """We subclass ShellCommand so that we can bubble up the timeout errors
    to tinderbox that normally only get appended to the buildbot slave logs.
    """
    def evaluateCommand(self, cmd):
        superResult = ShellCommand.evaluateCommand(self, cmd)
        for line in cmd.logs['stdio'].readlines(channel=HEADER):
            if "command timed out" in line:
                self.addCompleteLog('timeout',
                                    'buildbot.slave.commands.TimeoutError: ' +
                                    line +
                                    "TinderboxPrint: " +
                                    self.name + ' <em class="testfail">timeout</em><br/>\n')
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
    name = "check"
    warnOnFailure = True
    description = ["checking"]
    descriptionDone = ["check complete"]
    command = ["make", "-k", "check"]
   
    def createSummary(self, log):
        passCount = 0
        failCount = 0
        for line in log.readlines():
            if "TEST-PASS" in line:
                passCount = passCount + 1
            if "TEST-UNEXPECTED-" in line:
                failCount = failCount + 1
        summary = "TinderboxPrint: TUnit<br/>" + summaryText(passCount,failCount) + "\n"
        self.addCompleteLog('summary', summary)
    
    def evaluateCommand(self, cmd):
        superResult = ShellCommandReportTimeout.evaluateCommand(self, cmd)
        if SUCCESS != superResult:
            return WARNINGS
        if None != re.search('TEST-UNEXPECTED-', cmd.logs['stdio'].getText()):
            return WARNINGS
        return SUCCESS
    
class MozillaReftest(ShellCommandReportTimeout):
    warnOnFailure = True
    name = "reftest"
    description = ["reftest"]
    descriptionDone = ["reftest complete"]
   
    def createSummary(self, log):
        testCount = 0
        passCount = 0
        failCount = 0
        knownFailCount = 0
        for line in log.readlines():
            if "REFTEST" not in line:
                continue
            if "IMAGE" in line:
                continue
            if "(EXPECTED RANDOM)" in line:
                continue
            testCount += 1
            if "TEST-UNEXPECTED-" in line:
                failCount += 1
                continue
            if "TEST-KNOWN-FAIL" in line:
                knownFailCount += 1
            else:
                passCount += 1
        summary = "TinderboxPrint: " + self.name + "<br/>" + summaryText(passCount, failCount, knownFailCount) + "\n"
        self.addCompleteLog('summary', summary)
    
    def evaluateCommand(self, cmd):
        superResult = ShellCommandReportTimeout.evaluateCommand(self, cmd)
        if SUCCESS != superResult:
            return WARNINGS
        if re.search('TEST-UNEXPECTED-', cmd.logs['stdio'].getText()):
            return WARNINGS
        return SUCCESS
    
class MozillaUnixReftest(MozillaReftest):
    command = ["../../objdir/dist/bin/run-mozilla.sh",
               "../../objdir/dist/bin/firefox",
               "-P",
               "default",
               "-reftest",
               "reftest.list"]

class MozillaOSXReftest(MozillaReftest):
    command = ["../../objdir/dist/Minefield.app/Contents/MacOS/firefox",
               "-console",
               "-P",
               "default",
               "-reftest",
               "reftest.list"]

class MozillaWin32Reftest(MozillaReftest):
    command = [r'..\..\objdir\dist\bin\firefox.exe -P default -reftest reftest.list']

class MozillaCrashtest(MozillaReftest):
    name = "crashtest"
    description = ["crashtest"]
    descriptionDone = ["crashtest complete"]

class MozillaUnixCrashtest(MozillaCrashtest):
    command = ["../../objdir/dist/bin/run-mozilla.sh",
               "../../objdir/dist/bin/firefox",
               "-P",
               "default",
               "-reftest",
               "crashtests.list"]

class MozillaOSXCrashtest(MozillaCrashtest):
    command = ["../../objdir/dist/Minefield.app/Contents/MacOS/firefox",
               "-console",
               "-P",
               "default",
               "-reftest",
               "crashtests.list"]

class MozillaWin32Crashtest(MozillaCrashtest):
    command = [r'..\..\objdir\dist\bin\firefox.exe -P default -reftest crashtests.list']

class MozillaMochitest(ShellCommandReportTimeout):
    name = "mochitest"
    warnOnFailure = True
    description = ["mochitest"]
    descriptionDone = ["mochitest complete"]
    command = ["python",
               "runtests.py",
               "--appname=../../../dist/bin/firefox",
               "--autorun",
               "--console-level=INFO",
               "--close-when-done"]
     
    def createSummary(self, log):
        passCount = 0
        failCount = 0
        todoCount = 0
        for line in log.readlines():
            if "INFO Passed:" in line:
                passCount = int(line.split()[-1])
            if "INFO Failed:" in line:
                failCount = int(line.split()[-1])
            if "INFO Todo:" in line:
                todoCount = int(line.split()[-1])
        summary = "TinderboxPrint: mochitest<br/>"
        if not (passCount + failCount + todoCount):
            summary += "FAIL\n"
        else:
            summary +=  summaryText(passCount,failCount,todoCount) + "\n"
        self.addCompleteLog('summary', summary)
    
    def evaluateCommand(self, cmd):
        superResult = ShellCommandReportTimeout.evaluateCommand(self, cmd)
        if SUCCESS != superResult:
            return WARNINGS
        if re.search('TEST-UNEXPECTED-', cmd.logs['stdio'].getText()):
            return WARNINGS
        if re.search('FAIL Exited', cmd.logs['stdio'].getText()):
            return WARNINGS
        if not re.search('TEST-PASS', cmd.logs['stdio'].getText()):
            return WARNINGS
        return SUCCESS

class MozillaWin32Mochitest(MozillaMochitest):
    command = ['python runtests.py --appname=..\\..\\..\\dist\\bin\\firefox.exe --autorun --console-level=INFO --close-when-done']

class MozillaOSXMochitest(MozillaMochitest):
    command = ["python",
               "runtests.py",
               "--appname=../../../dist/Minefield.app/Contents/MacOS/firefox",
               "--autorun",
               "--console-level=INFO",
               "--close-when-done"]

class MozillaMochichrome(ShellCommandReportTimeout):
    name = "mochichrome"
    warnOnFailure = True
    description = ["mochichrome"]
    descriptionDone = ["mochichrome complete"]
    command = ["python",
              "runtests.py",
              "--appname=../../../dist/bin/firefox",
              "--chrome",
              "--autorun",
              "--console-level=INFO",
              "--close-when-done"]
    
    def createSummary(self, log):
        passCount = 0
        failCount = 0
        todoCount = 0
        for line in log.readlines():
            if "INFO Passed:" in line:
                passCount = int(line.split()[-1])
            if "INFO Failed:" in line:
                failCount = int(line.split()[-1])
            if "INFO Todo:" in line:
                todoCount = int(line.split()[-1])
        summary = "TinderboxPrint: chrome<br/>"
        if not (passCount + failCount + todoCount):
            summary += "FAIL\n"
        else:
            summary +=  summaryText(passCount,failCount,todoCount) + "\n"
        self.addCompleteLog('summary', summary)
    
    def evaluateCommand(self, cmd):
        superResult = ShellCommandReportTimeout.evaluateCommand(self, cmd)
        if SUCCESS != superResult:
            return WARNINGS
        if re.search('TEST-UNEXPECTED-', cmd.logs['stdio'].getText()):
            return WARNINGS
        if re.search('FAIL Exited', cmd.logs['stdio'].getText()):
            return WARNINGS
        if not re.search('TEST-PASS', cmd.logs['stdio'].getText()):
            return WARNINGS
        return SUCCESS
    

class MozillaWin32Mochichrome(MozillaMochichrome):
   command = ['python runtests.py --appname=..\\..\\..\\dist\\bin\\firefox.exe --chrome --autorun --console-level=INFO --close-when-done']

class MozillaOSXMochichrome(MozillaMochichrome):
   command = ["python",
              "runtests.py",
              "--appname=../../../dist/Minefield.app/Contents/MacOS/firefox",
              "--chrome",
              "--autorun",
              "--console-level=INFO",
              "--close-when-done",
              "--leak-threshold=8"]

class MozillaBrowserChromeTest(ShellCommandReportTimeout):
    name = "browser chrome test"
    warnOnFailure = True
    description = ["browser chrome test"]
    descriptionDone = ["browser chrome test complete"]
    command = ["python",
               "runtests.py",
               "--appname=../../../dist/bin/firefox",
               "--autorun",
               "--browser-chrome", 
               "--close-when-done"]
    
    def createSummary(self, log):
        passCount = 0
        failCount = 0
        todoCount = 0
        for line in log.readlines():
            if "Pass:" in line:
                passCount = int(line.split()[-1])
            if "Fail:" in line:
                failCount = int(line.split()[-1])
            if "Todo:" in line:
                todoCount = int(line.split()[-1])
        summary = "TinderboxPrint: browser<br/>"
        if not (passCount + failCount + todoCount):
            summary += "FAIL\n"
        else:
            summary +=  summaryText(passCount,failCount,todoCount) + "\n"
        self.addCompleteLog('summary', summary)
    
    def evaluateCommand(self, cmd):
        superResult = ShellCommandReportTimeout.evaluateCommand(self, cmd)
        if SUCCESS != superResult:
            return WARNINGS
        if re.search('TEST-UNEXPECTED-', cmd.logs['stdio'].getText()):
            return WARNINGS
        if re.search('FAIL Exited', cmd.logs['stdio'].getText()):
            return WARNINGS
        return SUCCESS
    
class MozillaWin32BrowserChromeTest(MozillaBrowserChromeTest):
    command = ['python runtests.py --appname=..\\..\\..\\dist\\bin\\firefox.exe --autorun --browser-chrome --close-when-done']

class MozillaOSXBrowserChromeTest(MozillaBrowserChromeTest):
    command = ["python",
               "runtests.py",
               "--appname=../../../dist/Minefield.app/Contents/MacOS/firefox",
               "--autorun",
               "--browser-chrome",
               "--close-when-done"]

class CreateProfile(ShellCommandReportTimeout):
    name = "create profile"
    warnOnFailure = True
    description = ["create profile"]
    descriptionDone = ["create profile complete"]
    command = r'python mozilla/testing/tools/profiles/createTestingProfile.py --binary mozilla/objdir/dist/bin/firefox'

class CreateProfileWin(CreateProfile):
    command = r'python mozilla\testing\tools\profiles\createTestingProfile.py --binary mozilla\objdir\dist\bin\firefox.exe'
