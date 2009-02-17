from datetime import datetime
import os.path, re
from time import strftime

from twisted.python import log

from buildbot.process.factory import BuildFactory
from buildbot.steps.shell import Compile, ShellCommand, WithProperties, \
  SetProperty
from buildbot.steps.source import CVS, Mercurial
from buildbot.steps.transfer import FileDownload

import buildbotcustom.steps.misc
import buildbotcustom.steps.release
import buildbotcustom.steps.test
import buildbotcustom.steps.transfer
import buildbotcustom.steps.updates
import buildbotcustom.unittest.steps
import buildbotcustom.env
reload(buildbotcustom.steps.misc)
reload(buildbotcustom.steps.release)
reload(buildbotcustom.steps.test)
reload(buildbotcustom.steps.transfer)
reload(buildbotcustom.steps.updates)
reload(buildbotcustom.unittest.steps)
reload(buildbotcustom.env)

from buildbotcustom.steps.misc import SetMozillaBuildProperties, TinderboxShellCommand
from buildbotcustom.steps.release import UpdateVerify, L10nVerifyMetaDiff
from buildbotcustom.steps.test import AliveTest, CompareBloatLogs, \
  CompareLeakLogs, Codesighs, GraphServerPost
from buildbotcustom.steps.transfer import MozillaStageUpload
from buildbotcustom.steps.updates import CreateCompleteUpdateSnippet
from buildbotcustom.env import MozillaEnvironments

import buildbotcustom.unittest.steps as unittest_steps


class BootstrapFactory(BuildFactory):
    def __init__(self, automation_tag, logdir, bootstrap_config, 
                 cvsroot="pserver:anonymous@cvs-mirror.mozilla.org", 
                 cvsmodule="mozilla"):
        """
    @type  cvsroot: string
    @param cvsroot: The CVSROOT to use for checking out Bootstrap.

    @type  cvsmodule: string
    @param cvsmodule: The CVS module to use for checking out Bootstrap.

    @type  automation_tag: string
    @param automation_tag: The CVS Tag to use for checking out Bootstrap.

    @type  logdir: string
    @param logdir: The log directory for Bootstrap to use. 
                   Note - will be created if it does not already exist.

    @type  bootstrap_config: string
    @param bootstrap_config: The location of the bootstrap.cfg file on the 
                             slave. This will be copied to "bootstrap.cfg"
                             in the builddir on the slave.
        """
        BuildFactory.__init__(self)
        self.addStep(ShellCommand, 
         description='clean checkout',
         workdir='.', 
         command=['rm', '-rf', 'build'],
         haltOnFailure=1)
        self.addStep(ShellCommand, 
         description='checkout', 
         workdir='.',
         command=['cvs', '-d', cvsroot, 'co', '-r', automation_tag,
                  '-d', 'build', cvsmodule],
         haltOnFailure=1,
        )
        self.addStep(ShellCommand, 
         description='copy bootstrap.cfg',
         command=['cp', bootstrap_config, 'bootstrap.cfg'],
         haltOnFailure=1,
        )
        self.addStep(ShellCommand, 
         description='echo bootstrap.cfg',
         command=['cat', 'bootstrap.cfg'],
         haltOnFailure=1,
        )
        self.addStep(ShellCommand, 
         description='(re)create logs area',
         command=['bash', '-c', 'mkdir -p ' + logdir], 
         haltOnFailure=1,
        )

        self.addStep(ShellCommand, 
         description='clean logs area',
         command=['bash', '-c', 'rm -rf ' + logdir + '/*.log'], 
         haltOnFailure=1,
        )
        self.addStep(ShellCommand, 
         description='unit tests',
         command=['make', 'test'], 
         haltOnFailure=1,
        )


class MozillaBuildFactory(BuildFactory):
    ignore_dirs = [
            'info',
            'repo_setup',
            'tag',
            'source',
            'updates',
            'final_verification',
            'l10n_verification',
            'macosx_update_verify',
            'macosx_build',
            'macosx_repack',
            'win32_update_verify',
            'win32_build',
            'win32_repack',
            'linux_update_verify',
            'linux_build',
            'linux_repack'
            ]

    def __init__(self, hgHost, repoPath, buildToolsRepoPath, buildSpace=0,
                 clobberURL=None, clobberTime=None, buildsBeforeReboot=None,
                 **kwargs):
        BuildFactory.__init__(self, **kwargs)

        if hgHost.endswith('/'):
            hgHost = hgHost.rstrip('/')
        self.hgHost = hgHost
        self.repoPath = repoPath
        self.buildToolsRepoPath = buildToolsRepoPath
        self.buildToolsRepo = self.getRepository(buildToolsRepoPath)
        self.buildSpace = buildSpace
        self.clobberURL = clobberURL
        self.clobberTime = clobberTime
        self.buildsBeforeReboot = buildsBeforeReboot

        self.repository = self.getRepository(repoPath)
        self.branchName = self.getRepoName(self.repository)

        self.addPreBuildCleanupSteps()

    def addPreBuildCleanupSteps(self):
        self.addStep(ShellCommand,
         command=['rm', '-rf', 'tools'],
         description=['clobber', 'build tools'],
         workdir='.'
        )
        self.addStep(ShellCommand,
         command=['bash', '-c',
          'if [ ! -d tools ]; then hg clone %s; fi' % self.buildToolsRepo],
         description=['clone', 'build tools'],
         workdir='.'
        )

        if self.clobberURL is not None and self.clobberTime is not None:
            command = ['python', 'tools/clobberer/clobberer.py',
             '-t', str(self.clobberTime), '-s', 'tools',
             self.clobberURL, self.branchName,
             WithProperties("%(buildername)s"),
             WithProperties("%(slavename)s")
            ]
            self.addStep(ShellCommand,
             command=command,
             description=['checking','clobber','times'],
             workdir='.',
             flunkOnFailure=False,
             timeout=3600, # One hour, because Windows is slow
            )

        if self.buildSpace > 0:
            command = ['python', 'tools/buildfarm/maintenance/purge_builds.py',
                 '-s', str(self.buildSpace)]

            for i in self.ignore_dirs:
                command.extend(["-n", i])
            command.append("..")

            self.addStep(ShellCommand,
             command=command,
             description=['cleaning', 'old', 'builds'],
             descriptionDone=['clean', 'old', 'builds'],
             warnOnFailure=True,
             flunkOnFailure=False,
             workdir='.',
             timeout=3600, # One hour, because Windows is slow
            )

    def addPeriodicRebootSteps(self):
        self.addStep(ShellCommand,
         command=['python', 'tools/buildfarm/maintenance/count_and_reboot.py',
                  '-f', '../reboot_count.txt',
                  '-n', str(self.buildsBeforeReboot),
                  '-z'],
         description=['maybe rebooting'],
         warnOnFailure=False,
         flunkOnFailure=False,
         alwaysRun=True,
         workdir='.'
        )

    def getRepoName(self, repo):
        return repo.rstrip('/').split('/')[-1]

    def getRepository(self, repoPath, hgHost=None, push=False):
        assert repoPath
        if repoPath.startswith('/'):
            repoPath = repoPath.lstrip('/')
        if not hgHost:
            hgHost = self.hgHost
        proto = 'ssh' if push else 'http'
        return '%s://%s/%s' % (proto, hgHost, repoPath)



class MercurialBuildFactory(MozillaBuildFactory):
    def __init__(self, env, objdir, platform, configRepoPath, configSubDir,
                 profiledBuild, mozconfig, productName=None, buildRevision=None,
                 stageServer=None, stageUsername=None, stageGroup=None,
                 stageSshKey=None, stageBasePath=None, ausBaseUploadDir=None,
                 updatePlatform=None, downloadBaseURL=None, ausUser=None,
                 ausHost=None, nightly=False, leakTest=False, codesighs=True,
                 graphServer=None, graphSelector=None, graphBranch=None,
                 baseName=None, uploadPackages=True, uploadSymbols=True,
                 createSnippet=False, doCleanup=True,
                 **kwargs):
        MozillaBuildFactory.__init__(self, **kwargs)
        self.env = env
        self.objdir = objdir
        self.platform = platform
        self.configRepoPath = configRepoPath
        self.configSubDir = configSubDir
        self.profiledBuild = profiledBuild
        self.productName = productName
        self.buildRevision = buildRevision
        self.stageServer = stageServer
        self.stageUsername = stageUsername
        self.stageGroup = stageGroup
        self.stageSshKey = stageSshKey
        self.stageBasePath = stageBasePath
        self.ausBaseUploadDir = ausBaseUploadDir
        self.updatePlatform = updatePlatform
        self.downloadBaseURL = downloadBaseURL
        self.ausUser = ausUser
        self.ausHost = ausHost
        self.nightly = nightly
        self.leakTest = leakTest
        self.codesighs = codesighs
        self.graphServer = graphServer
        self.graphSelector = graphSelector
        self.graphBranch = graphBranch
        self.baseName = baseName
        self.uploadPackages = uploadPackages
        self.uploadSymbols = uploadSymbols
        self.createSnippet = createSnippet
        self.doCleanup = doCleanup

        if self.uploadPackages:
            assert productName and stageServer and stageUsername and stageSshKey
            assert stageBasePath
        if self.createSnippet:
            assert ausBaseUploadDir and updatePlatform and downloadBaseURL
            assert ausUser and ausHost

            # this is a tad ugly because we need to python interpolation
            # as well as WithProperties
            # here's an example of what it translates to:
            # /opt/aus2/build/0/Firefox/mozilla2/WINNT_x86-msvc/2008010103/en-US
            self.ausFullUploadDir = '%s/%s/%%(buildid)s/en-US' % \
              (self.ausBaseUploadDir, self.updatePlatform)

        self.configRepo = self.getRepository(self.configRepoPath)

        self.mozconfig = 'configs/%s/%s/mozconfig' % (self.configSubDir,
                                                      mozconfig)

        # we don't need the extra cruft in 'platform' anymore
        self.platform = platform.split('-')[0].replace('64', '')
        assert self.platform in ('linux', 'win32', 'macosx')

        self.logUploadDir = 'tinderbox-builds/%s-%s/' % (self.branchName,
                                                         self.platform)
        # now, generate the steps
        #  regular dep builds (no clobber, no leaktest):
        #   addBuildSteps()
        #   addUploadSteps()
        #   addCodesighsSteps()
        #  leak test builds (no clobber, leaktest):
        #   addBuildSteps()
        #   addLeakTestSteps()
        #  nightly builds (clobber)
        #   addBuildSteps()
        #   addSymbolSteps()
        #   addUploadSteps()
        #   addUpdateSteps()
        #  for all dep and nightly builds (but not release builds):
        #   addCleanupSteps()
        self.addBuildSteps()
        if self.leakTest:
            self.addLeakTestSteps()
        if self.codesighs:
            self.addCodesighsSteps()
        if self.uploadSymbols or self.uploadPackages:
            self.addBuildSymbolsStep()
        if self.uploadSymbols:
            self.addUploadSymbolsStep()
        if self.uploadPackages:
            self.addUploadSteps()
        if self.createSnippet:
            self.addUpdateSteps()
        if self.doCleanup:
            self.addCleanupSteps()
        if self.buildsBeforeReboot and self.buildsBeforeReboot > 0:
            self.addPeriodicRebootSteps()

    def addBuildSteps(self):
        if self.nightly:
            self.addStep(ShellCommand,
             command=['rm', '-rf', 'build'],
             env=self.env,
             workdir='.',
             timeout=60*60 # 1 hour
            )
        self.addStep(ShellCommand,
         command=['echo', WithProperties('Building on: %(slavename)s')],
         env=self.env
        )
        self.addStep(ShellCommand,
         command="rm -rf %s/dist/firefox-* %s/dist/install/sea/*.exe " %
                  (self.objdir, self.objdir),
         env=self.env,
         description=['deleting', 'old', 'package'],
         descriptionDone=['delete', 'old', 'package']
        )
        if self.nightly:
            self.addStep(ShellCommand,
             command="find 20* -maxdepth 2 -mtime +7 -exec rm -rf {} \;",
             env=self.env,
             workdir='.',
             description=['cleanup', 'old', 'symbols'],
             flunkOnFailure=False
            )
        self.addStep(Mercurial,
         mode='update',
         baseURL='http://%s/' % self.hgHost,
         defaultBranch=self.repoPath,
         timeout=60*60, # 1 hour
        )
        if self.buildRevision:
            self.addStep(ShellCommand,
             command=['hg', 'up', '-C', '-r', self.buildRevision],
             haltOnFailure=True
            )
            self.addStep(SetProperty,
             command=['hg', 'identify', '-i'],
             property='got_revision'
            )
        changesetLink = '<a href=http://%s/%s/index.cgi/rev' % (self.hgHost,
                                                                self.repoPath)
        changesetLink += '/%(got_revision)s title="Built from revision %(got_revision)s">rev:%(got_revision)s</a>'
        self.addStep(ShellCommand,
         command=['echo', 'TinderboxPrint:', WithProperties(changesetLink)]
        )
        self.addStep(ShellCommand,
         command=['rm', '-rf', 'configs'],
         description=['removing', 'configs'],
         descriptionDone=['remove', 'configs'],
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['hg', 'clone', self.configRepo, 'configs'],
         description=['checking', 'out', 'configs'],
         descriptionDone=['checkout', 'configs'],
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         # cp configs/mozilla2/$platform/$repo/$type/mozconfig .mozconfig
         command=['cp', self.mozconfig, '.mozconfig'],
         description=['copying', 'mozconfig'],
         descriptionDone=['copy', 'mozconfig'],
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['cat', '.mozconfig'],
        )

        buildcmd = 'build'
        if self.profiledBuild:
            buildcmd = 'profiledbuild'
        self.addStep(Compile,
         command=['make', '-f', 'client.mk', buildcmd],
         env=self.env,
         haltOnFailure=True,
         timeout=5400 # 90 minutes, because windows PGO builds take a long time
        )

    def addLeakTestSteps(self):
        # we want the same thing run a few times here, with different
        # extraArgs
        for args in [['-register'], ['-CreateProfile', 'default'],
                     ['-P', 'default']]:
            self.addStep(AliveTest,
                env=self.env,
                workdir='build/%s/_leaktest' % self.objdir,
                extraArgs=args,
                warnOnFailure=True
            )
        # we only want this variable for this test - this sucks
        bloatEnv = self.env.copy()
        bloatEnv['XPCOM_MEM_BLOAT_LOG'] = '1' 
        self.addStep(AliveTest,
         env=bloatEnv,
         workdir='build/%s/_leaktest' % self.objdir,
         logfile='bloat.log',
         warnOnFailure=True
        )
        self.addStep(ShellCommand,
         env=self.env,
         workdir='.',
         command=['wget', '-O', 'bloat.log.old',
                  'http://%s/pub/mozilla.org/firefox/%s/bloat.log' % \
                    (self.stageServer, self.logUploadDir)]
        )
        self.addStep(ShellCommand,
         env=self.env,
         command=['mv', '%s/_leaktest/bloat.log' % self.objdir,
                  '../bloat.log'],
        )
        self.addStep(ShellCommand,
         env=self.env,
         command=['scp', '-o', 'User=%s' % self.stageUsername,
                  '-o', 'IdentityFile=~/.ssh/%s' % self.stageSshKey,
                  '../bloat.log',
                  '%s:%s/%s' % (self.stageServer, self.stageBasePath,
                                self.logUploadDir)]
        )
        self.addStep(CompareBloatLogs,
         bloatLog='../bloat.log',
         env=self.env,
        )
        self.addStep(GraphServerPost,
         server=self.graphServer,
         selector=self.graphSelector,
         branch=self.graphBranch,
         resultsname=self.baseName
        )
        self.addStep(AliveTest,
         env=self.env,
         workdir='build/%s/_leaktest' % self.objdir,
         extraArgs=['--trace-malloc', 'malloc.log',
                    '--shutdown-leaks=sdleak.log'],
         timeout=3600, # 1 hour, because this takes a long time on win32
         warnOnFailure=True
        )
        self.addStep(ShellCommand,
         env=self.env,
         workdir='.',
         command=['wget', '-O', 'malloc.log.old',
                  'http://%s/pub/mozilla.org/firefox/%s/malloc.log' % \
                    (self.stageServer, self.logUploadDir)]
        )
        self.addStep(ShellCommand,
         env=self.env,
         workdir='.',
         command=['wget', '-O', 'sdleak.tree.old',
                  'http://%s/pub/mozilla.org/firefox/%s/sdleak.tree' % \
                    (self.stageServer, self.logUploadDir)]
        )
        self.addStep(ShellCommand,
         env=self.env,
         command=['mv',
                  '%s/_leaktest/malloc.log' % self.objdir,
                  '../malloc.log'],
        )
        self.addStep(ShellCommand,
         env=self.env,
         command=['mv',
                  '%s/_leaktest/sdleak.log' % self.objdir,
                  '../sdleak.log'],
        )
        self.addStep(CompareLeakLogs,
         mallocLog='../malloc.log',
         platform=self.platform,
         env=self.env,
         testname='current'
        )
        self.addStep(GraphServerPost,
         server=self.graphServer,
         selector=self.graphSelector,
         branch=self.graphBranch,
         resultsname=self.baseName
        )
        self.addStep(CompareLeakLogs,
         mallocLog='../malloc.log.old',
         platform=self.platform,
         env=self.env,
         testname='previous'
        )
        self.addStep(ShellCommand,
         env=self.env,
         workdir='.',
         command=['bash', '-c',
                  'perl build/tools/trace-malloc/diffbloatdump.pl '
                  '--depth=15 --use-address /dev/null sdleak.log '
                  '> sdleak.tree']
        )
        if self.platform in ('macosx', 'linux'):
            self.addStep(ShellCommand,
             env=self.env,
             workdir='.',
             command=['mv', 'sdleak.tree', 'sdleak.tree.raw']
            )
            self.addStep(ShellCommand,
             env=self.env,
             workdir='.',
             command=['/bin/bash', '-c', 
                      'perl '
                      'build/tools/rb/fix-%s-stack.pl '
                      'sdleak.tree.raw '
                      '> sdleak.tree' % self.platform]
            )
        self.addStep(ShellCommand,
         env=self.env,
         command=['scp', '-o', 'User=%s' % self.stageUsername,
                  '-o', 'IdentityFile=~/.ssh/%s' % self.stageSshKey,
                  '../malloc.log', '../sdleak.tree',
                  '%s:%s/%s' % (self.stageServer, self.stageBasePath,
                                self.logUploadDir)]
        )
        self.addStep(ShellCommand,
         env=self.env,
         command=['perl', 'tools/trace-malloc/diffbloatdump.pl',
                  '--depth=15', '../sdleak.tree.old', '../sdleak.tree']
        )

    def addUploadSteps(self):
        self.addStep(ShellCommand,
         command=['make', 'package'],
         env=self.env,
         workdir='build/%s' % self.objdir,
         haltOnFailure=True
        )
        if self.platform.startswith("win32"):
         self.addStep(ShellCommand,
             command=['make', 'installer'],
             env=self.env,
             workdir='build/%s' % self.objdir,
             haltOnFailure=True
         )
        if self.createSnippet:
         self.addStep(ShellCommand,
             command=['make', '-C',
                      '%s/tools/update-packaging' % self.objdir],
             env=self.env,
             haltOnFailure=True
         )
        self.addStep(SetMozillaBuildProperties,
         objdir='build/%s' % self.objdir
        )

        # Call out to a subclass to do the actual uploading
        self.doUpload()
        
    def addCodesighsSteps(self):
        self.addStep(ShellCommand,
         command=['make'],
         workdir='build/%s/tools/codesighs' % self.objdir
        )
        self.addStep(ShellCommand,
         command=['wget', '-O', 'codesize-auto-old.log',
          'http://%s/pub/mozilla.org/firefox/%s/codesize-auto.log' % \
           (self.stageServer, self.logUploadDir)],
         workdir='.',
         env=self.env
        )
        self.addStep(Codesighs,
         objdir=self.objdir,
         platform=self.platform,
         env=self.env
        )
        self.addStep(GraphServerPost,
         server=self.graphServer,
         selector=self.graphSelector,
         branch=self.graphBranch,
         resultsname=self.baseName
        )
        self.addStep(ShellCommand,
         command=['cat', '../codesize-auto-diff.log']
        )
        self.addStep(ShellCommand,
         command=['scp', '-o', 'User=%s' % self.stageUsername,
          '-o', 'IdentityFile=~/.ssh/%s' % self.stageSshKey,
          '../codesize-auto.log',
          '%s:%s/%s' % (self.stageServer, self.stageBasePath,
                        self.logUploadDir)]
        )

    def addUpdateSteps(self):
        self.addStep(CreateCompleteUpdateSnippet,
         objdir='build/%s' % self.objdir,
         milestone=self.branchName,
         baseurl='%s/nightly' % self.downloadBaseURL
        )
        self.addStep(ShellCommand,
         command=['ssh', '-l', self.ausUser, self.ausHost,
                  WithProperties('mkdir -p %s' % self.ausFullUploadDir)],
         description=['create', 'aus', 'upload', 'dir'],
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['scp', '-o', 'User=%s' % self.ausUser,
                  'dist/update/complete.update.snippet',
                  WithProperties('%s:%s/complete.txt' % \
                    (self.ausHost, self.ausFullUploadDir))],
         workdir='build/%s' % self.objdir,
         description=['upload', 'complete', 'snippet'],
         haltOnFailure=True
        )

    def addBuildSymbolsStep(self):
        self.addStep(ShellCommand,
         command=['make', 'buildsymbols'],
         env=self.env,
         workdir='build/%s' % self.objdir,
         haltOnFailure=True
        )

    def addUploadSymbolsStep(self):
        self.addStep(ShellCommand,
         command=['make', 'uploadsymbols'],
         env=self.env,
         workdir='build/%s' % self.objdir,
         haltOnFailure=True
        )

    def addCleanupSteps(self):
        if self.nightly:
            self.addStep(ShellCommand,
             command=['rm', '-rf', 'build'],
             env=self.env,
             workdir='.',
             timeout=60*60 # 1 hour
            )
            # no need to clean-up temp files if we clobber the whole directory
            return

        # OS X builds eat up a ton of space with -save-temps enabled
        # until we have dwarf support we need to clean this up so we don't
        # fill up the disk
        if self.platform.startswith("macosx"):
            # For regular OS X builds the "objdir" passed in is objdir/ppc
            # For leak test builds the "objdir" passed in is objdir.
            # To properly cleanup we need to make sure we're in 'objdir',
            # otherwise we miss the entire i386 dir in the normal case
            # We can't just run this in the srcdir because there are other files
            # most notably hg metadata which have the same extensions
            baseObjdir = self.objdir.split('/')[0]
            self.addStep(ShellCommand,
             command=['find', '-d', '-E', '.', '-iregex',
                      '.*\.(mi|i|s|mii|ii)$',
                      '-exec', 'rm', '-rf', '{}', ';'],
             workdir='build/%s' % baseObjdir
            )



class NightlyBuildFactory(MercurialBuildFactory):
    def doUpload(self):
        uploadEnv = self.env.copy()
        uploadEnv.update({'UPLOAD_HOST': self.stageServer,
                          'UPLOAD_USER': self.stageUsername,
                          'UPLOAD_TO_TEMP': '1'})
        if self.stageSshKey:
            uploadEnv['UPLOAD_SSH_KEY'] = '~/.ssh/%s' % self.stageSshKey

        # Always upload builds to the dated tinderbox builds directories
        postUploadCmd = ['/home/ffxbld/bin/post_upload.py']
        postUploadCmd += ['--tinderbox-builds-dir %s-%s' % (self.branchName,
                                                            self.platform),
                          '-i %(buildid)s',
                          '-p %s' % self.productName,
                          '--release-to-tinderbox-dated-builds']
        if self.nightly:
            # If this is a nightly build also place them in the latest and
            # dated directories in nightly/
            postUploadCmd += ['-b %s' % self.branchName,
                              '--release-to-latest',
                              '--release-to-dated']

        uploadEnv['POST_UPLOAD_CMD'] = WithProperties(' '.join(postUploadCmd))

        self.addStep(ShellCommand,
         command=['make', 'upload'],
         env=uploadEnv,
         workdir='build/%s' % self.objdir
        )



class ReleaseBuildFactory(MercurialBuildFactory):
    def __init__(self, appVersion, buildNumber, **kwargs):
        self.appVersion = appVersion
        self.buildNumber = buildNumber

        # Make sure MOZ_PKG_PRETTYNAMES is on
        kwargs['env']['MOZ_PKG_PRETTYNAMES'] = '1'
        MercurialBuildFactory.__init__(self, **kwargs)

    def doUpload(self):
        # Make sure the complete MAR has been generated
        self.addStep(ShellCommand,
            command=['make', '-C',
                     '%s/tools/update-packaging' % self.objdir],
            env=self.env,
            haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=WithProperties('echo buildID=%(buildid)s > ' + \
                                '%s_info.txt' % self.platform),
         workdir='build/%s/dist' % self.objdir
        )

        uploadEnv = self.env.copy()
        uploadEnv.update({'UPLOAD_HOST': self.stageServer,
                          'UPLOAD_USER': self.stageUsername,
                          'UPLOAD_TO_TEMP': '1',
                          'UPLOAD_EXTRA_FILES': '%s_info.txt' % self.platform})
        if self.stageSshKey:
            uploadEnv['UPLOAD_SSH_KEY'] = '~/.ssh/%s' % self.stageSshKey
        
        uploadEnv['POST_UPLOAD_CMD'] = '/home/ffxbld/bin/post_upload.py ' + \
                                       '-p %s ' % self.productName + \
                                       '-v %s ' % self.appVersion + \
                                       '-n %s ' % self.buildNumber + \
                                       '--release-to-candidates-dir'
        self.addStep(ShellCommand,
         command=['make', 'upload'],
         env=uploadEnv,
         workdir='build/%s' % self.objdir
        )



class BaseRepackFactory(MozillaBuildFactory):
    # Override ignore_dirs so that we don't delete l10n nightly builds
    # before running a l10n nightly build
    ignore_dirs = MozillaBuildFactory.ignore_dirs + [
            'mozilla-central-macosx-l10n-nightly',
            'mozilla-central-linux-l10n-nightly',
            'mozilla-central-win32-l10n-nightly',
            'mozilla-1.9.1-macosx-l10n-nightly',
            'mozilla-1.9.1-linux-l10n-nightly',
            'mozilla-1.9.1-win32-l10n-nightly',
    ]

    def __init__(self, project, l10nRepoPath, stageServer, stageUsername,
                 stageSshKey=None, **kwargs):
        MozillaBuildFactory.__init__(self, **kwargs)

        self.project = project
        self.l10nRepoPath = l10nRepoPath
        self.stageServer = stageServer
        self.stageUsername = stageUsername
        self.stageSshKey = stageSshKey

        self.addStep(ShellCommand,
         command=['sh', '-c',
                  'if [ -d '+self.branchName+'/dist/upload ]; then ' +
                  'rm -rf '+self.branchName+'/dist/upload; ' +
                  'fi'],
         description="rm dist/upload",
         workdir='build',
         haltOnFailure=True
        )

        self.addStep(ShellCommand,
         command=['sh', '-c', 'mkdir -p %s' % l10nRepoPath],
         descriptionDone='mkdir '+ l10nRepoPath,
         workdir='build',
         flunkOnFailure=False
        )
        self.addStep(ShellCommand,
         command=['sh', '-c',
          WithProperties('if [ -d '+self.branchName+' ]; then ' +
                         'hg -R '+self.branchName+' pull -r default ; ' +
                         'else ' +
                         'hg clone ' +
                         'http://'+self.hgHost+'/'+self.repoPath+' ; ' +
                         'fi ' +
                         '&& hg -R '+self.branchName+' update -r %(en_revision)s')],
         descriptionDone="en-US source",
         workdir='build/',
         timeout=30*60 # 30 minutes
        )
        self.addStep(ShellCommand,
         command=['sh', '-c',
          WithProperties('if [ -d %(locale)s ]; then ' +
                         'hg -R %(locale)s pull -r default ; ' +
                         'else ' +
                         'hg clone ' +
                         'http://'+self.hgHost+'/'+l10nRepoPath+\
                           '/%(locale)s/ ; ' +
                         'fi ' +
                         '&& hg -R %(locale)s update -r %(l10n_revision)s')],
         descriptionDone="locale source",
         workdir='build/' + l10nRepoPath
        )

        # call out to subclass hooks to do any necessary setup
        self.updateSources()
        self.getMozconfig()

        self.addStep(ShellCommand,
         command=['bash', '-c', 'autoconf-2.13'],
         haltOnFailure=True,
         descriptionDone=['autoconf'],
         workdir='build/'+self.branchName
        )
        self.addStep(ShellCommand,
         command=['bash', '-c', 'autoconf-2.13'],
         haltOnFailure=True,
         descriptionDone=['autoconf js/src'],
         workdir='build/'+self.branchName+'/js/src'
        )
        self.addStep(Compile,
         command=['sh', '--',
                  './configure', '--enable-application=browser',
                  '--with-l10n-base=../%s' % l10nRepoPath],
         description='configure',
         descriptionDone='configure done',
         haltOnFailure=True,
         workdir='build/'+self.branchName
        )
        for dir in ('nsprpub', 'config'):
            self.addStep(ShellCommand,
             command=['make'],
             workdir='build/'+self.branchName+'/'+dir,
             description=['make ' + dir],
             haltOnFailure=True
            )

        self.downloadBuilds()
        self.doRepack()

        uploadEnv = self.env.copy() # pick up any env variables in our subclass
        uploadEnv.update({
            'AB_CD': WithProperties('%(locale)s'),
            'UPLOAD_HOST': stageServer,
            'UPLOAD_USER': stageUsername,
            'UPLOAD_TO_TEMP': '1',
            'POST_UPLOAD_CMD': self.postUploadCmd # defined in subclasses
        })
        if stageSshKey:
            uploadEnv['UPLOAD_SSH_KEY'] = '~/.ssh/%s' % stageSshKey
        self.addStep(ShellCommand,
         command=['make', WithProperties('l10n-upload-%(locale)s')],
         env=uploadEnv,
         workdir='build/'+self.branchName+'/browser/locales',
         flunkOnFailure=True
        )



class NightlyRepackFactory(BaseRepackFactory):
    def __init__(self, enUSBinaryURL, **kwargs):
        self.enUSBinaryURL = enUSBinaryURL
        # Unfortunately, we can't call BaseRepackFactory.__init__() before this
        # because it needs self.postUploadCmd set
        assert 'project' in kwargs
        assert 'repoPath' in kwargs
        uploadDir = '%s-l10n' % self.getRepoName(kwargs['repoPath'])
        self.postUploadCmd = '/home/ffxbld/bin/post_upload.py ' + \
                             '-p %s ' % kwargs['project'] + \
                             '-b %s ' % uploadDir + \
                             '--release-to-latest'

        self.env = {}

        BaseRepackFactory.__init__(self, **kwargs)

    def updateSources(self):
        self.addStep(ShellCommand,
         command=['hg', 'up', '-C', '-r', 'default'],
         description='update workdir',
         workdir=WithProperties('build/' + self.l10nRepoPath + '/%(locale)s'),
         haltOnFailure=True
        )

    def getMozconfig(self):
        pass

    def downloadBuilds(self):
        self.addStep(ShellCommand,
         command=['make', 'wget-en-US'],
         descriptionDone='wget en-US',
         env={'EN_US_BINARY_URL': self.enUSBinaryURL},
         haltOnFailure=True,
         workdir='build/'+self.branchName+'/browser/locales'
        )

    def doRepack(self):
        self.addStep(ShellCommand,
         command=['make', WithProperties('installers-%(locale)s')],
         haltOnFailure=True,
         workdir='build/'+self.branchName+'/browser/locales'
        )



class ReleaseFactory(MozillaBuildFactory):
    def getCandidatesDir(self, product, version, buildNumber):
        return '/home/ftp/pub/' + product + '/nightly/' + str(version) + \
               '-candidates/build' + str(buildNumber) + '/'

    def getShippedLocales(self, sourceRepo, baseTag, appName):
        return '%s/raw-file/%s_RELEASE/%s/locales/shipped-locales' % \
                 (sourceRepo, baseTag, appName)

    def getSshKeyOption(self, hgSshKey):
        if hgSshKey:
            return '-i %s' % hgSshKey
        return hgSshKey

    def makeLongVersion(self, version):
        version = re.sub('a([0-9]+)$', ' Alpha \\1', version)
        version = re.sub('b([0-9]+)$', ' Beta \\1', version)
        version = re.sub('rc([0-9]+)$', ' RC \\1', version)
        return version



class ReleaseRepackFactory(BaseRepackFactory, ReleaseFactory):
    def __init__(self, configRepoPath, configSubDir, mozconfig, platform,
                 buildRevision, appVersion, buildNumber, **kwargs):
        self.configRepoPath = configRepoPath
        self.configSubDir = configSubDir
        self.platform = platform
        self.buildRevision = buildRevision
        self.appVersion = appVersion
        self.buildNumber = buildNumber
        self.env = {'MOZ_PKG_PRETTYNAMES': '1'} # filled out in downloadBuilds

        self.configRepo = self.getRepository(self.configRepoPath,
                                             kwargs['hgHost'])

        self.mozconfig = 'configs/%s/%s/mozconfig' % (configSubDir, mozconfig)

        assert 'project' in kwargs
        # TODO: better place to put this/call this
        self.postUploadCmd = '/home/ffxbld/bin/post_upload.py ' + \
                             '-p %s ' % kwargs['project'] + \
                             '-v %s ' % self.appVersion + \
                             '-n %s ' % self.buildNumber + \
                             '--release-to-candidates-dir'
        BaseRepackFactory.__init__(self, **kwargs)

    def updateSources(self):
        self.addStep(ShellCommand,
         command=['hg', 'up', '-C', '-r', self.buildRevision],
         workdir='build/'+self.branchName,
         description=['update %s' % self.branchName,
                      'to %s' % self.buildRevision],
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['hg', 'up', '-C', '-r', self.buildRevision],
         workdir=WithProperties('build/' + self.l10nRepoPath + '/%(locale)s'),
         description=['update to', self.buildRevision]
        )

    def getMozconfig(self):
        self.addStep(ShellCommand,
         command=['rm', '-rf', 'configs'],
         description=['remove', 'configs'],
         workdir='build/'+self.branchName,
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['hg', 'clone', self.configRepo, 'configs'],
         description=['checkout', 'configs'],
         workdir='build/'+self.branchName,
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         # cp configs/mozilla2/$platform/$branchame/$type/mozconfig .mozconfig
         command=['cp', self.mozconfig, '.mozconfig'],
         description=['copy mozconfig'],
         workdir='build/'+self.branchName,
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['cat', '.mozconfig'],
         workdir='build/'+self.branchName
        )

    def downloadBuilds(self):
        # We need to know the absolute path to the input builds when we repack,
        # so we need retrieve at run-time as a build property
        self.addStep(SetProperty,
         command=['bash', '-c', 'pwd'],
         property='srcdir',
         workdir='build/'+self.branchName
        )

        candidatesDir = 'http://%s' % self.stageServer + \
                        '/pub/mozilla.org/firefox/nightly' + \
                        '/%s-candidates/build%s' % (self.appVersion,
                                                    self.buildNumber)
        longAppVersion = self.makeLongVersion(self.appVersion)

        # This block sets platform specific data that our wget command needs.
        #  build is mapping between the local and remote filenames
        #  platformDir is the platform specific directory builds are contained
        #    in on the stagingServer.
        # This block also sets the necessary environment variables that the
        # doRepack() steps rely on to locate their source build.
        builds = {}
        platformDir = None
        if self.platform.startswith('linux'):
            platformDir = 'linux-i686'
            builds['firefox.tar.bz2'] = 'firefox-%s.tar.bz2' % self.appVersion
            self.env['ZIP_IN'] = WithProperties('%(srcdir)s/firefox.tar.bz2')
        elif self.platform.startswith('macosx'):
            platformDir = 'mac'
            builds['firefox.dmg'] = 'Firefox %s.dmg' % longAppVersion
            self.env['ZIP_IN'] = WithProperties('%(srcdir)s/firefox.dmg')
        elif self.platform.startswith('win32'):
            platformDir = 'unsigned/win32'
            builds['firefox.zip'] = 'firefox-%s.zip' % self.appVersion
            builds['firefox.exe'] = 'Firefox Setup %s.exe' % longAppVersion
            self.env['ZIP_IN'] = WithProperties('%(srcdir)s/firefox.zip')
            self.env['WIN32_INSTALLER_IN'] = \
              WithProperties('%(srcdir)s/firefox.exe')
        else:
            raise "Unsupported platform"

        for name in builds:
            self.addStep(ShellCommand,
             command=['wget', '-O', name, '--no-check-certificate',
                      '%s/%s/en-US/%s' % (candidatesDir, platformDir,
                                          builds[name])],
             workdir='build/'+self.branchName,
             haltOnFailure=True
            )
        

    def doRepack(self):
        # Because we're generating updates we need to build the libmar tools
        for dir in ('nsprpub', 'config', 'modules/libmar'):
            self.addStep(ShellCommand,
             command=['make'],
             workdir='build/'+self.branchName+'/'+dir,
             description=['make ' + dir],
             haltOnFailure=True
            )

        self.env.update({'MOZ_MAKE_COMPLETE_MAR': '1'})
        self.addStep(ShellCommand,
         command=['make', WithProperties('installers-%(locale)s')],
         env=self.env,
         haltOnFailure=True,
         workdir='build/'+self.branchName+'/browser/locales'
        )



class StagingRepositorySetupFactory(ReleaseFactory):
    """This Factory should be run at the start of a staging release run. It
       deletes and reclones all of the repositories in 'repositories'. Note that
       the staging buildTools repository should _not_ be recloned, as it is
       used by many other builders, too.
    """
    def __init__(self, username, sshKey, repositories, **kwargs):
        # MozillaBuildFactory needs the 'repoPath' argument, but we don't
        ReleaseFactory.__init__(self, repoPath='nothing', **kwargs)
        for repoPath in sorted(repositories.keys()):
            repo = self.getRepository(repoPath)
            pushRepo = self.getRepository(repoPath, push=True)
            repoName = self.getRepoName(repoPath)

            # test for existence
            command = 'wget -O- %s >/dev/null' % repo
            command += ' && '
            # if it exists, delete it
            command += 'ssh -l %s -i %s %s edit %s delete YES' % \
              (username, sshKey, self.hgHost, repoName)
            command += '; '
            # either way, try to create it again
            # this kindof sucks, but if we '&&' we can't create repositories
            # that don't already exist, which is a huge pain when adding new
            # locales or repositories.
            command += 'ssh -l %s -i %s %s clone %s %s' % \
              (username, sshKey, self.hgHost, repoName, repoPath)

            self.addStep(ShellCommand,
             command=['bash', '-c', command],
             description=['recreate', repoName],
             timeout=30*60 # 30 minutes
            )



class ReleaseTaggingFactory(ReleaseFactory):
    def __init__(self, repositories, productName, appName, appVersion,
                 milestone, baseTag, buildNumber, hgUsername, hgSshKey=None,
                 buildSpace=1.5, **kwargs):
        """Repositories looks like this:
            repositories[name]['revision']: changeset# or tag
            repositories[name]['relbranchOverride']: branch name
            repositories[name]['bumpFiles']: [filesToBump]
           eg:
            repositories['http://hg.mozilla.org/mozilla-central']['revision']:
              d6a0a4fca081
            repositories['http://hg.mozilla.org/mozilla-central']['relbranchOverride']:
              GECKO191_20080828_RELBRANCH
            repositories['http://hg.mozilla.org/mozilla-central']['bumpFiles']:
              ['client.mk', 'browser/config/version.txt',
               'browser/app/module.ver', 'config/milestone.txt']
            relbranchOverride is typically used in two situations:
             1) During a respin (buildNumber > 1) when the "release" branch has
                already been created (during build1). In cases like this all
                repositories should have the relbranch specified
             2) During non-Firefox builds. Because Seamonkey, Thunderbird, etc.
                are releases off of the same platform code as Firefox, the
                "release branch" will already exist in mozilla-central but not
                comm-central, mobile-browser, domi, etc. In cases like this,
                mozilla-central and l10n should be specified with a
                relbranchOverride and the other source repositories should NOT
                specify one.
           productName: The name of the actual *product* being shipped.
                        Examples include: firefox, thunderbird, seamonkey.
                        This is only used for the automated check-in message
                        the version bump generates.
           appName: The "application" name (NOT product name). Examples:
                    browser, suite, mailnews. It is used in version bumping
                    code and assumed to be a subdirectory of the source
                    repository being bumped. Eg, for Firefox, appName should be
                    'browser', which is a subdirectory of 'mozilla-central'.
                    For Thunderbird, it would be 'mailnews', a subdirectory
                    of 'comm-central'.
           appVersion: The current version number of the application being
                       built. Eg, 3.0.2 for Firefox, 2.0 for Seamonkey, etc.
                       This is different than the platform version. See below.
           milestone: The current version of *Gecko*. This is generally
                      along the lines of: 1.8.1.14, 1.9.0.2, etc.
           baseTag: The prefix to use for BUILD/RELEASE tags. It will be 
                    post-fixed with _BUILD$buildNumber and _RELEASE. Generally,
                    this is something like: FIREFOX_3_0_2.
           buildNumber: The current build number. If this is the first time
                        attempting a release this is 1. Other times it may be
                        higher. It is used for post-fixing tags and some
                        internal logic.
           hgUsername: The username to use when pushing changes to the remote
                       repository.
           hgSshKey: The full path to the ssh key to use (if necessary) when
                     pushing changes to the remote repository.

        """
        # MozillaBuildFactory needs the 'repoPath' argument, but we don't
        ReleaseFactory.__init__(self, repoPath='nothing', buildSpace=buildSpace,
                                **kwargs)

        # extremely basic validation, to catch really dumb configurations
        assert len(repositories) > 0, \
          'You must provide at least one repository.'
        assert productName, 'You must provide a product name (eg. firefox).'
        assert appName, 'You must provide an application name (eg. browser).'
        assert appVersion, \
          'You must provide an application version (eg. 3.0.2).'
        assert milestone, 'You must provide a milestone (eg. 1.9.0.2).'
        assert baseTag, 'You must provide a baseTag (eg. FIREFOX_3_0_2).'
        assert buildNumber, 'You must provide a buildNumber.'

        # if we're doing a respin we already have a relbranch created
        if buildNumber > 1:
            for repo in repositories:
                assert repositories[repo]['relbranchOverride'], \
                  'No relbranchOverride specified for ' + repo + \
                  '. You must provide a relbranchOverride when buildNumber > 2'

        # now, down to work
        buildTag = '%s_BUILD%s' % (baseTag, str(buildNumber))
        releaseTag = '%s_RELEASE' % baseTag

        # generate the release branch name, which is based on the
        # version and the current date.
        # looks like: GECKO191_20080728_RELBRANCH
        # This can be overridden per-repository. This case is handled
        # in the loop below
        relbranchName = 'GECKO%s_%s_RELBRANCH' % (
          milestone.replace('.', ''), datetime.now().strftime('%Y%m%d'))
                
        for repoPath in sorted(repositories.keys()):
            repoName = self.getRepoName(repoPath)
            repo = self.getRepository(repoPath)
            pushRepo = self.getRepository(repoPath, push=True)

            sshKeyOption = self.getSshKeyOption(hgSshKey)

            repoRevision = repositories[repoPath]['revision']
            bumpFiles = repositories[repoPath]['bumpFiles']

            relbranchOverride = False
            if repositories[repoPath]['relbranchOverride']:
                relbranchOverride = True
                relbranchName = repositories[repoPath]['relbranchOverride']

            # For l10n we never bump any files, so this will never get
            # overridden. For source repos, we will do a version bump in build1
            # which we commit, and set this property again, so we tag
            # the right revision. For build2, we don't version bump, and this
            # will not get overridden
            self.addStep(SetProperty,
             command=['echo', repoRevision],
             property='%s-revision' % repoName,
             workdir='.',
             haltOnFailure=True
            )
            # 'hg clone -r' breaks in the respin case because the cloned
            # repository will not have ANY changesets from the release branch
            # and 'hg up -C' will fail
            self.addStep(ShellCommand,
             command=['hg', 'clone', repo, repoName],
             workdir='.',
             description=['clone %s' % repoName],
             haltOnFailure=True,
             timeout=30*60 # 30 minutes
            )
            # for build1 we need to create a branch
            if buildNumber == 1 and not relbranchOverride:
                # remember:
                # 'branch' in Mercurial does not actually create a new branch,
                # it switches the "current" branch to the one of the given name.
                # when buildNumber == 1 this will end up creating a new branch
                # when we commit version bumps and tags.
                # note: we don't actually have to switch to the release branch
                # to create tags, but it seems like a more sensible place to
                # have those commits
                self.addStep(ShellCommand,
                 command=['hg', 'up', '-r',
                          WithProperties('%s', '%s-revision' % repoName)],
                 workdir=repoName,
                 description=['update', repoName],
                 haltOnFailure=True
                )
                self.addStep(ShellCommand,
                 command=['hg', 'branch', relbranchName],
                 workdir=repoName,
                 description=['branch %s' % repoName],
                 haltOnFailure=True
                )
            # if buildNumber > 1 we need to switch to it with 'hg up -C'
            else:
                self.addStep(ShellCommand,
                 command=['hg', 'up', '-C', relbranchName],
                 workdir=repoName,
                 description=['switch to', relbranchName],
                 haltOnFailure=True
                )
            # we don't need to do any version bumping if this is a respin
            if buildNumber == 1 and len(bumpFiles) > 0:
                command = ['perl', 'tools/release/version-bump.pl',
                           '-w', repoName, '-t', releaseTag, '-a', appName,
                           '-v', appVersion, '-m', milestone]
                command.extend(bumpFiles)
                self.addStep(ShellCommand,
                 command=command,
                 workdir='.',
                 description=['bump %s' % repoName],
                 haltOnFailure=True
                )
                self.addStep(ShellCommand,
                 command=['hg', 'diff'],
                 workdir=repoName
                )
                self.addStep(ShellCommand,
                 # mozilla-central and other developer repositories have a
                 # 'CLOSED TREE' hook on them which rejects commits when the
                 # tree is declared closed. It is very common for us to tag
                 # and branch when the tree is in this state. Adding the 
                 # 'CLOSED TREE' string at the end will force the hook to
                 # let us commit regardless of the tree state.
                 command=['hg', 'commit', '-u', hgUsername, '-m',
                          'Automated checkin: version bump remove "pre" ' + \
                          ' from version number for ' + productName + ' ' + \
                          appVersion + ' release on ' + relbranchName + ' ' + \
                          'CLOSED TREE'],
                 workdir=repoName,
                 description=['commit %s' % repoName],
                 haltOnFailure=True
                )
                self.addStep(SetProperty,
                 command=['hg', 'identify', '-i'],
                 property='%s-revision' % repoName,
                 workdir=repoName,
                 haltOnFailure=True
                )
            for tag in (buildTag, releaseTag):
                self.addStep(ShellCommand,
                 command=['hg', 'tag', '-u', hgUsername, '-f', '-r',
                          WithProperties('%s', '%s-revision' % repoName),
                          tag],
                 workdir=repoName,
                 description=['tag %s' % repoName],
                 haltOnFailure=True
                )
            self.addStep(ShellCommand,
             command=['hg', 'out', '-e',
                      'ssh -l %s %s' % (hgUsername, sshKeyOption),
                      pushRepo],
             workdir=repoName,
             description=['hg out', repoName]
            )
            self.addStep(ShellCommand,
             command=['hg', 'push', '-e',
                      'ssh -l %s %s' % (hgUsername, sshKeyOption),
                      '-f', pushRepo],
             workdir=repoName,
             description=['push %s' % repoName],
             haltOnFailure=True
            )



class SingleSourceFactory(ReleaseFactory):
    def __init__(self, productName, appVersion, baseTag, stagingServer,
                 stageUsername, stageSshKey, buildNumber, autoconfDirs=['.'],
                 buildSpace=1, **kwargs):
        ReleaseFactory.__init__(self, buildSpace=buildSpace, **kwargs)
        releaseTag = '%s_RELEASE' % (baseTag)
        bundleFile = 'source/%s-%s.bundle' % (productName, appVersion)
        sourceTarball = 'source/%s-%s-source.tar.bz2' % (productName,
                                                         appVersion)
        # '-c' is for "release to candidates dir"
        postUploadCmd = 'python ~/bin/post_upload.py -p %s -v %s -n %s -c' % \
          (productName, appVersion, buildNumber)
        uploadEnv = {'UPLOAD_HOST': stagingServer,
                     'UPLOAD_USER': stageUsername,
                     'UPLOAD_SSH_KEY': '~/.ssh/%s' % stageSshKey,
                     'UPLOAD_TO_TEMP': '1',
                     'POST_UPLOAD_CMD': postUploadCmd}

        self.addStep(ShellCommand,
         command=['rm', '-rf', 'source'],
         workdir='.',
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['mkdir', 'source'],
         workdir='.',
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['hg', 'clone', self.repository, self.branchName],
         workdir='.',
         description=['clone %s' % self.branchName],
         haltOnFailure=True,
         timeout=30*60 # 30 minutes
        )
        # This will get us to the version we're building the release with
        self.addStep(ShellCommand,
         command=['hg', 'up', '-C', '-r', releaseTag],
         workdir=self.branchName,
         description=['update to', releaseTag],
         haltOnFailure=True
        )
        # ...And this will get us the tags so people can do things like
        # 'hg up -r FIREFOX_3_1b1_RELEASE' with the bundle
        self.addStep(ShellCommand,
         command=['hg', 'up'],
         workdir=self.branchName,
         description=['update to', 'include tag revs'],
         haltOnFailure=True
        )
        self.addStep(SetProperty,
         command=['hg', 'identify', '-i'],
         property='revision',
         workdir=self.branchName,
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['hg', '-R', self.branchName, 'bundle', '--base', 'null',
                  '-r', WithProperties('%(revision)s'),
                  bundleFile],
         workdir='.',
         description=['create bundle'],
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['rm', '-rf', '.hg'],
         workdir=self.branchName,
         description=['delete metadata'],
         haltOnFailure=True
        )
        for dir in autoconfDirs:
            self.addStep(ShellCommand,
             command=['autoconf-2.13'],
             workdir='%s/%s' % (self.branchName, dir),
             haltOnFailure=True
            )
        self.addStep(ShellCommand,
         command=['tar', '-cjf', sourceTarball, self.branchName],
         workdir='.',
         description=['create tarball'],
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['python', '%s/build/upload.py' % self.branchName,
                  '--base-path', '.',
                  bundleFile, sourceTarball],
         workdir='.',
         env=uploadEnv,
         description=['upload files'],
        )



class ReleaseUpdatesFactory(ReleaseFactory):
    def __init__(self, cvsroot, patcherToolsTag, patcherConfig, baseTag,
                 appName, productName, appVersion, oldVersion, buildNumber,
                 ftpServer, bouncerServer, stagingServer, useBetaChannel,
                 stageUsername, stageSshKey, ausUser, ausHost,
                 commitPatcherConfig=True, buildSpace=13, **kwargs):
        """cvsroot: The CVSROOT to use when pulling patcher, patcher-configs,
                    Bootstrap/Util.pm, and MozBuild. It is also used when
                    commiting the version-bumped patcher config so it must have
                    write permission to the repository if commitPatcherConfig
                    is True.
           patcherToolsTag: A tag that has been applied to all of:
                              sourceRepo, buildTools, patcher,
                              MozBuild, Bootstrap.
                            This version of all of the above tools will be
                            used - NOT tip.
           patcherConfig: The filename of the patcher config file to bump,
                          and pass to patcher.
           commitPatcherConfig: This flag simply controls whether or not
                                the bumped patcher config file will be
                                commited to the CVS repository.
        """
        ReleaseFactory.__init__(self, buildSpace=buildSpace, **kwargs)

        patcherConfigFile = 'patcher-configs/%s' % patcherConfig
        shippedLocales = self.getShippedLocales(self.repository, baseTag,
                                                appName)
        candidatesDir = self.getCandidatesDir(productName, appVersion,
                                              buildNumber)
        updateDir = 'build/temp/%s/%s-%s' % \
          (productName, oldVersion, appVersion)
        marDir = '%s/ftp/%s/nightly/%s-candidates/build%s' % \
          (updateDir, productName, appVersion, buildNumber)

        # If useBetaChannel is False the unnamed snippet type will be
        # 'beta' channel snippets (and 'release' if we're into stable releases).
        # If useBetaChannel is True the unnamed type will be 'release'
        # channel snippets
        snippetTypes = ['', 'test']
        if useBetaChannel:
            snippetTypes.append('beta')

        self.addStep(CVS,
         cvsroot=cvsroot,
         branch=patcherToolsTag,
         cvsmodule='mozilla/tools/patcher'
        )
        self.addStep(ShellCommand,
         command=['cvs', '-d', cvsroot, 'co', '-r', patcherToolsTag,
                  '-d', 'MozBuild',
                  'mozilla/tools/release/MozBuild'],
         description=['checkout', 'MozBuild'],
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['cvs', '-d', cvsroot, 'co', '-r', patcherToolsTag,
                  '-d' 'Bootstrap',
                  'mozilla/tools/release/Bootstrap/Util.pm'],
         description=['checkout', 'Bootstrap/Util.pm'],
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['cvs', '-d', cvsroot, 'co', '-d' 'patcher-configs',
                  'mozilla/tools/patcher-configs'],
         description=['checkout', 'patcher-configs'],
         haltonFailure=True
        )
        self.addStep(ShellCommand,
         command=['hg', 'up', '-r', patcherToolsTag],
         description=['update', 'build tools to', patcherToolsTag],
         workdir='tools',
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['wget', '-O', 'shipped-locales', shippedLocales],
         description=['get', 'shipped-locales'],
         haltOnFailure=True
        )

        bumpCommand = ['perl', '../tools/release/patcher-config-bump.pl',
                       '-p', productName, '-v', appVersion, '-a', appVersion,
                       '-o', oldVersion, '-b', str(buildNumber),
                       '-c', patcherConfigFile, '-t', stagingServer,
                       '-f', ftpServer, '-d', bouncerServer,
                       '-l', 'shipped-locales']
        if useBetaChannel:
            bumpCommand.append('-u')
        self.addStep(ShellCommand,
         command=bumpCommand,
         description=['bump', patcherConfig],
         haltOnFailure=True
        )
        self.addStep(TinderboxShellCommand,
         command=['cvs', 'diff', '-u', patcherConfigFile],
         description=['diff', patcherConfig],
         ignoreCodes=[1]
        )
        if commitPatcherConfig:
            self.addStep(ShellCommand,
             command=['cvs', 'commit', '-m',
                      'Automated configuration bump: ' + \
                      '%s, from %s to %s build %s' % \
                        (patcherConfig, oldVersion, appVersion, buildNumber)
                     ],
             workdir='build/patcher-configs',
             description=['commit', patcherConfig],
             haltOnFailure=True
            )
        self.addStep(ShellCommand,
         command=['perl', 'patcher2.pl', '--build-tools-hg', 
                  '--tools-revision=%s' % patcherToolsTag,
                  '--app=%s' % productName,
                  '--config=%s' % patcherConfigFile],
         description=['patcher:', 'build tools'],
         env={'HGROOT': self.repository},
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['perl', 'patcher2.pl', '--download',
                  '--app=%s' % productName,
                  '--config=%s' % patcherConfigFile],
         description=['patcher:', 'download builds'],
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['perl', 'patcher2.pl', '--create-patches',
                  '--partial-patchlist-file=patchlist.cfg',
                  '--app=%s' % productName,
                  '--config=%s' % patcherConfigFile],
         description=['patcher:', 'create patches'],
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['rsync', '-av',
                  '-e', 'ssh -oIdentityFile=~/.ssh/%s' % stageSshKey,
                  '--exclude=*complete.mar',
                  'update',
                  '%s@%s:%s' % (stageUsername, stagingServer, candidatesDir)],
         workdir=marDir,
         description=['upload', 'partial mars'],
         haltOnFailure=True
        )
        # It gets a little hairy down here
        date = strftime('%Y%m%d')
        for type in snippetTypes:
            # Patcher generates an 'aus2' directory and 'aus2.snippetType'
            # directories for each snippetType. Typically, there is 'aus2',
            # 'aus2.test', and (when we're out of beta) 'aus2.beta'.
            localDir = 'aus2'
            # On the AUS server we store each type of snippet in a directory
            # named thusly, with the snippet type appended to it
            remoteDir = '%s-%s-%s' % (date, productName.title(), appVersion)
            if type != '':
                localDir = localDir + '.%s' % type
                remoteDir = remoteDir + '-%s' % type
            snippetDir = '/opt/aus2/snippets/staging/%s' % remoteDir

            self.addStep(ShellCommand,
             command=['rsync', '-av', localDir + '/',
                      '%s@%s:%s' % (ausUser, ausHost, snippetDir)],
             workdir=updateDir,
             description=['upload', '%s snippets' % type],
             haltOnFailure=True
            )

            # We only push test channel snippets from automation.
            if type == 'test':
                self.addStep(ShellCommand,
                 command=['ssh', '-l', ausUser, ausHost,
                          '~/bin/backupsnip %s' % remoteDir],
                 timeout=7200, # 2 hours
                 description=['backupsnip'],
                 haltOnFailure=True
                )
                self.addStep(ShellCommand,
                 command=['ssh', '-l', ausUser, ausHost,
                          '~/bin/pushsnip %s' % remoteDir],
                 timeout=3600, # 1 hour
                 description=['pushsnip'],
                 haltOnFailure=True
                )
                # Wait for timeout on AUS's NFS caching to expire before
                # attempting to test newly-pushed snippets
                self.addStep(ShellCommand,
                 command=['sleep','360'],
                 description=['wait for live snippets']
                )



class UpdateVerifyFactory(ReleaseFactory):
    def __init__(self, cvsroot, patcherToolsTag, hgUsername, baseTag, appName,
                 platform, productName, oldVersion, oldBuildNumber, version,
                 buildNumber, ausServerUrl, stagingServer, verifyConfig,
                 oldAppVersion=None, appVersion=None, hgSshKey=None,
                 buildSpace=.3, **kwargs):
        ReleaseFactory.__init__(self, buildSpace=buildSpace, **kwargs)

        if not oldAppVersion:
            oldAppVersion = oldVersion
        if not appVersion:
            appVersion = version

        oldLongVersion = self.makeLongVersion(oldVersion)
        longVersion = self.makeLongVersion(version)
        # Unfortunately we can't use the getCandidatesDir() function here
        # because that returns it as a file path on the server and we need
        # an http:// compatible path
        oldCandidatesDir = \
          '/pub/mozilla.org/%s/nightly/%s-candidates/build%s' % \
            (productName, oldVersion, oldBuildNumber)

        verifyConfigPath = 'release/updates/%s' % verifyConfig
        shippedLocales = self.getShippedLocales(self.repository, baseTag,
                                                appName)
        pushRepo = self.getRepository(self.buildToolsRepoPath, push=True)
        sshKeyOption = self.getSshKeyOption(hgSshKey)

        self.addStep(ShellCommand,
         command=['cvs', '-d', cvsroot, 'co', '-r', patcherToolsTag,
                  '-d', 'MozBuild',
                  'mozilla/tools/release/MozBuild'],
         description=['checkout', 'MozBuild'],
         workdir='tools',
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['cvs', '-d', cvsroot, 'co', '-r', patcherToolsTag,
                  '-d', 'Bootstrap',
                  'mozilla/tools/release/Bootstrap/Util.pm'],
         description=['checkout', 'Bootstrap/Util.pm'],
         workdir='tools',
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['wget', '-O', 'shipped-locales', shippedLocales],
         description=['get', 'shipped-locales'],
         workdir='tools',
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['perl', 'release/update-verify-bump.pl',
                  '-o', platform, '-p', productName,
                  '--old-version=%s' % oldVersion,
                  '--old-app-version=%s' % oldAppVersion,
                  '--old-long-version=%s' % oldLongVersion,
                  '-v', version, '--app-version=%s' % appVersion,
                  '--long-version=%s' % longVersion,
                  '-n', str(buildNumber), '-a', ausServerUrl,
                  '-s', stagingServer, '-c', verifyConfigPath,
                  '-d', oldCandidatesDir, '-l', 'shipped-locales',
                  '--pretty-candidates-dir'],
         description=['bump', verifyConfig],
         workdir='tools'
        )
        self.addStep(ShellCommand,
         command=['hg', 'commit', '-m',
                  'Automated configuration bump: ' + \
                  '%s, from %s to %s build %s' % \
                    (verifyConfig, oldVersion, version, buildNumber)],
         description=['commit', verifyConfig],
         workdir='tools',
         haltOnFailure=True
        )
        self.addStep(ShellCommand,
         command=['hg', 'push', '-e',
                  'ssh -l %s %s' % (hgUsername, sshKeyOption),
                  '-f', pushRepo],
         description=['push updated', 'config'],
         workdir='tools',
         haltOnFailure=True
        )
        self.addStep(UpdateVerify,
         command=['bash', 'verify.sh', '-c', verifyConfig],
         workdir='tools/release/updates',
         description=['./verify.sh', verifyConfig]
        )



class ReleaseFinalVerification(ReleaseFactory):
    def __init__(self, linuxConfig, macConfig, win32Config, **kwargs):
        # MozillaBuildFactory needs the 'repoPath' argument, but we don't
        ReleaseFactory.__init__(self, repoPath='nothing', **kwargs)
        self.addStep(ShellCommand,
         command=['bash', 'final-verification.sh',
                  linuxConfig, macConfig, win32Config],
         description=['final-verification.sh'],
         workdir='tools/release'
        )

class UnittestBuildFactory(MozillaBuildFactory):
    def __init__(self, platform, brand_name, config_repo_path, config_dir,
                 **kwargs):
        self.env = {}
        MozillaBuildFactory.__init__(self, **kwargs)
        self.config_repo_path = config_repo_path
        self.config_dir = config_dir
        self.brand_name = brand_name

        self.config_repo_url = self.getRepository(self.config_repo_path)

        env_map = {
                'linux': 'linux-centos-unittest',
                'macosx': 'mac-osx-unittest',
                'win32': 'win32-vc8-mozbuild-unittest',
                }

        config_dir_map = {
                'linux': 'linux/%s/unittest' % self.branchName,
                'macosx': 'macosx/%s/unittest' % self.branchName,
                'win32': 'win32/%s/unittest' % self.branchName,
                }

        self.platform = platform.split('-')[0].replace('64', '')
        assert self.platform in ('linux', 'win32', 'macosx')

        self.env = MozillaEnvironments[env_map[self.platform]]

        if self.platform == 'win32':
            self.addStep(TinderboxShellCommand, name="kill sh",
             description='kill sh',
             descriptionDone="killed sh",
             command="pskill -t sh.exe",
             workdir="D:\\Utilities"
            )
            self.addStep(TinderboxShellCommand, name="kill make",
             description='kill make',
             descriptionDone="killed make",
             command="pskill -t make.exe",
             workdir="D:\\Utilities"
            )
            self.addStep(TinderboxShellCommand, name="kill firefox",
             description='kill firefox',
             descriptionDone="killed firefox",
             command="pskill -t firefox.exe",
             workdir="D:\\Utilities"
            )

        self.addStep(ShellCommand,
         command=['echo', WithProperties('Building on: %(slavename)s')],
         env=self.env
        )
        self.addStepNoEnv(Mercurial, mode='update',
         baseURL='http://%s/' % self.hgHost,
         defaultBranch=self.repoPath
        )

        self.addPrintChangesetStep()

        self.addStep(ShellCommand,
         name="clean configs",
         command=['rm', '-rf', 'mozconfigs'],
         workdir='.'
        )

        self.addStep(ShellCommand,
         name="buildbot configs",
         command=['hg', 'clone', self.config_repo_url, 'mozconfigs'],
         workdir='.'
        )

        self.addStep(ShellCommand, name="copy mozconfig",
         command=['cp',
                  'mozconfigs/%s/%s/mozconfig' % \
                    (self.config_dir, config_dir_map[self.platform]),
                  'build/.mozconfig'],
         description=['copy mozconfig'],
         workdir='.'
        )

        # TODO: Do we need this special windows rule?
        if self.platform == 'win32':
            self.addStep(ShellCommand, name="mozconfig contents",
             command=["type", ".mozconfig"]
            )
        else:
            self.addStep(ShellCommand, name='mozconfig contents',
             command=['cat', '.mozconfig']
            )

        # TODO: Do we need this special windows rule?
        if self.platform == 'win32':
            self.addStep(Compile,
             command=["make", "-f", "client.mk", "build"],
             timeout=60*20,
             warningPattern=''
            )
        else:
            self.addStep(Compile,
             warningPattern='',
             command=['make', '-f', 'client.mk', 'build']
            )

        # TODO: Do we need this special windows rule?
        if self.platform == 'win32':
            self.addStep(unittest_steps.MozillaCheck, warnOnWarnings=True,
             workdir="build\\objdir",
             timeout=60*5
            )
        else:
            self.addStep(unittest_steps.MozillaCheck,
             warnOnWarnings=True,
             timeout=60*5,
             workdir="build/objdir"
            )

        if self.platform == 'win32':
            self.addStep(unittest_steps.CreateProfileWin,
             warnOnWarnings=True,
             workdir="build",
             command = r'python testing\tools\profiles\createTestingProfile.py --clobber --binary objdir\dist\bin\firefox.exe',
             clobber=True
            )
        else:
            self.addStep(unittest_steps.CreateProfile,
             warnOnWarnings=True,
             workdir="build",
             command = r'python testing/tools/profiles/createTestingProfile.py --clobber --binary objdir/dist/bin/firefox',
             clobber=True
            )

        if self.platform == 'linux':
            self.addStep(unittest_steps.MozillaUnixReftest, warnOnWarnings=True,
             workdir="build/layout/reftests",
             timeout=60*5
            )
            self.addStep(unittest_steps.MozillaUnixCrashtest, warnOnWarnings=True,
             workdir="build/testing/crashtest"
            )
            self.addStep(unittest_steps.MozillaMochitest, warnOnWarnings=True,
             workdir="build/objdir/_tests/testing/mochitest",
             timeout=60*5
            )
            self.addStep(unittest_steps.MozillaMochichrome, warnOnWarnings=True,
             workdir="build/objdir/_tests/testing/mochitest"
            )
            self.addStep(unittest_steps.MozillaBrowserChromeTest, warnOnWarnings=True,
             workdir="build/objdir/_tests/testing/mochitest"
            )
            self.addStep(unittest_steps.MozillaA11YTest, warnOnWarnings=True,
             workdir="build/objdir/_tests/testing/mochitest"
            )
        elif self.platform == 'macosx':
            self.addStep(unittest_steps.MozillaOSXReftest, brand_name=self.brand_name,
             warnOnWarnings=True, workdir="build/layout/reftests", timeout=60*5
            )
            self.addStep(unittest_steps.MozillaOSXCrashtest, brand_name=self.brand_name,
             warnOnWarnings=True, workdir="build/testing/crashtest"
            )
            self.addStep(unittest_steps.MozillaMochitest, warnOnWarnings=True,
             workdir="build/objdir/_tests/testing/mochitest",
             timeout=60*5
            )
            self.addStep(unittest_steps.MozillaMochichrome, warnOnWarnings=True,
             workdir="build/objdir/_tests/testing/mochitest"
            )
            self.addStep(unittest_steps.MozillaBrowserChromeTest, warnOnWarnings=True,
             workdir="build/objdir/_tests/testing/mochitest"
            )
        elif self.platform == 'win32':
            self.addStep(unittest_steps.MozillaWin32Reftest, warnOnWarnings=True,
             workdir="build\\layout\\reftests",
             timeout=60*5
            )
            self.addStep(unittest_steps.MozillaWin32Crashtest, warnOnWarnings=True,
             workdir="build\\testing\\crashtest"
            )
            self.addStep(unittest_steps.MozillaMochitest, warnOnWarnings=True,
             workdir="build\\objdir\\_tests\\testing\\mochitest",
             timeout=60*5
            )
            # Can use the regular build step here. Perl likes the PATHs that way anyway.
            self.addStep(unittest_steps.MozillaMochichrome, warnOnWarnings=True,
             workdir="build\\objdir\\_tests\\testing\\mochitest"
            )
            self.addStep(unittest_steps.MozillaBrowserChromeTest, warnOnWarnings=True,
             workdir="build\\objdir\\_tests\\testing\\mochitest"
            )
            self.addStep(unittest_steps.MozillaA11YTest, warnOnWarnings=True,
             workdir="build\\objdir\\_tests\\testing\\mochitest"
            )
        if self.buildsBeforeReboot and self.buildsBeforeReboot > 0:
            self.addPeriodicRebootSteps()

    def addPrintChangesetStep(self):
        changesetLink = ''.join(['<a href=http://hg.mozilla.org/',
            self.repoPath,
            '/index.cgi/rev/%(got_revision)s title="Built from revision %(got_revision)s">rev:%(got_revision)s</a>'])
        self.addStep(ShellCommand,
         command=['echo', 'TinderboxPrint:', WithProperties(changesetLink)],
        )

    def addStep(self, *args, **kw):
        kw.setdefault('env', self.env)
        return BuildFactory.addStep(self, *args, **kw)

    def addStepNoEnv(self, *args, **kw):
        return BuildFactory.addStep(self, *args, **kw)


class L10nVerifyFactory(ReleaseFactory):
    def __init__(self, cvsroot, stagingServer, productName, appVersion,
                 buildNumber, oldAppVersion, oldBuildNumber, verifyDir='verify',
                 linuxExtension='bz2', buildSpace=14, **kwargs):
        # MozillaBuildFactory needs the 'repoPath' argument, but we don't
        ReleaseFactory.__init__(self, repoPath='nothing', buildSpace=buildSpace,
                                **kwargs)

        productDir = 'build/%s/%s-%s' % (verifyDir, 
                                         productName,
                                         appVersion)
        verifyDirVersion = 'tools/release/l10n'

        # Remove existing verify dir 
        self.addStep(ShellCommand,
         description=['remove', 'verify', 'dir'],
         descriptionDone=['removed', 'verify', 'dir'],
         command=['rm', '-rf', verifyDir],
         haltOnFailure=True,
        )

        self.addStep(ShellCommand,
         description=['(re)create', 'verify', 'dir'],
         descriptionDone=['(re)created', 'verify', 'dir'],
         command=['bash', '-c', 'mkdir -p ' + verifyDirVersion], 
         haltOnFailure=True,
        )
        
        # Download current release
        self.addStep(ShellCommand,
         description=['download', 'current', 'release'],
         descriptionDone=['downloaded', 'current', 'release'],
         command=['rsync',
                  '-Lav', 
                  '-e', 'ssh', 
                  '--exclude=*.asc',
                  '--exclude=source',
                  '--exclude=xpi',
                  '--exclude=unsigned',
                  '--exclude=update',
                  '%s:/home/ftp/pub/%s/nightly/%s-candidates/build%s/*' %
                   (stagingServer, productName, appVersion, str(buildNumber)),
                  '%s-%s-build%s/' % (productName, 
                                      appVersion, 
                                      str(buildNumber))
                  ],
         workdir=verifyDirVersion,
         haltOnFailure=True,
         timeout=60*60
        )

        # Download previous release
        self.addStep(ShellCommand,
         description=['download', 'previous', 'release'],
         descriptionDone =['downloaded', 'previous', 'release'],
         command=['rsync',
                  '-Lav', 
                  '-e', 'ssh', 
                  '--exclude=*.asc',
                  '--exclude=source',
                  '--exclude=xpi',
                  '--exclude=unsigned',
                  '--exclude=update',
                  '%s:/home/ftp/pub/%s/nightly/%s-candidates/build%s/*' %
                   (stagingServer, 
                    productName, 
                    oldAppVersion,
                    str(oldBuildNumber)),
                  '%s-%s-build%s/' % (productName, 
                                      oldAppVersion,
                                      str(oldBuildNumber))
                  ],
         workdir=verifyDirVersion,
         haltOnFailure=True,
         timeout=60*60
        )

        currentProduct = '%s-%s-build%s' % (productName, 
                                            appVersion,
                                            str(buildNumber))
        previousProduct = '%s-%s-build%s' % (productName, 
                                             oldAppVersion,
                                             str(oldBuildNumber))

        for product in [currentProduct, previousProduct]:
            self.addStep(ShellCommand,
                         description=['(re)create', 'product', 'dir'],
                         descriptionDone=['(re)created', 'product', 'dir'],
                         command=['bash', '-c', 'mkdir -p %s/%s' % (verifyDirVersion, product)], 
                         workdir='.',
                         haltOnFailure=True,
                        )
            self.addStep(ShellCommand,
                         description=['verify', 'l10n', product],
                         descriptionDone=['verified', 'l10n', product],
                         command=["bash", "-c", 
                                  "./verify_l10n.sh " + product],
                         workdir=verifyDirVersion,
                         haltOnFailure=True,
                        )

        self.addStep(L10nVerifyMetaDiff,
                     currentProduct=currentProduct,
                     previousProduct=previousProduct,
                     workdir=verifyDirVersion,
                     )



class MobileBuildFactory(MozillaBuildFactory):
    def __init__(self, configRepoPath, mobileRepoPath, platform,
                 configSubDir, mozconfig, objdir="objdir",
                 stageUsername=None, stageSshKey=None, stageServer=None,
                 stageBasePath=None, stageGroup=None,
                 patchRepoPath=None, baseWorkDir='build',
                 **kwargs):
        """
    mobileRepoPath: the path to the mobileRepo (mobile-browser)
    platform: the mobile platform (linux-arm, wince-arm)
    baseWorkDir: the path to the default slave workdir
        """
        MozillaBuildFactory.__init__(self, **kwargs)
        self.platform = platform
        self.configRepository = self.getRepository(configRepoPath)
        self.mobileRepository = self.getRepository(mobileRepoPath)
        self.mobileBranchName = self.getRepoName(self.mobileRepository)
        self.configSubDir = configSubDir
        self.mozconfig = mozconfig
        self.stageUsername = stageUsername
        self.stageSshKey = stageSshKey
        self.stageServer = stageServer
        self.stageBasePath = stageBasePath
        self.stageGroup = stageGroup
        self.baseWorkDir = baseWorkDir
        self.objdir = objdir
        self.mozconfig = 'configs/%s/%s/mozconfig' % (self.configSubDir,
                                                      self.mozconfig)

    def addHgPullSteps(self, repository=None, patchRepository=None,
                       targetDirectory=None, workdir=None,
                       cloneTimeout=60*20):
        assert (repository and workdir)
        if (targetDirectory == None):
            targetDirectory = self.getRepoName(repository)

        self.addStep(ShellCommand,
            command=['bash', '-c',
                     'if [ ! -d %s ]; then hg clone %s %s; fi' %
                     (targetDirectory, repository, targetDirectory)],
            workdir=workdir,
            description=['checking', 'out', targetDirectory],
            descriptionDone=['checked', 'out', targetDirectory],
            timeout=cloneTimeout
        )
        # TODO: Remove when we no longer need mq
        if patchRepository:
            self.addStep(ShellCommand,
                command=['hg', 'revert', 'nsprpub/configure'],
                workdir='%s/%s' % (workdir, targetDirectory),
                description=['reverting', 'nsprpub'],
                descriptionDone=['reverted', 'nsprpub']
            )
            self.addStep(ShellCommand,
                command=['hg', 'qpop', '-a'],
                workdir='%s/%s' % (workdir, targetDirectory),
                description=['backing', 'out', 'patches'],
                descriptionDone=['backed', 'out', 'patches'],
                haltOnFailure=True
            )
        self.addStep(ShellCommand,
            command=['hg', 'pull', '-u'],
            workdir="%s/%s" % (workdir, targetDirectory),
            description=['updating', targetDirectory],
            descriptionDone=['updated', targetDirectory],
            haltOnFailure=True
        )
        # TODO: Remove when we no longer need mq
        if patchRepository:
            self.addHgPullSteps(repository=patchRepository,
                                workdir='%s/%s/.hg' %
                                (workdir, targetDirectory),
                                targetDirectory='patches')
            self.addStep(ShellCommand,
                command=['hg', 'qpush', '-a'],
                workdir='%s/%s' % (workdir, targetDirectory),
                description=['applying', 'patches'],
                descriptionDone=['applied', 'patches'],
                haltOnFailure=True
            )

    def getMozconfig(self):
        self.addHgPullSteps(repository=self.configRepository,
                            workdir=self.baseWorkDir,
                            targetDirectory='configs')
        self.addStep(ShellCommand,
            command=['cp', self.mozconfig,
                     '%s/.mozconfig' % self.branchName],
            workdir=self.baseWorkDir,
            description=['copying', 'mozconfig'],
            descriptionDone=['copied', 'mozconfig'],
            haltOnFailure=True
        )
        self.addStep(ShellCommand,
            command=['cat', '.mozconfig'],
            workdir='%s/%s' % (self.baseWorkDir, self.branchName),
            description=['cat', 'mozconfig']
        )

    def addUploadSteps(self, platform):
        self.addStep(SetProperty,
            command=['python', 'config/printconfigsetting.py',
                     '%s/mobile/dist/bin/application.ini' % self.objdir,
                     'App', 'BuildID'],
            property='buildid',
            workdir='%s/%s' % (self.baseWorkDir, self.branchName),
            description=['getting', 'buildid'],
            descriptionDone=['got', 'buildid']
        )
        self.addStep(MozillaStageUpload,
            objdir="%s/%s" % (self.branchName, self.objdir),
            username=self.stageUsername,
            milestone=self.mobileBranchName,
            remoteHost=self.stageServer,
            remoteBasePath=self.stageBasePath,
            platform=platform,
            group=self.stageGroup,
            packageGlob=self.packageGlob,
            sshKey=self.stageSshKey,
            uploadCompleteMar=False,
            releaseToLatest=True,
            releaseToDated=False,
            releaseToTinderboxBuilds=True,
            tinderboxBuildsDir='%s-%s' % (self.mobileBranchName,
                                          self.platform),
            dependToDated=True,
            workdir='%s/%s/%s' % (self.baseWorkDir, self.branchName,
                                        self.objdir)
        )


class MaemoBuildFactory(MobileBuildFactory):
    def __init__(self, scratchboxPath="/scratchbox/moz_scratchbox",
                 packageGlob="mobile/dist/*.tar.bz2 " +
                 "xulrunner/xulrunner/*.deb mobile/mobile/*.deb",
                 **kwargs):
        MobileBuildFactory.__init__(self, **kwargs)
        self.packageGlob = packageGlob
        self.scratchboxPath = scratchboxPath

        self.addPrecleanSteps()
        self.addHgPullSteps(repository=self.repository,
                            workdir=self.baseWorkDir,
                            cloneTimeout=60*30)
        self.addHgPullSteps(repository=self.mobileRepository,
                            workdir='%s/%s' % (self.baseWorkDir,
                                               self.branchName),
                            targetDirectory='mobile')
        self.getMozconfig()
        self.addBuildSteps()
        self.addPackageSteps()
        self.addUploadSteps(platform='linux')

    def addPrecleanSteps(self):
        self.addStep(ShellCommand,
            command = 'rm -f /tmp/*_cltbld.log',
            description=['removing', 'logfile'],
            descriptionDone=['removed', 'logfile']
        )
        self.addStep(ShellCommand,
            command=['bash', '-c', 'rm -rf %s/%s/mobile/dist/fennec* ' %
                     (self.branchName, self.objdir) +
                     '%s/%s/xulrunner/xulrunner/*.deb ' %
                     (self.branchName, self.objdir) +
                     '%s/%s/mobile/mobile/*.deb' %
                     (self.branchName, self.objdir)],
            workdir=self.baseWorkDir,
            description=['removing', 'old', 'builds'],
            descriptionDone=['removed', 'old', 'builds']
        )

    def addBuildSteps(self):
        self.addStep(Compile,
            command=[self.scratchboxPath, '-p', '-d',
                     'build/%s' % self.branchName,
                     'make -f client.mk build'],
            env={'PKG_CONFIG_PATH': '/usr/lib/pkgconfig:/usr/local/lib/pkgconfig'},
            haltOnFailure=True
        )

    def addPackageSteps(self):
        self.addStep(ShellCommand,
            command=[self.scratchboxPath, '-p', '-d',
                     'build/%s/%s/mobile' % (self.branchName, self.objdir),
                     'make package'],
            description=['make', 'package'],
            haltOnFailure=True
        )
        self.addStep(ShellCommand,
            command=[self.scratchboxPath, '-p', '-d',
                     'build/%s/%s/mobile' % (self.branchName,
                                                self.objdir),
                     'make deb'],
            description=['make', 'mobile', 'deb'],
            haltOnFailure=True
        )
        self.addStep(ShellCommand,
            command=[self.scratchboxPath, '-p', '-d',
                     'build/%s/%s/xulrunner' % (self.branchName,
                                                self.objdir),
                     'make deb'],
            description=['make', 'xulrunner', 'deb'],
            haltOnFailure=True
        )

class WinceBuildFactory(MobileBuildFactory):
    def __init__(self, patchRepoPath=None,
                 packageGlob="xulrunner/dist/*.zip mobile/dist/*.zip",
                 **kwargs):
        MobileBuildFactory.__init__(self, **kwargs)
        self.packageGlob = packageGlob
        self.patchRepository = None
        if patchRepoPath:
            self.patchRepository = self.getRepository(patchRepoPath)

        self.addPrecleanSteps()
        self.addHgPullSteps(repository=self.repository,
                            workdir=self.baseWorkDir,
                            cloneTimeout=60*30,
                            patchRepository=self.patchRepository)
        self.addHgPullSteps(repository=self.mobileRepository,
                            workdir='%s/%s' % (self.baseWorkDir,
                                               self.branchName),
                            targetDirectory='mobile')
        self.getMozconfig()
        self.addBuildSteps()
        self.addPackageSteps()
        self.addUploadSteps(platform='win32')

    def addPrecleanSteps(self):
        self.addStep(ShellCommand,
            command = ['bash', '-c', 'rm -rf %s/%s/mobile/dist/*.zip ' %
                       (self.branchName, self.objdir) +
                       '%s/%sxulrunner/dist/*.zip' %
                       (self.branchName, self.objdir)],
            workdir=self.baseWorkDir,
            description=['removing', 'old', 'builds'],
            descriptionDone=['removed', 'old', 'builds']
        )

    def addBuildSteps(self):
        self.addStep(SetProperty,
            command=['bash', '-c', 'pwd'],
            property='topsrcdir',
            workdir='%s/%s' % (self.baseWorkDir, self.branchName),
            description=['getting', 'pwd'],
            descriptionDone=['got', 'pwd']
        )
        self.addStep(Compile,
            command=['make', '-f', 'client.mk', 'build'],
            workdir='%s/%s' % (self.baseWorkDir, self.branchName),
            env={'TOPSRCDIR': WithProperties('%s', 'topsrcdir')},
            haltOnFailure=True
        )

    def addPackageSteps(self):
        self.addStep(ShellCommand,
            command=['make', 'package'],
            workdir='%s/%s/%s/mobile' % (self.baseWorkDir, self.branchName,
                                         self.objdir),
            env={'MOZ_PKG_FORMAT': 'ZIP'},
            description=['make', 'mobile', 'package'],
            haltOnFailure=True
        )
        self.addStep(ShellCommand,
            command=['make', 'package'],
            workdir='%s/%s/%s/xulrunner' % (self.baseWorkDir,
                                            self.branchName, self.objdir),
            env={'MOZ_PKG_FORMAT': 'ZIP'},
            description=['make', 'xulrunner', 'package'],
            haltOnFailure=True
        )
