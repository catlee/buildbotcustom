from buildbot.status.builder import FAILURE, SUCCESS, WARNINGS, worst_status

from buildbotcustom.steps.base import ShellCommand
from buildbotcustom.steps.misc import TinderboxShellCommand

class UpdateVerify(ShellCommand):
    def __init__(self, **kwargs):
        self.super_class = ShellCommand
        self.super_class.__init__(self, **kwargs)

    def evaluateCommand(self, cmd):
        worst = self.super_class.evaluateCommand(self, cmd)
        for line in cmd.logs['stdio'].getText().split("\n"):
            if line.startswith('FAIL'):
                worst = worst_status(worst, FAILURE)
        return worst

class L10nVerifyMetaDiff(TinderboxShellCommand):
    """Run the l10n verification script.
    """
    name='l10n metadiff'
    description=['create', 'metadiff']
    descriptionDone=['created', 'metadiff']

    def __init__(self, 
                 currentProduct=None, 
                 previousProduct=None,
                 **kwargs):
        self.super_class = TinderboxShellCommand
        self.super_class.__init__(self, ignoreCodes=[0,1], **kwargs)
        self.addFactoryArguments(currentProduct=currentProduct,
                                 previousProduct=previousProduct)
        if not 'command' in kwargs:
            if currentProduct is None:
                return FAILURE
            if previousProduct is None:
                return FAILURE
            self.command=['diff', '-r',
                          '%s/diffs' % currentProduct,
                          '%s/diffs' % previousProduct]
    
    def evaluateCommand(self, cmd):
        fileWarnings = self.getProperty('fileWarnings')
        if fileWarnings and len(fileWarnings) > 0:
            return WARNINGS
        '''We ignore failures here on purpose, since diff will 
           return 1(FAILURE) if it actually finds anything to output.
        '''
        return self.super_class.evaluateCommand(self, cmd)
    
    def createSummary(self, log):
        fileWarnings = []
        unmatchedFiles = []
        for line in log.readlines():
            # We want to know about files that are only in one build or the 
            # other, but we don't consider this worthy of a warning,
            # e.g. changed search plugins
            if line.startswith('Only'):
                unmatchedFiles.append(line)
                continue
            # These entries are nice to know about, but aren't fatal. We create
            # a separate warnings log for them.
            if line.startswith('> FAIL') or line.startswith('> Binary'):
                fileWarnings.append(line)
                continue

        if unmatchedFiles and len(unmatchedFiles) > 0:
            self.addCompleteLog('Only in...', "".join(unmatchedFiles))
                              
        self.setProperty('fileWarnings', fileWarnings)
        if fileWarnings and len(fileWarnings) > 0:
            self.addCompleteLog('Warnings', "".join(fileWarnings))
