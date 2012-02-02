from __future__ import absolute_import

from datetime import datetime
import os.path, re
import urllib
import random

from twisted.python import log

from buildbot.process.buildstep import regex_log_evaluator
from buildbot.process.factory import BuildFactory
from buildbot.steps.shell import WithProperties
from buildbot.steps.transfer import FileDownload, JSONPropertiesDownload, JSONStringDownload
from buildbot.steps.dummy import Dummy
from buildbot import locks
from buildbot.status.builder import worst_status

import buildbotcustom.common
import buildbotcustom.status.errors
import buildbotcustom.steps.base
import buildbotcustom.steps.misc
import buildbotcustom.steps.release
import buildbotcustom.steps.source
import buildbotcustom.steps.test
import buildbotcustom.steps.transfer
import buildbotcustom.steps.updates
import buildbotcustom.steps.talos
import buildbotcustom.steps.unittest
import buildbotcustom.steps.signing
import buildbotcustom.env
import buildbotcustom.misc_scheduler
import build.paths
import release.info
import release.paths
reload(buildbotcustom.common)
reload(buildbotcustom.status.errors)
reload(buildbotcustom.steps.base)
reload(buildbotcustom.steps.misc)
reload(buildbotcustom.steps.release)
reload(buildbotcustom.steps.source)
reload(buildbotcustom.steps.test)
reload(buildbotcustom.steps.transfer)
reload(buildbotcustom.steps.updates)
reload(buildbotcustom.steps.talos)
reload(buildbotcustom.steps.unittest)
reload(buildbotcustom.steps.signing)
reload(buildbotcustom.env)
reload(build.paths)
reload(release.info)
reload(release.paths)

from buildbotcustom.status.errors import purge_error, global_errors, \
  upload_errors
from buildbotcustom.steps.base import ShellCommand, SetProperty, Mercurial, \
  Trigger, RetryingShellCommand, RetryingSetProperty
from buildbotcustom.steps.misc import TinderboxShellCommand, SendChangeStep, \
  GetBuildID, MozillaClobberer, FindFile, DownloadFile, UnpackFile, \
  SetBuildProperty, DisconnectStep, OutputStep, ScratchboxCommand, \
  RepackPartners, UnpackTest, FunctionalStep, setBuildIDProps, \
  RetryingScratchboxProperty
from buildbotcustom.steps.release import UpdateVerify, L10nVerifyMetaDiff, \
  SnippetComparison
from buildbotcustom.steps.source import MercurialCloneCommand
from buildbotcustom.steps.test import AliveTest, \
  CompareLeakLogs, Codesighs, GraphServerPost
from buildbotcustom.steps.updates import CreateCompleteUpdateSnippet, \
  CreatePartialUpdateSnippet
from buildbotcustom.steps.signing import SigningServerAuthenication
from buildbotcustom.env import MozillaEnvironments
from buildbotcustom.common import getSupportedPlatforms, getPlatformFtpDir, \
  genBuildID, reallyShort

import buildbotcustom.steps.unittest as unittest_steps

import buildbotcustom.steps.talos as talos_steps
from buildbot.status.builder import SUCCESS, FAILURE

from release.paths import makeCandidatesDir

# limit the number of clones of the try repository so that we don't kill
# dm-vcview04 if the master is restarted, or there is a large number of pushes
hg_try_lock = locks.MasterLock("hg_try_lock", maxCount=20)

hg_l10n_lock = locks.MasterLock("hg_l10n_lock", maxCount=20)

SIGNING_SERVER_CERT = os.path.join(
    os.path.dirname(build.paths.__file__),
    '../../../release/signing/host.cert')

class DummyFactory(BuildFactory):
    def __init__(self, delay=5, triggers=None):
        BuildFactory.__init__(self)
        self.addStep(Dummy(delay))
        if triggers:
            self.addStep(Trigger(
                schedulerNames=triggers,
                waitForFinish=False,
                ))

def makeDummyBuilder(name, slaves, category=None, delay=0, triggers=None):
    builder = {
            'name': name,
            'factory': DummyFactory(delay, triggers),
            'builddir': name,
            'slavenames': slaves,
            }
    if category:
        builder['category'] = category
    return builder

def postUploadCmdPrefix(upload_dir=None,
        branch=None,
        product=None,
        revision=None,
        version=None,
        who=None,
        builddir=None,
        buildid=None,
        buildNumber=None,
        to_tinderbox_dated=False,
        to_tinderbox_builds=False,
        to_dated=False,
        to_latest=False,
        to_try=False,
        to_shadow=False,
        to_candidates=False,
        to_mobile_candidates=False,
        nightly_dir=None,
        as_list=True,
        signed=False,
        ):
    """Returns a post_upload.py command line for the given arguments.

    If as_list is True (the default), the command line will be returned as a
    list of arguments.  Some arguments may be WithProperties instances.

    If as_list is False, the command will be returned as a WithProperties
    instance representing the entire command line as a single string.

    It is expected that the returned value is augmented with the list of files
    to upload, and where to upload it.
    """

    cmd = ["post_upload.py"]

    if upload_dir:
        cmd.extend(["--tinderbox-builds-dir", upload_dir])
    if branch:
        cmd.extend(["-b", branch])
    if product:
        cmd.extend(["-p", product])
    if buildid:
        cmd.extend(['-i', buildid])
    if buildNumber:
        cmd.extend(['-n', buildNumber])
    if version:
        cmd.extend(['-v', version])
    if revision:
        cmd.extend(['--revision', revision])
    if who:
        cmd.extend(['--who', who])
    if builddir:
        cmd.extend(['--builddir', builddir])
    if to_tinderbox_dated:
        cmd.append('--release-to-tinderbox-dated-builds')
    if to_tinderbox_builds:
        cmd.append('--release-to-tinderbox-builds')
    if to_try:
        cmd.append('--release-to-try-builds')
    if to_latest:
        cmd.append("--release-to-latest")
    if to_dated:
        cmd.append("--release-to-dated")
    if to_shadow:
        cmd.append("--release-to-shadow-central-builds")
    if to_candidates:
        cmd.append("--release-to-candidates-dir")
    if to_mobile_candidates:
        cmd.append("--release-to-mobile-candidates-dir")
    if nightly_dir:
        cmd.append("--nightly-dir=%s" % nightly_dir)
    if signed:
        cmd.append("--signed")

    if as_list:
        return cmd
    else:
        # Remove WithProperties instances and replace them with their fmtstring
        for i,a in enumerate(cmd):
            if isinstance(a, WithProperties):
                cmd[i] = a.fmtstring
        return WithProperties(' '.join(cmd))

def parse_make_upload(rc, stdout, stderr):
    ''' This function takes the output and return code from running
    the upload make target and returns a dictionary of important
    file urls.'''
    retval = {}
    for m in re.findall("^(https?://.*?\.(?:tar\.bz2|dmg|zip|apk|rpm))",
                        "\n".join([stdout, stderr]), re.M):
        if 'devel' in m and m.endswith('.rpm'):
            retval['develRpmUrl'] = m
        elif 'tests' in m and m.endswith('.rpm'):
            retval['testsRpmUrl'] = m
        elif m.endswith('.rpm'):
            retval['packageRpmUrl'] = m
        elif m.endswith("crashreporter-symbols.zip"):
            retval['symbolsUrl'] = m
        elif m.endswith("tests.tar.bz2") or m.endswith("tests.zip"):
            retval['testsUrl'] = m
        elif m.endswith('apk') and 'unsigned-unaligned' in m:
            retval['unsignedApkUrl'] = m
        elif m.endswith('apk') and 'robocop' in m:
            retval['robocopApkUrl'] = m
        elif 'jsshell-' in m and m.endswith('.zip'):
            retval['jsshellUrl'] = m
        else:
            retval['packageUrl'] = m
    return retval

def short_hash(rc, stdout, stderr):
    ''' This function takes an hg changeset id and returns just the first 12 chars'''
    retval = {}
    retval['got_revision'] = stdout[:12]
    return retval

def get_signing_cmd(signingServers, python):
    if not python:
        python = 'python'
    cmd = [
        python,
        '%(toolsdir)s/release/signing/signtool.py',
        '--cachedir', '%(basedir)s/signing_cache',
        '-t', '%(basedir)s/build/token',
        '-n', '%(basedir)s/build/nonce',
        '-c', '%(toolsdir)s/release/signing/host.cert',
    ]
    for ss, user, passwd in signingServers:
        cmd.extend(['-H', ss])
    return ' '.join(cmd)

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
        self.addStep(ShellCommand(
         name='rm_builddir',
         description='clean checkout',
         workdir='.',
         command=['rm', '-rf', 'build'],
         haltOnFailure=1))
        self.addStep(RetryingShellCommand(
         name='checkout',
         description='checkout',
         workdir='.',
         command=['cvs', '-d', cvsroot, 'co', '-r', automation_tag,
                  '-d', 'build', cvsmodule],
         haltOnFailure=1,
        ))
        self.addStep(ShellCommand(
         name='copy_bootstrap',
         description='copy bootstrap.cfg',
         command=['cp', bootstrap_config, 'bootstrap.cfg'],
         haltOnFailure=1,
        ))
        self.addStep(ShellCommand(
         name='echo_bootstrap',
         description='echo bootstrap.cfg',
         command=['cat', 'bootstrap.cfg'],
         haltOnFailure=1,
        ))
        self.addStep(ShellCommand(
         name='create_logdir',
         description='(re)create logs area',
         command=['bash', '-c', 'mkdir -p ' + logdir],
         haltOnFailure=1,
        ))

        self.addStep(ShellCommand(
         name='rm_old_logs',
         description='clean logs area',
         command=['bash', '-c', 'rm -rf ' + logdir + '/*.log'],
         haltOnFailure=1,
        ))
        self.addStep(ShellCommand(
         name='make_test',
         description='unit tests',
         command=['make', 'test'],
         haltOnFailure=1,
        ))

def getPlatformMinidumpPath(platform):
    platform_minidump_path = {
        'linux': WithProperties('%(toolsdir:-)s/breakpad/linux/minidump_stackwalk'),
        'linuxqt': WithProperties('%(toolsdir:-)s/breakpad/linux/minidump_stackwalk'),
        'linux64': WithProperties('%(toolsdir:-)s/breakpad/linux64/minidump_stackwalk'),
        'win32': WithProperties('%(toolsdir:-)s/breakpad/win32/minidump_stackwalk.exe'),
        'win64': WithProperties('%(toolsdir:-)s/breakpad/win64/minidump_stackwalk.exe'),
        'macosx': WithProperties('%(toolsdir:-)s/breakpad/osx/minidump_stackwalk'),
        'macosx64': WithProperties('%(toolsdir:-)s/breakpad/osx64/minidump_stackwalk'),
        # Android uses OSX because the Foopies are OSX.
        'linux-android': WithProperties('%(toolsdir:-)s/breakpad/osx/minidump_stackwalk'),
        'android': WithProperties('%(toolsdir:-)s/breakpad/osx/minidump_stackwalk'),
        'android-xul': WithProperties('%(toolsdir:-)s/breakpad/osx/minidump_stackwalk'),
        }
    return platform_minidump_path[platform]

class RequestSortingBuildFactory(BuildFactory):
    """Base class used for sorting build requests according to buildid.

    In most cases the buildid of the request is calculated at the time when the
    build is scheduled.  For tests, the buildid of the sendchange corresponds
    to the buildid of the build. Sorting the test requests by buildid allows us
    to order them according to the order in which the builds were scheduled.
    This avoids the problem where build A of revision 1 completes (and triggers
    tests) after build B of revision 2. Without the explicit sorting done here,
    the test requests would be sorted [r2, r1], and buildbot would choose the
    latest of the set to run.

    We sort according to the following criteria:
        * If the request looks like a rebuild, use the request's submission time
        * If the request or any of the changes contains a 'buildid' property,
          use the greatest of these property values
        * Otherwise use the request's submission time
    """
    def newBuild(self, requests):
        def sortkey(request):
            # Ignore any buildids if we're rebuilding
            # Catch things like "The web-page 'rebuild' ...", or self-serve
            # messages, "Rebuilt by ..."
            if 'rebuil' in request.reason.lower():
                return int(genBuildID(request.submittedAt))

            buildids = []

            props = [request.properties] + [c.properties for c in request.source.changes]

            for p in props:
                try:
                    buildids.append(int(p['buildid']))
                except:
                    pass

            if buildids:
                return max(buildids)
            return int(genBuildID(request.submittedAt))

        try:
            sorted_requests = sorted(requests, key=sortkey)
            return BuildFactory.newBuild(self, sorted_requests)
        except:
            # Something blew up!
            # Return the orginal list
            log.msg("Error sorting build requests")
            log.err()
            return BuildFactory.newBuild(self, requests)

class MozillaBuildFactory(RequestSortingBuildFactory):
    ignore_dirs = [ 'info', 'rel-*']

    def __init__(self, hgHost, repoPath, buildToolsRepoPath, buildSpace=0,
            clobberURL=None, clobberTime=None, buildsBeforeReboot=None,
            branchName=None, baseWorkDir='build', hashType='sha512',
            baseMirrorUrls=None, baseBundleUrls=None, signingServers=None,
            enableSigning=True, env={}, **kwargs):
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
        self.baseWorkDir = baseWorkDir
        self.hashType = hashType
        self.baseMirrorUrls = baseMirrorUrls
        self.baseBundleUrls = baseBundleUrls
        self.signingServers = signingServers
        self.enableSigning = enableSigning
        self.env = env.copy()

        self.repository = self.getRepository(repoPath)
        if branchName:
          self.branchName = branchName
        else:
          self.branchName = self.getRepoName(self.repository)

        if self.signingServers and self.enableSigning:
            self.signing_command = get_signing_cmd(
                self.signingServers, self.env.get('PYTHON26'))
            self.env['MOZ_SIGN_CMD'] = WithProperties(self.signing_command)

        self.addStep(OutputStep(
         name='get_buildername',
         data=WithProperties('Building on: %(slavename)s'),
        ))
        self.addStep(OutputStep(
         name='tinderboxprint_buildername',
         data=WithProperties('TinderboxPrint: s: %(slavename)s'),
        ))
        self.addStep(OutputStep(
         name='tinderboxsummarymessage_buildername',
         data=WithProperties('TinderboxSummaryMessage: s: %(slavename)s'),
        ))
        self.addInitialSteps()

    def addInitialSteps(self):
        self.addStep(SetProperty(
            name='set_basedir',
            command=['bash', '-c', 'pwd'],
            property='basedir',
            workdir='.',
        ))
        # We need the basename of the current working dir so we can
        # ignore that dir when purging builds later.
        self.addStep(SetProperty(
            name='set_builddir',
            command=['bash', '-c', 'basename "$PWD"'],
            property='builddir',
            workdir='.',
        ))
        self.addStep(ShellCommand(
         name='rm_buildtools',
         command=['rm', '-rf', 'tools'],
         description=['clobber', 'build tools'],
         workdir='.'
        ))
        self.addStep(MercurialCloneCommand(
         name='clone_buildtools',
         command=['hg', 'clone', self.buildToolsRepo, 'tools'],
         description=['clone', 'build tools'],
         workdir='.',
         retry=False
        ))
        self.addStep(SetProperty(
            name='set_toolsdir',
            command=['bash', '-c', 'pwd'],
            property='toolsdir',
            workdir='tools',
        ))

        if self.clobberURL is not None:
            self.addStep(MozillaClobberer(
             name='checking_clobber_times',
             branch=self.branchName,
             clobber_url=self.clobberURL,
             clobberer_path=WithProperties('%(builddir)s/tools/clobberer/clobberer.py'),
             clobberTime=self.clobberTime
            ))

        if self.buildSpace > 0:
            command = ['python', 'tools/buildfarm/maintenance/purge_builds.py',
                 '-s', str(self.buildSpace)]

            for i in self.ignore_dirs:
                command.extend(["-n", i])

            # These are the base_dirs that get passed to purge_builds.py.
            # The scratchbox dir is only present on linux slaves, but since
            # not all classes that inherit from MozillaBuildFactory provide
            # a platform property we can use for limiting the base_dirs, it
            # is easier to include scratchbox by default and simply have
            # purge_builds.py skip the dir when it isn't present.
            command.extend(["..","/scratchbox/users/cltbld/home/cltbld/build"])

            def parse_purge_builds(rc, stdout, stderr):
                properties = {}
                for stream in (stdout, stderr):
                    m = re.search('unable to free (?P<size>[.\d]+) (?P<unit>\w+) ', stream, re.M)
                    if m:
                        properties['purge_target'] = '%s%s' % (m.group('size'), m.group('unit'))
                    m = None
                    m = re.search('space only (?P<size>[.\d]+) (?P<unit>\w+)', stream, re.M)
                    if m:
                        properties['purge_actual'] = '%s%s' % (m.group('size'), m.group('unit'))
                    m = None
                    m = re.search('(?P<size>[.\d]+) (?P<unit>\w+) of space available', stream, re.M)
                    if m:
                        properties['purge_actual'] = '%s%s' % (m.group('size'), m.group('unit'))
                if not properties.has_key('purge_target'):
                    properties['purge_target'] = '%sGB' % str(self.buildSpace)
                return properties

            self.addStep(SetProperty(
             name='clean_old_builds',
             command=command,
             description=['cleaning', 'old', 'builds'],
             descriptionDone=['clean', 'old', 'builds'],
             haltOnFailure=True,
             workdir='.',
             timeout=3600, # One hour, because Windows is slow
             extract_fn=parse_purge_builds,
             log_eval_func=lambda c,s: regex_log_evaluator(c, s, purge_error),
             env=self.env,
            ))

    def addPeriodicRebootSteps(self):
        def do_disconnect(cmd):
            try:
                if 'SCHEDULED REBOOT' in cmd.logs['stdio'].getText():
                    return True
            except:
                pass
            return False
        self.addStep(DisconnectStep(
         name='maybe_rebooting',
         command=['python', 'tools/buildfarm/maintenance/count_and_reboot.py',
                  '-f', '../reboot_count.txt',
                  '-n', str(self.buildsBeforeReboot),
                  '-z'],
         description=['maybe rebooting'],
         force_disconnect=do_disconnect,
         warnOnFailure=False,
         flunkOnFailure=False,
         alwaysRun=True,
         workdir='.'
        ))

    def getRepoName(self, repo):
        return repo.rstrip('/').split('/')[-1]

    def getRepository(self, repoPath, hgHost=None, push=False):
        assert repoPath
        for prefix in ('http://', 'ssh://'):
            if repoPath.startswith(prefix):
                return repoPath
        if repoPath.startswith('/'):
            repoPath = repoPath.lstrip('/')
        if not hgHost:
            hgHost = self.hgHost
        proto = 'ssh' if push else 'http'
        return '%s://%s/%s' % (proto, hgHost, repoPath)

    def getPackageFilename(self, platform, platform_variation):
        if 'android' in self.complete_platform:
            packageFilename = '*arm.apk' #the arm.apk is to avoid
                                         #unsigned/unaligned apks
        elif 'maemo' in self.complete_platform:
            packageFilename = '*.linux-*-arm.tar.*'
        elif platform.startswith("linux64"):
            packageFilename = '*.linux-x86_64.tar.bz2'
        elif platform.startswith("linux"):
            packageFilename = '*.linux-i686.tar.bz2'
        elif platform.startswith("macosx"):
            packageFilename = '*.dmg'
        elif platform.startswith("win32"):
            packageFilename = '*.win32.zip'
        elif platform.startswith("win64"):
            packageFilename = '*.win64-x86_64.zip'
        else:
            return False
        return packageFilename

    def parseFileSize(self, propertyName):
        def getSize(rv, stdout, stderr):
            stdout = stdout.strip()
            return {propertyName: stdout.split()[4]}
        return getSize

    def parseFileHash(self, propertyName):
        def getHash(rv, stdout, stderr):
            stdout = stdout.strip()
            return {propertyName: stdout.split(' ',2)[1]}
        return getHash

    def unsetFilepath(self, rv, stdout, stderr):
        return {'filepath': None}

    def addFilePropertiesSteps(self, filename, directory, fileType, 
                               doStepIf=True, maxDepth=1, haltOnFailure=False):
        self.addStep(FindFile(
            name='find_filepath',
            description=['find', 'filepath'],
            doStepIf=doStepIf,
            filename=filename,
            directory=directory,
            filetype='file',
            max_depth=maxDepth,
            property_name='filepath',
            workdir='.',
            haltOnFailure=haltOnFailure
        ))
        self.addStep(SetProperty(
            description=['set', fileType.lower(), 'filename'],
            doStepIf=doStepIf,
            command=['basename', WithProperties('%(filepath)s')],
            property=fileType+'Filename',
            workdir='.',
            name='set_'+fileType.lower()+'_filename',
            haltOnFailure=haltOnFailure
        ))
        self.addStep(SetProperty(
            description=['set', fileType.lower(), 'size',],
            doStepIf=doStepIf,
            command=['bash', '-c', 
                     WithProperties("ls -l %(filepath)s")],
            workdir='.',
            name='set_'+fileType.lower()+'_size',
            extract_fn = self.parseFileSize(propertyName=fileType+'Size'),
            haltOnFailure=haltOnFailure
        ))
        self.addStep(SetProperty(
            description=['set', fileType.lower(), 'hash',],
            doStepIf=doStepIf,
            command=['bash', '-c', 
                     WithProperties('openssl ' + 'dgst -' + self.hashType +
                                    ' %(filepath)s')],
            workdir='.',
            name='set_'+fileType.lower()+'_hash',
            extract_fn=self.parseFileHash(propertyName=fileType+'Hash'),
            haltOnFailure=haltOnFailure
        ))
        self.addStep(SetProperty(
            description=['unset', 'filepath',],
            doStepIf=doStepIf,
            name='unset_filepath',
            command='echo "filepath:"',
            workdir=directory,
            extract_fn = self.unsetFilepath,
        ))

    def makeHgtoolStep(self, name='hg_update', repo_url=None, wc=None,
            mirrors=None, bundles=None, env=None,
            clone_by_revision=False, rev=None, workdir='build',
            use_properties=True, locks=None):

        if not env:
            env = self.env

        if use_properties:
            env = env.copy()
            env['PROPERTIES_FILE'] = 'buildprops.json'

        cmd = ['python', WithProperties("%(toolsdir)s/buildfarm/utils/hgtool.py")]

        if clone_by_revision:
            cmd.append('--clone-by-revision')

        if mirrors is None and self.baseMirrorUrls:
            mirrors = ["%s/%s" % (url, self.repoPath) for url in self.baseMirrorUrls]

        if mirrors:
            for mirror in mirrors:
                cmd.extend(["--mirror", mirror])

        if bundles is None and self.baseBundleUrls:
            bundles = ["%s/%s.hg" % (url, self.getRepoName(self.repository)) for url in self.baseBundleUrls]

        if bundles:
            for bundle in bundles:
                cmd.extend(["--bundle", bundle])

        if not repo_url:
            repo_url = self.repository

        if rev:
            cmd.extend(["--rev", rev])

        cmd.append(repo_url)

        if wc:
            cmd.append(wc)

        if locks is None:
            locks = []

        return RetryingShellCommand(
            name=name,
            command=cmd,
            timeout=60*60,
            env=env,
            workdir=workdir,
            haltOnFailure=True,
            flunkOnFailure=True,
            locks=locks,
        )

    def addGetTokenSteps(self):
        token = "build/token"
        nonce = "build/nonce"
        self.addStep(ShellCommand(
            command=['rm', '-f', nonce],
            workdir='.',
            name='rm_nonce',
            description=['remove', 'old', 'nonce'],
        ))
        self.addStep(SigningServerAuthenication(
            servers=self.signingServers,
            server_cert=SIGNING_SERVER_CERT,
            slavedest=token,
            workdir='.',
            name='download_token',
        ))


class MercurialBuildFactory(MozillaBuildFactory):
    def __init__(self, objdir, platform, configRepoPath, configSubDir,
                 profiledBuild, mozconfig, srcMozconfig=None,
                 use_scratchbox=False, productName=None,
                 scratchbox_target='FREMANTLE_ARMEL', android_signing=False,
                 buildRevision=None, stageServer=None, stageUsername=None,
                 stageGroup=None, stageSshKey=None, stageBasePath=None,
                 stageProduct=None, post_upload_include_platform=False,
                 ausBaseUploadDir=None, updatePlatform=None,
                 downloadBaseURL=None, ausUser=None, ausSshKey=None,
                 ausHost=None, nightly=False, leakTest=False,
                 checkTest=False, valgrindCheck=False, codesighs=True,
                 graphServer=None, graphSelector=None, graphBranch=None,
                 baseName=None, uploadPackages=True, uploadSymbols=True,
                 createSnippet=False, createPartial=False, doCleanup=True,
                 packageSDK=False, packageTests=False, mozillaDir=None,
                 enable_ccache=False, stageLogBaseUrl=None,
                 triggeredSchedulers=None, triggerBuilds=False,
                 mozconfigBranch="production", useSharedCheckouts=False,
                 stagePlatform=None, testPrettyNames=False, l10nCheckTest=False,
                 doBuildAnalysis=False,
                 downloadSubdir=None,
                 multiLocale=False,
                 multiLocaleMerge=True,
                 compareLocalesRepoPath=None,
                 compareLocalesTag='RELEASE_AUTOMATION',
                 mozharnessRepoPath=None,
                 mozharnessTag='default',
                 multiLocaleScript=None,
                 multiLocaleConfig=None,
                 mozharnessMultiOptions=None,
                 **kwargs):
        MozillaBuildFactory.__init__(self, **kwargs)

        # Make sure we have a buildid and builduid
        self.addStep(FunctionalStep(
         name='set_buildids',
         func=setBuildIDProps,
        ))

        self.objdir = objdir
        self.platform = platform
        self.configRepoPath = configRepoPath
        self.configSubDir = configSubDir
        self.profiledBuild = profiledBuild
        self.mozconfig = mozconfig
        self.srcMozconfig = srcMozconfig
        self.productName = productName
        self.buildRevision = buildRevision
        self.stageServer = stageServer
        self.stageUsername = stageUsername
        self.stageGroup = stageGroup
        self.stageSshKey = stageSshKey
        self.stageBasePath = stageBasePath
        if stageBasePath and not stageProduct:
            self.stageProduct = productName
        else:
            self.stageProduct = stageProduct
        self.doBuildAnalysis = doBuildAnalysis
        self.ausBaseUploadDir = ausBaseUploadDir
        self.updatePlatform = updatePlatform
        self.downloadBaseURL = downloadBaseURL
        self.downloadSubdir = downloadSubdir
        self.ausUser = ausUser
        self.ausSshKey = ausSshKey
        self.ausHost = ausHost
        self.nightly = nightly
        self.leakTest = leakTest
        self.checkTest = checkTest
        self.valgrindCheck = valgrindCheck
        self.codesighs = codesighs
        self.graphServer = graphServer
        self.graphSelector = graphSelector
        self.graphBranch = graphBranch
        self.baseName = baseName
        self.uploadPackages = uploadPackages
        self.uploadSymbols = uploadSymbols
        self.createSnippet = createSnippet
        self.createPartial = createPartial
        self.doCleanup = doCleanup
        self.packageSDK = packageSDK
        self.packageTests = packageTests
        self.enable_ccache = enable_ccache
        if self.enable_ccache:
            self.env['CCACHE_BASEDIR'] = WithProperties('%(basedir:-)s')
        self.triggeredSchedulers = triggeredSchedulers
        self.triggerBuilds = triggerBuilds
        self.mozconfigBranch = mozconfigBranch
        self.use_scratchbox = use_scratchbox
        self.scratchbox_target = scratchbox_target
        self.android_signing = android_signing
        self.post_upload_include_platform = post_upload_include_platform
        self.useSharedCheckouts = useSharedCheckouts
        self.testPrettyNames = testPrettyNames
        self.l10nCheckTest = l10nCheckTest

        if self.uploadPackages:
            assert productName and stageServer and stageUsername
            assert stageBasePath
        if self.createSnippet:
            if 'android' in platform: #happens before we trim platform
                assert downloadSubdir
            assert ausBaseUploadDir and updatePlatform and downloadBaseURL
            assert ausUser and ausSshKey and ausHost

            # To preserve existing behavior, we need to set the 
            # ausFullUploadDir differently for when we are create all the
            # mars (complete+partial) ourselves. 
            if self.createPartial:
                # e.g.:
                # /opt/aus2/incoming/2/Firefox/mozilla-central/WINNT_x86-msvc
                self.ausFullUploadDir = '%s/%s' % (self.ausBaseUploadDir,
                                                   self.updatePlatform)
            else:
                # this is a tad ugly because we need python interpolation
                # as well as WithProperties, e.g.:
                # /opt/aus2/build/0/Firefox/mozilla-central/WINNT_x86-msvc/2008010103/en-US
                self.ausFullUploadDir = '%s/%s/%%(buildid)s/en-US' % \
                                          (self.ausBaseUploadDir, 
                                           self.updatePlatform)

        self.complete_platform = self.platform
        # we don't need the extra cruft in 'platform' anymore
        self.platform = platform.split('-')[0]
        self.stagePlatform = stagePlatform
        # it turns out that the cruft is useful for dealing with multiple types
        # of builds that are all done using the same self.platform.
        # Examples of what happens:
        #   platform = 'linux' sets self.platform_variation to []
        #   platform = 'linux-opt' sets self.platform_variation to ['opt']
        #   platform = 'linux-opt-rpm' sets self.platform_variation to ['opt','rpm']
        platform_chunks = self.complete_platform.split('-', 1)
        if len(platform_chunks) > 1:
                self.platform_variation = platform_chunks[1].split('-')
        else:
                self.platform_variation = []

        assert self.platform in getSupportedPlatforms()

        if self.graphServer is not None:
            self.tbPrint = False
        else:
            self.tbPrint = True

        # SeaMonkey/Thunderbird make use of mozillaDir. Firefox does not.
        if mozillaDir:
            self.mozillaDir = '/%s' % mozillaDir
            self.mozillaObjdir = '%s%s' % (self.objdir, self.mozillaDir)
        else:
            self.mozillaDir = ''
            self.mozillaObjdir = self.objdir

        # These following variables are useful for sharing build steps (e.g.
        # update generation) with subclasses that don't use object dirs (e.g.
        # l10n repacks).
        # 
        # We also concatenate the baseWorkDir at the outset to avoid having to
        # do that everywhere.
        self.mozillaSrcDir = '.%s' % self.mozillaDir
        self.absMozillaSrcDir = '%s%s' % (self.baseWorkDir, self.mozillaDir)
        self.absMozillaObjDir = '%s/%s' % (self.baseWorkDir, self.mozillaObjdir)

        self.latestDir = '/pub/mozilla.org/%s' % self.stageProduct + \
                         '/nightly/latest-%s' % self.branchName
        if self.post_upload_include_platform:
            self.latestDir += '-%s' % self.stagePlatform

        self.stageLogBaseUrl = stageLogBaseUrl
        if self.stageLogBaseUrl:
            # yes, the branchName is needed twice here so that log uploads work for all
            self.logUploadDir = '%s/%s-%s/' % (self.branchName, self.branchName,
                                               self.stagePlatform)
            self.logBaseUrl = '%s/%s' % (self.stageLogBaseUrl, self.logUploadDir)
        else:
            self.logUploadDir = 'tinderbox-builds/%s-%s/' % (self.branchName,
                                                             self.stagePlatform)
            self.logBaseUrl = 'http://%s/pub/mozilla.org/%s/%s' % \
                        ( self.stageServer, self.stageProduct, self.logUploadDir)

        # Need to override toolsdir as set by MozillaBuildFactory because
        # we need Windows-style paths.
        if self.platform.startswith('win'):
            self.addStep(SetProperty(
                command=['bash', '-c', 'pwd -W'],
                property='toolsdir',
                workdir='tools'
            ))
        if self.use_scratchbox:
            self.addStep(ScratchboxCommand(
                command=["sb-conf", "select", self.scratchbox_target],
                name='set_target',
                env=self.env,
                sb=True,
                workdir='/',
            ))

        if self.enable_ccache:
            self.addStep(ShellCommand(command=['ccache', '-z'],
                     name="clear_ccache_stats", warnOnFailure=False,
                     flunkOnFailure=False, haltOnFailure=False, env=self.env))
        if multiLocale:
            assert compareLocalesRepoPath and compareLocalesTag
            assert mozharnessRepoPath and mozharnessTag
            assert multiLocaleScript and multiLocaleConfig
            self.compareLocalesRepoPath = compareLocalesRepoPath
            self.compareLocalesTag = compareLocalesTag
            self.mozharnessRepoPath = mozharnessRepoPath
            self.mozharnessTag = mozharnessTag
            self.multiLocaleScript = multiLocaleScript
            self.multiLocaleConfig = multiLocaleConfig
            self.multiLocaleMerge = multiLocaleMerge
            if mozharnessMultiOptions:
                self.mozharnessMultiOptions = mozharnessMultiOptions
            else:
                self.mozharnessMultiOptions = ['--only-pull-locale-source',
                                               '--only-add-locales',
                                               '--only-package-multi']

            self.addMultiLocaleRepoSteps()

        self.multiLocale = multiLocale

        self.addBuildSteps()
        if self.uploadSymbols or self.packageTests or self.leakTest:
            self.addBuildSymbolsStep()
        if self.uploadSymbols:
            self.addUploadSymbolsStep()
        if self.uploadPackages:
            self.addUploadSteps()
        if self.testPrettyNames:
            self.addTestPrettyNamesSteps()
        if self.leakTest:
            self.addLeakTestSteps()
        if self.l10nCheckTest:
            self.addL10nCheckTestSteps()
        if self.checkTest:
            self.addCheckTestSteps()
        if self.valgrindCheck:
            self.addValgrindCheckSteps()
        if self.codesighs:
            self.addCodesighsSteps()
        if self.createSnippet:
            self.addUpdateSteps()
        if self.triggerBuilds:
            self.addTriggeredBuildsSteps()
        if self.doCleanup:
            self.addPostBuildCleanupSteps()
        if self.enable_ccache:
            self.addStep(ShellCommand(command=['ccache', '-s'],
                     name="print_ccache_stats", warnOnFailure=False,
                     flunkOnFailure=False, haltOnFailure=False, env=self.env))
        if self.buildsBeforeReboot and self.buildsBeforeReboot > 0:
            self.addPeriodicRebootSteps()

    def addMultiLocaleRepoSteps(self):
        for repo,tag in ((self.compareLocalesRepoPath,self.compareLocalesTag),
                            (self.mozharnessRepoPath,self.mozharnessTag)):
            name=repo.rstrip('/').split('/')[-1]
            self.addStep(ShellCommand(
                name='rm_%s'%name,
                command=['rm', '-rf', '%s' % name],
                description=['removing', name],
                descriptionDone=['remove', name],
                haltOnFailure=True,
                workdir='.',
            ))
            self.addStep(MercurialCloneCommand(
                name='hg_clone_%s' % name,
                command=['hg', 'clone', self.getRepository(repo), name],
                description=['checking', 'out', name],
                descriptionDone=['checkout', name],
                haltOnFailure=True,
                workdir='.',
            ))
            self.addStep(ShellCommand(
                name='hg_update_%s'% name,
                command=['hg', 'update', '-r', tag],
                description=['updating', name, 'to', tag],
                workdir=name,
                haltOnFailure=True
           ))


    def addTriggeredBuildsSteps(self,
                                triggeredSchedulers=None):
        '''Trigger other schedulers.
        We don't include these steps by default because different
        children may want to trigger builds at different stages.

        If triggeredSchedulers is None, then the schedulers listed in
        self.triggeredSchedulers will be triggered.
        '''
        if triggeredSchedulers is None:
            if self.triggeredSchedulers is None:
                return True
            triggeredSchedulers = self.triggeredSchedulers

        for triggeredScheduler in triggeredSchedulers:
            self.addStep(Trigger(
                schedulerNames=[triggeredScheduler],
                copy_properties=['buildid', 'builduid'],
                waitForFinish=False))

    def addBuildSteps(self):
        self.addPreBuildSteps()
        self.addSourceSteps()
        self.addConfigSteps()
        self.addDoBuildSteps()
        if self.signingServers and self.enableSigning:
            self.addGetTokenSteps()
        if self.doBuildAnalysis:
            self.addBuildAnalysisSteps()

    def addPreBuildSteps(self):
        if self.nightly:
            self.addStep(ShellCommand(
             name='rm_builddir',
             command=['rm', '-rf', 'build'],
             env=self.env,
             workdir='.',
             timeout=60*60 # 1 hour
            ))
        pkg_patterns = []
        for product in ('firefox-', 'fennec', 'seamonkey'):
            pkg_patterns.append('%s/dist/%s*' % (self.mozillaObjdir,
                                                  product))

        self.addStep(ShellCommand(
         name='rm_old_pkg',
         command="rm -rf %s %s/dist/install/sea/*.exe " %
                  (' '.join(pkg_patterns), self.mozillaObjdir),
         env=self.env,
         description=['deleting', 'old', 'package'],
         descriptionDone=['delete', 'old', 'package']
        ))
        if self.nightly:
            self.addStep(ShellCommand(
             name='rm_old_symbols',
             command="find 20* -maxdepth 2 -mtime +7 -exec rm -rf {} \;",
             env=self.env,
             workdir='.',
             description=['cleanup', 'old', 'symbols'],
             flunkOnFailure=False
            ))

    def addSourceSteps(self):
        if self.hgHost.startswith('ssh'):
            self.addStep(Mercurial(
             name='hg_ssh_clone',
             mode='update',
             baseURL= '%s/' % self.hgHost,
             defaultBranch=self.repoPath,
             timeout=60*60, # 1 hour
            ))
        elif self.useSharedCheckouts:
            self.addStep(JSONPropertiesDownload(
                name="download_props",
                slavedest="buildprops.json",
                workdir='.'
            ))

            self.addStep(self.makeHgtoolStep(wc='build', workdir='.'))

            self.addStep(SetProperty(
                name = 'set_got_revision',
                command=['hg', 'parent', '--template={node}'],
                extract_fn = short_hash
            ))
        else:
            self.addStep(Mercurial(
             name='hg_update',
             mode='update',
             baseURL='http://%s/' % self.hgHost,
             defaultBranch=self.repoPath,
             timeout=60*60, # 1 hour
            ))

        if self.buildRevision:
            self.addStep(ShellCommand(
             name='hg_update',
             command=['hg', 'up', '-C', '-r', self.buildRevision],
             haltOnFailure=True
            ))
            self.addStep(SetProperty(
             name='set_got_revision',
             command=['hg', 'identify', '-i'],
             property='got_revision'
            ))
        #Fix for bug 612319 to correct http://ssh:// changeset links
        if self.hgHost[0:5] == "ssh://":
            changesetLink = '<a href=https://%s/%s/rev' % (self.hgHost[6:],
                                                           self.repoPath)
        else: 
            changesetLink = '<a href=http://%s/%s/rev' % (self.hgHost,
                                                          self.repoPath)
        changesetLink += '/%(got_revision)s title="Built from revision %(got_revision)s">rev:%(got_revision)s</a>'
        self.addStep(OutputStep(
         name='tinderboxprint_changeset',
         data=['TinderboxPrint:', WithProperties(changesetLink)]
        ))
        self.addStep(SetBuildProperty(
            name='set_comments',
            property_name="comments",
            value=lambda build:build.source.changes[-1].comments if len(build.source.changes) > 0 else "",
        ))

    def addConfigSteps(self):
        assert self.configRepoPath is not None
        assert self.configSubDir is not None
        assert self.mozconfig is not None

        configRepo = self.getRepository(self.configRepoPath)
        hg_mozconfig = '%s/raw-file/%s/%s/%s/mozconfig' % (
                configRepo, self.mozconfigBranch, self.configSubDir, self.mozconfig)
        if self.srcMozconfig:
            cmd = ['bash', '-c',
                    '''if [ -f "%(src_mozconfig)s" ]; then
                        echo Using in-tree mozconfig;
                        cp %(src_mozconfig)s .mozconfig;
                    else
                        echo Downloading mozconfig;
                        wget -O .mozconfig %(hg_mozconfig)s;
                    fi'''.replace("\n","") % {'src_mozconfig': self.srcMozconfig, 'hg_mozconfig': hg_mozconfig}]
        else:
            cmd = ['wget', '-O', '.mozconfig', hg_mozconfig]

        self.addStep(RetryingShellCommand(
            name='get_mozconfig',
            command=cmd,
            description=['getting', 'mozconfig'],
            descriptionDone=['got', 'mozconfig'],
            haltOnFailure=True
        ))
        self.addStep(ShellCommand(
         name='cat_mozconfig',
         command=['cat', '.mozconfig'],
        ))

    def addDoBuildSteps(self):
        workdir=WithProperties('%(basedir)s/build')
        if self.platform.startswith('win'):
            workdir="build"
        # XXX This is a hack but we don't have any intention of adding
        # more branches that are missing MOZ_PGO.  Once these branches
        # are all EOL'd, lets remove this and just put 'build' in the 
        # command argv
        if self.complete_platform == 'win32' and \
           self.branchName in ('mozilla-1.9.1', 'mozilla-1.9.2', 'mozilla-2.0') and \
           self.productName == 'firefox':
            bldtgt = 'profiledbuild'
        else:
            bldtgt = 'build'

        command = ['make', '-f', 'client.mk', bldtgt,
                   WithProperties('MOZ_BUILD_DATE=%(buildid:-)s')]

        if self.profiledBuild:
            command.append('MOZ_PGO=1')
        self.addStep(ScratchboxCommand(
         name='compile',
         command=command,
         description=['compile'],
         env=self.env,
         haltOnFailure=True,
         timeout=10800,
         # bug 650202 'timeout=7200', bumping to stop the bleeding while we diagnose
         # the root cause of the linker time out.
         sb=self.use_scratchbox,
         workdir=workdir,
        ))

    def addBuildInfoSteps(self):
        """Helper function for getting build information into properties.
        Looks for self._gotBuildInfo to make sure we only run this set of steps
        once."""
        if not getattr(self, '_gotBuildInfo', False):
            self.addStep(SetProperty(
                command=['python', 'build%s/config/printconfigsetting.py' % self.mozillaDir,
                'build/%s/dist/bin/application.ini' % self.mozillaObjdir,
                'App', 'BuildID'],
                property='buildid',
                workdir='.',
                description=['getting', 'buildid'],
                descriptionDone=['got', 'buildid'],
            ))
            self.addStep(SetProperty(
                command=['python', 'build%s/config/printconfigsetting.py' % self.mozillaDir,
                'build/%s/dist/bin/application.ini' % self.mozillaObjdir,
                'App', 'SourceStamp'],
                property='sourcestamp',
                workdir='.',
                description=['getting', 'sourcestamp'],
                descriptionDone=['got', 'sourcestamp']
            ))
            self._gotBuildInfo = True

    def addBuildAnalysisSteps(self):
        if self.platform in ('linux', 'linux64'):
            # Analyze the number of ctors
            def get_ctors(rc, stdout, stderr):
                try:
                    output = stdout.split("\t")
                    num_ctors = int(output[0])
                    testresults = [ ('num_ctors', 'num_ctors', num_ctors, str(num_ctors)) ]
                    return dict(num_ctors=num_ctors, testresults=testresults)
                except:
                    return {'testresults': []}

            self.addStep(SetProperty(
                name='get_ctors',
                command=['python', WithProperties('%(toolsdir)s/buildfarm/utils/count_ctors.py'),
                    '%s/dist/bin/libxul.so' % self.mozillaObjdir],
                extract_fn=get_ctors,
                ))

            if self.graphServer:
                self.addBuildInfoSteps()
                self.addStep(JSONPropertiesDownload(slavedest="properties.json"))
                gs_env = self.env.copy()
                gs_env['PYTHONPATH'] = WithProperties('%(toolsdir)s/lib/python')
                self.addStep(GraphServerPost(server=self.graphServer,
                                             selector=self.graphSelector,
                                             branch=self.graphBranch,
                                             resultsname=self.baseName,
                                             env=gs_env,
                                             propertiesFile="properties.json"))
            else:
                self.addStep(OutputStep(
                    name='tinderboxprint_ctors',
                    data=WithProperties('TinderboxPrint: num_ctors: %(num_ctors:-unknown)s'),
                    ))

    def addLeakTestStepsCommon(self, baseUrl, leakEnv, graphAndUpload):
        """ Helper function for the two implementations of addLeakTestSteps.
        * baseUrl Base of the URL where we get the old logs.
        * leakEnv Environment used for running firefox.
        * graphAndUpload Used to prevent the try jobs from doing talos graph
          posts and uploading logs."""
        self.addStep(AliveTest(
          env=leakEnv,
          workdir='build/%s/_leaktest' % self.mozillaObjdir,
          extraArgs=['--trace-malloc', 'malloc.log',
                     '--shutdown-leaks=sdleak.log'],
          timeout=3600, # 1 hour, because this takes a long time on win32
          warnOnFailure=True,
          haltOnFailure=True
          ))

        # Download and unpack the old versions of malloc.log and sdleak.tree
        cmd = ['bash', '-c', 
                WithProperties('%(toolsdir)s/buildfarm/utils/wget_unpack.sh ' +
                               baseUrl + ' logs.tar.gz '+
                               'malloc.log:malloc.log.old sdleak.tree:sdleak.tree.old') ]
        self.addStep(ShellCommand(
            name='get_logs',
            env=self.env,
            workdir='.',
            command=cmd,
        ))

        self.addStep(ShellCommand(
          name='mv_malloc_log',
          env=self.env,
          command=['mv',
                   '%s/_leaktest/malloc.log' % self.mozillaObjdir,
                   '../malloc.log'],
          ))
        self.addStep(ShellCommand(
          name='mv_sdleak_log',
          env=self.env,
          command=['mv',
                   '%s/_leaktest/sdleak.log' % self.mozillaObjdir,
                   '../sdleak.log'],
          ))
        self.addStep(CompareLeakLogs(
          name='compare_current_leak_log',
          mallocLog='../malloc.log',
          platform=self.platform,
          env=self.env,
          objdir=self.mozillaObjdir,
          testname='current',
          tbPrint=self.tbPrint,
          warnOnFailure=True,
          haltOnFailure=True
          ))
        if self.graphServer and graphAndUpload:
            gs_env = self.env.copy()
            gs_env['PYTHONPATH'] = WithProperties('%(toolsdir)s/lib/python')
            self.addBuildInfoSteps()
            self.addStep(JSONPropertiesDownload(slavedest="properties.json"))
            self.addStep(GraphServerPost(server=self.graphServer,
                                         selector=self.graphSelector,
                                         branch=self.graphBranch,
                                         resultsname=self.baseName,
                                         env=gs_env,
                                         propertiesFile="properties.json"))
        self.addStep(CompareLeakLogs(
          name='compare_previous_leak_log',
          mallocLog='../malloc.log.old',
          platform=self.platform,
          env=self.env,
          objdir=self.mozillaObjdir,
          testname='previous'
          ))
        self.addStep(ShellCommand(
          name='create_sdleak_tree',
          env=self.env,
          workdir='.',
          command=['bash', '-c',
                   'perl build%s/tools/trace-malloc/diffbloatdump.pl '
                   '--depth=15 --use-address /dev/null sdleak.log '
                   '> sdleak.tree' % self.mozillaDir],
          warnOnFailure=True,
          haltOnFailure=True
          ))
        if self.platform in ('macosx', 'macosx64', 'linux', 'linux64'):
            self.addStep(ShellCommand(
              name='create_sdleak_raw',
              env=self.env,
              workdir='.',
              command=['mv', 'sdleak.tree', 'sdleak.tree.raw']
              ))
            # Bug 571443 - disable fix-macosx-stack.pl
            if self.platform == 'macosx64':
                self.addStep(ShellCommand(
                  workdir='.',
                  command=['cp', 'sdleak.tree.raw', 'sdleak.tree'],
                  ))
            else:
                self.addStep(ShellCommand(
                  name='get_fix_stack',
                  env=self.env,
                  workdir='.',
                  command=['/bin/bash', '-c',
                           'perl '
                           'build%s/tools/rb/fix-%s-stack.pl '
                           'sdleak.tree.raw '
                           '> sdleak.tree' % (self.mozillaDir,
                                              self.platform.replace("64", "")),
                           ],
                  warnOnFailure=True,
                  haltOnFailure=True
                    ))
        if graphAndUpload:
            cmd = ['bash', '-c', 
                    WithProperties('%(toolsdir)s/buildfarm/utils/pack_scp.sh ' +
                        'logs.tar.gz ' + ' .. ' +
                        '%s ' % self.stageUsername +
                        '%s ' % self.stageSshKey +
                        # Notice the '/' after the ':'. This prevents windows from trying to modify
                        # the path
                        '%s:/%s/%s ' % (self.stageServer, self.stageBasePath,
                        self.logUploadDir) +
                        'malloc.log sdleak.tree') ]
            self.addStep(ShellCommand(
                name='upload_logs',
                env=self.env,
                command=cmd,
                ))
        self.addStep(ShellCommand(
          name='compare_sdleak_tree',
          env=self.env,
          workdir='.',
          command=['perl', 'build%s/tools/trace-malloc/diffbloatdump.pl' % self.mozillaDir,
                   '--depth=15', 'sdleak.tree.old', 'sdleak.tree']
          ))

    def addLeakTestSteps(self):
        leakEnv = self.env.copy()
        leakEnv['MINIDUMP_STACKWALK'] = getPlatformMinidumpPath(self.platform)
        leakEnv['MINIDUMP_SAVE_PATH'] = WithProperties('%(basedir:-)s/minidumps')
        self.addStep(AliveTest(
          env=leakEnv,
          workdir='build/%s/_leaktest' % self.mozillaObjdir,
          extraArgs=['-register'],
          warnOnFailure=True,
          haltOnFailure=True
        ))
        self.addStep(AliveTest(
          env=leakEnv,
          workdir='build/%s/_leaktest' % self.mozillaObjdir,
          warnOnFailure=True,
          haltOnFailure=True
        ))

        self.addLeakTestStepsCommon(self.logBaseUrl, leakEnv, True)

    def addCheckTestSteps(self):
        env = self.env.copy()
        env['MINIDUMP_STACKWALK'] = getPlatformMinidumpPath(self.platform)
        env['MINIDUMP_SAVE_PATH'] = WithProperties('%(basedir:-)s/minidumps')
        self.addStep(unittest_steps.MozillaCheck,
         test_name="check",
         warnOnWarnings=True,
         workdir="build/%s" % self.objdir,
         timeout=5*60, # 5 minutes.
         env=env,
        )

    def addL10nCheckTestSteps(self):
        # We override MOZ_SIGN_CMD here because it's not necessary
        # Disable signing for l10n check steps
        env = self.env
        if 'MOZ_SIGN_CMD' in env:
            env = env.copy()
            del env['MOZ_SIGN_CMD']

        self.addStep(ShellCommand(
         name='make l10n check',
         command=['make', 'l10n-check'],
         workdir='build/%s' % self.objdir,
         env=env,
         haltOnFailure=False,
         flunkOnFailure=False,
         warnOnFailure=True,
        ))

    def addValgrindCheckSteps(self):
        env = self.env.copy()
        env['MINIDUMP_STACKWALK'] = getPlatformMinidumpPath(self.platform)
        env['MINIDUMP_SAVE_PATH'] = WithProperties('%(basedir:-)s/minidumps')
        self.addStep(unittest_steps.MozillaCheck,
         test_name="check-valgrind",
         warnOnWarnings=True,
         workdir="build/%s/js/src" % self.mozillaObjdir,
         timeout=5*60, # 5 minutes.
         env=env,
        )

    def addCreateUpdateSteps(self):
        self.addStep(ShellCommand(
            name='make_complete_mar',
            command=['make', '-C',
                     '%s/tools/update-packaging' % self.mozillaObjdir],
            env=self.env,
            haltOnFailure=True,
        ))
        self.addFilePropertiesSteps(
            filename='*.complete.mar',
            directory='%s/dist/update' % self.absMozillaObjDir,
            fileType='completeMar',
            haltOnFailure=True,
        )

    def addTestPrettyNamesSteps(self):
        # Disable signing for l10n check steps
        env = self.env
        if 'MOZ_SIGN_CMD' in env:
            env = env.copy()
            del env['MOZ_SIGN_CMD']

        if 'mac' in self.platform:
            # Need to run this target or else the packaging targets will
            # fail.
            self.addStep(ShellCommand(
             name='postflight_all',
             command=['make', '-f', 'client.mk', 'postflight_all'],
             env=env,
             haltOnFailure=False,
             flunkOnFailure=False,
             warnOnFailure=False,
            ))
        pkg_targets = ['package']
        if 'win' in self.platform:
            pkg_targets.append('installer')
        for t in pkg_targets:
            self.addStep(ShellCommand(
             name='make %s pretty' % t,
             command=['make', t, 'MOZ_PKG_PRETTYNAMES=1'],
             env=env,
             workdir='build/%s' % self.objdir,
             haltOnFailure=False,
             flunkOnFailure=False,
             warnOnFailure=True,
            ))
        self.addStep(ShellCommand(
             name='make update pretty',
             command=['make', '-C',
                      '%s/tools/update-packaging' % self.mozillaObjdir,
                      'MOZ_PKG_PRETTYNAMES=1'],
             env=env,
             haltOnFailure=False,
             flunkOnFailure=False,
             warnOnFailure=True,
         ))
        if self.l10nCheckTest:
            self.addStep(ShellCommand(
                 name='make l10n check pretty',
                command=['make', 'l10n-check', 'MOZ_PKG_PRETTYNAMES=1'],
                workdir='build/%s' % self.objdir,
                env=env,
                haltOnFailure=False,
                flunkOnFailure=False,
                warnOnFailure=True,
            ))

    def addUploadSteps(self, pkgArgs=None, pkgTestArgs=None):
        pkgArgs = pkgArgs or []
        pkgTestArgs = pkgTestArgs or []

        if self.env:
            pkg_env = self.env.copy()
        else:
            pkg_env = {}
        if self.android_signing:
            pkg_env['JARSIGNER'] = WithProperties('%(toolsdir)s/release/signing/mozpass.py')

        objdir = WithProperties('%(basedir)s/build/' + self.objdir)
        if self.platform.startswith('win'):
            objdir = "build/%s" % self.objdir
        workdir = WithProperties('%(basedir)s/build')
        if self.platform.startswith('win'):
            workdir = "build/"
        if 'rpm' in self.platform_variation:
            pkgArgs.append("MOZ_PKG_FORMAT=RPM")
        if self.packageSDK:
            self.addStep(ScratchboxCommand(
             name='make_sdk',
             command=['make', '-f', 'client.mk', 'sdk'],
             env=pkg_env,
             workdir=workdir,
             sb=self.use_scratchbox,
             haltOnFailure=True,
            ))
        if self.packageTests:
            self.addStep(ScratchboxCommand(
             name='make_pkg_tests',
             command=['make', 'package-tests'] + pkgTestArgs,
             env=pkg_env,
             workdir=objdir,
             sb=self.use_scratchbox,
             haltOnFailure=True,
            ))
        self.addStep(ScratchboxCommand(
            name='make_pkg',
            command=['make', 'package'] + pkgArgs,
            env=pkg_env,
            workdir=objdir,
            sb=self.use_scratchbox,
            haltOnFailure=True
        ))
        # Get package details
        packageFilename = self.getPackageFilename(self.platform,
                                                  self.platform_variation)
        if packageFilename and 'rpm' not in self.platform_variation:
            self.addFilePropertiesSteps(filename=packageFilename,
                                        directory='build/%s/dist' % self.mozillaObjdir,
                                        fileType='package',
                                        haltOnFailure=True)
        # Maemo special cases
        if 'maemo' in self.complete_platform:
            self.addStep(ScratchboxCommand(
                name='make_deb',
                command=['make','deb'] + pkgArgs,
                env=pkg_env,
                workdir=WithProperties('%(basedir)s/build/' + self.objdir),
                sb=self.use_scratchbox,
                haltOnFailure=True,
            ))
        # Windows special cases
        if self.platform.startswith("win") and \
           'mobile' not in self.complete_platform and \
           self.productName != 'xulrunner':
            self.addStep(ShellCommand(
                name='make_installer',
                command=['make', 'installer'] + pkgArgs,
                env=pkg_env,
                workdir='build/%s' % self.objdir,
                haltOnFailure=True
            ))
            self.addFilePropertiesSteps(filename='*.installer.exe', 
                                        directory='build/%s/dist/install/sea' % self.mozillaObjdir,
                                        fileType='installer',
                                        haltOnFailure=True)

        if self.productName == 'xulrunner':
            self.addStep(SetProperty(
                command=['python', 'build%s/config/printconfigsetting.py' % self.mozillaDir,
                         'build/%s/dist/bin/platform.ini' % self.mozillaObjdir,
                         'Build', 'BuildID'],
                property='buildid',
                workdir='.',
                name='get_build_id',
            ))
        else:
            self.addStep(SetProperty(
                command=['python', 'build%s/config/printconfigsetting.py' % self.mozillaDir,
                         'build/%s/dist/bin/application.ini' % self.mozillaObjdir,
                         'App', 'BuildID'],
                property='buildid',
                workdir='.',
                name='get_build_id',
            ))
            self.addStep(SetProperty(
                command=['python', 'build%s/config/printconfigsetting.py' % self.mozillaDir,
                         'build/%s/dist/bin/application.ini' % self.mozillaObjdir,
                         'App', 'Version'],
                property='appVersion',
                workdir='.',
                name='get_app_version',
            ))

        if self.multiLocale:
            self.doUpload(postUploadBuildDir='en-US')
            cmd = ['python', 'mozharness/%s' % self.multiLocaleScript,
                   '--config-file', self.multiLocaleConfig]
            if self.multiLocaleMerge:
                cmd.append('--merge-locales')
            cmd.extend(self.mozharnessMultiOptions)
            self.addStep(ShellCommand(
                name='mozharness_multilocale',
                command=cmd,
                env=pkg_env,
                workdir='.',
                haltOnFailure=True,
            ))
            # We need to set packageFilename to the multi apk
            self.addFilePropertiesSteps(filename=packageFilename,
                                        directory='build/%s/dist' % self.mozillaObjdir,
                                        fileType='package',
                                        haltOnFailure=True)

        if self.createSnippet and 'android' not in self.complete_platform:
            self.addCreateUpdateSteps();

        # Call out to a subclass to do the actual uploading
        self.doUpload(uploadMulti=self.multiLocale)

    def addCodesighsSteps(self):
        codesighs_dir = WithProperties('%(basedir)s/build/' + self.mozillaObjdir +
                                       '/tools/codesighs')
        if self.platform.startswith('win'):
            codesighs_dir = 'build/%s/tools/codesighs' % self.mozillaObjdir
        self.addStep(ScratchboxCommand(
         name='make_codesighs',
         command=['make'],
         workdir=codesighs_dir,
         sb=self.use_scratchbox,
        ))

        cmd = ['/bin/bash', '-c', 
                WithProperties('%(toolsdir)s/buildfarm/utils/wget_unpack.sh ' +
                               self.logBaseUrl + ' codesize-auto.tar.gz '+
                               'codesize-auto.log:codesize-auto.log.old') ]
        self.addStep(ShellCommand(
            name='get_codesize_logs',
            env=self.env,
            workdir='.',
            command=cmd,
        ))

        if self.mozillaDir == '':
            codesighsObjdir = self.objdir
        else:
            codesighsObjdir = '../%s' % self.mozillaObjdir

        self.addStep(Codesighs(
         name='get_codesighs_diff',
         objdir=codesighsObjdir,
         platform=self.platform,
         workdir='build%s' % self.mozillaDir,
         env=self.env,
         tbPrint=self.tbPrint,
        ))

        if self.graphServer:
            gs_env = self.env.copy()
            gs_env['PYTHONPATH'] = WithProperties('%(toolsdir)s/lib/python')
            self.addBuildInfoSteps()
            self.addStep(JSONPropertiesDownload(slavedest="properties.json"))
            self.addStep(GraphServerPost(server=self.graphServer,
                                         selector=self.graphSelector,
                                         branch=self.graphBranch,
                                         resultsname=self.baseName,
                                         env=gs_env,
                                         propertiesFile="properties.json"))
        self.addStep(ShellCommand(
         name='echo_codesize_log',
         command=['cat', '../codesize-auto-diff.log'],
         workdir='build%s' % self.mozillaDir
        ))

        cmd = ['/bin/bash', '-c', 
                WithProperties('%(toolsdir)s/buildfarm/utils/pack_scp.sh ' +
                    'codesize-auto.tar.gz ' + ' .. ' +
                    '%s ' % self.stageUsername +
                    '%s ' % self.stageSshKey +
                    # Notice the '/' after the ':'. This prevents windows from trying to modify
                    # the path
                    '%s:/%s/%s ' % (self.stageServer, self.stageBasePath,
                        self.logUploadDir) +
                    'codesize-auto.log') ]
        self.addStep(ShellCommand(
            name='upload_codesize_logs',
            command=cmd,
            workdir='build%s' % self.mozillaDir
            ))

    def addCreateSnippetsSteps(self, milestone_extra=''):
        if 'android' in self.complete_platform:
            cmd = [
                'python',
                WithProperties('%(toolsdir)s/scripts/android/android_snippet.py'),
                '--download-base-url', self.downloadBaseURL,
                '--download-subdir', self.downloadSubdir,
                '--abi', self.updatePlatform,
                '--aus-base-path', self.ausBaseUploadDir,
                '--aus-host', self.ausHost,
                '--aus-user', self.ausUser,
                '--aus-ssh-key', '~/.ssh/%s' % self.ausSshKey,
                WithProperties(self.objdir + '/dist/%(packageFilename)s')
            ]
            self.addStep(ShellCommand(
                name='create_android_snippet',
                command=cmd,
                haltOnFailure=False,
            ))
        else:
            milestone = self.branchName + milestone_extra
            self.addStep(CreateCompleteUpdateSnippet(
                name='create_complete_snippet',
                objdir=self.absMozillaObjDir,
                milestone=milestone,
                baseurl='%s/nightly' % self.downloadBaseURL,
                hashType=self.hashType,
            ))
            self.addStep(ShellCommand(
                name='cat_complete_snippet',
                description=['cat', 'complete', 'snippet'],
                command=['cat', 'complete.update.snippet'],
                workdir='%s/dist/update' % self.absMozillaObjDir,
            ))

    def addUploadSnippetsSteps(self):
        self.addStep(RetryingShellCommand(
            name='create_aus_updir',
            command=['bash', '-c',
                     WithProperties('ssh -l %s ' % self.ausUser +
                                    '-i ~/.ssh/%s %s ' % (self.ausSshKey,self.ausHost) +
                                    'mkdir -p %s' % self.ausFullUploadDir)],
            description=['create', 'aus', 'upload', 'dir'],
            haltOnFailure=True,
        ))
        self.addStep(RetryingShellCommand(
            name='upload_complete_snippet',
            command=['scp', '-o', 'User=%s' % self.ausUser,
                     '-o', 'IdentityFile=~/.ssh/%s' % self.ausSshKey,
                     'dist/update/complete.update.snippet',
                     WithProperties('%s:%s/complete.txt' % (self.ausHost,
                                                            self.ausFullUploadDir))],
             workdir=self.absMozillaObjDir,
             description=['upload', 'complete', 'snippet'],
             haltOnFailure=True,
        ))
 
    def addUpdateSteps(self):
        self.addCreateSnippetsSteps()
        self.addUploadSnippetsSteps()

    def addBuildSymbolsStep(self):
        objdir = WithProperties('%(basedir)s/build/' + self.objdir)
        if self.platform.startswith('win'):
            objdir = 'build/%s' % self.objdir
        self.addStep(ScratchboxCommand(
         name='make_buildsymbols',
         command=['make', 'buildsymbols'],
         env=self.env,
         workdir=objdir,
         sb=self.use_scratchbox,
         haltOnFailure=True,
         timeout=60*60,
        ))

    def addUploadSymbolsStep(self):
        objdir = WithProperties('%(basedir)s/build/' + self.objdir)
        if self.platform.startswith('win'):
            objdir = 'build/%s' % self.objdir
        self.addStep(ScratchboxCommand(
         name='make_uploadsymbols',
         command=['make', 'uploadsymbols'],
         env=self.env,
         workdir=objdir,
         sb=self.use_scratchbox,
         haltOnFailure=True,
         timeout=2400, # 40 minutes
        ))

    def addPostBuildCleanupSteps(self):
        if self.nightly:
            self.addStep(ShellCommand(
             name='rm_builddir',
             command=['rm', '-rf', 'build'],
             env=self.env,
             workdir='.',
             timeout=5400 # 1.5 hours
            ))

class TryBuildFactory(MercurialBuildFactory):
    def __init__(self,talosMasters=None, unittestMasters=None, packageUrl=None,
                 packageDir=None, unittestBranch=None, tinderboxBuildsDir=None,
                 **kwargs):

        self.packageUrl = packageUrl
        # The directory the packages go into
        self.packageDir = packageDir

        if talosMasters is None:
            self.talosMasters = []
        else:
            assert packageUrl
            self.talosMasters = talosMasters

        self.unittestMasters = unittestMasters or []
        self.unittestBranch = unittestBranch

        if self.unittestMasters:
            assert self.unittestBranch
            assert packageUrl

        self.tinderboxBuildsDir = tinderboxBuildsDir

        MercurialBuildFactory.__init__(self, **kwargs)

    def addSourceSteps(self):
        if self.useSharedCheckouts:
            # We normally rely on the Mercurial step to clobber for us, but
            # since we're managing the checkout ourselves...
            self.addStep(ShellCommand(
                name='clobber_build',
                command=['rm', '-rf', 'build'],
                workdir='.',
                timeout=60*60,
            ))
            self.addStep(JSONPropertiesDownload(
                name="download_props",
                slavedest="buildprops.json",
                workdir='.'
            ))

            step = self.makeHgtoolStep(
                    clone_by_revision=True,
                    bundles=[],
                    wc='build',
                    workdir='.',
                    locks=[hg_try_lock.access('counting')],
                    )
            self.addStep(step)

        else:
            self.addStep(Mercurial(
            name='hg_update',
            mode='clobber',
            baseURL='http://%s/' % self.hgHost,
            defaultBranch=self.repoPath,
            timeout=60*60, # 1 hour
            locks=[hg_try_lock.access('counting')],
            ))

        if self.buildRevision:
            self.addStep(ShellCommand(
             name='hg_update',
             command=['hg', 'up', '-C', '-r', self.buildRevision],
             haltOnFailure=True
            ))
        self.addStep(SetProperty(
         name = 'set_got_revision',
         command=['hg', 'parent', '--template={node}'],
         extract_fn = short_hash
        ))
        changesetLink = '<a href=http://%s/%s/rev' % (self.hgHost,
                                                      self.repoPath)
        changesetLink += '/%(got_revision)s title="Built from revision %(got_revision)s">rev:%(got_revision)s</a>'
        self.addStep(OutputStep(
         name='tinderboxprint_changeset_link',
         data=['TinderboxPrint:', WithProperties(changesetLink)]
        ))
        self.addStep(SetBuildProperty(
            name='set_comments',
            property_name="comments",
            value=lambda build:build.source.changes[-1].comments if len(build.source.changes) > 0 else "",
        ))

    def addLeakTestSteps(self):
        # we want the same thing run a few times here, with different
        # extraArgs
        leakEnv = self.env.copy()
        leakEnv['MINIDUMP_STACKWALK'] = getPlatformMinidumpPath(self.platform)
        leakEnv['MINIDUMP_SAVE_PATH'] = WithProperties('%(basedir:-)s/minidumps')
        for args in [['-register'], ['-CreateProfile', 'default'],
                     ['-P', 'default']]:
            self.addStep(AliveTest(
                env=leakEnv,
                workdir='build/%s/_leaktest' % self.mozillaObjdir,
                extraArgs=args,
                warnOnFailure=True,
                haltOnFailure=True
            ))
        baseUrl = 'http://%s/pub/mozilla.org/%s/tinderbox-builds/mozilla-central-%s' % \
            (self.stageServer, self.productName, self.platform)

        self.addLeakTestStepsCommon(baseUrl, leakEnv, False)

    def addCodesighsSteps(self):
        self.addStep(ShellCommand(
         name='make_codesighs',
         command=['make'],
         workdir='build/%s/tools/codesighs' % self.mozillaObjdir
        ))
        self.addStep(RetryingShellCommand(
         name='get_codesize_log',
         command=['wget', '-O', 'codesize-auto-old.log',
         'http://%s/pub/mozilla.org/%s/tinderbox-builds/mozilla-central-%s/codesize-auto.log' % \
           (self.stageServer, self.productName, self.platform)],
         workdir='.',
         env=self.env
        ))
        if self.mozillaDir == '':
            codesighsObjdir = self.objdir
        else:
            codesighsObjdir = '../%s' % self.mozillaObjdir

        self.addStep(Codesighs(
         name='get_codesighs_diff',
         objdir=codesighsObjdir,
         platform=self.platform,
         workdir='build%s' % self.mozillaDir,
         env=self.env,
         tbPrint=self.tbPrint,
        ))

        self.addStep(ShellCommand(
         name='echo_codesize_log',
         command=['cat', '../codesize-auto-diff.log'],
         workdir='build%s' % self.mozillaDir
        ))

    def doUpload(self, postUploadBuildDir=None, uploadMulti=False):
        self.addStep(SetBuildProperty(
             name='set_who',
             property_name='who',
             value=lambda build:str(build.source.changes[0].who) if len(build.source.changes) > 0 else "",
             haltOnFailure=True
        ))

        uploadEnv = self.env.copy()
        uploadEnv.update({
            'UPLOAD_HOST': self.stageServer,
            'UPLOAD_USER': self.stageUsername,
            'UPLOAD_TO_TEMP': '1',
        })

        if self.stageSshKey:
            uploadEnv['UPLOAD_SSH_KEY'] = '~/.ssh/%s' % self.stageSshKey

        # Set up the post upload to the custom try tinderboxBuildsDir
        tinderboxBuildsDir = self.packageDir

        uploadEnv['POST_UPLOAD_CMD'] = postUploadCmdPrefix(
                upload_dir=tinderboxBuildsDir,
                product=self.productName,
                revision=WithProperties('%(got_revision)s'),
                who=WithProperties('%(who)s'),
                builddir=WithProperties('%(branch)s-%(stage_platform)s'),
                buildid=WithProperties('%(buildid)s'),
                to_try=True,
                to_dated=False,
                as_list=False,
                )

        objdir = WithProperties('%(basedir)s/build/' + self.objdir)
        if self.platform.startswith('win'):
            objdir = 'build/%s' % self.objdir
        self.addStep(RetryingScratchboxProperty(
             command=['make', 'upload'],
             env=uploadEnv,
             workdir=objdir,
             sb=self.use_scratchbox,
             extract_fn = parse_make_upload,
             haltOnFailure=True,
             description=["upload"],
             timeout=40*60, # 40 minutes
             log_eval_func=lambda c,s: regex_log_evaluator(c, s, upload_errors),
        ))

        talosBranch = "%s-%s-talos" % (self.branchName, self.complete_platform)
        sendchange_props = {
                'buildid': WithProperties('%(buildid:-)s'),
                'builduid': WithProperties('%(builduid:-)s'),
                }

        for master, warn, retries in self.talosMasters:
            self.addStep(SendChangeStep(
             name='sendchange_%s' % master,
             warnOnFailure=warn,
             master=master,
             retries=retries,
             branch=talosBranch,
             revision=WithProperties('%(got_revision)s'),
             files=[WithProperties('%(packageUrl)s')],
             user=WithProperties('%(who)s'),
             comments=WithProperties('%(comments:-)s'),
             sendchange_props=sendchange_props,
            ))
        for master, warn, retries in self.unittestMasters:
            self.addStep(SendChangeStep(
             name='sendchange_%s' % master,
             warnOnFailure=warn,
             master=master,
             retries=retries,
             branch=self.unittestBranch,
             revision=WithProperties('%(got_revision)s'),
             files=[WithProperties('%(packageUrl)s'),
                     WithProperties('%(testsUrl)s')],
             user=WithProperties('%(who)s'),
             comments=WithProperties('%(comments:-)s'),
             sendchange_props=sendchange_props,
            ))

class CCMercurialBuildFactory(MercurialBuildFactory):
    def __init__(self, skipBlankRepos=False, mozRepoPath='',
                 inspectorRepoPath='', venkmanRepoPath='',
                 chatzillaRepoPath='', cvsroot='', **kwargs):
        self.skipBlankRepos = skipBlankRepos
        self.mozRepoPath = mozRepoPath
        self.inspectorRepoPath = inspectorRepoPath
        self.venkmanRepoPath = venkmanRepoPath
        self.chatzillaRepoPath = chatzillaRepoPath
        self.cvsroot = cvsroot
        MercurialBuildFactory.__init__(self, mozillaDir='mozilla',
            mozconfigBranch='default', **kwargs)

    def addSourceSteps(self):
        # First set our revisions, if no property by the name, use 'default'
        comm_rev = WithProperties("%(polled_comm_revision:-default)s")
        moz_rev = WithProperties("%(polled_moz_revision:-default)s")

        if self.useSharedCheckouts:
            self.addStep(JSONPropertiesDownload(
                name="download_props",
                slavedest="buildprops.json",
                workdir='.'
            ))

            stepCC = self.makeHgtoolStep(
                wc='build',
                workdir='.',
                rev=comm_rev,
                )
            self.addStep(stepCC)
            
            stepMC = self.makeHgtoolStep(
                name="moz_hg_update",
                wc='build%s' % self.mozillaDir,
                workdir='.',
                rev=moz_rev,
                repo_url=self.getRepository(self.mozRepoPath),
                )
            self.addStep(stepMC)
        else:
            self.addStep(Mercurial(
                name='hg_update',
                mode='update',
                baseURL='http://%s/' % self.hgHost,
                defaultBranch=self.repoPath,
                alwaysUseLatest=True,
                timeout=60*60 # 1 hour
            ))

        if self.buildRevision:
            self.addStep(ShellCommand(
             name='hg_update',
             command=['hg', 'up', '-C', '-r', self.buildRevision],
             haltOnFailure=True
            ))
        self.addStep(SetProperty(
         name='set_got_revision',
         command=['hg', 'identify', '-i'],
         property='got_revision'
        ))
        #Fix for bug 612319 to correct http://ssh:// changeset links
        if self.hgHost[0:5] == "ssh://":
            changesetLink = '<a href=https://%s/%s/rev' % (self.hgHost[6:],
                                                           self.repoPath)
        else: 
            changesetLink = '<a href=http://%s/%s/rev' % (self.hgHost,
                                                          self.repoPath)
        changesetLink += '/%(got_revision)s title="Built from revision %(got_revision)s">rev:%(got_revision)s</a>'
        self.addStep(OutputStep(
         name='tinderboxprint_changeset',
         data=['TinderboxPrint:', WithProperties(changesetLink)]
        ))
        # build up the checkout command with all options
        co_command = ['python', 'client.py', 'checkout']
        # comm-* is handled by code above, no need to do network churn here
        co_command.append("--skip-comm")
        if (not self.useSharedCheckouts) and self.mozRepoPath:
            co_command.append('--mozilla-repo=%s' % self.getRepository(self.mozRepoPath))
        if self.inspectorRepoPath:
            co_command.append('--inspector-repo=%s' % self.getRepository(self.inspectorRepoPath))
        elif self.skipBlankRepos:
            co_command.append('--skip-inspector')
        if self.venkmanRepoPath:
            co_command.append('--venkman-repo=%s' % self.getRepository(self.venkmanRepoPath))
        elif self.skipBlankRepos:
            co_command.append('--skip-venkman')
        if self.chatzillaRepoPath:
            co_command.append('--chatzilla-repo=%s' % self.getRepository(self.chatzillaRepoPath))
        elif self.skipBlankRepos:
            co_command.append('--skip-chatzilla')
        if self.cvsroot:
            co_command.append('--cvsroot=%s' % self.cvsroot)
        if self.buildRevision:
            co_command.append('--mozilla-rev=%s' % self.buildRevision)
            co_command.append('--inspector-rev=%s' % self.buildRevision)
            co_command.append('--venkman-rev=%s' % self.buildRevision)
            co_command.append('--chatzilla-rev=%s' % self.buildRevision)
        # execute the checkout
        self.addStep(ShellCommand(
         command=co_command,
         description=['running', 'client.py', 'checkout'],
         descriptionDone=['client.py', 'checkout'],
         haltOnFailure=True,
         timeout=60*60 # 1 hour
        ))

        self.addStep(SetProperty(
         name='set_hg_revision',
         command=['hg', 'identify', '-i'],
         workdir='build%s' % self.mozillaDir,
         property='hg_revision'
        ))
        #Fix for bug 612319 to correct http://ssh:// changeset links
        if self.hgHost[0:5] == "ssh://":
            changesetLink = '<a href=https://%s/%s/rev' % (self.hgHost[6:],
                                                           self.mozRepoPath)
        else: 
            changesetLink = '<a href=http://%s/%s/rev' % (self.hgHost,
                                                          self.mozRepoPath)
        changesetLink += '/%(hg_revision)s title="Built from Mozilla revision %(hg_revision)s">moz:%(hg_revision)s</a>'
        self.addStep(OutputStep(
         name='tinderboxprint_changeset',
         data=['TinderboxPrint:', WithProperties(changesetLink)]
        ))
        self.addStep(SetBuildProperty(
            name='set_comments',
            property_name="comments",
            value=lambda build:build.source.changes[-1].comments if len(build.source.changes) > 0 else "",
        ))

    def addUploadSteps(self, pkgArgs=None):
        MercurialBuildFactory.addUploadSteps(self, pkgArgs)
        self.addStep(ShellCommand(
         command=['make', 'package-compare'],
         workdir='build/%s' % self.objdir,
         haltOnFailure=False
        ))


def marFilenameToProperty(prop_name=None):
    '''Parse a file listing and return the first mar filename found as
       a named property.
    '''
    def parseMarFilename(rc, stdout, stderr):
        if prop_name is not None:
            for line in filter(None, stdout.split('\n')):
                line = line.strip()
                if re.search(r'\.mar$', line):
                    return {prop_name: line}
        return {}
    return parseMarFilename


class NightlyBuildFactory(MercurialBuildFactory):
    def __init__(self, talosMasters=None, unittestMasters=None,
            unittestBranch=None, tinderboxBuildsDir=None, 
            **kwargs):

        self.talosMasters = talosMasters or []

        self.unittestMasters = unittestMasters or []
        self.unittestBranch = unittestBranch

        if self.unittestMasters:
            assert self.unittestBranch

        self.tinderboxBuildsDir = tinderboxBuildsDir

        MercurialBuildFactory.__init__(self, **kwargs)

    def makePartialTools(self):
        '''The mar and bsdiff tools are created by default when 
           --enable-update-packaging is specified, but some subclasses may 
           need to explicitly build the tools.
        '''
        pass

    def getCompleteMarPatternMatch(self):
        marPattern = getPlatformFtpDir(self.platform)
        if not marPattern:
            return False
        marPattern += '.complete.mar'
        return marPattern

    def previousMarExists(self, step):
        return step.build.getProperties().has_key("previousMarFilename") and len(step.build.getProperty("previousMarFilename")) > 0;

    def addCreatePartialUpdateSteps(self, extraArgs=None):
        '''This function expects that the following build properties are
           already set: buildid, completeMarFilename
        '''
        self.makePartialTools()
        # These tools (mar+mbsdiff) should now be built.
        mar='../dist/host/bin/mar'
        mbsdiff='../dist/host/bin/mbsdiff'
        # Unpack the current complete mar we just made.
        updateEnv = self.env.copy()
        updateEnv['MAR'] = mar
        updateEnv['MBSDIFF'] = mbsdiff
        self.addStep(ShellCommand(
            name='rm_unpack_dirs',
            command=['rm', '-rf', 'current', 'previous'],
            env=updateEnv,
            workdir=self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        self.addStep(ShellCommand(
            name='make_unpack_dirs',
            command=['mkdir', 'current', 'previous'],
            env=updateEnv,
            workdir=self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        self.addStep(ShellCommand(
            name='unpack_current_mar',
            command=['bash', '-c',
                     WithProperties('%(basedir)s/' +
                                    self.absMozillaSrcDir +
                                    '/tools/update-packaging/unwrap_full_update.pl ' +
                                    '../dist/update/%(completeMarFilename)s')],
            env=updateEnv,
            haltOnFailure=True,
            workdir='%s/current' % self.absMozillaObjDir,
        ))
        # The mar file name will be the same from one day to the next,
        # *except* when we do a version bump for a release. To cope with
        # this, we get the name of the previous complete mar directly
        # from staging. Version bumps can also often involve multiple mars
        # living in the latest dir, so we grab the latest one.            
        marPattern = self.getCompleteMarPatternMatch()
        self.addStep(SetProperty(
            name='get_previous_mar_filename',
            description=['get', 'previous', 'mar', 'filename'],
            command=['bash', '-c',
                     WithProperties('ssh -l %s -i ~/.ssh/%s %s ' % (self.stageUsername,
                                                                    self.stageSshKey,
                                                                    self.stageServer) +
                                    'ls -1t %s | grep %s$ | head -n 1' % (self.latestDir,
                                                                        marPattern))
                     ],
            extract_fn=marFilenameToProperty(prop_name='previousMarFilename'),
            flunkOnFailure=False,
            haltOnFailure=False,
            warnOnFailure=True
        ))
        previousMarURL = WithProperties('http://%s' % self.stageServer + \
                          '%s' % self.latestDir + \
                          '/%(previousMarFilename)s')
        self.addStep(RetryingShellCommand(
            name='get_previous_mar',
            description=['get', 'previous', 'mar'],
            doStepIf = self.previousMarExists,
            command=['wget', '-O', 'previous.mar', '--no-check-certificate',
                     previousMarURL],
            workdir='%s/dist/update' % self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        # Unpack the previous complete mar.                                    
        self.addStep(ShellCommand(
            name='unpack_previous_mar',
            description=['unpack', 'previous', 'mar'],
            doStepIf = self.previousMarExists,
            command=['bash', '-c',
                     WithProperties('%(basedir)s/' +
                                    self.absMozillaSrcDir +
                                    '/tools/update-packaging/unwrap_full_update.pl ' +
                                    '../dist/update/previous.mar')],
            env=updateEnv,
            workdir='%s/previous' % self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        # Extract the build ID from the unpacked previous complete mar.
        self.addStep(FindFile(
            name='find_inipath',
            description=['find', 'inipath'],
            doStepIf = self.previousMarExists,
            filename='application.ini',
            directory='previous',
            filetype='file',
            max_depth=4,
            property_name='previous_inipath',
            workdir=self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        self.addStep(SetProperty(
            name='set_previous_buildid',
            description=['set', 'previous', 'buildid'],
            doStepIf = self.previousMarExists,
            command=['python',
                     '%s/config/printconfigsetting.py' % self.absMozillaSrcDir,
                     WithProperties(self.absMozillaObjDir + '/%(previous_inipath)s'),
                     'App', 'BuildID'],
            property='previous_buildid',
            workdir='.',
            haltOnFailure=True,
        ))
        # Generate the partial patch from the two unpacked complete mars.
        partialMarCommand=['make', '-C',
                           'tools/update-packaging', 'partial-patch',
                           'STAGE_DIR=../../dist/update',
                           'SRC_BUILD=../../previous',
                           WithProperties('SRC_BUILD_ID=%(previous_buildid)s'),
                           'DST_BUILD=../../current',
                           WithProperties('DST_BUILD_ID=%(buildid)s')]
        if extraArgs is not None:
            partialMarCommand.extend(extraArgs)
        self.addStep(ShellCommand(
            name='make_partial_mar',
            description=['make', 'partial', 'mar'],
            doStepIf = self.previousMarExists,
            command=partialMarCommand,
            env=updateEnv,
            workdir=self.absMozillaObjDir,
            flunkOnFailure=True,
            haltOnFailure=False,
        ))
        self.addStep(ShellCommand(
            name='rm_previous_mar',
            description=['rm', 'previous', 'mar'],
            doStepIf = self.previousMarExists,
            command=['rm', '-rf', 'previous.mar'],
            env=self.env,
            workdir='%s/dist/update' % self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        # Update the build properties to pickup information about the partial.
        self.addFilePropertiesSteps(
            filename='*.partial.*.mar',
            doStepIf = self.previousMarExists,
            directory='%s/dist/update' % self.absMozillaObjDir,
            fileType='partialMar',
            haltOnFailure=True,
        )

    def addCreateUpdateSteps(self):
        self.addStep(ShellCommand(
            name='rm_existing_mars',
            command=['bash', '-c', 'rm -rvf *.mar'],
            env=self.env,
            workdir='%s/dist/update' % self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        # Run the parent steps to generate the complete mar.
        MercurialBuildFactory.addCreateUpdateSteps(self)
        if self.createPartial:
            self.addCreatePartialUpdateSteps()

    def addCreateSnippetsSteps(self, milestone_extra=''):
        MercurialBuildFactory.addCreateSnippetsSteps(self, milestone_extra)
        milestone = self.branchName + milestone_extra
        if self.createPartial:
            self.addStep(CreatePartialUpdateSnippet(
                name='create_partial_snippet',
                doStepIf = self.previousMarExists,
                objdir=self.absMozillaObjDir,
                milestone=milestone,
                baseurl='%s/nightly' % self.downloadBaseURL,
                hashType=self.hashType,
            ))
            self.addStep(ShellCommand(
                name='cat_partial_snippet',
                description=['cat', 'partial', 'snippet'],
                doStepIf = self.previousMarExists,
                command=['cat', 'partial.update.snippet'],
                workdir='%s/dist/update' % self.absMozillaObjDir,
            ))

    def getPreviousBuildUploadDir(self):
        # Uploading the complete snippet occurs regardless of whether we are
        # generating partials on the slave or not, it just goes to a different
        # path for eventual consumption by the central update generation 
        # server.

        # ausFullUploadDir is expected to point to the correct base path on the
        # AUS server for each case:
        #
        # updates generated centrally: /opt/aus2/build/0/...
        # updates generated on slave:  /opt/aus2/incoming/2/...
        if self.createPartial:
            return "%s/%%(previous_buildid)s/en-US" % \
                                         self.ausFullUploadDir
        else:
            return self.ausFullUploadDir
        
    def getCurrentBuildUploadDir(self):
        if self.createPartial:
            return "%s/%%(buildid)s/en-US" % self.ausFullUploadDir
        else:
            return self.ausFullUploadDir

    def addUploadSnippetsSteps(self):
        ausPreviousBuildUploadDir = self.getPreviousBuildUploadDir()
        self.addStep(RetryingShellCommand(
            name='create_aus_previous_updir',
            doStepIf = self.previousMarExists,
            command=['bash', '-c',
                     WithProperties('ssh -l %s ' %  self.ausUser +
                                    '-i ~/.ssh/%s %s ' % (self.ausSshKey,self.ausHost) +
                                    'mkdir -p %s' % ausPreviousBuildUploadDir)],
            description=['create', 'aus', 'previous', 'upload', 'dir'],
            haltOnFailure=True,
            ))
        self.addStep(RetryingShellCommand(
            name='upload_complete_snippet',
            description=['upload', 'complete', 'snippet'],
            doStepIf = self.previousMarExists,
            command=['scp', '-o', 'User=%s' % self.ausUser,
                     '-o', 'IdentityFile=~/.ssh/%s' % self.ausSshKey,
                     'dist/update/complete.update.snippet',
                     WithProperties('%s:%s/complete.txt' % (self.ausHost,
                                                            ausPreviousBuildUploadDir))],
            workdir=self.absMozillaObjDir,
            haltOnFailure=True,
        ))

        # We only need to worry about empty snippets (and partials obviously)
        # if we are creating partial patches on the slaves.
        if self.createPartial:
            self.addStep(RetryingShellCommand(
                name='upload_partial_snippet',
                doStepIf = self.previousMarExists,
                command=['scp', '-o', 'User=%s' % self.ausUser,
                         '-o', 'IdentityFile=~/.ssh/%s' % self.ausSshKey,
                         'dist/update/partial.update.snippet',
                         WithProperties('%s:%s/partial.txt' % (self.ausHost,
                                                               ausPreviousBuildUploadDir))],
                workdir=self.absMozillaObjDir,
                description=['upload', 'partial', 'snippet'],
                haltOnFailure=True,
            ))
            ausCurrentBuildUploadDir = self.getCurrentBuildUploadDir()
            self.addStep(RetryingShellCommand(
                name='create_aus_current_updir',
                doStepIf = self.previousMarExists,
                command=['bash', '-c',
                         WithProperties('ssh -l %s ' %  self.ausUser +
                                        '-i ~/.ssh/%s %s ' % (self.ausSshKey,self.ausHost) +
                                        'mkdir -p %s' % ausCurrentBuildUploadDir)],
                description=['create', 'aus', 'current', 'upload', 'dir'],
                haltOnFailure=True,
            ))
            # Create remote empty complete/partial snippets for current build.
            # Also touch the remote platform dir to defeat NFS caching on the
            # AUS webheads.
            self.addStep(RetryingShellCommand(
                name='create_empty_snippets',
                doStepIf = self.previousMarExists,
                command=['bash', '-c',
                         WithProperties('ssh -l %s ' %  self.ausUser +
                                        '-i ~/.ssh/%s %s ' % (self.ausSshKey,self.ausHost) +
                                        'touch %s/complete.txt %s/partial.txt %s' % (ausCurrentBuildUploadDir,
                                                                                     ausCurrentBuildUploadDir,
                                                                                     self.ausFullUploadDir))],
                description=['create', 'empty', 'snippets'],
                haltOnFailure=True,
            ))

    def doUpload(self, postUploadBuildDir=None, uploadMulti=False):
        # Because of how the RPM packaging works,
        # we need to tell make upload to look for RPMS
        if 'rpm' in self.complete_platform:
            upload_vars = ["MOZ_PKG_FORMAT=RPM"]
        else:
            upload_vars = []
        uploadEnv = self.env.copy()
        uploadEnv.update({'UPLOAD_HOST': self.stageServer,
                          'UPLOAD_USER': self.stageUsername,
                          'UPLOAD_TO_TEMP': '1'})
        if self.stageSshKey:
            uploadEnv['UPLOAD_SSH_KEY'] = '~/.ssh/%s' % self.stageSshKey

        # Always upload builds to the dated tinderbox builds directories
        if self.tinderboxBuildsDir is None:
            tinderboxBuildsDir = "%s-%s" % (self.branchName, self.stagePlatform)
        else:
            tinderboxBuildsDir = self.tinderboxBuildsDir

        uploadArgs = dict(
                upload_dir=tinderboxBuildsDir,
                product=self.stageProduct,
                buildid=WithProperties("%(buildid)s"),
                revision=WithProperties("%(got_revision)s"),
                as_list=False,
            )
        if self.hgHost.startswith('ssh'):
            uploadArgs['to_shadow'] = True
            uploadArgs['to_tinderbox_dated'] = False
        else:
            uploadArgs['to_shadow'] = False
            uploadArgs['to_tinderbox_dated'] = True

        if self.nightly:
            uploadArgs['to_dated'] = True
            if 'rpm' in self.complete_platform:
                uploadArgs['to_latest'] = False
            else:
                uploadArgs['to_latest'] = True
            if self.post_upload_include_platform:
                # This was added for bug 557260 because of a requirement for
                # mobile builds to upload in a slightly different location
                uploadArgs['branch'] = '%s-%s' % (self.branchName, self.stagePlatform)
            else:
                uploadArgs['branch'] = self.branchName
        if uploadMulti:
            upload_vars.append("AB_CD=multi")
        if postUploadBuildDir:
            uploadArgs['builddir'] = postUploadBuildDir
        uploadEnv['POST_UPLOAD_CMD'] = postUploadCmdPrefix(**uploadArgs)

        if self.productName == 'xulrunner':
            self.addStep(RetryingSetProperty(
             command=['make', '-f', 'client.mk', 'upload'],
             env=uploadEnv,
             workdir='build',
             extract_fn = parse_make_upload,
             haltOnFailure=True,
             description=["upload"],
             timeout=60*60, # 60 minutes
             log_eval_func=lambda c,s: regex_log_evaluator(c, s, upload_errors),
            ))
        else:
            objdir = WithProperties('%(basedir)s/' + self.baseWorkDir + '/' + self.objdir)
            if self.platform.startswith('win'):
                objdir = '%s/%s' % (self.baseWorkDir, self.objdir)
            self.addStep(RetryingScratchboxProperty(
                name='make_upload',
                command=['make', 'upload'] + upload_vars,
                env=uploadEnv,
                workdir=objdir,
                extract_fn = parse_make_upload,
                haltOnFailure=True,
                description=['make', 'upload'],
                sb=self.use_scratchbox,
                timeout=40*60, # 40 minutes
                log_eval_func=lambda c,s: regex_log_evaluator(c, s, upload_errors),
            ))

        if self.profiledBuild and self.branchName not in ('mozilla-1.9.1', 'mozilla-1.9.2', 'mozilla-2.0'):
            talosBranch = "%s-%s-pgo-talos" % (self.branchName, self.complete_platform)
        else:
            talosBranch = "%s-%s-talos" % (self.branchName, self.complete_platform)

        sendchange_props = {
                'buildid': WithProperties('%(buildid:-)s'),
                'builduid': WithProperties('%(builduid:-)s'),
                }
        if self.nightly:
            sendchange_props['nightly_build'] = True
        # Not sure if this having this property is useful now
        # but it is
        if self.profiledBuild:
            sendchange_props['pgo_build'] = True
        else:
            sendchange_props['pgo_build'] = False

        if not uploadMulti:
            for master, warn, retries in self.talosMasters:
                self.addStep(SendChangeStep(
                 name='sendchange_%s' % master,
                 warnOnFailure=warn,
                 master=master,
                 retries=retries,
                 branch=talosBranch,
                 revision=WithProperties("%(got_revision)s"),
                 files=[WithProperties('%(packageUrl)s')],
                 user="sendchange",
                 comments=WithProperties('%(comments:-)s'),
                 sendchange_props=sendchange_props,
                ))

            files = [WithProperties('%(packageUrl)s')]
            if '1.9.1' not in self.branchName:
                files.append(WithProperties('%(testsUrl)s'))

            for master, warn, retries in self.unittestMasters:
                self.addStep(SendChangeStep(
                 name='sendchange_%s' % master,
                 warnOnFailure=warn,
                 master=master,
                 retries=retries,
                 branch=self.unittestBranch,
                 revision=WithProperties("%(got_revision)s"),
                 files=files,
                 user="sendchange-unittest",
                 comments=WithProperties('%(comments:-)s'),
                 sendchange_props=sendchange_props,
                ))


class CCNightlyBuildFactory(CCMercurialBuildFactory, NightlyBuildFactory):
    def __init__(self, skipBlankRepos=False, mozRepoPath='',
                 inspectorRepoPath='', venkmanRepoPath='',
                 chatzillaRepoPath='', cvsroot='', **kwargs):
        self.skipBlankRepos = skipBlankRepos
        self.mozRepoPath = mozRepoPath
        self.inspectorRepoPath = inspectorRepoPath
        self.venkmanRepoPath = venkmanRepoPath
        self.chatzillaRepoPath = chatzillaRepoPath
        self.cvsroot = cvsroot
        NightlyBuildFactory.__init__(self, mozillaDir='mozilla',
            mozconfigBranch='default', **kwargs)

    # MercurialBuildFactory defines those, and our inheritance chain makes us
    # look there before NightlyBuildFactory, so we need to define them here and
    # call the actually wanted implementation.
    def addCreateUpdateSteps(self):
        NightlyBuildFactory.addCreateUpdateSteps(self)

    def addCreateSnippetsSteps(self, milestone_extra=''):
        NightlyBuildFactory.addCreateSnippetsSteps(self, milestone_extra)

    def addUploadSnippetsSteps(self):
        NightlyBuildFactory.addUploadSnippetsSteps(self)


class ReleaseBuildFactory(MercurialBuildFactory):
    def __init__(self, env, version, buildNumber, brandName=None,
            unittestMasters=None, unittestBranch=None, talosMasters=None,
            usePrettyNames=True, enableUpdatePackaging=True, oldVersion=None,
            oldBuildNumber=None, **kwargs):
        self.version = version
        self.buildNumber = buildNumber

        self.talosMasters = talosMasters or []
        self.unittestMasters = unittestMasters or []
        self.unittestBranch = unittestBranch
        self.enableUpdatePackaging = enableUpdatePackaging
        if self.unittestMasters:
            assert self.unittestBranch
        self.oldVersion = oldVersion
        self.oldBuildNumber = oldBuildNumber

        if brandName:
            self.brandName = brandName
        else:
            self.brandName = kwargs['productName'].capitalize()
        self.UPLOAD_EXTRA_FILES = []
        # Copy the environment to avoid screwing up other consumers of
        # MercurialBuildFactory
        env = env.copy()
        # Make sure MOZ_PKG_PRETTYNAMES is on and override MOZ_PKG_VERSION
        # The latter is only strictly necessary for RCs.
        if usePrettyNames:
            env['MOZ_PKG_PRETTYNAMES'] = '1'
        env['MOZ_PKG_VERSION'] = version
        MercurialBuildFactory.__init__(self, env=env, **kwargs)

    def addFilePropertiesSteps(self, filename=None, directory=None,
                               fileType=None, maxDepth=1, haltOnFailure=False):
        # We don't need to do this for release builds.
        pass

    def addCreatePartialUpdateSteps(self):
        updateEnv = self.env.copy()
        # need to use absolute paths since mac may have difeerent layout
        # objdir/arch/dist vs objdir/dist
        updateEnv['MAR'] = WithProperties(
            '%(basedir)s' + '/%s/dist/host/bin/mar' % self.absMozillaObjDir)
        updateEnv['MBSDIFF'] = WithProperties(
            '%(basedir)s' + '/%s/dist/host/bin/mbsdiff' % self.absMozillaObjDir)
        current_mar_name = '%s-%s.complete.mar' % (self.productName,
                                                   self.version)
        previous_mar_name = '%s-%s.complete.mar' % (self.productName,
                                                    self.oldVersion)
        partial_mar_name = '%s-%s-%s.partial.mar' % \
            (self.productName, self.oldVersion, self.version)
        oldCandidatesDir = makeCandidatesDir(
            self.productName, self.oldVersion, self.oldBuildNumber,
            protocol='http', server=self.stageServer)
        previousMarURL = '%s/update/%s/en-US/%s' % \
            (oldCandidatesDir, getPlatformFtpDir(self.platform),
             previous_mar_name)
        mar_unpack_cmd = WithProperties(
            '%(basedir)s' + '/%s/tools/update-packaging/unwrap_full_update.pl' % self.absMozillaSrcDir)
        partial_mar_cmd = WithProperties(
            '%(basedir)s' + '/%s/tools/update-packaging/make_incremental_update.sh' % self.absMozillaSrcDir)
        update_dir = 'update/%s/en-US' % getPlatformFtpDir(self.platform)

        self.addStep(ShellCommand(
            name='rm_unpack_dirs',
            command=['rm', '-rf', 'current', 'previous'],
            env=updateEnv,
            workdir=self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        self.addStep(ShellCommand(
            name='make_unpack_dirs',
            command=['mkdir', 'current', 'previous'],
            env=updateEnv,
            workdir=self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        self.addStep(RetryingShellCommand(
            name='get_previous_mar',
            description=['get', 'previous', 'mar'],
            command=['wget', '-O', 'previous.mar', '--no-check-certificate',
                     previousMarURL],
            workdir='%s/dist' % self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        self.addStep(ShellCommand(
            name='unpack_previous_mar',
            description=['unpack', 'previous', 'mar'],
            command=['perl', mar_unpack_cmd, '../dist/previous.mar'],
            env=updateEnv,
            workdir='%s/previous' % self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        self.addStep(ShellCommand(
            name='unpack_current_mar',
            command=['perl', mar_unpack_cmd,
                     '../dist/%s/%s' % (update_dir, current_mar_name)],
            env=updateEnv,
            haltOnFailure=True,
            workdir='%s/current' % self.absMozillaObjDir,
        ))
        self.addStep(ShellCommand(
            name='make_partial_mar',
            description=['make', 'partial', 'mar'],
            command=['bash', partial_mar_cmd,
                     '%s/%s' % (update_dir, partial_mar_name),
                     '../previous', '../current'],
            env=updateEnv,
            workdir='%s/dist' % self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        # the previous script exits 0 always, need to check if mar exists
        self.addStep(ShellCommand(
                    name='check_partial_mar',
                    description=['check', 'partial', 'mar'],
                    command=['ls', '-l',
                             '%s/%s' % (update_dir, partial_mar_name)],
                    workdir='%s/dist' % self.absMozillaObjDir,
                    haltOnFailure=True,
        ))
        self.UPLOAD_EXTRA_FILES.append('%s/%s' % (update_dir, partial_mar_name))
        if self.enableSigning and self.signingServers:
            partial_mar_path = '"%s/dist/%s/%s"' % \
                (self.absMozillaObjDir, update_dir, partial_mar_name)
            cmd = '%s -f gpg -f mar "%s"' % (self.signing_command,
                                             partial_mar_path)
            self.addStep(ShellCommand(
                name='sign_partial_mar',
                description=['sign', 'partial', 'mar'],
                command=['bash', '-c', WithProperties(cmd)],
                env=updateEnv,
                workdir='.',
                haltOnFailure=True,
            ))
            self.UPLOAD_EXTRA_FILES.append('%s/%s.asc' % (update_dir,
                                                          partial_mar_name))

    def doUpload(self, postUploadBuildDir=None, uploadMulti=False):
        info_txt = '%s_info.txt' % self.complete_platform
        self.UPLOAD_EXTRA_FILES.append(info_txt)
        # Make sure the complete MAR has been generated
        if self.enableUpdatePackaging:
            self.addStep(ShellCommand(
                name='make_update_pkg',
                command=['make', '-C',
                         '%s/tools/update-packaging' % self.mozillaObjdir],
                env=self.env,
                haltOnFailure=True
            ))
            if self.createPartial:
                self.addCreatePartialUpdateSteps()
        self.addStep(ShellCommand(
         name='echo_buildID',
         command=['bash', '-c',
                  WithProperties('echo buildID=%(buildid)s > ' + info_txt)],
         workdir='build/%s/dist' % self.mozillaObjdir
        ))

        uploadEnv = self.env.copy()
        uploadEnv.update({'UPLOAD_HOST': self.stageServer,
                          'UPLOAD_USER': self.stageUsername,
                          'UPLOAD_TO_TEMP': '1',
                          'UPLOAD_EXTRA_FILES': ' '.join(self.UPLOAD_EXTRA_FILES)})
        if self.stageSshKey:
            uploadEnv['UPLOAD_SSH_KEY'] = '~/.ssh/%s' % self.stageSshKey

        uploadArgs = dict(
            product=self.productName,
            version=self.version,
            buildNumber=str(self.buildNumber),
            as_list=False)
        if self.enableSigning and self.signingServers:
            uploadArgs['signed'] = True
        upload_vars = []

        if self.productName == 'fennec':
            builddir = '%s/en-US' % self.stagePlatform
            if uploadMulti:
                builddir = '%s/multi' % self.stagePlatform
                upload_vars = ['AB_CD=multi']
            if postUploadBuildDir:
                builddir = '%s/%s' % (self.stagePlatform, postUploadBuildDir)
            uploadArgs['builddir'] = builddir
            uploadArgs['to_mobile_candidates'] = True
            uploadArgs['nightly_dir'] = 'candidates'
            uploadArgs['product'] = 'mobile'
        else:
            uploadArgs['to_candidates'] = True

        uploadEnv['POST_UPLOAD_CMD'] = postUploadCmdPrefix(**uploadArgs)

        objdir = WithProperties('%(basedir)s/build/' + self.objdir)
        if self.platform.startswith('win'):
            objdir = 'build/%s' % self.objdir
        self.addStep(RetryingScratchboxProperty(
         name='make_upload',
         command=['make', 'upload'] + upload_vars,
         env=uploadEnv,
         workdir=objdir,
         extract_fn=parse_make_upload,
         haltOnFailure=True,
         description=['upload'],
         timeout=60*60, # 60 minutes
         log_eval_func=lambda c,s: regex_log_evaluator(c, s, upload_errors),
         sb=self.use_scratchbox,
        ))

        if self.productName == 'fennec' and not uploadMulti:
            cmd = ['scp']
            if self.stageSshKey:
                cmd.append('-oIdentityFile=~/.ssh/%s' % self.stageSshKey)
            cmd.append(info_txt)
            candidates_dir = makeCandidatesDir(self.productName, self.version,
                                               self.buildNumber)
            cmd.append('%s@%s:%s' % (self.stageUsername, self.stageServer,
                                     candidates_dir))
            self.addStep(RetryingShellCommand(
                name='upload_buildID',
                command=cmd,
                workdir='build/%s/dist' % self.mozillaObjdir
            ))

        # Send to the "release" branch on talos, it will do
        # super-duper-extra testing
        talosBranch = "release-%s-%s-talos" % (self.branchName, self.complete_platform)
        sendchange_props = {
                'buildid': WithProperties('%(buildid:-)s'),
                'builduid': WithProperties('%(builduid:-)s'),
                }
        for master, warn, retries in self.talosMasters:
            self.addStep(SendChangeStep(
             name='sendchange_%s' % master,
             warnOnFailure=warn,
             master=master,
             retries=retries,
             branch=talosBranch,
             revision=WithProperties("%(got_revision)s"),
             files=[WithProperties('%(packageUrl)s')],
             user="sendchange",
             comments=WithProperties('%(comments:-)s'),
             sendchange_props=sendchange_props,
            ))

        for master, warn, retries in self.unittestMasters:
            self.addStep(SendChangeStep(
             name='sendchange_%s' % master,
             warnOnFailure=warn,
             master=master,
             retries=retries,
             branch=self.unittestBranch,
             revision=WithProperties("%(got_revision)s"),
             files=[WithProperties('%(packageUrl)s'),
                    WithProperties('%(testsUrl)s')],
             user="sendchange-unittest",
             comments=WithProperties('%(comments:-)s'),
             sendchange_props=sendchange_props,
            ))

class XulrunnerReleaseBuildFactory(ReleaseBuildFactory):
    def doUpload(self, postUploadBuildDir=None, uploadMulti=False):
        uploadEnv = self.env.copy()
        uploadEnv.update({'UPLOAD_HOST': self.stageServer,
                          'UPLOAD_USER': self.stageUsername,
                          'UPLOAD_TO_TEMP': '1'})
        if self.stageSshKey:
            uploadEnv['UPLOAD_SSH_KEY'] = '~/.ssh/%s' % self.stageSshKey

        uploadEnv['POST_UPLOAD_CMD'] = 'post_upload.py ' + \
                                       '-p %s ' % self.productName + \
                                       '-v %s ' % self.version + \
                                       '-n %s ' % self.buildNumber + \
                                       '--release-to-candidates-dir'
        def get_url(rc, stdout, stderr):
            for m in re.findall("^(http://.*?\.(?:tar\.bz2|dmg|zip))", "\n".join([stdout, stderr]), re.M):
                if m.endswith("crashreporter-symbols.zip"):
                    continue
                if m.endswith("tests.tar.bz2"):
                    continue
                return {'packageUrl': m}
            return {'packageUrl': ''}

        self.addStep(RetryingSetProperty(
         command=['make', '-f', 'client.mk', 'upload'],
         env=uploadEnv,
         workdir='build',
         extract_fn = get_url,
         haltOnFailure=True,
         description=['upload'],
         log_eval_func=lambda c,s: regex_log_evaluator(c, s, upload_errors),
        ))

class CCReleaseBuildFactory(CCMercurialBuildFactory, ReleaseBuildFactory):
    def __init__(self, mozRepoPath='', inspectorRepoPath='',
                 venkmanRepoPath='', chatzillaRepoPath='', cvsroot='',
                 **kwargs):
        self.skipBlankRepos = True
        self.mozRepoPath = mozRepoPath
        self.inspectorRepoPath = inspectorRepoPath
        self.venkmanRepoPath = venkmanRepoPath
        self.chatzillaRepoPath = chatzillaRepoPath
        self.cvsroot = cvsroot
        ReleaseBuildFactory.__init__(self, mozillaDir='mozilla',
            mozconfigBranch='default', **kwargs)

    def addFilePropertiesSteps(self, filename=None, directory=None,
                               fileType=None, maxDepth=1, haltOnFailure=False):
        # We don't need to do this for release builds.
        pass


def identToProperties(default_prop=None):
    '''Create a method that is used in a SetProperty step to map the
    output of make ident to build properties.

    To be backwards compat, this allows for a property name to be specified
    to be used for a single hg revision.
    '''
    def list2dict(rv, stdout, stderr):
        props = {}
        stdout = stdout.strip()
        if default_prop is not None and re.match(r'[0-9a-f]{12}\+?', stdout):
            # a single hg version
            props[default_prop] = stdout
        else:
            for l in filter(None, stdout.split('\n')):
                e = filter(None, l.split())
                props[e[0]] = e[1]
        return props
    return list2dict


class BaseRepackFactory(MozillaBuildFactory):
    # Override ignore_dirs so that we don't delete l10n nightly builds
    # before running a l10n nightly build
    ignore_dirs = MozillaBuildFactory.ignore_dirs + [reallyShort('*-nightly')]

    extraConfigureArgs = []

    def __init__(self, project, appName, l10nRepoPath,
                 compareLocalesRepoPath, compareLocalesTag, stageServer,
                 stageUsername, stageSshKey=None, objdir='', platform='',
                 mozconfig=None, configRepoPath=None, configSubDir=None,
                 tree="notset", mozillaDir=None, l10nTag='default',
                 mergeLocales=True, mozconfigBranch="production",
                 testPrettyNames=False, **kwargs):
        MozillaBuildFactory.__init__(self, **kwargs)

        self.platform = platform
        self.project = project
        self.productName = project
        self.appName = appName
        self.l10nRepoPath = l10nRepoPath
        self.l10nTag = l10nTag
        self.compareLocalesRepoPath = compareLocalesRepoPath
        self.compareLocalesTag = compareLocalesTag
        self.mergeLocales = mergeLocales
        self.stageServer = stageServer
        self.stageUsername = stageUsername
        self.stageSshKey = stageSshKey
        self.tree = tree
        self.mozconfig = mozconfig
        self.mozconfigBranch = mozconfigBranch
        self.testPrettyNames = testPrettyNames

        # WinCE is the only platform that will do repackages with
        # a mozconfig for now. This will be fixed in bug 518359
        if mozconfig and configSubDir and configRepoPath:
            self.mozconfig = 'configs/%s/%s/mozconfig' % (configSubDir,
                                                          mozconfig)
            self.configRepoPath = configRepoPath
            self.configRepo = self.getRepository(self.configRepoPath,
                                             kwargs['hgHost'])

        self.addStep(SetBuildProperty(
         property_name='tree',
         value=self.tree,
         haltOnFailure=True
        ))

        self.origSrcDir = self.branchName

        # Mozilla subdir
        if mozillaDir:
            self.mozillaDir = '/%s' % mozillaDir
            self.mozillaSrcDir = '%s/%s' % (self.origSrcDir, mozillaDir)
        else:
            self.mozillaDir = ''
            self.mozillaSrcDir = self.origSrcDir

        # self.mozillaObjdir is used in SeaMonkey's and Thunderbird's case
        self.objdir = objdir or self.origSrcDir
        self.mozillaObjdir = '%s%s' % (self.objdir, self.mozillaDir)

        # These following variables are useful for sharing build steps (e.g.
        # update generation) from classes that use object dirs (e.g. nightly
        # repacks).
        # 
        # We also concatenate the baseWorkDir at the outset to avoid having to
        # do that everywhere.
        self.absMozillaSrcDir = "%s/%s" % (self.baseWorkDir, self.mozillaSrcDir)
        self.absMozillaObjDir = '%s/%s' % (self.baseWorkDir, self.mozillaObjdir)

        self.latestDir = '/pub/mozilla.org/%s' % self.productName + \
                         '/nightly/latest-%s-l10n' % self.branchName
        
        if objdir != '':
            # L10NBASEDIR is relative to MOZ_OBJDIR
            self.env.update({'MOZ_OBJDIR': objdir,
                             'L10NBASEDIR':  '../../%s' % self.l10nRepoPath})            

        if platform == 'macosx64':
            # use "mac" instead of "mac64" for macosx64
            self.env.update({'MOZ_PKG_PLATFORM': 'mac'})

        self.uploadEnv = self.env.copy() # pick up any env variables in our subclass
        self.uploadEnv.update({
            'AB_CD': WithProperties('%(locale)s'),
            'UPLOAD_HOST': stageServer,
            'UPLOAD_USER': stageUsername,
            'UPLOAD_TO_TEMP': '1',
            'POST_UPLOAD_CMD': self.postUploadCmd # defined in subclasses
        })
        if stageSshKey:
            self.uploadEnv['UPLOAD_SSH_KEY'] = '~/.ssh/%s' % stageSshKey

        self.preClean()

        # Need to override toolsdir as set by MozillaBuildFactory because
        # we need Windows-style paths.
        if self.platform.startswith('win'):
            self.addStep(SetProperty(
                command=['bash', '-c', 'pwd -W'],
                property='toolsdir',
                workdir='tools'
            ))

        self.addStep(ShellCommand(
         name='mkdir_l10nrepopath',
         command=['sh', '-c', 'mkdir -p %s' % self.l10nRepoPath],
         descriptionDone='mkdir '+ self.l10nRepoPath,
         workdir=self.baseWorkDir,
         flunkOnFailure=False
        ))

        # call out to overridable functions
        self.getSources()
        self.updateSources()
        self.getMozconfig()
        self.configure()
        self.tinderboxPrintBuildInfo()
        self.downloadBuilds()
        self.updateEnUS()
        self.tinderboxPrintRevisions()
        self.compareLocalesSetup()
        self.compareLocales()
        if self.signingServers and self.enableSigning:
            self.addGetTokenSteps()
        self.doRepack()
        self.doUpload()
        if self.testPrettyNames:
            self.doTestPrettyNames()

    def processCommand(self, **kwargs):
        '''This function is overriden by MaemoNightlyRepackFactory to
        adjust the command and workdir approprietaly for scratchbox
        '''
        return kwargs
    
    def getMozconfig(self):
        if self.mozconfig:
            self.addStep(ShellCommand(
             name='rm_configs',
             command=['rm', '-rf', 'configs'],
             description=['remove', 'configs'],
             workdir='build/'+self.origSrcDir,
             haltOnFailure=True
            ))
            self.addStep(MercurialCloneCommand(
             name='checkout_configs',
             command=['hg', 'clone', self.configRepo, 'configs'],
             description=['checkout', 'configs'],
             workdir='build/'+self.origSrcDir,
             haltOnFailure=True
            ))
            self.addStep(ShellCommand(
             name='hg_update',
             command=['hg', 'update', '-r', self.mozconfigBranch],
             description=['updating', 'mozconfigs'],
             workdir="build/%s/configs" % self.origSrcDir,
             haltOnFailure=True
            ))
            self.addStep(ShellCommand(
             # cp configs/mozilla2/$platform/$branchname/$type/mozconfig .mozconfig
             name='copy_mozconfig',
             command=['cp', self.mozconfig, '.mozconfig'],
             description=['copy mozconfig'],
             workdir='build/'+self.origSrcDir,
             haltOnFailure=True
            ))
            self.addStep(ShellCommand(
             name='cat_mozconfig',
             command=['cat', '.mozconfig'],
             workdir='build/'+self.origSrcDir
            ))

    def configure(self):
        self.addStep(ShellCommand(
         name='autoconf',
         command=['bash', '-c', 'autoconf-2.13'],
         haltOnFailure=True,
         descriptionDone=['autoconf'],
         workdir='%s/%s' % (self.baseWorkDir, self.origSrcDir)
        ))
        if (self.mozillaDir):
            self.addStep(ShellCommand(
             name='autoconf_mozilla',
             command=['bash', '-c', 'autoconf-2.13'],
             haltOnFailure=True,
             descriptionDone=['autoconf mozilla'],
             workdir='%s/%s' % (self.baseWorkDir, self.mozillaSrcDir)
            ))
        self.addStep(ShellCommand(
         name='autoconf_js_src',
         command=['bash', '-c', 'autoconf-2.13'],
         haltOnFailure=True,
         descriptionDone=['autoconf js/src'],
         workdir='%s/%s/js/src' % (self.baseWorkDir, self.mozillaSrcDir)
        ))
        # WinCE is the only platform that will do repackages with
        # a mozconfig for now. This will be fixed in bug 518359
        # For backward compatibility where there is no mozconfig
        self.addStep(ShellCommand( **self.processCommand(
         name='configure',
         command=['sh', '--',
                  './configure', '--enable-application=%s' % self.appName,
                  '--with-l10n-base=../%s' % self.l10nRepoPath ] +
                  self.extraConfigureArgs,
         description='configure',
         descriptionDone='configure done',
         haltOnFailure=True,
         workdir='%s/%s' % (self.baseWorkDir, self.origSrcDir)
        )))
        self.addStep(ShellCommand( **self.processCommand(
         name='make_config',
         command=['make'],
         workdir='%s/%s/config' % (self.baseWorkDir, self.mozillaObjdir),
         description=['make config'],
         haltOnFailure=True
        )))

    def tinderboxPrint(self, propName, propValue):
        self.addStep(OutputStep(
                     name='tinderboxprint_%s' % propName,
                     data=['TinderboxPrint:',
                           '%s:' % propName,
                           propValue]
        ))

    def tinderboxPrintBuildInfo(self):
        '''Display some build properties for scraping in Tinderbox.
        '''
        self.tinderboxPrint('locale',WithProperties('%(locale)s'))
        self.tinderboxPrint('tree',self.tree)
        self.tinderboxPrint('buildnumber',WithProperties('%(buildnumber)s'))

    def doUpload(self, postUploadBuildDir=None, uploadMulti=False):
        self.addStep(RetryingShellCommand(
         name='make_upload',
         command=['make', 'upload', WithProperties('AB_CD=%(locale)s')],
         env=self.uploadEnv,
         workdir='%s/%s/%s/locales' % (self.baseWorkDir, self.objdir,
                                       self.appName),
         haltOnFailure=True,
         flunkOnFailure=True,
         log_eval_func=lambda c,s: regex_log_evaluator(c, s, upload_errors),
        ))

    def getSources(self):
        step = self.makeHgtoolStep(
                name='get_enUS_src',
                wc=self.origSrcDir,
                rev=WithProperties("%(en_revision)s"),
                locks=[hg_l10n_lock.access('counting')],
                workdir=self.baseWorkDir,
                use_properties=False,
                )
        self.addStep(step)

        step = self.makeHgtoolStep(
                name='get_locale_src',
                rev=WithProperties("%(l10n_revision)s"),
                repo_url=WithProperties("http://" + self.hgHost + "/" + \
                                 self.l10nRepoPath + "/%(locale)s"),
                workdir='%s/%s' % (self.baseWorkDir, self.l10nRepoPath),
                locks=[hg_l10n_lock.access('counting')],
                use_properties=False,
                mirrors=[],
                )
        self.addStep(step)

    def updateEnUS(self):
        '''Update the en-US source files to the revision used by
        the repackaged build.

        This is implemented in the subclasses.
        '''
        pass

    def tinderboxPrintRevisions(self):
        '''Display the various revisions used in building for
        scraping in Tinderbox.
        This is implemented in the subclasses.
        '''  
        pass

    def compareLocalesSetup(self):
        compareLocalesRepo = self.getRepository(self.compareLocalesRepoPath)
        self.addStep(ShellCommand(
         name='rm_compare_locales',
         command=['rm', '-rf', 'compare-locales'],
         description=['remove', 'compare-locales'],
         workdir=self.baseWorkDir,
         haltOnFailure=True
        ))
        self.addStep(MercurialCloneCommand(
         name='clone_compare_locales',
         command=['hg', 'clone', compareLocalesRepo, 'compare-locales'],
         description=['checkout', 'compare-locales'],
         workdir=self.baseWorkDir,
         haltOnFailure=True
        ))
        self.addStep(ShellCommand(
         name='update_compare_locales',
         command=['hg', 'up', '-C', '-r', self.compareLocalesTag],
         description='update compare-locales',
         workdir='%s/compare-locales' % self.baseWorkDir,
         haltOnFailure=True
        ))

    def compareLocales(self):
        if self.mergeLocales:
            mergeLocaleOptions = ['-m', 'merged']
            flunkOnFailure = False
            haltOnFailure = False
            warnOnFailure = True
        else:
            mergeLocaleOptions = []
            flunkOnFailure = True
            haltOnFailure = True
            warnOnFailure = False
        self.addStep(ShellCommand(
         name='rm_merged',
         command=['rm', '-rf', 'merged'],
         description=['remove', 'merged'],
         workdir="%s/%s/%s/locales" % (self.baseWorkDir,
                                       self.origSrcDir,
                                       self.appName),
         haltOnFailure=True
        ))
        self.addStep(ShellCommand(
         name='run_compare_locales',
         command=['python',
                  '../../../compare-locales/scripts/compare-locales'] +
                  mergeLocaleOptions +
                  ["l10n.ini",
                  "../../../%s" % self.l10nRepoPath,
                  WithProperties('%(locale)s')],
         description='comparing locale',
         env={'PYTHONPATH': ['../../../compare-locales/lib']},
         flunkOnFailure=flunkOnFailure,
         warnOnFailure=warnOnFailure,
         haltOnFailure=haltOnFailure,
         workdir="%s/%s/%s/locales" % (self.baseWorkDir,
                                       self.origSrcDir,
                                       self.appName),
        ))

    def doRepack(self):
        '''Perform the repackaging.

        This is implemented in the subclasses.
        '''
        pass

    def preClean(self):
        self.addStep(ShellCommand(
         name='rm_dist_upload',
         command=['sh', '-c',
                  'if [ -d '+self.mozillaObjdir+'/dist/upload ]; then ' +
                  'rm -rf '+self.mozillaObjdir+'/dist/upload; ' +
                  'fi'],
         description="rm dist/upload",
         workdir=self.baseWorkDir,
         haltOnFailure=True
        ))

        self.addStep(ShellCommand(
         name='rm_dist_update',
         command=['sh', '-c',
                  'if [ -d '+self.mozillaObjdir+'/dist/update ]; then ' +
                  'rm -rf '+self.mozillaObjdir+'/dist/update; ' +
                  'fi'],
         description="rm dist/update",
         workdir=self.baseWorkDir,
         haltOnFailure=True
        ))

    def doTestPrettyNames(self):
        # Need to re-download this file because it gets removed earlier
        self.addStep(ShellCommand(
         name='wget_enUS',
         command=['make', 'wget-en-US'],
         description='wget en-US',
         env=self.env,
         haltOnFailure=True,
         workdir='%s/%s/%s/locales' % (self.baseWorkDir, self.objdir, self.appName)
        ))
        self.addStep(ShellCommand(
         name='make_unpack',
         command=['make', 'unpack'],
         description='unpack en-US',
         haltOnFailure=True,
         env=self.env,
         workdir='%s/%s/%s/locales' % (self.baseWorkDir, self.objdir, self.appName)
        ))
        # We need to override ZIP_IN because it defaults to $(PACKAGE), which
        # will be the pretty name version here.
        self.addStep(SetProperty(
         command=['make', '--no-print-directory', 'echo-variable-ZIP_IN'],
         property='zip_in',
         env=self.env,
         workdir='%s/%s/%s/locales' % (self.baseWorkDir, self.objdir, self.appName),
         haltOnFailure=True,
        ))
        prettyEnv = self.env.copy()
        prettyEnv['MOZ_PKG_PRETTYNAMES'] = '1'
        prettyEnv['ZIP_IN'] = WithProperties('%(zip_in)s')
        if self.platform.startswith('win'):
            self.addStep(SetProperty(
             command=['make', '--no-print-directory', 'echo-variable-WIN32_INSTALLER_IN'],
             property='win32_installer_in',
             env=self.env,
             workdir='%s/%s/%s/locales' % (self.baseWorkDir, self.objdir, self.appName),
             haltOnFailure=True,
            ))
            prettyEnv['WIN32_INSTALLER_IN'] = WithProperties('%(win32_installer_in)s')
        self.addStep(ShellCommand(
         name='repack_installers_pretty',
         description=['repack', 'installers', 'pretty'],
         command=['sh', '-c',
                  WithProperties('make installers-%(locale)s LOCALE_MERGEDIR=$PWD/merged')],
         env=prettyEnv,
         haltOnFailure=False,
         flunkOnFailure=False,
         warnOnFailure=True,
         workdir='%s/%s/%s/locales' % (self.baseWorkDir, self.objdir, self.appName),
        ))

class CCBaseRepackFactory(BaseRepackFactory):
    # Override ignore_dirs so that we don't delete l10n nightly builds
    # before running a l10n nightly build
    ignore_dirs = MozillaBuildFactory.ignore_dirs + ['*-nightly']

    def __init__(self, skipBlankRepos=False, mozRepoPath='',
                 inspectorRepoPath='', venkmanRepoPath='',
                 chatzillaRepoPath='', cvsroot='', buildRevision='',
                 **kwargs):
        self.skipBlankRepos = skipBlankRepos
        self.mozRepoPath = mozRepoPath
        self.inspectorRepoPath = inspectorRepoPath
        self.venkmanRepoPath = venkmanRepoPath
        self.chatzillaRepoPath = chatzillaRepoPath
        self.cvsroot = cvsroot
        self.buildRevision = buildRevision
        BaseRepackFactory.__init__(self, mozillaDir='mozilla',
            mozconfigBranch='default', **kwargs)

    def getSources(self):
        BaseRepackFactory.getSources(self)
        # build up the checkout command with all options
        co_command = ['python', 'client.py', 'checkout',
                      WithProperties('--comm-rev=%(en_revision)s')]
        if self.mozRepoPath:
            co_command.append('--mozilla-repo=%s' % self.getRepository(self.mozRepoPath))
        if self.inspectorRepoPath:
            co_command.append('--inspector-repo=%s' % self.getRepository(self.inspectorRepoPath))
        elif self.skipBlankRepos:
            co_command.append('--skip-inspector')
        if self.venkmanRepoPath:
            co_command.append('--venkman-repo=%s' % self.getRepository(self.venkmanRepoPath))
        elif self.skipBlankRepos:
            co_command.append('--skip-venkman')
        if self.chatzillaRepoPath:
            co_command.append('--chatzilla-repo=%s' % self.getRepository(self.chatzillaRepoPath))
        elif self.skipBlankRepos:
            co_command.append('--skip-chatzilla')
        if self.cvsroot:
            co_command.append('--cvsroot=%s' % self.cvsroot)
        if self.buildRevision:
            co_command.append('--comm-rev=%s' % self.buildRevision)
            co_command.append('--mozilla-rev=%s' % self.buildRevision)
            co_command.append('--inspector-rev=%s' % self.buildRevision)
            co_command.append('--venkman-rev=%s' % self.buildRevision)
            co_command.append('--chatzilla-rev=%s' % self.buildRevision)
        # execute the checkout
        self.addStep(ShellCommand(
         command=co_command,
         description=['running', 'client.py', 'checkout'],
         descriptionDone=['client.py', 'checkout'],
         haltOnFailure=True,
         workdir='%s/%s' % (self.baseWorkDir, self.origSrcDir),
         timeout=60*60*3 # 3 hours (crazy, but necessary for now)
        ))

class NightlyRepackFactory(BaseRepackFactory, NightlyBuildFactory):
    extraConfigureArgs = []

    def __init__(self, enUSBinaryURL, nightly=False, env={},
                 ausBaseUploadDir=None, updatePlatform=None,
                 downloadBaseURL=None, ausUser=None, ausSshKey=None,
                 ausHost=None, l10nNightlyUpdate=False, l10nDatedDirs=False,
                 createPartial=False, extraConfigureArgs=[], **kwargs):
        self.nightly = nightly
        self.l10nNightlyUpdate = l10nNightlyUpdate
        self.ausBaseUploadDir = ausBaseUploadDir
        self.updatePlatform = updatePlatform
        self.downloadBaseURL = downloadBaseURL
        self.ausUser = ausUser
        self.ausSshKey = ausSshKey
        self.ausHost = ausHost
        self.createPartial = createPartial
        self.geriatricMasters = []
        self.extraConfigureArgs = extraConfigureArgs

        # This is required because this __init__ doesn't call the
        # NightlyBuildFactory __init__ where self.complete_platform
        # is set.  This is only used for android, which doesn't
        # use this factory, so is safe
        self.complete_platform = ''

        env = env.copy()

        env.update({'EN_US_BINARY_URL':enUSBinaryURL})

        # Unfortunately, we can't call BaseRepackFactory.__init__() before this
        # because it needs self.postUploadCmd set
        assert 'project' in kwargs
        assert 'repoPath' in kwargs

        # 1) upload preparation
        if 'branchName' in kwargs:
          uploadDir = '%s-l10n' % kwargs['branchName']
        else:
          uploadDir = '%s-l10n' % self.getRepoName(kwargs['repoPath'])

        uploadArgs = dict(
                product=kwargs['project'],
                branch=uploadDir,
                as_list=False,
                )
        if l10nDatedDirs:
            # nightly repacks and on-change upload to different places
            if self.nightly:
                uploadArgs['buildid'] = WithProperties("%(buildid)s")
                uploadArgs['to_latest'] = True
                uploadArgs['to_dated'] = True
            else:
                # For the repack-on-change scenario we just want to upload
                # to tinderbox builds
                uploadArgs['upload_dir'] = uploadDir
                uploadArgs['to_tinderbox_builds'] = True
        else:
            # for backwards compatibility when the nightly and repack on-change
            # runs were the same 
            uploadArgs['to_latest'] = True

        self.postUploadCmd = postUploadCmdPrefix(**uploadArgs)

        # 2) preparation for updates
        if l10nNightlyUpdate and self.nightly:
            env.update({'MOZ_MAKE_COMPLETE_MAR': '1', 
                        'DOWNLOAD_BASE_URL': '%s/nightly' % self.downloadBaseURL})
            self.extraConfigureArgs += ['--enable-update-packaging']


        BaseRepackFactory.__init__(self, env=env, **kwargs)

        if l10nNightlyUpdate:
            assert ausBaseUploadDir and updatePlatform and downloadBaseURL
            assert ausUser and ausSshKey and ausHost

            # To preserve existing behavior, we need to set the
            # ausFullUploadDir differently for when we are create all the
            # mars (complete+partial) ourselves.
            if self.createPartial:
                # e.g.:
                # /opt/aus2/incoming/2/Firefox/mozilla-central/WINNT_x86-msvc
                self.ausFullUploadDir = '%s/%s' % (self.ausBaseUploadDir,
                                                   self.updatePlatform)
            else:
                # this is a tad ugly because we need python interpolation
                # as well as WithProperties, e.g.:
                # /opt/aus2/build/0/Firefox/mozilla-central/WINNT_x86-msvc/2008010103/en-US
                self.ausFullUploadDir = '%s/%s/%%(buildid)s/%%(locale)s' % \
                  (self.ausBaseUploadDir, self.updatePlatform)
            NightlyBuildFactory.addCreateSnippetsSteps(self,
                                                       milestone_extra='-l10n')
            NightlyBuildFactory.addUploadSnippetsSteps(self)

    def getPreviousBuildUploadDir(self):
        if self.createPartial:
            return "%s/%%(previous_buildid)s/%%(locale)s" % \
                                         self.ausFullUploadDir
        else:
            return self.ausFullUploadDir

    def getCurrentBuildUploadDir(self):
        if self.createPartial:
            return "%s/%%(buildid)s/%%(locale)s" % self.ausFullUploadDir
        else:
            return self.ausFullUploadDir

    def updateSources(self):
        self.addStep(ShellCommand(
         name='update_locale_source',
         command=['hg', 'up', '-C', '-r', self.l10nTag],
         description='update workdir',
         workdir=WithProperties('build/' + self.l10nRepoPath + '/%(locale)s'),
         haltOnFailure=True
        ))
        self.addStep(SetProperty(
                     command=['hg', 'ident', '-i'],
                     haltOnFailure=True,
                     property='l10n_revision',
                     workdir=WithProperties('build/' + self.l10nRepoPath + 
                                            '/%(locale)s')
        ))

    def downloadBuilds(self):
        self.addStep(RetryingShellCommand(
         name='wget_enUS',
         command=['make', 'wget-en-US'],
         descriptionDone='wget en-US',
         env=self.env,
         haltOnFailure=True,
         workdir='%s/%s/%s/locales' % (self.baseWorkDir, self.objdir, self.appName)
        ))

    def updateEnUS(self):
        '''Update en-US to the source stamp we get from make ident.

        Requires that we run make unpack first.
        '''
        self.addStep(ShellCommand(
                     name='make_unpack',
                     command=['make', 'unpack'],
                     descriptionDone='unpacked en-US',
                     haltOnFailure=True,
                     env=self.env,
                     workdir='%s/%s/%s/locales' % (self.baseWorkDir, self.objdir, self.appName),
                     ))
        self.addStep(SetProperty(
                     command=['make', 'ident'],
                     haltOnFailure=True,
                     workdir='%s/%s/%s/locales' % (self.baseWorkDir, self.objdir, self.appName),
                     extract_fn=identToProperties('fx_revision')
        ))
        self.addStep(ShellCommand(
                     name='update_enUS_revision',
                     command=['hg', 'update', '-C', '-r',
                              WithProperties('%(fx_revision)s')],
                     haltOnFailure=True,
                     workdir='build/' + self.origSrcDir))

    def tinderboxPrintRevisions(self):
        self.tinderboxPrint('fx_revision',WithProperties('%(fx_revision)s'))
        self.tinderboxPrint('l10n_revision',WithProperties('%(l10n_revision)s'))

    def makePartialTools(self):
        # Build the tools we need for update-packaging, specifically bsdiff.
        # Configure can take a while.
        self.addStep(ShellCommand(
            name='make_bsdiff',
            command=['sh', '-c',
                     'if [ ! -e dist/host/bin/mbsdiff ]; then ' +
                     'make tier_nspr; make -C config;' +
                     'make -C modules/libmar; make -C modules/libbz2;' +
                     'make -C other-licenses/bsdiff;'
                     'fi'],
            description=['make', 'bsdiff'],
            workdir=self.absMozillaObjDir,
            haltOnFailure=True,
        ))

    # The parent class gets us most of the way there, we just need to add the
    # locale.
    def getCompleteMarPatternMatch(self):
        return '.%(locale)s.' + NightlyBuildFactory.getCompleteMarPatternMatch(self)

    def doRepack(self):
        self.addStep(ShellCommand(
         name='make_tier_nspr',
         command=['make', 'tier_nspr'],
         workdir='%s/%s' % (self.baseWorkDir, self.mozillaObjdir),
         description=['make tier_nspr'],
         haltOnFailure=True
        ))
        if self.l10nNightlyUpdate:
            # Because we're generating updates we need to build the libmar tools
            self.addStep(ShellCommand(
             name='make_libmar',
             command=['make'],
             workdir='%s/%s/modules/libmar' % (self.baseWorkDir, self.mozillaObjdir),
             description=['make', 'modules/libmar'],
             haltOnFailure=True
            ))
        self.addStep(ShellCommand(
         name='repack_installers',
         description=['repack', 'installers'],
         command=['sh','-c',
                  WithProperties('make installers-%(locale)s LOCALE_MERGEDIR=$PWD/merged')],
         env = self.env,
         haltOnFailure=True,
         workdir='%s/%s/%s/locales' % (self.baseWorkDir, self.objdir, self.appName),
        ))
        self.addStep(FindFile(
            name='find_inipath',
            filename='application.ini',
            directory='dist/l10n-stage',
            filetype='file',
            max_depth=5,
            property_name='inipath',
            workdir=self.absMozillaObjDir,
            haltOnFailure=True,
        ))
        self.addStep(SetProperty(
            command=['python', 'config/printconfigsetting.py',
                     WithProperties('%(inipath)s'),
                     'App', 'BuildID'],
            property='buildid',
            name='get_build_id',
            workdir=self.absMozillaSrcDir,
        ))
        if self.l10nNightlyUpdate:
            # We need the appVersion to create snippets
            self.addStep(SetProperty(
                command=['python', 'config/printconfigsetting.py',
                         WithProperties('%(inipath)s'),
                         'App', 'Version'],
                property='appVersion',
                name='get_app_version',
                workdir=self.absMozillaSrcDir,
            ))
            self.addFilePropertiesSteps(filename='*.complete.mar',
                                        directory='%s/dist/update' % self.absMozillaSrcDir,
                                        fileType='completeMar',
                                        haltOnFailure=True)

        # Remove the source (en-US) package so as not to confuse later steps
        # that look up build details.
        self.addStep(ShellCommand(name='rm_en-US_build',
                                  command=['bash', '-c', 'rm -rvf *.en-US.*'],
                                  description=['remove','en-US','build'],
                                  env=self.env,
                                  workdir='%s/dist' % self.absMozillaObjDir,
                                  haltOnFailure=True)
         )
        if self.l10nNightlyUpdate and self.createPartial:
            self.addCreatePartialUpdateSteps(extraArgs=[WithProperties('AB_CD=%(locale)s')])


class CCNightlyRepackFactory(CCBaseRepackFactory, NightlyRepackFactory):
    def __init__(self, skipBlankRepos=False, mozRepoPath='',
                 inspectorRepoPath='', venkmanRepoPath='',
                 chatzillaRepoPath='', cvsroot='', buildRevision='',
                 **kwargs):
        self.skipBlankRepos = skipBlankRepos
        self.mozRepoPath = mozRepoPath
        self.inspectorRepoPath = inspectorRepoPath
        self.venkmanRepoPath = venkmanRepoPath
        self.chatzillaRepoPath = chatzillaRepoPath
        self.cvsroot = cvsroot
        self.buildRevision = buildRevision
        NightlyRepackFactory.__init__(self, mozillaDir='mozilla',
            mozconfigBranch='default', **kwargs)

    # it sucks to override all of updateEnUS but we need to do it that way
    # this is basically mirroring what mobile does
    def updateEnUS(self):
        '''Update en-US to the source stamp we get from make ident.

        Requires that we run make unpack first.
        '''
        self.addStep(ShellCommand(
                     name='make_unpack',
                     command=['make', 'unpack'],
                     descriptionDone='unpacked en-US',
                     haltOnFailure=True,
                     env=self.env,
                     workdir='%s/%s/%s/locales' % (self.baseWorkDir, self.objdir, self.appName),
        ))
        
        self.addStep(SetProperty(
                     command=['make', 'ident'],
                     haltOnFailure=True,
                     workdir='%s/%s/%s/locales' % (self.baseWorkDir, self.objdir, self.appName),
                     extract_fn=identToProperties()
        ))
        self.addStep(ShellCommand(
                     name='update_comm_enUS_revision',
                     command=['hg', 'update', '-C', '-r',
                              WithProperties('%(comm_revision)s')],
                     haltOnFailure=True,
                     workdir='%s/%s' % (self.baseWorkDir, self.origSrcDir)))
        self.addStep(ShellCommand(
                     name='update_mozilla_enUS_revision',
                     command=['hg', 'update', '-C', '-r',
                              WithProperties('%(moz_revision)s')],
                     haltOnFailure=True,
                     workdir='%s/%s' % (self.baseWorkDir, self.mozillaSrcDir)))

    def tinderboxPrintRevisions(self):
        self.tinderboxPrint('comm_revision',WithProperties('%(comm_revision)s'))
        self.tinderboxPrint('moz_revision',WithProperties('%(moz_revision)s'))
        self.tinderboxPrint('l10n_revision',WithProperties('%(l10n_revision)s'))

    # BaseRepackFactory defines that, and our inheritance chain makes us look
    # there before NightlyRepackFactory, so we need to define it here and call
    # the actually wanted implementation.
    def doRepack(self):
        NightlyRepackFactory.doRepack(self)


class ReleaseFactory(MozillaBuildFactory):
    def getCandidatesDir(self, product, version, buildNumber,
                         nightlyDir="nightly"):
        # can be used with rsync, eg host + ':' + getCandidatesDir()
        # and "http://' + host + getCandidatesDir()
        return '/pub/mozilla.org/' + product + '/' + nightlyDir + '/' + \
               str(version) + '-candidates/build' + str(buildNumber) + '/'

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

class CCReleaseRepackFactory(CCBaseRepackFactory, ReleaseFactory):
    def __init__(self, platform, buildRevision, version, buildNumber,
                 env={}, brandName=None, mergeLocales=False,
                 mozRepoPath='', inspectorRepoPath='', venkmanRepoPath='',
                 chatzillaRepoPath='', cvsroot='', **kwargs):
        self.skipBlankRepos = True
        self.mozRepoPath = mozRepoPath
        self.inspectorRepoPath = inspectorRepoPath
        self.venkmanRepoPath = venkmanRepoPath
        self.chatzillaRepoPath = chatzillaRepoPath
        self.cvsroot = cvsroot
        self.buildRevision = buildRevision
        self.version = version
        self.buildNumber = buildNumber
        if brandName:
            self.brandName = brandName
        else:
            self.brandName = kwargs['project'].capitalize()
        # more vars are added in downloadBuilds
        env.update({
            'MOZ_PKG_PRETTYNAMES': '1',
            'MOZ_PKG_VERSION': self.version,
            'MOZ_MAKE_COMPLETE_MAR': '1'
        })

        assert 'project' in kwargs
        # TODO: better place to put this/call this
        self.postUploadCmd = 'post_upload.py ' + \
                             '-p %s ' % kwargs['project'] + \
                             '-v %s ' % self.version + \
                             '-n %s ' % self.buildNumber + \
                             '--release-to-candidates-dir'
        BaseRepackFactory.__init__(self, env=env, platform=platform,
                                   mergeLocales=mergeLocales, mozillaDir='mozilla',
                                   mozconfigBranch='default', **kwargs)

    # Repeated here since the Parent classes fail hgtool/checkouts due to
    # relbranch issues, and not actually having the tag after clone
    def getSources(self):
        self.addStep(MercurialCloneCommand(
         name='get_enUS_src',
         command=['sh', '-c',
          WithProperties('if [ -d '+self.origSrcDir+'/.hg ]; then ' +
                         'hg -R '+self.origSrcDir+' pull && '+
                         'hg -R '+self.origSrcDir+' up -C ;'+
                         'else ' +
                         'hg clone ' +
                         'http://'+self.hgHost+'/'+self.repoPath+' ' +
                         self.origSrcDir+' ; ' +
                         'fi ' +
                         '&& hg -R '+self.origSrcDir+' update -C -r %(en_revision)s')],
         descriptionDone="en-US source",
         workdir=self.baseWorkDir,
         haltOnFailure=True,
         timeout=30*60 # 30 minutes
        ))
        self.addStep(MercurialCloneCommand(
         name='get_locale_src',
         command=['sh', '-c',
          WithProperties('if [ -d %(locale)s/.hg ]; then ' +
                         'hg -R %(locale)s pull -r default ; ' +
                         'else ' +
                         'hg clone ' +
                         'http://'+self.hgHost+'/'+self.l10nRepoPath+ 
                           '/%(locale)s/ ; ' +
                         'fi ' +
                         '&& hg -R %(locale)s update -C -r %(l10n_revision)s')],
         descriptionDone="locale source",
         timeout=10*60, # 10 minutes
         haltOnFailure=True,
         workdir='%s/%s' % (self.baseWorkDir, self.l10nRepoPath)
        ))
        # build up the checkout command with all options
        co_command = ['python', 'client.py', 'checkout',
                      WithProperties('--comm-rev=%(en_revision)s')]
        if self.mozRepoPath:
            co_command.append('--mozilla-repo=%s' % self.getRepository(self.mozRepoPath))
        if self.inspectorRepoPath:
            co_command.append('--inspector-repo=%s' % self.getRepository(self.inspectorRepoPath))
        elif self.skipBlankRepos:
            co_command.append('--skip-inspector')
        if self.venkmanRepoPath:
            co_command.append('--venkman-repo=%s' % self.getRepository(self.venkmanRepoPath))
        elif self.skipBlankRepos:
            co_command.append('--skip-venkman')
        if self.chatzillaRepoPath:
            co_command.append('--chatzilla-repo=%s' % self.getRepository(self.chatzillaRepoPath))
        elif self.skipBlankRepos:
            co_command.append('--skip-chatzilla')
        if self.cvsroot:
            co_command.append('--cvsroot=%s' % self.cvsroot)
        if self.buildRevision:
            co_command.append('--comm-rev=%s' % self.buildRevision)
            co_command.append('--mozilla-rev=%s' % self.buildRevision)
            co_command.append('--inspector-rev=%s' % self.buildRevision)
            co_command.append('--venkman-rev=%s' % self.buildRevision)
            co_command.append('--chatzilla-rev=%s' % self.buildRevision)
        # execute the checkout
        self.addStep(ShellCommand(
         command=co_command,
         description=['running', 'client.py', 'checkout'],
         descriptionDone=['client.py', 'checkout'],
         haltOnFailure=True,
         workdir='%s/%s' % (self.baseWorkDir, self.origSrcDir),
         timeout=60*60*3 # 3 hours (crazy, but necessary for now)
        ))

    def updateSources(self):
        self.addStep(ShellCommand(
         name='update_sources',
         command=['hg', 'up', '-C', '-r', self.buildRevision],
         workdir='build/'+self.origSrcDir,
         description=['update %s' % self.branchName,
                      'to %s' % self.buildRevision],
         haltOnFailure=True
        ))
        self.addStep(ShellCommand(
         name='update_locale_sources',
         command=['hg', 'up', '-C', '-r', self.buildRevision],
         workdir=WithProperties('build/' + self.l10nRepoPath + '/%(locale)s'),
         description=['update to', self.buildRevision]
        ))
        self.addStep(SetProperty(
                     command=['hg', 'ident', '-i'],
                     haltOnFailure=True,
                     property='l10n_revision',
                     workdir=WithProperties('build/' + self.l10nRepoPath + 
                                            '/%(locale)s')
        ))
        self.addStep(ShellCommand(
         command=['hg', 'up', '-C', '-r', self.buildRevision],
         workdir='build/'+self.mozillaSrcDir,
         description=['update mozilla',
                      'to %s' % self.buildRevision],
         haltOnFailure=True
        ))
        if self.venkmanRepoPath:
            self.addStep(ShellCommand(
             command=['hg', 'up', '-C', '-r', self.buildRevision],
             workdir='build/'+self.mozillaSrcDir+'/extensions/venkman',
             description=['update venkman',
                          'to %s' % self.buildRevision],
             haltOnFailure=True
            ))
        if self.inspectorRepoPath:
            self.addStep(ShellCommand(
             command=['hg', 'up', '-C', '-r', self.buildRevision],
             workdir='build/'+self.mozillaSrcDir+'/extensions/inspector',
             description=['update inspector',
                          'to %s' % self.buildRevision],
             haltOnFailure=True
            ))
        if self.chatzillaRepoPath:
            self.addStep(ShellCommand(
             command=['hg', 'up', '-C', '-r', self.buildRevision],
             workdir='build/'+self.mozillaSrcDir+'/extensions/irc',
             description=['update chatzilla',
                          'to %s' % self.buildRevision],
             haltOnFailure=True
            ))

    def preClean(self):
        # We need to know the absolute path to the input builds when we repack,
        # so we need retrieve at run-time as a build property
        self.addStep(SetProperty(
         command=['bash', '-c', 'pwd'],
         property='srcdir',
         workdir='build/'+self.origSrcDir
        ))

    def downloadBuilds(self):
        candidatesDir = 'http://%s' % self.stageServer + \
                        '/pub/mozilla.org/%s/nightly' % self.project + \
                        '/%s-candidates/build%s' % (self.version,
                                                    self.buildNumber)

        # This block sets platform specific data that our wget command needs.
        #  build is mapping between the local and remote filenames
        #  platformDir is the platform specific directory builds are contained
        #    in on the stagingServer.
        # This block also sets the necessary environment variables that the
        # doRepack() steps rely on to locate their source build.
        builds = {}
        platformDir = getPlatformFtpDir(self.platform.split("-")[0])
        if self.platform.startswith('linux'):
            filename = '%s.tar.bz2' % self.project
            builds[filename] = '%s-%s.tar.bz2' % (self.project, self.version)
            self.env['ZIP_IN'] = WithProperties('%(srcdir)s/' + filename)
        elif self.platform.startswith('macosx'):
            filename = '%s.dmg' % self.project
            builds[filename] = '%s %s.dmg' % (self.brandName,
                                              self.version)
            self.env['ZIP_IN'] = WithProperties('%(srcdir)s/' + filename)
        elif self.platform.startswith('win32'):
            platformDir = 'unsigned/' + platformDir
            filename = '%s.zip' % self.project
            instname = '%s.exe' % self.project
            builds[filename] = '%s-%s.zip' % (self.project, self.version)
            builds[instname] = '%s Setup %s.exe' % (self.brandName,
                                                    self.version)
            self.env['ZIP_IN'] = WithProperties('%(srcdir)s/' + filename)
            self.env['WIN32_INSTALLER_IN'] = \
              WithProperties('%(srcdir)s/' + instname)
        else:
            raise "Unsupported platform"

        for name in builds:
            self.addStep(ShellCommand(
             name='get_candidates_%s' % name,
             command=['wget', '-O', name, '--no-check-certificate',
                      '%s/%s/en-US/%s' % (candidatesDir, platformDir,
                                          builds[name])],
             workdir='build/'+self.origSrcDir,
             haltOnFailure=True
            ))

    def doRepack(self):
        # Because we're generating updates we need to build the libmar tools
        self.addStep(ShellCommand(
            name='make_tier_nspr',
            command=['make','tier_nspr'],
            workdir='build/'+self.mozillaObjdir,
            description=['make tier_nspr'],
            haltOnFailure=True
            ))
        self.addStep(ShellCommand(
             name='make_libmar',
             command=['make'],
             workdir='build/'+self.mozillaObjdir+'/modules/libmar',
             description=['make libmar'],
             haltOnFailure=True
            ))

        self.addStep(ShellCommand(
         name='repack_installers',
         description=['repack', 'installers'],
         command=['sh','-c',
                  WithProperties('make installers-%(locale)s LOCALE_MERGEDIR=$PWD/merged')],
         env=self.env,
         haltOnFailure=True,
         workdir='build/'+self.objdir+'/'+self.appName+'/locales'
        ))

class StagingRepositorySetupFactory(ReleaseFactory):
    """This Factory should be run at the start of a staging release run. It
       deletes and reclones all of the repositories in 'repositories'. Note that
       the staging buildTools repository should _not_ be recloned, as it is
       used by many other builders, too.
    """
    def __init__(self, username, sshKey, repositories, userRepoRoot,
                 **kwargs):
        # MozillaBuildFactory needs the 'repoPath' argument, but we don't
        ReleaseFactory.__init__(self, repoPath='nothing', **kwargs)
        for repoPath in sorted(repositories.keys()):
            repo = self.getRepository(repoPath)
            repoName = self.getRepoName(repoPath)
            # Don't use cache for user repos
            rnd = random.randint(100000, 999999)
            userRepoURL = '%s/%s?rnd=%s' % (self.getRepository(userRepoRoot),
                                        repoName, rnd)

            # test for existence
            command = 'wget -O /dev/null %s' % repo
            command += ' && { '
            command += 'if wget -q -O /dev/null %s; then ' % userRepoURL
            # if it exists, delete it
            command += 'echo "Deleting %s"; ' % repoName
            command += 'ssh -l %s -i %s %s edit %s delete YES; ' % \
              (username, sshKey, self.hgHost, repoName)
            command += 'else echo "Not deleting %s"; exit 0; fi }' % repoName

            self.addStep(ShellCommand(
             name='delete_repo',
             command=['bash', '-c', command],
             description=['delete', repoName],
             haltOnFailure=True,
             timeout=30*60 # 30 minutes
            ))

        # Wait for hg.m.o to catch up
        self.addStep(ShellCommand(
         name='wait_for_hg',
         command=['sleep', '600'],
         description=['wait', 'for', 'hg'],
        ))

        for repoPath in sorted(repositories.keys()):
            repo = self.getRepository(repoPath)
            repoName = self.getRepoName(repoPath)
            timeout = 60*60
            command = ['python',
                       WithProperties('%(toolsdir)s/buildfarm/utils/retry.py'),
                       '--timeout', timeout,
                       'ssh', '-l', username, '-oIdentityFile=%s' % sshKey,
                       self.hgHost, 'clone', repoName, repoPath]

            self.addStep(ShellCommand(
             name='recreate_repo',
             command=command,
             description=['recreate', repoName],
             timeout=timeout
            ))

        # Wait for hg.m.o to catch up
        self.addStep(ShellCommand(
         name='wait_for_hg',
         command=['sleep', '600'],
         description=['wait', 'for', 'hg'],
        ))



class ReleaseTaggingFactory(ReleaseFactory):
    def __init__(self, repositories, productName, appName, version, appVersion,
                 milestone, baseTag, buildNumber, hgUsername, hgSshKey=None,
                 relbranchPrefix=None, buildSpace=1.5, **kwargs):
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
               'js/src/config/milestone.txt', 'config/milestone.txt']
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
           version: What this build is actually called. I most cases this is
                    the version number of the application, eg, 3.0.6, 3.1b2.
                    During the RC phase we "call" builds, eg, 3.1 RC1, but the
                    version of the application is still 3.1. In these cases,
                    version should be set to, eg, 3.1rc1.
           appVersion: The current version number of the application being
                       built. Eg, 3.0.2 for Firefox, 2.0 for Seamonkey, etc.
                       This is different than the platform version. See below.
                       This is usually the same as 'version', except during the
                       RC phase. Eg, when version is 3.1rc1 appVersion is still
                       3.1.
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
           relbranchPrefix: the prefix to start relelease branch names with
                            (defaults to 'GECKO')

        """
        # MozillaBuildFactory needs the 'repoPath' argument, but we don't
        ReleaseFactory.__init__(self, repoPath='nothing', buildSpace=buildSpace,
                                **kwargs)

        # extremely basic validation, to catch really dumb configurations
        assert len(repositories) > 0, \
          'You must provide at least one repository.'
        assert productName, 'You must provide a product name (eg. firefox).'
        assert appName, 'You must provide an application name (eg. browser).'
        assert version, \
          'You must provide an application version (eg. 3.0.2).'
        assert milestone, 'You must provide a milestone (eg. 1.9.0.2).'
        assert baseTag, 'You must provide a baseTag (eg. FIREFOX_3_0_2).'
        assert buildNumber, 'You must provide a buildNumber.'

        # if we're doing a respin we already have a relbranch created
        if buildNumber > 1:
            for repo in repositories:
                assert repositories[repo]['relbranchOverride'], \
                  'No relbranchOverride specified for ' + repo + \
                  '. You must provide a relbranchOverride when buildNumber > 1'

        # now, down to work
        self.buildTag = '%s_BUILD%s' % (baseTag, str(buildNumber))
        self.releaseTag = '%s_RELEASE' % baseTag

        # generate the release branch name, which is based on the
        # version and the current date.
        # looks like: GECKO191_20080728_RELBRANCH
        # This can be overridden per-repository. This case is handled
        # in the loop below
        if not relbranchPrefix:
            relbranchPrefix = 'GECKO'
        relbranchName = '%s%s_%s_RELBRANCH' % (
          relbranchPrefix, milestone.replace('.', ''),
          datetime.now().strftime('%Y%m%d'))

        for repoPath in sorted(repositories.keys()):
            repoName = self.getRepoName(repoPath)
            repo = self.getRepository(repoPath)
            pushRepo = self.getRepository(repoPath, push=True)

            sshKeyOption = self.getSshKeyOption(hgSshKey)

            repoRevision = repositories[repoPath]['revision']
            bumpFiles = repositories[repoPath]['bumpFiles']

            # use repo-specific variable so that a changed name doesn't
            # propagate to later repos without an override
            relbranchOverride = False
            if repositories[repoPath]['relbranchOverride']:
                relbranchOverride = True
                repoRelbranchName = repositories[repoPath]['relbranchOverride']
            else:
                repoRelbranchName = relbranchName

            # For l10n we never bump any files, so this will never get
            # overridden. For source repos, we will do a version bump in build1
            # which we commit, and set this property again, so we tag
            # the right revision. For build2, we don't version bump, and this
            # will not get overridden
            self.addStep(SetBuildProperty(
             property_name="%s-revision" % repoName,
             value=repoRevision,
             haltOnFailure=True
            ))
            # 'hg clone -r' breaks in the respin case because the cloned
            # repository will not have ANY changesets from the release branch
            # and 'hg up -C' will fail
            self.addStep(MercurialCloneCommand(
             name='hg_clone',
             command=['hg', 'clone', repo, repoName],
             workdir='.',
             description=['clone %s' % repoName],
             haltOnFailure=True,
             timeout=30*60 # 30 minutes
            ))
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
                self.addStep(ShellCommand(
                 name='hg_update',
                 command=['hg', 'up', '-C', '-r',
                          WithProperties('%s', '%s-revision' % repoName)],
                 workdir=repoName,
                 description=['update', repoName],
                 haltOnFailure=True
                ))

                self.addStep(SetProperty(
                 command=['sh', '-c', 'hg branches | grep %s | wc -l' % repoRelbranchName],
                 property='branch_match_count',
                 workdir=repoName,
                 haltOnFailure=True,
                ))

                self.addStep(ShellCommand(
                 name='hg_branch',
                 command=['hg', 'branch', repoRelbranchName],
                 workdir=repoName,
                 description=['branch %s' % repoName],
                 doStepIf=lambda step: int(step.getProperty('branch_match_count')) == 0,
                 haltOnFailure=True
                ))

                self.addStep(ShellCommand(
                 name='switch_branch',
                 command=['hg', 'up', '-C', repoRelbranchName],
                 workdir=repoName,
                 description=['switch to', repoRelbranchName],
                 doStepIf=lambda step: int(step.getProperty('branch_match_count')) > 0,
                 haltOnFailure=True
                ))
            # if buildNumber > 1 we need to switch to it with 'hg up -C'
            else:
                self.addStep(ShellCommand(
                 name='switch_branch',
                 command=['hg', 'up', '-C', repoRelbranchName],
                 workdir=repoName,
                 description=['switch to', repoRelbranchName],
                 haltOnFailure=True
                ))
            # we don't need to do any version bumping if this is a respin
            if buildNumber == 1 and len(bumpFiles) > 0:
                command = ['perl', 'tools/scripts/release/version-bump.pl',
                           '-w', repoName, '-a', appName,
                           '-v', appVersion, '-m', milestone]
                command.extend(bumpFiles)
                self.addStep(ShellCommand(
                 name='bump',
                 command=command,
                 workdir='.',
                 description=['bump %s' % repoName],
                 haltOnFailure=True
                ))
                self.addStep(ShellCommand(
                 name='hg_diff',
                 command=['hg', 'diff'],
                 workdir=repoName
                ))
                self.addStep(ShellCommand(
                 # mozilla-central and other developer repositories have a
                 # 'CLOSED TREE' or 'APPROVAL REQUIRED' hook on them which
                 # rejects commits when the tree is declared closed/approval
                 # required. It is very common for us to tag
                 # and branch when the tree is in this state. Adding the
                 # 'CLOSED TREE a=release' string at the end will force the
                 # hook to let us commit regardless of the tree state.
                 name='hg_commit',
                 command=['hg', 'commit', '-u', hgUsername, '-m',
                          'Automated checkin: version bump remove "pre" ' + \
                          ' from version number for ' + productName + ' ' + \
                          version + ' release on ' + repoRelbranchName + ' ' + \
                          'CLOSED TREE a=release'],
                 workdir=repoName,
                 description=['commit %s' % repoName],
                 haltOnFailure=True
                ))
                self.addStep(SetProperty(
                 command=['hg', 'identify', '-i'],
                 property='%s-revision' % repoName,
                 workdir=repoName,
                 haltOnFailure=True
                ))
            for tag in (self.buildTag, self.releaseTag):
                self.addStep(ShellCommand(
                 name='hg_tag',
                 command=['hg', 'tag', '-u', hgUsername, '-f', '-r',
                          WithProperties('%s', '%s-revision' % repoName),
                          '-m',
                          # This part is pretty ugly. Because we need both
                          # WithProperties interpolation (for repoName-revision)
                          # and regular variables we need to piece it together
                          # this way.
                          WithProperties('Added tag ' + tag + \
                            ' for changeset ' + \
                            '%(' + repoName + '-revision' + ')s. ' + \
                            'CLOSED TREE a=release'),
                          tag],
                 workdir=repoName,
                 description=['tag %s' % repoName],
                 haltOnFailure=True
                ))
            self.addStep(ShellCommand(
             name='hg_out',
             command=['hg', 'out', '-e',
                      'ssh -l %s %s' % (hgUsername, sshKeyOption),
                      pushRepo],
             workdir=repoName,
             description=['hg out', repoName]
            ))
            self.addStep(ShellCommand(
             name='hg_push',
             command=['hg', 'push', '-e',
                      'ssh -l %s %s' % (hgUsername, sshKeyOption),
                      '-f', pushRepo],
             workdir=repoName,
             description=['push %s' % repoName],
             haltOnFailure=True
            ))


class SingleSourceFactory(ReleaseFactory):
    def __init__(self, productName, version, baseTag, stagingServer,
                 stageUsername, stageSshKey, buildNumber, mozconfig,
                 configRepoPath, configSubDir, objdir='',
                 mozillaDir=None, autoconfDirs=['.'], buildSpace=1,
                 mozconfigBranch="production", **kwargs):
        ReleaseFactory.__init__(self, buildSpace=buildSpace, **kwargs)

        self.mozconfig = mozconfig
        self.configRepoPath=configRepoPath
        self.configSubDir=configSubDir
        self.mozconfigBranch = mozconfigBranch
        self.releaseTag = '%s_RELEASE' % (baseTag)
        self.bundleFile = 'source/%s-%s.bundle' % (productName, version)
        self.sourceTarball = 'source/%s-%s.source.tar.bz2' % (productName, version)

        self.origSrcDir = self.branchName

        # Mozilla subdir
        if mozillaDir:
            self.mozillaDir = '/%s' % mozillaDir
            self.mozillaSrcDir = '%s/%s' % (self.origSrcDir, mozillaDir)
        else:
            self.mozillaDir = ''
            self.mozillaSrcDir = self.origSrcDir

        # self.mozillaObjdir is used in SeaMonkey's and Thunderbird's case
        self.objdir = objdir or self.origSrcDir
        self.mozillaObjdir = '%s%s' % (self.objdir, self.mozillaDir)
        self.distDir = "%s/dist" % self.mozillaObjdir

        # Make sure MOZ_PKG_PRETTYNAMES is set so that our source package is
        # created in the expected place.
        self.env['MOZ_OBJDIR'] = self.objdir
        self.env['MOZ_PKG_PRETTYNAMES'] = '1'
        self.env['MOZ_PKG_VERSION'] = version
        self.env['MOZ_PKG_APPNAME'] = productName

        # '-c' is for "release to candidates dir"
        postUploadCmd = 'post_upload.py -p %s -v %s -n %s -c' % \
          (productName, version, buildNumber)
        if productName == 'fennec':
            postUploadCmd = 'post_upload.py -p mobile --nightly-dir candidates -v %s -n %s -c' % \
                          (version, buildNumber)
        uploadEnv = {'UPLOAD_HOST': stagingServer,
                     'UPLOAD_USER': stageUsername,
                     'UPLOAD_SSH_KEY': '~/.ssh/%s' % stageSshKey,
                     'UPLOAD_TO_TEMP': '1',
                     'POST_UPLOAD_CMD': postUploadCmd}

        self.addStep(ShellCommand(
         name='rm_srcdir',
         command=['rm', '-rf', 'source'],
         workdir='.',
         haltOnFailure=True
        ))
        self.addStep(ShellCommand(
         name='make_srcdir',
         command=['mkdir', 'source'],
         workdir='.',
         haltOnFailure=True
        ))
        self.addStep(MercurialCloneCommand(
         name='hg_clone',
         command=['hg', 'clone', self.repository, self.branchName],
         workdir='.',
         description=['clone %s' % self.branchName],
         haltOnFailure=True,
         timeout=30*60 # 30 minutes
        ))
        # This will get us to the version we're building the release with
        self.addStep(ShellCommand(
         name='hg_update',
         command=['hg', 'up', '-C', '-r', self.releaseTag],
         workdir=self.mozillaSrcDir,
         description=['update to', self.releaseTag],
         haltOnFailure=True
        ))
        # ...And this will get us the tags so people can do things like
        # 'hg up -r FIREFOX_3_1b1_RELEASE' with the bundle
        self.addStep(ShellCommand(
         name='hg_update_incl_tags',
         command=['hg', 'up', '-C'],
         workdir=self.mozillaSrcDir,
         description=['update to', 'include tag revs'],
         haltOnFailure=True
        ))
        self.addStep(SetProperty(
         name='hg_ident_revision',
         command=['hg', 'identify', '-i'],
         property='revision',
         workdir=self.mozillaSrcDir,
         haltOnFailure=True
        ))
        self.addStep(ShellCommand(
         name='create_bundle',
         command=['hg', '-R', self.branchName, 'bundle', '--base', 'null',
                  '-r', WithProperties('%(revision)s'),
                  self.bundleFile],
         workdir='.',
         description=['create bundle'],
         haltOnFailure=True,
         timeout=30*60 # 30 minutes
        ))
        self.addStep(ShellCommand(
         name='delete_metadata',
         command=['rm', '-rf', '.hg'],
         workdir=self.mozillaSrcDir,
         description=['delete metadata'],
         haltOnFailure=True
        ))
        self.addConfigSteps(workdir=self.mozillaSrcDir)
        self.addStep(ShellCommand(
         name='configure',
         command=['make', '-f', 'client.mk', 'configure'],
         workdir=self.mozillaSrcDir,
         env=self.env,
         description=['configure'],
         haltOnFailure=True
        ))
        self.addStep(ShellCommand(
         name='make_source-package',
         command=['make','source-package'],
         workdir="%s/%s" % (self.mozillaSrcDir, self.mozillaObjdir),
         env=self.env,
         description=['make source-package'],
         haltOnFailure=True,
         timeout=30*60 # 30 minutes
        ))
        self.addStep(ShellCommand(
         name='mv_source-package',
         command=['mv','%s/%s/%s' % (self.branchName,
                                     self.distDir,
                                     self.sourceTarball),
                  self.sourceTarball],
         workdir=".",
         env=self.env,
         description=['mv source-package'],
         haltOnFailure=True
        ))
        files = [self.bundleFile, self.sourceTarball]
        if self.enableSigning and self.signingServers:
            self.addGetTokenSteps()
            # use a copy of files variable to prevent endless loops
            for f in list(files):
                signingcmd = WithProperties(
                    '%s --formats gpg "%s"' % (self.signing_command, f))
                self.addStep(ShellCommand(
                    name='sign_file',
                    command=signingcmd,
                    workdir='.',
                    env=self.env,
                    description=['sign', f],
                    haltOnFailure=True,
                ))
                files.append('%s.asc' % f)
        self.addStep(RetryingShellCommand(
         name='upload_files',
         command=['python', '%s/build/upload.py' % self.branchName,
                  '--base-path', '.'] + files,
         workdir='.',
         env=uploadEnv,
         description=['upload files'],
        ))

    def addConfigSteps(self, workdir='build'):
        assert self.configRepoPath is not None
        assert self.configSubDir is not None
        assert self.mozconfig is not None
        configRepo = self.getRepository(self.configRepoPath)

        self.mozconfig = 'configs/%s/%s/mozconfig' % (self.configSubDir,
                                                      self.mozconfig)
        self.addStep(ShellCommand(
                     name='rm_configs',
                     command=['rm', '-rf', 'configs'],
                     description=['removing', 'configs'],
                     descriptionDone=['remove', 'configs'],
                     haltOnFailure=True,
                     workdir='.'
        ))
        self.addStep(MercurialCloneCommand(
                     name='hg_clone_configs',
                     command=['hg', 'clone', configRepo, 'configs'],
                     description=['checking', 'out', 'configs'],
                     descriptionDone=['checkout', 'configs'],
                     haltOnFailure=True,
                     workdir='.'
        ))
        self.addStep(ShellCommand(
                     name='hg_update',
                     command=['hg', 'update', '-r', self.mozconfigBranch],
                     description=['updating', 'mozconfigs'],
                     haltOnFailure=True,
                     workdir='./configs'
        ))
        self.addStep(ShellCommand(
                     # cp configs/mozilla2/$platform/$repo/$type/mozconfig .mozconfig
                     name='cp_mozconfig',
                     command=['cp', self.mozconfig, '%s/.mozconfig' % workdir],
                     description=['copying', 'mozconfig'],
                     descriptionDone=['copy', 'mozconfig'],
                     haltOnFailure=True,
                     workdir='.'
        ))
        self.addStep(ShellCommand(
                     name='cat_mozconfig',
                     command=['cat', '.mozconfig'],
                     workdir=workdir
        ))

class MultiSourceFactory(ReleaseFactory):
    """You need to pass in a repoConfig, which will be a list that
       looks like this:
       repoConfig = [{
           'repoPath': repoPath,
           'location': branchName,
           'bundleName': '%s-%s.bundle' % (productName, version)
       }]"""
    def __init__(self, productName, version, baseTag, stagingServer,
                 stageUsername, stageSshKey, buildNumber, autoconfDirs=['.'],
                 buildSpace=1, repoConfig=None, uploadProductName=None,
                 stageNightlyDir="nightly", **kwargs):
        ReleaseFactory.__init__(self, buildSpace=buildSpace, **kwargs)
        releaseTag = '%s_RELEASE' % (baseTag)
        bundleFiles = []
        sourceTarball = 'source/%s-%s.source.tar.bz2' % (productName,
                                                         version)
        if not uploadProductName:
            uploadProductName = productName

        assert repoConfig
        # '-c' is for "release to candidates dir"
        postUploadCmd = 'post_upload.py -p %s -v %s -n %s -c --nightly-dir %s' % \
          (uploadProductName, version, buildNumber, stageNightlyDir)
        uploadEnv = {'UPLOAD_HOST': stagingServer,
                     'UPLOAD_USER': stageUsername,
                     'UPLOAD_SSH_KEY': '~/.ssh/%s' % stageSshKey,
                     'UPLOAD_TO_TEMP': '1',
                     'POST_UPLOAD_CMD': postUploadCmd}

        self.addStep(ShellCommand(
         name='rm_srcdir',
         command=['rm', '-rf', 'source'],
         workdir='.',
         haltOnFailure=True
        ))
        self.addStep(ShellCommand(
         name='make_srcdir',
         command=['mkdir', 'source'],
         workdir='.',
         haltOnFailure=True
        ))
        for repo in repoConfig:
            repository = self.getRepository(repo['repoPath'])
            location = repo['location']
            bundleFiles.append('source/%s' % repo['bundleName'])

            self.addStep(MercurialCloneCommand(
             name='hg_clone',
             command=['hg', 'clone', repository, location],
             workdir='.',
             description=['clone %s' % location],
             haltOnFailure=True,
             timeout=30*60 # 30 minutes
            ))
            # This will get us to the version we're building the release with
            self.addStep(ShellCommand(
             name='hg_update',
             command=['hg', 'up', '-C', '-r', releaseTag],
             workdir=location,
             description=['update to', releaseTag],
             haltOnFailure=True
            ))
            # ...And this will get us the tags so people can do things like
            # 'hg up -r FIREFOX_3_1b1_RELEASE' with the bundle
            self.addStep(ShellCommand(
             name='hg_update_incl_tags',
             command=['hg', 'up', '-C'],
             workdir=location,
             description=['update to', 'include tag revs'],
             haltOnFailure=True
            ))
            self.addStep(SetProperty(
             name='hg_ident_revision',
             command=['hg', 'identify', '-i'],
             property='revision',
             workdir=location,
             haltOnFailure=True
            ))
            self.addStep(ShellCommand(
             name='create_bundle',
             command=['hg', '-R', location, 'bundle', '--base', 'null',
                      '-r', WithProperties('%(revision)s'),
                      'source/%s' % repo['bundleName']],
             workdir='.',
             description=['create bundle'],
             haltOnFailure=True
            ))
            self.addStep(ShellCommand(
             name='delete_metadata',
             command=['rm', '-rf', '.hg'],
             workdir=location,
             description=['delete metadata'],
             haltOnFailure=True
            ))
        for dir in autoconfDirs:
            self.addStep(ShellCommand(
             name='autoconf',
             command=['autoconf-2.13'],
             workdir='%s/%s' % (self.branchName, dir),
             haltOnFailure=True
            ))
        self.addStep(ShellCommand(
         name='create_tarball',
         command=['tar', '-cjf', sourceTarball, self.branchName],
         workdir='.',
         description=['create tarball'],
         haltOnFailure=True
        ))
        self.addStep(ShellCommand(
         name='upload_files',
         command=['python', '%s/build/upload.py' % self.branchName,
                  '--base-path', '.'] + bundleFiles + [sourceTarball],
         workdir='.',
         env=uploadEnv,
         description=['upload files'],
        ))

class CCSourceFactory(ReleaseFactory):
    def __init__(self, productName, version, baseTag, stagingServer,
                 stageUsername, stageSshKey, buildNumber, mozRepoPath,
                 inspectorRepoPath='', venkmanRepoPath='',
                 chatzillaRepoPath='', cvsroot='', autoconfDirs=['.'],
                 buildSpace=1, **kwargs):
        ReleaseFactory.__init__(self, buildSpace=buildSpace, **kwargs)
        releaseTag = '%s_RELEASE' % (baseTag)
        sourceTarball = 'source/%s-%s.source.tar.bz2' % (productName,
                                                         version)
        # '-c' is for "release to candidates dir"
        postUploadCmd = 'post_upload.py -p %s -v %s -n %s -c' % \
          (productName, version, buildNumber)
        uploadEnv = {'UPLOAD_HOST': stagingServer,
                     'UPLOAD_USER': stageUsername,
                     'UPLOAD_SSH_KEY': '~/.ssh/%s' % stageSshKey,
                     'UPLOAD_TO_TEMP': '1',
                     'POST_UPLOAD_CMD': postUploadCmd}

        self.addStep(ShellCommand(
         command=['rm', '-rf', 'source'],
         workdir='.',
         haltOnFailure=True
        ))
        self.addStep(ShellCommand(
         command=['mkdir', 'source'],
         workdir='.',
         haltOnFailure=True
        ))
        self.addStep(MercurialCloneCommand(
         command=['hg', 'clone', self.repository, self.branchName],
         workdir='.',
         description=['clone %s' % self.branchName],
         haltOnFailure=True,
         timeout=30*60 # 30 minutes
        ))
        # build up the checkout command that will bring us up to the release version
        co_command = ['python', 'client.py', 'checkout',
                      '--comm-rev=%s' % releaseTag,
                      '--mozilla-repo=%s' % self.getRepository(mozRepoPath),
                      '--mozilla-rev=%s' % releaseTag]
        if inspectorRepoPath:
            co_command.append('--inspector-repo=%s' % self.getRepository(inspectorRepoPath))
            co_command.append('--inspector-rev=%s' % releaseTag)
        else:
            co_command.append('--skip-inspector')
        if venkmanRepoPath:
            co_command.append('--venkman-repo=%s' % self.getRepository(venkmanRepoPath))
            co_command.append('--venkman-rev=%s' % releaseTag)
        else:
            co_command.append('--skip-venkman')
        if chatzillaRepoPath:
            co_command.append('--chatzilla-repo=%s' % self.getRepository(chatzillaRepoPath))
            co_command.append('--chatzilla-rev=%s' % releaseTag)
        else:
            co_command.append('--skip-chatzilla')
        if cvsroot:
            co_command.append('--cvsroot=%s' % cvsroot)
        # execute the checkout
        self.addStep(ShellCommand(
         command=co_command,
         workdir=self.branchName,
         description=['update to', releaseTag],
         haltOnFailure=True,
         timeout=60*60 # 1 hour
        ))
        # the autoconf and actual tarring steps
        # should be replaced by calling the build target
        for dir in autoconfDirs:
            self.addStep(ShellCommand(
             command=['autoconf-2.13'],
             workdir='%s/%s' % (self.branchName, dir),
             haltOnFailure=True
            ))
        self.addStep(ShellCommand(
         command=['tar', '-cj', '--owner=0', '--group=0', '--numeric-owner',
                  '--mode=go-w', '--exclude=.hg*', '--exclude=CVS',
                  '--exclude=.cvs*', '-f', sourceTarball, self.branchName],
         workdir='.',
         description=['create tarball'],
         haltOnFailure=True
        ))
        self.addStep(ShellCommand(
         command=['python', '%s/mozilla/build/upload.py' % self.branchName,
                  '--base-path', '.', sourceTarball],
         workdir='.',
         env=uploadEnv,
         description=['upload files'],
        ))



class ReleaseUpdatesFactory(ReleaseFactory):
    snippetStagingDir = '/opt/aus2/snippets/staging'
    def __init__(self, cvsroot, patcherToolsTag, patcherConfig, verifyConfigs,
                 appName, productName,
                 version, appVersion, baseTag, buildNumber,
                 oldVersion, oldAppVersion, oldBaseTag,  oldBuildNumber,
                 ftpServer, bouncerServer, stagingServer, useBetaChannel,
                 stageUsername, stageSshKey, ausUser, ausSshKey, ausHost,
                 ausServerUrl, hgSshKey, hgUsername, commitPatcherConfig=True,
                 mozRepoPath=None, oldRepoPath=None, brandName=None,
                 buildSpace=22, triggerSchedulers=None, releaseNotesUrl=None,
                 binaryName=None, oldBinaryName=None, testOlderPartials=False,
                 fakeMacInfoTxt=False, longVersion=None, oldLongVersion=None,
                 schema=None, useBetaChannelForRelease=False,
                 useChecksums=False, **kwargs):
        """cvsroot: The CVSROOT to use when pulling patcher, patcher-configs,
                    Bootstrap/Util.pm, and MozBuild. It is also used when
                    commiting the version-bumped patcher config so it must have
                    write permission to the repository if commitPatcherConfig
                    is True.
           patcherToolsTag: A tag that has been applied to all of:
                              sourceRepo, patcher, MozBuild, Bootstrap.
                            This version of all of the above tools will be
                            used - NOT tip.
           patcherConfig: The filename of the patcher config file to bump,
                          and pass to patcher.
           commitPatcherConfig: This flag simply controls whether or not
                                the bumped patcher config file will be
                                commited to the CVS repository.
           mozRepoPath: The path for the Mozilla repo to hand patcher as the
                        HGROOT (if omitted, the default repoPath is used).
                        Apps not rooted in the Mozilla repo need this.
           brandName: The brand name as used on the updates server. If omitted,
                      the first letter of the brand name is uppercased.
           fakeMacInfoTxt: When True, symlink macosx64_info.txt to
                           macosx_info.txt in the candidates directory on the
                           staging server (to cope with the transition in mac
                           builds, see bug 630085)
           schema: The style of snippets to write (changed in bug 459972)
        """
        if useChecksums:
            buildSpace = 1
        ReleaseFactory.__init__(self, buildSpace=buildSpace, **kwargs)

        self.cvsroot = cvsroot
        self.patcherToolsTag = patcherToolsTag
        self.patcherConfig = patcherConfig
        self.verifyConfigs = verifyConfigs
        self.appName = appName
        self.productName = productName
        self.version = version
        self.appVersion = appVersion
        self.baseTag = baseTag
        self.buildNumber = buildNumber
        self.oldVersion = oldVersion
        self.oldAppVersion = oldAppVersion
        self.oldBaseTag = oldBaseTag
        self.oldBuildNumber = oldBuildNumber
        self.ftpServer = ftpServer
        self.bouncerServer = bouncerServer
        self.stagingServer = stagingServer
        self.useBetaChannel = useBetaChannel
        self.stageUsername = stageUsername
        self.stageSshKey = stageSshKey
        self.ausUser = ausUser
        self.ausSshKey = ausSshKey
        self.ausHost = ausHost
        self.ausServerUrl = ausServerUrl
        self.hgSshKey = hgSshKey
        self.hgUsername = hgUsername
        self.commitPatcherConfig = commitPatcherConfig
        self.oldRepoPath = oldRepoPath or kwargs['repoPath']
        self.oldRepository = self.getRepository(self.oldRepoPath)
        self.triggerSchedulers = triggerSchedulers
        self.binaryName = binaryName
        self.oldBinaryName = oldBinaryName
        self.testOlderPartials = testOlderPartials
        self.fakeMacInfoTxt = fakeMacInfoTxt
        self.longVersion = longVersion or self.version
        self.oldLongVersion = oldLongVersion or self.oldVersion
        self.schema = schema
        self.useBetaChannelForRelease = useBetaChannelForRelease
        self.useChecksums = useChecksums

        self.patcherConfigFile = 'patcher-configs/%s' % patcherConfig
        self.shippedLocales = self.getShippedLocales(self.repository, baseTag,
                                                appName)
        self.oldShippedLocales = self.getShippedLocales(self.oldRepository,
                                                        self.oldBaseTag,
                                                        self.appName)
        self.candidatesDir = self.getCandidatesDir(productName, version, buildNumber)
        self.updateDir = 'build/temp/%s/%s-%s' % (productName, oldVersion, version)
        self.marDir = '%s/ftp/%s/nightly/%s-candidates/build%s' % \
          (self.updateDir, productName, version, buildNumber)

        if mozRepoPath:
          self.mozRepository = self.getRepository(mozRepoPath)
        else:
          self.mozRepository = self.repository


        self.brandName = brandName or productName.capitalize()
        self.releaseNotesUrl = releaseNotesUrl

        self.setChannelData()
        self.setup()
        self.bumpPatcherConfig()
        self.bumpVerifyConfigs()
        if not self.useChecksums:
            self.buildTools()
        self.downloadBuilds()
        self.createPatches()
        if buildNumber >= 2:
            self.createBuildNSnippets()
        if not self.useChecksums:
            self.uploadMars()
        self.uploadSnippets()
        self.verifySnippets()
        self.trigger()

    def setChannelData(self):
        # This method figures out all the information needed to push snippets
        # to AUS, push test snippets live, and do basic verifications on them.
        # Test snippets always end up in the same local and remote directories
        # All of the beta and (if applicable) release channel information
        # is dependent on the useBetaChannel flag. When false, there is no
        # release channel, and the beta channel is comprable to 'releasetest'
        # rather than 'betatest'
        baseSnippetDir = self.getSnippetDir()
        self.dirMap = {
            'aus2.test': '%s-test' % baseSnippetDir,
            'aus2': baseSnippetDir
        }

        self.channels = {
            'betatest': { 'dir': 'aus2.test' },
            'releasetest': { 'dir': 'aus2.test' },
            'beta': {}
        }
        if self.useBetaChannel:
            if self.useBetaChannelForRelease:
                self.dirMap['aus2.beta'] = '%s-beta' % baseSnippetDir
                self.channels['beta']['dir'] = 'aus2.beta'
            self.channels['release'] = {
                'dir': 'aus2',
                'compareTo': 'releasetest',
            }
        else:
            self.channels['beta']['dir'] = 'aus2'
            self.channels['beta']['compareTo'] = 'releasetest'

    def setup(self):
        # General setup
        self.addStep(RetryingShellCommand(
         name='checkout_patcher',
         command=['cvs', '-d', self.cvsroot, 'co', '-r', self.patcherToolsTag,
                  '-d', 'build', 'mozilla/tools/patcher'],
         description=['checkout', 'patcher'],
         workdir='.',
         haltOnFailure=True
        ))
        self.addStep(RetryingShellCommand(
         name='checkout_mozbuild',
         command=['cvs', '-d', self.cvsroot, 'co', '-r', self.patcherToolsTag,
                  '-d', 'MozBuild',
                  'mozilla/tools/release/MozBuild'],
         description=['checkout', 'MozBuild'],
         haltOnFailure=True
        ))
        self.addStep(RetryingShellCommand(
         name='checkout_bootstrap_util',
         command=['cvs', '-d', self.cvsroot, 'co', '-r', self.patcherToolsTag,
                  '-d' 'Bootstrap',
                  'mozilla/tools/release/Bootstrap/Util.pm'],
         description=['checkout', 'Bootstrap/Util.pm'],
         haltOnFailure=True
        ))
        self.addStep(RetryingShellCommand(
         name='checkout_patcher_configs',
         command=['cvs', '-d', self.cvsroot, 'co', '-d' 'patcher-configs',
                  'mozilla/tools/patcher-configs'],
         description=['checkout', 'patcher-configs'],
         haltOnFailure=True
        ))
        # Bump the patcher config
        self.addStep(RetryingShellCommand(
         name='get_shipped_locales',
         command=['wget', '-O', 'shipped-locales', self.shippedLocales],
         description=['get', 'shipped-locales'],
         haltOnFailure=True
        ))
        self.addStep(RetryingShellCommand(
         name='get_old_shipped_locales',
         command=['wget', '-O', 'old-shipped-locales', self.oldShippedLocales],
         description=['get', 'old-shipped-locales'],
         haltOnFailure=True
        ))


    def bumpPatcherConfig(self):
        bumpCommand = ['perl', '../tools/release/patcher-config-bump.pl',
                       '-p', self.productName, '-r', self.brandName,
                       '-v', self.version, '-a', self.appVersion,
                       '-o', self.oldVersion, '-b', str(self.buildNumber),
                       '-c', WithProperties(self.patcherConfigFile),
                       '-t', self.stagingServer, '-f', self.ftpServer,
                       '-d', self.bouncerServer, '-l', 'shipped-locales']
        for platform in sorted(self.verifyConfigs.keys()):
            bumpCommand.extend(['--platform', platform])
        if self.binaryName:
            bumpCommand.extend(['--marname', self.binaryName.lower()])
        if self.oldBinaryName:
            bumpCommand.extend(['--oldmarname', self.oldBinaryName.lower()])
        if self.useBetaChannel:
            bumpCommand.append('-u')
        if self.releaseNotesUrl:
            bumpCommand.extend(['-n', self.releaseNotesUrl])
        if self.schema:
            bumpCommand.extend(['-s', str(self.schema)])
        self.addStep(ShellCommand(
         name='bump',
         command=bumpCommand,
         description=['bump patcher config'],
         env={'PERL5LIB': '../tools/lib/perl'},
         haltOnFailure=True
        ))
        self.addStep(TinderboxShellCommand(
         name='diff_patcher_config',
         command=['bash', '-c',
                  '(cvs diff -u "%s") && (grep \
                    "build%s/update/.platform./.locale./%s-%s.complete.mar" \
                    "%s" || return 2)' % (self.patcherConfigFile,
                                          self.buildNumber, self.productName,
                                          self.version,
                                          self.patcherConfigFile)],
         description=['diff patcher config'],
         ignoreCodes=[0,1]
        ))
        if self.commitPatcherConfig:
            self.addStep(ShellCommand(
             name='commit_patcher_config',
             command=['cvs', 'commit', '-m',
                      WithProperties('Automated configuration bump: ' + \
                      '%s, from %s to %s build %s' % \
                        (self.patcherConfig, self.oldVersion,
                         self.version, self.buildNumber))
                     ],
             workdir='build/patcher-configs',
             description=['commit patcher config'],
             haltOnFailure=True
            ))

    def bumpVerifyConfigs(self):
        # Bump the update verify config
        pushRepo = self.getRepository(self.buildToolsRepoPath, push=True)
        sshKeyOption = self.getSshKeyOption(self.hgSshKey)
        tags = [release.info.getRuntimeTag(t) for t in release.info.getTags(self.baseTag, self.buildNumber)]

        for platform in sorted(self.verifyConfigs.keys()):
            bumpCommand = self.getUpdateVerifyBumpCommand(platform)
            self.addStep(ShellCommand(
             name='bump_verify_configs',
             command=bumpCommand,
             description=['bump', self.verifyConfigs[platform]],
            ))
        self.addStep(ShellCommand(
         name='commit_verify_configs',
         command=['hg', 'commit', '-u', self.hgUsername, '-m',
                  'Automated configuration bump: update verify configs ' + \
                  'for %s %s build %s' % (self.brandName, self.version,
                                          self.buildNumber)
                 ],
         description=['commit verify configs'],
         workdir='tools',
         haltOnFailure=True
        ))
        self.addStep(SetProperty(
            command=['hg', 'identify', '-i'],
            property='configRevision',
            workdir='tools',
            haltOnFailure=True
        ))
        for t in tags:
            self.addStep(ShellCommand(
             name='tag_verify_configs',
             command=['hg', 'tag', '-u', self.hgUsername, '-f',
                      '-r', WithProperties('%(configRevision)s'), t],
             description=['tag verify configs'],
             workdir='tools',
             haltOnFailure=True
            ))
        self.addStep(RetryingShellCommand(
         name='push_verify_configs',
         command=['hg', 'push', '-e',
                  'ssh -l %s %s' % (self.hgUsername, sshKeyOption),
                  '-f', pushRepo],
         description=['push verify configs'],
         workdir='tools',
         haltOnFailure=True
        ))

    def buildTools(self):
        # Generate updates from here
        self.addStep(ShellCommand(
         name='patcher_build_tools',
         command=['perl', 'patcher2.pl', '--build-tools-hg',
                  '--tools-revision=%s' % self.patcherToolsTag,
                  '--app=%s' % self.productName,
                  '--brand=%s' % self.brandName,
                  WithProperties('--config=%s' % self.patcherConfigFile)],
         description=['patcher:', 'build tools'],
         env={'HGROOT': self.mozRepository},
         haltOnFailure=True,
         timeout=3600,
        ))

    def downloadBuilds(self):
        command = ['perl', 'patcher2.pl', '--download',
                   '--app=%s' % self.productName,
                   '--brand=%s' % self.brandName,
                   WithProperties('--config=%s' % self.patcherConfigFile)]
        if self.useChecksums:
            command.append('--use-checksums')
        self.addStep(ShellCommand(
         name='patcher_download_builds',
         command=command,
         description=['patcher:', 'download builds'],
         haltOnFailure=True
        ))

    def createPatches(self):
        command=['perl', 'patcher2.pl', '--create-patches',
                 '--partial-patchlist-file=patchlist.cfg',
                 '--app=%s' % self.productName,
                 '--brand=%s' % self.brandName,
                 WithProperties('--config=%s' % self.patcherConfigFile)]
        if self.useChecksums:
            command.append('--use-checksums')
        self.addStep(ShellCommand(
         name='patcher_create_patches',
         command=command,
         description=['patcher:', 'create patches'],
         haltOnFailure=True
        ))
        # XXX: For Firefox 10 betas we want the appVersion in the snippets to
        #      show a version change with each one, so we change them from the
        #      the appVersion (which is always "10.0") to "11.0", which will
        #      trigger the new behaviour, and allows betas to update to newer
        #      ones.
        if self.productName == 'firefox' and self.version.startswith('10.0b'):
            cmd = ['bash', WithProperties('%(toolsdir)s/release/edit-snippets.sh'),
                   'appVersion', '11.0']
            cmd.extend(self.dirMap.keys())
            self.addStep(ShellCommand(
             name='switch_appv_in_snippets',
             command=cmd,
             haltOnFailure=True,
             workdir=self.updateDir,
            ))

    def createBuildNSnippets(self):
        command = ['python',
                   WithProperties('%(toolsdir)s/release/generate-candidate-build-updates.py'),
                   '--brand', self.brandName,
                   '--product', self.productName,
                   '--app-name', self.appName,
                   '--version', self.version,
                   '--app-version', self.appVersion,
                   '--old-version', self.oldVersion,
                   '--old-app-version', self.oldAppVersion,
                   '--build-number', self.buildNumber,
                   '--old-build-number', self.oldBuildNumber,
                   '--channel', 'betatest', '--channel', 'releasetest',
                   '--channel', 'beta',
                   '--stage-server', self.stagingServer,
                   '--old-base-snippet-dir', '.',
                   '--workdir', '.',
                   '--hg-server', self.getRepository('/'),
                   '--source-repo', self.repoPath,
                   '--verbose']
        for p in (self.verifyConfigs.keys()):
            command.extend(['--platform', p])
        if self.useBetaChannel:
            command.extend(['--channel', 'release'])
        if self.testOlderPartials:
            command.extend(['--generate-partials'])
        self.addStep(ShellCommand(
         name='create_buildN_snippets',
         command=command,
         description=['generate snippets', 'for prior',
                      '%s builds' % self.version],
         env={'PYTHONPATH': WithProperties('%(toolsdir)s/lib/python')},
         haltOnFailure=False,
         flunkOnFailure=False,
         workdir=self.updateDir
        ))

    def uploadMars(self):
        self.addStep(ShellCommand(
         name='chmod_partial_mars',
         command=['find', self.marDir, '-type', 'f',
                  '-exec', 'chmod', '644', '{}', ';'],
         workdir='.',
         description=['chmod 644', 'partial mar files']
        ))
        self.addStep(ShellCommand(
         name='chmod_partial_mar_dirs',
         command=['find', self.marDir, '-type', 'd',
                  '-exec', 'chmod', '755', '{}', ';'],
         workdir='.',
         description=['chmod 755', 'partial mar dirs']
        ))
        self.addStep(RetryingShellCommand(
         name='upload_partial_mars',
         command=['rsync', '-av',
                  '-e', 'ssh -oIdentityFile=~/.ssh/%s' % self.stageSshKey,
                  '--exclude=*complete.mar',
                  'update',
                  '%s@%s:%s' % (self.stageUsername, self.stagingServer,
                                self.candidatesDir)],
         workdir=self.marDir,
         description=['upload', 'partial mars'],
         haltOnFailure=True
        ))

    def uploadSnippets(self):
        for localDir,remoteDir in self.dirMap.iteritems():
            snippetDir = self.snippetStagingDir + '/' + remoteDir
            self.addStep(RetryingShellCommand(
             name='upload_snippets',
             command=['rsync', '-av',
                      '-e', 'ssh -oIdentityFile=~/.ssh/%s' % self.ausSshKey,
                      localDir + '/',
                      '%s@%s:%s' % (self.ausUser, self.ausHost, snippetDir)],
             workdir=self.updateDir,
             description=['upload', '%s snippets' % localDir],
             haltOnFailure=True
            ))
            # We only push test channel snippets from automation.
            if localDir.endswith('test'):
                self.addStep(RetryingShellCommand(
                 name='backupsnip',
                 command=['bash', '-c',
                          'ssh -t -l %s ' %  self.ausUser +
                          '-i ~/.ssh/%s %s ' % (self.ausSshKey,self.ausHost) +
                          '~/bin/backupsnip %s' % remoteDir],
                 timeout=7200, # 2 hours
                 description=['backupsnip'],
                 haltOnFailure=True
                ))
                self.addStep(RetryingShellCommand(
                 name='pushsnip',
                 command=['bash', '-c',
                          'ssh -t -l %s ' %  self.ausUser +
                          '-i ~/.ssh/%s %s ' % (self.ausSshKey,self.ausHost) +
                          '~/bin/pushsnip %s' % remoteDir],
                 timeout=7200, # 2 hours
                 description=['pushsnip'],
                 haltOnFailure=True
                ))

    def verifySnippets(self):
        channelComparisons = [(c, self.channels[c]['compareTo']) for c in self.channels if 'compareTo' in self.channels[c]]
        for chan1,chan2 in channelComparisons:
            self.addStep(SnippetComparison(
                chan1=chan1,
                chan2=chan2,
                dir1=self.channels[chan1]['dir'],
                dir2=self.channels[chan2]['dir'],
                workdir=self.updateDir
            ))

    def trigger(self):
        if self.triggerSchedulers:
            self.addStep(Trigger(
             schedulerNames=self.triggerSchedulers,
             waitForFinish=False
            ))

    def getUpdateVerifyBumpCommand(self, platform):
        oldCandidatesDir = self.getCandidatesDir(self.productName,
                                                 self.oldVersion,
                                                 self.oldBuildNumber)
        verifyConfigPath = '../tools/release/updates/%s' % \
                            self.verifyConfigs[platform]

        bcmd = ['perl', '../tools/release/update-verify-bump.pl',
                '-o', platform, '-p', self.productName,
                '-r', self.brandName,
                '--old-version=%s' % self.oldVersion,
                '--old-app-version=%s' % self.oldAppVersion,
                '--old-long-version=%s' % self.oldLongVersion,
                '-v', self.version, '--app-version=%s' % self.appVersion,
                '--long-version=%s' % self.longVersion,
                '-n', str(self.buildNumber), '-a', self.ausServerUrl,
                '-s', self.stagingServer, '-c', verifyConfigPath,
                '-d', oldCandidatesDir, '-l', 'shipped-locales',
                '--old-shipped-locales', 'old-shipped-locales',
                '--pretty-candidates-dir']
        if self.binaryName:
            bcmd.extend(['--binary-name', self.binaryName])
        if self.oldBinaryName:
            bcmd.extend(['--old-binary-name', self.oldBinaryName])
        if self.testOlderPartials:
            bcmd.extend(['--test-older-partials'])
        return bcmd

    def getSnippetDir(self):
        return build.paths.getSnippetDir(self.brandName, self.version,
                                          self.buildNumber)



class MajorUpdateFactory(ReleaseUpdatesFactory):
    def bumpPatcherConfig(self):
        if self.commitPatcherConfig:
            self.addStep(ShellCommand(
             name='add_patcher_config',
             command=['bash', '-c', 
                      WithProperties('if [ ! -f ' + self.patcherConfigFile + 
                                     ' ]; then touch ' + self.patcherConfigFile + 
                                     ' && cvs add ' + self.patcherConfigFile + 
                                     '; fi')],
             description=['add patcher config'],
            ))
        if self.fakeMacInfoTxt:
            self.addStep(RetryingShellCommand(
                name='symlink_mac_info_txt',
                command=['ssh', '-oIdentityFile=~/.ssh/%s' % self.stageSshKey,
                         '%s@%s' % (self.stageUsername, self.stagingServer),
                         'cd %s && ln -sf macosx64_info.txt macosx_info.txt' % self.candidatesDir],
                description='symlink macosx64_info.txt to macosx_info.txt',
                haltOnFailure=True,
            ))
        bumpCommand = ['perl', '../tools/release/patcher-config-creator.pl',
                       '-p', self.productName, '-r', self.brandName,
                       '-v', self.version, '-a', self.appVersion,
                       '-o', self.oldVersion,
                       '--old-app-version=%s' % self.oldAppVersion,
                       '-b', str(self.buildNumber),
                       '--old-build-number=%s' % str(self.oldBuildNumber),
                       '-c', WithProperties(self.patcherConfigFile),
                       '-t', self.stagingServer, '-f', self.ftpServer,
                       '-d', self.bouncerServer, '-l', 'shipped-locales',
                       '--old-shipped-locales=old-shipped-locales',
                       '--update-type=major']
        for platform in sorted(self.verifyConfigs.keys()):
            bumpCommand.extend(['--platform', platform])
        if self.useBetaChannel:
            bumpCommand.append('-u')
        if self.releaseNotesUrl:
            bumpCommand.extend(['-n', self.releaseNotesUrl])
        if self.schema:
            bumpCommand.extend(['-s', str(self.schema)])
        self.addStep(ShellCommand(
         name='create_config',
         command=bumpCommand,
         description=['create patcher config'],
         env={'PERL5LIB': '../tools/lib/perl'},
         haltOnFailure=True
        ))
        self.addStep(TinderboxShellCommand(
         name='diff_patcher_config',
         command=['bash', '-c',
                  '(cvs diff -Nu "%s") && (grep \
                    "build%s/update/.platform./.locale./%s-%s.complete.mar" \
                    "%s" || return 2)' % (self.patcherConfigFile,
                                          self.buildNumber, self.productName,
                                          self.version,
                                          self.patcherConfigFile)],
         description=['diff patcher config'],
         ignoreCodes=[1]
        ))
        if self.commitPatcherConfig:
            self.addStep(ShellCommand(
             name='commit_patcher_config',
             command=['cvs', 'commit', '-m',
                      WithProperties('Automated configuration creation: ' + \
                      '%s, from %s to %s build %s' % \
                        (self.patcherConfig, self.oldVersion,
                         self.version, self.buildNumber))
                     ],
             workdir='build/patcher-configs',
             description=['commit patcher config'],
             haltOnFailure=True
            ))

    def downloadBuilds(self):
        ReleaseUpdatesFactory.downloadBuilds(self)
        self.addStep(ShellCommand(
            name='symlink_mar_dir',
            command=['ln', '-s', self.version, '%s-%s' % (self.oldVersion,
                                                          self.version)],
            workdir='build/temp/%s' % self.productName,
            description=['symlink mar dir']
        ))

    def createBuildNSnippets(self):
        pass

    def uploadMars(self):
        pass

    def getUpdateVerifyBumpCommand(self, platform):
        cmd = ReleaseUpdatesFactory.getUpdateVerifyBumpCommand(self, platform)
        cmd.append('--major')
        return cmd

    def getSnippetDir(self):
        return build.paths.getMUSnippetDir(self.brandName, self.oldVersion,
                                            self.oldBuildNumber, self.version,
                                            self.buildNumber)


class UpdateVerifyFactory(ReleaseFactory):
    def __init__(self, verifyConfig, buildSpace=.3, useOldUpdater=False,
                 **kwargs):
        ReleaseFactory.__init__(self, repoPath='nothing',
                                buildSpace=buildSpace, **kwargs)
        command=['bash', 'verify.sh', '-c', verifyConfig]
        if useOldUpdater:
            command.append('--old-updater')
        self.addStep(UpdateVerify(
         command=command,
         workdir='tools/release/updates',
         description=['./verify.sh', verifyConfig]
        ))


class ReleaseFinalVerification(ReleaseFactory):
    def __init__(self, verifyConfigs, platforms=None, **kwargs):
        # MozillaBuildFactory needs the 'repoPath' argument, but we don't
        ReleaseFactory.__init__(self, repoPath='nothing', **kwargs)
        verifyCommand = ['bash', 'final-verification.sh']
        platforms = platforms or sorted(verifyConfigs.keys())
        for platform in platforms:
            verifyCommand.append(verifyConfigs[platform])
        self.addStep(ShellCommand(
         name='final_verification',
         command=verifyCommand,
         description=['final-verification.sh'],
         workdir='tools/release'
        ))

class TuxedoEntrySubmitterFactory(ReleaseFactory):
    def __init__(self, baseTag, appName, config, productName, version,
                 tuxedoServerUrl, enUSPlatforms, l10nPlatforms,
                 extraPlatforms=None, bouncerProductName=None, brandName=None,
                 oldVersion=None, credentialsFile=None, verbose=True,
                 dryRun=False, milestone=None, bouncerProductSuffix=None,
                 **kwargs):
        ReleaseFactory.__init__(self, **kwargs)

        extraPlatforms = extraPlatforms or []
        cmd = ['python', 'tuxedo-add.py',
               '--config', config,
               '--product', productName,
               '--version', version,
               '--tuxedo-server-url', tuxedoServerUrl]

        if l10nPlatforms:
            cmd.extend(['--shipped-locales', 'shipped-locales'])
            shippedLocales = self.getShippedLocales(self.repository, baseTag,
                                                    appName)
            self.addStep(ShellCommand(
             name='get_shipped_locales',
             command=['wget', '-O', 'shipped-locales', shippedLocales],
             description=['get', 'shipped-locales'],
             haltOnFailure=True,
             workdir='tools/release'
            ))

        bouncerProductName = bouncerProductName or productName.capitalize()
        cmd.extend(['--bouncer-product-name', bouncerProductName])
        brandName = brandName or productName.capitalize()
        cmd.extend(['--brand-name', brandName])

        if oldVersion:
            cmd.append('--add-mars')
            cmd.extend(['--old-version', oldVersion])

        if milestone:
            cmd.extend(['--milestone', milestone])

        if bouncerProductSuffix:
            cmd.extend(['--bouncer-product-suffix', bouncerProductSuffix])

        for platform in sorted(enUSPlatforms):
            cmd.extend(['--platform', platform])

        for platform in sorted(extraPlatforms):
            cmd.extend(['--platform', platform])

        if credentialsFile:
            target_file_name = os.path.basename(credentialsFile)
            cmd.extend(['--credentials-file', target_file_name])
            self.addStep(FileDownload(
             mastersrc=credentialsFile,
             slavedest=target_file_name,
             workdir='tools/release',
            ))

        self.addStep(RetryingShellCommand(
         name='tuxedo_add',
         command=cmd,
         description=['tuxedo-add.py'],
         env={'PYTHONPATH': ['../lib/python']},
         workdir='tools/release',
        ))

class UnittestBuildFactory(MozillaBuildFactory):
    def __init__(self, platform, productName, config_repo_path, config_dir,
            objdir, mochitest_leak_threshold=None,
            crashtest_leak_threshold=None, uploadPackages=False,
            unittestMasters=None, unittestBranch=None, stageUsername=None,
            stageServer=None, stageSshKey=None, run_a11y=True,
            env={}, **kwargs):
        self.env = {}

        MozillaBuildFactory.__init__(self, env=env, **kwargs)

        self.productName = productName
        self.stageServer = stageServer
        self.stageUsername = stageUsername
        self.stageSshKey = stageSshKey
        self.uploadPackages = uploadPackages
        self.config_repo_path = config_repo_path
        self.config_dir = config_dir
        self.objdir = objdir
        self.run_a11y = run_a11y
        self.crashtest_leak_threshold = crashtest_leak_threshold
        self.mochitest_leak_threshold = mochitest_leak_threshold

        self.unittestMasters = unittestMasters or []
        self.unittestBranch = unittestBranch
        if self.unittestMasters:
            assert self.unittestBranch

        self.config_repo_url = self.getRepository(self.config_repo_path)

        env_map = {
                'linux': 'linux-unittest',
                'linux64': 'linux64-unittest',
                'macosx': 'macosx-unittest',
                'macosx64': 'macosx64-unittest',
                'win32': 'win32-unittest',
                }

        self.platform = platform.split('-')[0]
        assert self.platform in getSupportedPlatforms()

        self.env = MozillaEnvironments[env_map[self.platform]].copy()
        self.env['MOZ_OBJDIR'] = self.objdir
        self.env.update(env)

        if self.platform == 'win32':
            self.addStep(SetProperty(
                command=['bash', '-c', 'pwd -W'],
                property='toolsdir',
                workdir='tools'
            ))

        self.addStep(Mercurial(
         name='hg_update',
         mode='update',
         baseURL='http://%s/' % self.hgHost,
         defaultBranch=self.repoPath,
         timeout=60*60 # 1 hour
        ))

        self.addPrintChangesetStep()

        self.addStep(ShellCommand(
         name='rm_configs',
         command=['rm', '-rf', 'mozconfigs'],
         workdir='.'
        ))

        self.addStep(MercurialCloneCommand(
         name='buildbot_configs',
         command=['hg', 'clone', self.config_repo_url, 'mozconfigs'],
         workdir='.'
        ))

        self.addCopyMozconfigStep()

        # TODO: Do we need this special windows rule?
        if self.platform == 'win32':
            self.addStep(ShellCommand(
             name='mozconfig_contents',
             command=["type", ".mozconfig"]
            ))
        else:
            self.addStep(ShellCommand(
             name='mozconfig_contents',
             command=['cat', '.mozconfig']
            ))

        self.addStep(ShellCommand(
         name='compile',
         command=["make", "-f", "client.mk", "build"],
         description=['compile'],
         timeout=60*60, # 1 hour
         haltOnFailure=1,
         env=self.env,
        ))

        self.addStep(ShellCommand(
         name='make_buildsymbols',
         command=['make', 'buildsymbols'],
         workdir='build/%s' % self.objdir,
         timeout=60*60,
         env=self.env,
        ))

        # Need to override toolsdir as set by MozillaBuildFactory because
        # we need Windows-style paths.
        if self.platform.startswith('win'):
            self.addStep(SetProperty(
                command=['bash', '-c', 'pwd -W'],
                property='toolsdir',
                workdir='tools'
            ))

        self.doUpload()

        self.env['MINIDUMP_STACKWALK'] = getPlatformMinidumpPath(self.platform)
        self.env['MINIDUMP_SAVE_PATH'] = WithProperties('%(basedir:-)s/minidumps')

        self.addPreTestSteps()

        self.addTestSteps()

        self.addPostTestSteps()

        if self.buildsBeforeReboot and self.buildsBeforeReboot > 0:
            self.addPeriodicRebootSteps()

    def doUpload(self, postUploadBuildDir=None, uploadMulti=False):
        if self.uploadPackages:
            self.addStep(ShellCommand(
             name='make_pkg',
             command=['make', 'package'],
             env=self.env,
             workdir='build/%s' % self.objdir,
             haltOnFailure=True
            ))
            self.addStep(ShellCommand(
             name='make_pkg_tests',
             command=['make', 'package-tests'],
             env=self.env,
             workdir='build/%s' % self.objdir,
             haltOnFailure=True
            ))
            self.addStep(GetBuildID(
             name='get_build_id',
             objdir=self.objdir,
            ))

            uploadEnv = self.env.copy()
            uploadEnv.update({'UPLOAD_HOST': self.stageServer,
                              'UPLOAD_USER': self.stageUsername,
                              'UPLOAD_TO_TEMP': '1'})
            if self.stageSshKey:
                uploadEnv['UPLOAD_SSH_KEY'] = '~/.ssh/%s' % self.stageSshKey

            # Always upload builds to the dated tinderbox builds directories
            uploadEnv['POST_UPLOAD_CMD'] = postUploadCmdPrefix(
                    as_list=False,
                    upload_dir="%s-%s-unittest" % (self.branchName, self.platform),
                    buildid=WithProperties("%(buildid)s"),
                    product=self.productName,
                    to_tinderbox_dated=True,
                    )
            self.addStep(SetProperty(
             name='make_upload',
             command=['make', 'upload'],
             env=uploadEnv,
             workdir='build/%s' % self.objdir,
             extract_fn = parse_make_upload,
             haltOnFailure=True,
             description=['upload'],
             timeout=60*60, # 60 minutes
             log_eval_func=lambda c,s: regex_log_evaluator(c, s, upload_errors),
            ))

            sendchange_props = {
                    'buildid': WithProperties('%(buildid:-)s'),
                    'builduid': WithProperties('%(builduid:-)s'),
                    }
            for master, warn, retries in self.unittestMasters:
                self.addStep(SendChangeStep(
                 name='sendchange_%s' % master,
                 warnOnFailure=warn,
                 master=master,
                 retries=retries,
                 revision=WithProperties('%(got_revision)s'),
                 branch=self.unittestBranch,
                 files=[WithProperties('%(packageUrl)s'),
                        WithProperties('%(testsUrl)s')],
                 user="sendchange-unittest",
                 comments=WithProperties('%(comments:-)s'),
                 sendchange_props=sendchange_props,
                ))

    def addTestSteps(self):
        self.addStep(unittest_steps.MozillaCheck,
         test_name="check",
         warnOnWarnings=True,
         workdir="build/%s" % self.objdir,
         timeout=5*60, # 5 minutes.
         env=self.env,
        )

    def addPrintChangesetStep(self):
        changesetLink = ''.join(['<a href=http://hg.mozilla.org/',
            self.repoPath,
            '/rev/%(got_revision)s title="Built from revision %(got_revision)s">rev:%(got_revision)s</a>'])
        self.addStep(OutputStep(
         name='tinderboxprint_changeset',
         data=['TinderboxPrint:', WithProperties(changesetLink)],
        ))

    def addCopyMozconfigStep(self):
        config_dir_map = {
                'linux': 'linux/%s/unittest' % self.branchName,
                'linux64': 'linux64/%s/unittest' % self.branchName,
                'macosx': 'macosx/%s/unittest' % self.branchName,
                'macosx64': 'macosx64/%s/unittest' % self.branchName,
                'win32': 'win32/%s/unittest' % self.branchName,
                }
        mozconfig = 'mozconfigs/%s/%s/mozconfig' % \
            (self.config_dir, config_dir_map[self.platform])

        self.addStep(ShellCommand(
         name='copy_mozconfig',
         command=['cp', mozconfig, 'build/.mozconfig'],
         description=['copy mozconfig'],
         workdir='.'
        ))

    def addPreTestSteps(self):
        pass

    def addPostTestSteps(self):
        pass

class TryUnittestBuildFactory(UnittestBuildFactory):
    def __init__(self, **kwargs):

        UnittestBuildFactory.__init__(self, **kwargs)

    def doUpload(self, postUploadBuildDir=None, uploadMulti=False):
        if self.uploadPackages:
            self.addStep(ShellCommand(
             name='make_pkg',
             command=['make', 'package'],
             env=self.env,
             workdir='build/%s' % self.objdir,
             haltOnFailure=True
            ))
            self.addStep(ShellCommand(
             name='make_pkg_tests',
             command=['make', 'package-tests'],
             env=self.env,
             workdir='build/%s' % self.objdir,
             haltOnFailure=True
            ))
            self.addStep(GetBuildID(
             name='get_build_id',
             objdir=self.objdir,
            ))
            self.addStep(SetBuildProperty(
             property_name="who",
             value=lambda build:build.source.changes[0].who if len(build.source.changes) > 0 else "",
             haltOnFailure=True
            ))

            uploadEnv = self.env.copy()
            uploadEnv.update({'UPLOAD_HOST': self.stageServer,
                              'UPLOAD_USER': self.stageUsername,
                              'UPLOAD_TO_TEMP': '1'})
            if self.stageSshKey:
                uploadEnv['UPLOAD_SSH_KEY'] = '~/.ssh/%s' % self.stageSshKey

            uploadEnv['POST_UPLOAD_CMD'] = postUploadCmdPrefix(
                    as_list=False,
                    upload_dir="%s-%s-unittest" % (self.branchName, self.platform),
                    buildid=WithProperties("%(buildid)s"),
                    product=self.productName,
                    revision=WithProperties('%(got_revision)s'),
                    who=WithProperties('%(who)s'),
                    builddir=WithProperties('%(builddir)s'),
                    to_try=True,
                    )

            self.addStep(SetProperty(
             command=['make', 'upload'],
             env=uploadEnv,
             workdir='build/%s' % self.objdir,
             extract_fn = parse_make_upload,
             haltOnFailure=True,
             description=['upload'],
             log_eval_func=lambda c,s: regex_log_evaluator(c, s, upload_errors),
            ))

            for master, warn, retries in self.unittestMasters:
                self.addStep(SendChangeStep(
                 name='sendchange_%s' % master,
                 warnOnFailure=warn,
                 master=master,
                 retries=retries,
                 revision=WithProperties('%(got_revision)s'),
                 branch=self.unittestBranch,
                 files=[WithProperties('%(packageUrl)s')],
                 user=WithProperties('%(who)s')),
                 comments=WithProperties('%(comments:-)s'),
                )

class CCUnittestBuildFactory(MozillaBuildFactory):
    def __init__(self, platform, productName, config_repo_path, config_dir,
            objdir, mozRepoPath, brandName=None, mochitest_leak_threshold=None,
            mochichrome_leak_threshold=None, mochibrowser_leak_threshold=None,
            crashtest_leak_threshold=None, uploadPackages=False,
            unittestMasters=None, unittestBranch=None, stageUsername=None,
            stageServer=None, stageSshKey=None, exec_xpcshell_suites=True,
            exec_reftest_suites=True, exec_mochi_suites=True,
            exec_mozmill_suites=False, run_a11y=True, env={}, **kwargs):
        self.env = {}

        MozillaBuildFactory.__init__(self, env=env, **kwargs)

        self.productName = productName
        self.stageServer = stageServer
        self.stageUsername = stageUsername
        self.stageSshKey = stageSshKey
        self.uploadPackages = uploadPackages
        self.config_repo_path = config_repo_path
        self.mozRepoPath = mozRepoPath
        self.config_dir = config_dir
        self.objdir = objdir
        self.run_a11y = run_a11y
        self.unittestMasters = unittestMasters or []
        self.unittestBranch = unittestBranch
        if self.unittestMasters:
            assert self.unittestBranch
        if brandName:
            self.brandName = brandName
        else:
            self.brandName = productName.capitalize()
        self.mochitest_leak_threshold = mochitest_leak_threshold
        self.mochichrome_leak_threshold = mochichrome_leak_threshold
        self.mochibrowser_leak_threshold = mochibrowser_leak_threshold
        self.crashtest_leak_threshold = crashtest_leak_threshold
        self.exec_xpcshell_suites = exec_xpcshell_suites
        self.exec_reftest_suites = exec_reftest_suites
        self.exec_mochi_suites = exec_mochi_suites
        self.exec_mozmill_suites = exec_mozmill_suites

        self.config_repo_url = self.getRepository(self.config_repo_path)

        env_map = {
                'linux': 'linux-unittest',
                'linux64': 'linux64-unittest',
                'macosx': 'macosx-unittest',
                'macosx64': 'macosx64-unittest',
                'win32': 'win32-unittest',
                }

        self.platform = platform.split('-')[0]
        assert self.platform in getSupportedPlatforms()

        # Mozilla subdir and objdir
        self.mozillaDir = '/mozilla'
        self.mozillaObjdir = '%s%s' % (self.objdir, self.mozillaDir)

        self.env = MozillaEnvironments[env_map[self.platform]].copy()
        self.env['MOZ_OBJDIR'] = self.objdir
        self.env.update(env)

        if self.platform == 'win32':
            self.addStep(TinderboxShellCommand(
             name='kill_hg',
             description='kill hg',
             descriptionDone="killed hg",
             command="pskill -t hg.exe",
             workdir="D:\\Utilities"
            ))
            self.addStep(TinderboxShellCommand(
             name='kill_sh',
             description='kill sh',
             descriptionDone="killed sh",
             command="pskill -t sh.exe",
             workdir="D:\\Utilities"
            ))
            self.addStep(TinderboxShellCommand(
             name='kill_make',
             description='kill make',
             descriptionDone="killed make",
             command="pskill -t make.exe",
             workdir="D:\\Utilities"
            ))
            self.addStep(TinderboxShellCommand(
             name="kill_%s" % self.productName,
             description='kill %s' % self.productName,
             descriptionDone="killed %s" % self.productName,
             command="pskill -t %s.exe" % self.productName,
             workdir="D:\\Utilities"
            ))
            self.addStep(TinderboxShellCommand(
             name="kill xpcshell",
             description='kill_xpcshell',
             descriptionDone="killed xpcshell",
             command="pskill -t xpcshell.exe",
             workdir="D:\\Utilities"
            ))

        self.addStep(Mercurial(
         name='hg_update',
         mode='update',
         baseURL='http://%s/' % self.hgHost,
         defaultBranch=self.repoPath,
         alwaysUseLatest=True,
         timeout=60*60 # 1 hour
        ))

        self.addPrintChangesetStep()

        self.addStep(ShellCommand(
         name='checkout_client.py',
         command=['python', 'client.py', 'checkout',
                  '--mozilla-repo=%s' % self.getRepository(self.mozRepoPath)],
         description=['running', 'client.py', 'checkout'],
         descriptionDone=['client.py', 'checkout'],
         haltOnFailure=True,
         timeout=60*60 # 1 hour
        ))

        self.addPrintMozillaChangesetStep()

        self.addStep(ShellCommand(
         name='rm_configs',
         command=['rm', '-rf', 'mozconfigs'],
         workdir='.'
        ))

        self.addStep(MercurialCloneCommand(
         name='buildbot_configs',
         command=['hg', 'clone', self.config_repo_url, 'mozconfigs'],
         workdir='.'
        ))

        self.addCopyMozconfigStep()

        self.addStep(ShellCommand(
         name='mozconfig_contents',
         command=['cat', '.mozconfig']
        ))

        self.addStep(ShellCommand(
         name='compile',
         command=["make", "-f", "client.mk", "build"],
         description=['compile'],
         timeout=60*60, # 1 hour
         haltOnFailure=1,
         env=self.env,
        ))

        self.addStep(ShellCommand(
         name='make_buildsymbols',
         command=['make', 'buildsymbols'],
         workdir='build/%s' % self.objdir,
         env=self.env,
        ))

        # Need to override toolsdir as set by MozillaBuildFactory because
        # we need Windows-style paths.
        if self.platform == 'win32':
            self.addStep(SetProperty(
                command=['bash', '-c', 'pwd -W'],
                property='toolsdir',
                workdir='tools'
            ))

        self.doUpload()

        self.env['MINIDUMP_STACKWALK'] = getPlatformMinidumpPath(self.platform)
        self.env['MINIDUMP_SAVE_PATH'] = WithProperties('%(basedir:-)s/minidumps')

        self.addPreTestSteps()

        self.addTestSteps()

        self.addPostTestSteps()

        if self.buildsBeforeReboot and self.buildsBeforeReboot > 0:
            self.addPeriodicRebootSteps()

    def doUpload(self, postUploadBuildDir=None, uploadMulti=False):
        if self.uploadPackages:
            self.addStep(ShellCommand(
             name='make_pkg',
             command=['make', 'package'],
             env=self.env,
             workdir='build/%s' % self.objdir,
             haltOnFailure=True
            ))
            self.addStep(ShellCommand(
             name='make_pkg_tests',
             command=['make', 'package-tests'],
             env=self.env,
             workdir='build/%s' % self.objdir,
             haltOnFailure=True
            ))
            if self.mozillaDir == '':
                getpropsObjdir = self.objdir
            else:
                getpropsObjdir = '../%s' % self.mozillaObjdir
            self.addStep(GetBuildID(
             objdir=getpropsObjdir,
             workdir='build%s' % self.mozillaDir,
            ))

            uploadEnv = self.env.copy()
            uploadEnv.update({'UPLOAD_HOST': self.stageServer,
                              'UPLOAD_USER': self.stageUsername,
                              'UPLOAD_TO_TEMP': '1'})
            if self.stageSshKey:
                uploadEnv['UPLOAD_SSH_KEY'] = '~/.ssh/%s' % self.stageSshKey

            # Always upload builds to the dated tinderbox builds directories
            uploadEnv['POST_UPLOAD_CMD'] = postUploadCmdPrefix(
                    as_list=False,
                    upload_dir="%s-%s-unittest" % (self.branchName, self.platform),
                    buildid=WithProperties("%(buildid)s"),
                    product=self.productName,
                    to_tinderbox_dated=True,
                    )
            self.addStep(SetProperty(
             name='make_upload',
             command=['make', 'upload'],
             env=uploadEnv,
             workdir='build/%s' % self.objdir,
             extract_fn = parse_make_upload,
             haltOnFailure=True,
             description=['upload'],
             timeout=60*60, # 60 minutes
             log_eval_func=lambda c,s: regex_log_evaluator(c, s, upload_errors),
            ))

            sendchange_props = {
                    'buildid': WithProperties('%(buildid:-)s'),
                    'builduid': WithProperties('%(builduid:-)s'),
                    }
            for master, warn, retries in self.unittestMasters:
                self.addStep(SendChangeStep(
                 name='sendchange_%s' % master,
                 warnOnFailure=warn,
                 master=master,
                 retries=retries,
                 revision=WithProperties('%(got_revision)s'),
                 branch=self.unittestBranch,
                 files=[WithProperties('%(packageUrl)s'),
                        WithProperties('%(testsUrl)s')],
                 user="sendchange-unittest",
                 comments=WithProperties('%(comments:-)s'),
                 sendchange_props=sendchange_props,
                ))

    def addTestSteps(self):
        self.addStep(unittest_steps.MozillaCheck,
         test_name="check",
         warnOnWarnings=True,
         workdir="build/%s" % self.objdir,
         timeout=5*60, # 5 minutes.
        )

        if self.exec_xpcshell_suites:
            self.addStep(unittest_steps.MozillaCheck,
             test_name="xpcshell-tests",
             warnOnWarnings=True,
             workdir="build/%s" % self.objdir,
             timeout=5*60, # 5 minutes.
            )

        if self.exec_mozmill_suites:
            mozmillEnv = self.env.copy()
            mozmillEnv['NO_EM_RESTART'] = "0"
            self.addStep(unittest_steps.MozillaCheck,
             test_name="mozmill",
             warnOnWarnings=True,
             workdir="build/%s" % self.objdir,
             timeout=5*60, # 5 minutes.
             env=mozmillEnv,
            )

        if self.exec_reftest_suites:
            self.addStep(unittest_steps.MozillaReftest, warnOnWarnings=True,
             test_name="reftest",
             workdir="build/%s" % self.objdir,
             timeout=5*60,
            )
            self.addStep(unittest_steps.MozillaReftest, warnOnWarnings=True,
             test_name="crashtest",
             leakThreshold=self.crashtest_leak_threshold,
             workdir="build/%s" % self.objdir,
            )

        if self.exec_mochi_suites:
            self.addStep(unittest_steps.MozillaMochitest, warnOnWarnings=True,
             test_name="mochitest-plain",
             workdir="build/%s" % self.objdir,
             leakThreshold=self.mochitest_leak_threshold,
             timeout=5*60,
            )
            self.addStep(unittest_steps.MozillaMochitest, warnOnWarnings=True,
             test_name="mochitest-chrome",
             workdir="build/%s" % self.objdir,
             leakThreshold=self.mochichrome_leak_threshold,
            )
            self.addStep(unittest_steps.MozillaMochitest, warnOnWarnings=True,
             test_name="mochitest-browser-chrome",
             workdir="build/%s" % self.objdir,
             leakThreshold=self.mochibrowser_leak_threshold,
            )
            if self.run_a11y:
                self.addStep(unittest_steps.MozillaMochitest, warnOnWarnings=True,
                 test_name="mochitest-a11y",
                 workdir="build/%s" % self.objdir,
                )

    def addPrintChangesetStep(self):
        changesetLink = '<a href=http://%s/%s/rev' % (self.hgHost, self.repoPath)
        changesetLink += '/%(got_revision)s title="Built from revision %(got_revision)s">rev:%(got_revision)s</a>'
        self.addStep(OutputStep(
         name='tinderboxprint_changeset',
         data=['TinderboxPrint:', WithProperties(changesetLink)],
        ))

    def addPrintMozillaChangesetStep(self):
        self.addStep(SetProperty(
         command=['hg', 'identify', '-i'],
         workdir='build%s' % self.mozillaDir,
         property='hg_revision'
        ))
        changesetLink = '<a href=http://%s/%s/rev' % (self.hgHost, self.mozRepoPath)
        changesetLink += '/%(hg_revision)s title="Built from Mozilla revision %(hg_revision)s">moz:%(hg_revision)s</a>'
        self.addStep(OutputStep(
         name='tinderboxprint_changeset',
         data=['TinderboxPrint:', WithProperties(changesetLink)]
        ))

    def addCopyMozconfigStep(self):
        config_dir_map = {
                'linux': 'linux/%s/unittest' % self.branchName,
                'linux64': 'linux64/%s/unittest' % self.branchName,
                'macosx': 'macosx/%s/unittest' % self.branchName,
                'macosx64': 'macosx64/%s/unittest' % self.branchName,
                'win32': 'win32/%s/unittest' % self.branchName,
                }
        mozconfig = 'mozconfigs/%s/%s/mozconfig' % \
            (self.config_dir, config_dir_map[self.platform])

        self.addStep(ShellCommand(
         name='copy_mozconfig',
         command=['cp', mozconfig, 'build/.mozconfig'],
         description=['copy mozconfig'],
         workdir='.'
        ))

    def addPreTestSteps(self):
        pass

    def addPostTestSteps(self):
        pass

class CodeCoverageFactory(UnittestBuildFactory):
    def addCopyMozconfigStep(self):
        config_dir_map = {
                'linux': 'linux/%s/codecoverage' % self.branchName,
                'linux64': 'linux64/%s/codecoverage' % self.branchName,
                'macosx': 'macosx/%s/codecoverage' % self.branchName,
                'macosx64': 'macosx64/%s/codecoverage' % self.branchName,
                'win32': 'win32/%s/codecoverage' % self.branchName,
                }
        mozconfig = 'mozconfigs/%s/%s/mozconfig' % \
            (self.config_dir, config_dir_map[self.platform])

        self.addStep(ShellCommand(
         name='copy_mozconfig',
         command=['cp', mozconfig, 'build/.mozconfig'],
         description=['copy mozconfig'],
         workdir='.'
        ))

    def addInitialSteps(self):
        # Always clobber code coverage builds
        self.addStep(ShellCommand(
         name='rm_builddir',
         command=['rm', '-rf', 'build'],
         workdir=".",
         timeout=30*60,
        ))
        UnittestBuildFactory.addInitialSteps(self)

    def addPreTestSteps(self):
        self.addStep(ShellCommand(
         name='mv_bin_original',
         command=['mv','bin','bin-original'],
         workdir="build/%s/dist" % self.objdir,
        ))
        self.addStep(ShellCommand(
         name='jscoverage_bin',
         command=['jscoverage', '--mozilla',
                  '--no-instrument=defaults',
                  '--no-instrument=greprefs.js',
                  '--no-instrument=chrome/browser/content/browser/places/treeView.js',
                  'bin-original', 'bin'],
         workdir="build/%s/dist" % self.objdir,
        ))

    def addPostTestSteps(self):
        self.addStep(ShellCommand(
         name='lcov_app_info',
         command=['lcov', '-c', '-d', '.', '-o', 'app.info'],
         workdir="build/%s" % self.objdir,
        ))
        self.addStep(ShellCommand(
         name='rm_cc_html',
         command=['rm', '-rf', 'codecoverage_html'],
         workdir="build",
        ))
        self.addStep(ShellCommand(
         name='mkdir_cc_html',
         command=['mkdir', 'codecoverage_html'],
         workdir="build",
        ))
        self.addStep(ShellCommand(
         name='generate_html',
         command=['genhtml', '../%s/app.info' % self.objdir],
         workdir="build/codecoverage_html",
        ))
        self.addStep(ShellCommand(
         name='cp_cc_html',
         command=['cp', '%s/dist/bin/application.ini' % self.objdir, 'codecoverage_html'],
         workdir="build",
        ))
        tarfile = "codecoverage-%s.tar.bz2" % self.branchName
        self.addStep(ShellCommand(
         name='tar_cc_html',
         command=['tar', 'jcvf', tarfile, 'codecoverage_html'],
         workdir="build",
        ))

        uploadEnv = self.env.copy()
        uploadEnv.update({'UPLOAD_HOST': self.stageServer,
                          'UPLOAD_USER': self.stageUsername,
                          'UPLOAD_PATH': '/home/ftp/pub/firefox/nightly/experimental/codecoverage'})
        if self.stageSshKey:
            uploadEnv['UPLOAD_SSH_KEY'] = '~/.ssh/%s' % self.stageSshKey
        if 'POST_UPLOAD_CMD' in uploadEnv:
            del uploadEnv['POST_UPLOAD_CMD']
        self.addStep(ShellCommand(
         name='upload_tar',
         env=uploadEnv,
         command=['python', 'build/upload.py', tarfile],
         workdir="build",
        ))

        # Tar up and upload the js report
        tarfile = "codecoverage-%s-jsreport.tar.bz2" % self.branchName
        self.addStep(ShellCommand(
         name='tar_cc_jsreport',
         command=['tar', 'jcv', '-C', '%s/dist/bin' % self.objdir, '-f', tarfile, 'jscoverage-report'],
         workdir="build",
        ))
        self.addStep(ShellCommand(
         name='upload_jsreport',
         env=uploadEnv,
         command=['python', 'build/upload.py', tarfile],
         workdir="build",
        ))

        # And the logs too
        tarfile = "codecoverage-%s-logs.tar" % self.branchName
        self.addStep(ShellCommand(
         name='tar_cc_logs',
         command=['tar', 'cvf', tarfile, 'logs'],
         workdir="build",
        ))
        self.addStep(ShellCommand(
         name='upload_logs',
         env=uploadEnv,
         command=['python', 'build/upload.py', tarfile],
         workdir="build",
        ))

        # Clean up after ourselves
        self.addStep(ShellCommand(
         name='rm_builddir',
         command=['rm', '-rf', 'build'],
         workdir=".",
         timeout=30*60,
        ))

    def addTestSteps(self):
        self.addStep(ShellCommand(
         command=['rm', '-rf', 'logs'],
         workdir="build",
        ))
        self.addStep(ShellCommand(
         command=['mkdir', 'logs'],
         workdir="build",
        ))

        commands = [
                ('check', ['make', '-k', 'check'], 10*60),
                ('xpcshell', ['make', 'xpcshell-tests'], 1*60*60),
                ('reftest', ['make', 'reftest'], 1*60*60),
                ('crashtest', ['make', 'crashtest'], 12*60*60),
                ('mochitest-chrome', ['make', 'mochitest-chrome'], 1*60*60),
                ('mochitest-browser-chrome', ['make', 'mochitest-browser-chrome'], 12*60*60),
                ]

        # This should be replaced with 'make mochitest-plain-serial'
        # or chunked calls once those are available.
        mochitest_dirs = ['browser', 'caps', 'content', 'docshell', 'dom',
                'editor', 'embedding', 'extensions', 'fonts', 'intl', 'js',
                'layout', 'MochiKit_Unit_Tests', 'modules', 'parser',
                'toolkit', 'uriloader',]

        for test_dir in mochitest_dirs:
            commands.append(
                ('mochitest-plain-%s' % test_dir,
                 ['make', 'TEST_PATH=%s' % test_dir, 'mochitest-plain'],
                 4*60*60,)
                )

        if self.run_a11y:
            commands.append(
                ('mochitest-a11y', ['make', 'mochitest-a11y'], 4*60*60),
            )

        for name, command, timeout in commands:
            real_command = " ".join(command)
            real_command += " 2>&1 | tee >(bzip2 -c > ../logs/%s.log.bz2)" % name
            self.addStep(ShellCommand(
             name=name,
             command=['bash', '-c', real_command],
             workdir="build/%s" % self.objdir,
             timeout=timeout,
            ))

class L10nVerifyFactory(ReleaseFactory):
    def __init__(self, cvsroot, stagingServer, productName, version,
                 buildNumber, oldVersion, oldBuildNumber,
                 platform, verifyDir='verify', linuxExtension='bz2',
                 buildSpace=4, **kwargs):
        # MozillaBuildFactory needs the 'repoPath' argument, but we don't
        ReleaseFactory.__init__(self, repoPath='nothing', buildSpace=buildSpace,
                                **kwargs)

        verifyDirVersion = 'tools/release/l10n'
        platformFtpDir = getPlatformFtpDir(platform)

        # Remove existing verify dir
        self.addStep(ShellCommand(
         name='rm_verify_dir',
         description=['remove', 'verify', 'dir'],
         descriptionDone=['removed', 'verify', 'dir'],
         command=['rm', '-rf', verifyDir],
         workdir='.',
         haltOnFailure=True,
        ))

        self.addStep(ShellCommand(
         name='mkdir_verify',
         description=['(re)create', 'verify', 'dir'],
         descriptionDone=['(re)created', 'verify', 'dir'],
         command=['bash', '-c', 'mkdir -p ' + verifyDirVersion],
         workdir='.',
         haltOnFailure=True,
        ))

        # Download current release
        self.addStep(RetryingShellCommand(
         name='download_current_release',
         description=['download', 'current', 'release'],
         descriptionDone=['downloaded', 'current', 'release'],
         command=['rsync',
                  '-Lav',
                  '-e', 'ssh',
                  '--exclude=*.asc',
                  '--exclude=*.checksums',
                  '--exclude=source',
                  '--exclude=xpi',
                  '--exclude=unsigned',
                  '--exclude=update',
                  '--exclude=*.crashreporter-symbols.zip',
                  '--exclude=*.tests.zip',
                  '--exclude=*.tests.tar.bz2',
                  '--exclude=*.txt',
                  '--exclude=logs',
                  '%s:/home/ftp/pub/%s/nightly/%s-candidates/build%s/%s' %
                   (stagingServer, productName, version, str(buildNumber),
                    platformFtpDir),
                  '%s-%s-build%s/' % (productName,
                                      version,
                                      str(buildNumber))
                  ],
         workdir=verifyDirVersion,
         haltOnFailure=True,
         timeout=60*60
        ))

        # Download previous release
        self.addStep(RetryingShellCommand(
         name='download_previous_release',
         description=['download', 'previous', 'release'],
         descriptionDone =['downloaded', 'previous', 'release'],
         command=['rsync',
                  '-Lav',
                  '-e', 'ssh',
                  '--exclude=*.asc',
                  '--exclude=*.checksums',
                  '--exclude=source',
                  '--exclude=xpi',
                  '--exclude=unsigned',
                  '--exclude=update',
                  '--exclude=*.crashreporter-symbols.zip',
                  '--exclude=*.tests.zip',
                  '--exclude=*.tests.tar.bz2',
                  '--exclude=*.txt',
                  '--exclude=logs',
                  '%s:/home/ftp/pub/%s/nightly/%s-candidates/build%s/%s' %
                   (stagingServer,
                    productName,
                    oldVersion,
                    str(oldBuildNumber),
                    platformFtpDir),
                  '%s-%s-build%s/' % (productName,
                                      oldVersion,
                                      str(oldBuildNumber))
                  ],
         workdir=verifyDirVersion,
         haltOnFailure=True,
         timeout=60*60
        ))

        currentProduct = '%s-%s-build%s' % (productName,
                                            version,
                                            str(buildNumber))
        previousProduct = '%s-%s-build%s' % (productName,
                                             oldVersion,
                                             str(oldBuildNumber))

        for product in [currentProduct, previousProduct]:
            self.addStep(ShellCommand(
                         name='recreate_product_dir',
                         description=['(re)create', 'product', 'dir'],
                         descriptionDone=['(re)created', 'product', 'dir'],
                         command=['bash', '-c', 'mkdir -p %s/%s' % (verifyDirVersion, product)],
                         workdir='.',
                         haltOnFailure=True,
            ))
            self.addStep(ShellCommand(
                         name='verify_l10n',
                         description=['verify', 'l10n', product],
                         descriptionDone=['verified', 'l10n', product],
                         command=["bash", "-c",
                                  "./verify_l10n.sh %s %s" % (product,
                                                              platformFtpDir)],
                         workdir=verifyDirVersion,
                         haltOnFailure=True,
            ))

        self.addStep(L10nVerifyMetaDiff(
                     currentProduct=currentProduct,
                     previousProduct=previousProduct,
                     workdir=verifyDirVersion,
        ))


def parse_sendchange_files(build, include_substr='', exclude_substrs=[]):
    '''Given a build object, figure out which files have the include_substr
    in them, then exclude files that have one of the exclude_substrs. This
    function uses substring pattern matching instead of regular expressions
    as it meets the need without incurring as much overhead.'''
    potential_files=[]
    for file in build.source.changes[-1].files:
        if include_substr in file and file not in potential_files:
            potential_files.append(file)
    assert len(potential_files) > 0, 'sendchange missing this archive type'
    for substring in exclude_substrs:
        for f in potential_files[:]:
            if substring in f:
                potential_files.remove(f)
    assert len(potential_files) == 1, 'Ambiguous testing sendchange!'
    return potential_files[0]


class MozillaTestFactory(MozillaBuildFactory):
    def __init__(self, platform, productName='firefox',
                 downloadSymbols=True, downloadTests=False,
                 posixBinarySuffix='-bin', resetHwClock=False, stackwalk_cgi=None,
                 **kwargs):
        #Note: the posixBinarySuffix is needed because some products (firefox)
        #use 'firefox-bin' and some (fennec) use 'fennec' for the name of the
        #actual application binary.  This is only applicable to posix-like
        #systems.  Windows always uses productName.exe (firefox.exe and
        #fennec.exe)
        self.platform = platform.split('-')[0]
        self.productName = productName
        if not posixBinarySuffix:
            #all forms of no should result in empty string
            self.posixBinarySuffix = ''
        else:
            self.posixBinarySuffix = posixBinarySuffix
        self.downloadSymbols = downloadSymbols
        self.downloadTests = downloadTests
        self.resetHwClock = resetHwClock
        self.stackwalk_cgi = stackwalk_cgi

        assert self.platform in getSupportedPlatforms()

        MozillaBuildFactory.__init__(self, **kwargs)

        self.ignoreCerts = False
        if self.branchName.lower().startswith('shadow'):
            self.ignoreCerts = True

        self.addCleanupSteps()
        self.addPrepareBuildSteps()
        if self.downloadSymbols:
            self.addPrepareSymbolsSteps()
        if self.downloadTests:
            self.addPrepareTestsSteps()
        self.addIdentifySteps()
        self.addSetupSteps()
        self.addRunTestSteps()
        self.addTearDownSteps()

    def addInitialSteps(self):
        def get_revision(build):
            try:
                revision = build.source.changes[-1].revision
                return revision
            except:
                return "not-set"
        self.addStep(SetBuildProperty(
         property_name="revision",
         value=get_revision,
        ))

        def get_who(build):
            try:
                revision = build.source.changes[-1].who
                return revision
            except:
                return "not-set"

        self.addStep(SetBuildProperty(
         property_name="who",
         value=get_who,
        ))

        MozillaBuildFactory.addInitialSteps(self)

    def addCleanupSteps(self):
        '''Clean up the relevant places before starting a build'''
        #On windows, we should try using cmd's attrib and native rmdir
        self.addStep(ShellCommand(
            name='rm_builddir',
            command=['rm', '-rf', 'build'],
            workdir='.'
        ))

    def addPrepareBuildSteps(self):
        '''This function understands how to prepare a build for having tests run
        against it.  It downloads, unpacks then sets important properties for use
        during testing'''
        def get_build_url(build):
            '''Make sure that there is at least one build in the file list'''
            assert len(build.source.changes[-1].files) > 0, 'Unittest sendchange has no files'
            return parse_sendchange_files(build, exclude_substrs=['.crashreporter-symbols.',
                                                   '.tests.'])
        self.addStep(DownloadFile(
            url_fn=get_build_url,
            filename_property='build_filename',
            url_property='build_url',
            haltOnFailure=True,
            ignore_certs=self.ignoreCerts,
            name='download_build',
        ))
        self.addStep(UnpackFile(
            filename=WithProperties('%(build_filename)s'),
            scripts_dir='../tools/buildfarm/utils',
            haltOnFailure=True,
            name='unpack_build',
        ))
        # Find the application binary!
        if self.platform.startswith('macosx'):
            self.addStep(FindFile(
                filename="%s%s" % (self.productName, self.posixBinarySuffix),
                directory=".",
                max_depth=4,
                property_name="exepath",
                name="find_executable",
            ))
        elif self.platform.startswith('win'):
            self.addStep(SetBuildProperty(
             property_name="exepath",
             value="%s/%s.exe" % (self.productName, self.productName),
            ))
        else:
            self.addStep(SetBuildProperty(
             property_name="exepath",
             value="%s/%s%s" % (self.productName, self.productName,
                                self.posixBinarySuffix),
            ))

        def get_exedir(build):
            return os.path.dirname(build.getProperty('exepath'))
        self.addStep(SetBuildProperty(
         property_name="exedir",
         value=get_exedir,
        ))

        # Need to override toolsdir as set by MozillaBuildFactory because
        # we need Windows-style paths for the stack walker.
        if self.platform.startswith('win'):
            self.addStep(SetProperty(
             command=['bash', '-c', 'pwd -W'],
             property='toolsdir',
             workdir='tools'
            ))


    def addPrepareSymbolsSteps(self):
        '''This function knows how to setup the symbols for a build to be useful'''
        def get_symbols_url(build):
            '''If there are two files, we assume that the second file is the tests tarball
            and use the same location as the build, with the build's file extension replaced
            with .crashreporter-symbols.zip.  If there are three or more files then we figure
            out which is the real file'''
            if len(build.source.changes[-1].files) < 3:
                build_url = build.getProperty('build_url')
                for suffix in ('.tar.bz2', '.zip', '.dmg', '.exe', '.apk'):
                    if build_url.endswith(suffix):
                        return build_url[:-len(suffix)] + '.crashreporter-symbols.zip'
            else:
                return parse_sendchange_files(build, include_substr='.crashreporter-symbols.')

        if not self.stackwalk_cgi:
            self.addStep(DownloadFile(
                url_fn=get_symbols_url,
                filename_property='symbols_filename',
                url_property='symbols_url',
                name='download_symbols',
                ignore_certs=self.ignoreCerts,
                workdir='build/symbols'
            ))
            self.addStep(UnpackFile(
                filename=WithProperties('%(symbols_filename)s'),
                name='unpack_symbols',
                workdir='build/symbols'
            ))
        else:
            self.addStep(SetBuildProperty(
                property_name='symbols_url',
                value=get_symbols_url,
            ))

    def addPrepareTestsSteps(self):
        def get_tests_url(build):
            '''If there is only one file, we assume that the tests package is at
            the same location with the file extension of the browser replaced with
            .tests.tar.bz2, otherwise we try to find the explicit file'''
            if len(build.source.changes[-1].files) < 2:
                build_url = build.getProperty('build_url')
                for suffix in ('.tar.bz2', '.zip', '.dmg', '.exe'):
                    if build_url.endswith(suffix):
                        return build_url[:-len(suffix)] + '.tests.tar.bz2'
            else:
                return parse_sendchange_files(build, include_substr='.tests.')
        self.addStep(DownloadFile(
            url_fn=get_tests_url,
            filename_property='tests_filename',
            url_property='tests_url',
            haltOnFailure=True,
            ignore_certs=self.ignoreCerts,
            name='download tests',
        ))

    def addIdentifySteps(self):
        '''This function knows how to figure out which build this actually is
        and display it in a useful way'''
        # Figure out build ID and TinderboxPrint revisions
        def get_build_info(rc, stdout, stderr):
            retval = {}
            stdout = "\n".join([stdout, stderr])
            m = re.search("^buildid: (\w+)", stdout, re.M)
            if m:
                retval['buildid'] = m.group(1)
            return retval
        self.addStep(SetProperty(
         command=['python', WithProperties('%(toolsdir)s/buildfarm/utils/printbuildrev.py'),
                  WithProperties('%(exedir)s')],
         workdir='build',
         extract_fn=get_build_info,
         name='get build info',
        ))

    def addSetupSteps(self):
        '''This stub is for implementing classes to do harness specific setup'''
        pass

    def addRunTestSteps(self):
        '''This stub is for implementing classes to do the actual test runs'''
        pass

    def addTearDownSteps(self):
        self.addCleanupSteps()
        if 'macosx64' in self.platform:
            # This will fail cleanly and silently on 10.6
            # but is important on 10.7
            self.addStep(ShellCommand(
                name="clear_saved_state",
                flunkOnFailure=False,
                warnOnFailure=False,
                haltOnFailure=False,
                workdir='/Users/cltbld',
                command=['bash', '-c', 'rm -rf Library/Saved\ Application\ State/*.savedState']
            ))
        if self.buildsBeforeReboot and self.buildsBeforeReboot > 0:
            #This step is to deal with minis running linux that don't reboot properly
            #see bug561442
            if self.resetHwClock and 'linux' in self.platform:
                self.addStep(ShellCommand(
                    name='set_time',
                    description=['set', 'time'],
                    alwaysRun=True,
                    command=['bash', '-c',
                             'sudo hwclock --set --date="$(date +%m/%d/%y\ %H:%M:%S)"'],
                ))
            self.addPeriodicRebootSteps()


def resolution_step():
    return ShellCommand(
        name='show_resolution',
        flunkOnFailure=False,
        warnOnFailure=False,
        haltOnFailure=False,
        workdir='/Users/cltbld',
        command=['bash', '-c', 'screenresolution get && screenresolution list && system_profiler SPDisplaysDataType']
    )

class UnittestPackagedBuildFactory(MozillaTestFactory):
    def __init__(self, platform, test_suites, env, productName='firefox',
                 mochitest_leak_threshold=None,
                 crashtest_leak_threshold=None, totalChunks=None,
                 thisChunk=None, chunkByDir=None, stackwalk_cgi=None,
                 **kwargs):
        platform = platform.split('-')[0]
        self.test_suites = test_suites
        self.totalChunks = totalChunks
        self.thisChunk = thisChunk
        self.chunkByDir = chunkByDir

        testEnv = MozillaEnvironments['%s-unittest' % platform].copy()
        if stackwalk_cgi and kwargs.get('downloadSymbols'):
            testEnv['MINIDUMP_STACKWALK_CGI'] = stackwalk_cgi
        else:
            testEnv['MINIDUMP_STACKWALK'] = getPlatformMinidumpPath(platform)
        testEnv['MINIDUMP_SAVE_PATH'] = WithProperties('%(basedir:-)s/minidumps')
        testEnv.update(env)

        self.leak_thresholds = {'mochitest-plain': mochitest_leak_threshold,
                                'crashtest': crashtest_leak_threshold,}

        MozillaTestFactory.__init__(self, platform, productName, env=testEnv,
                                    downloadTests=True, stackwalk_cgi=stackwalk_cgi,
                                    **kwargs)

    def addSetupSteps(self):
        if 'linux' in self.platform:
            self.addStep(ShellCommand(
                name='disable_screensaver',
                command=['xset', 's', 'reset'],
                env=self.env,
            ))

    def addRunTestSteps(self):
        if self.platform.startswith('macosx64'):
            self.addStep(resolution_step())
        # Run them!
        if self.stackwalk_cgi and self.downloadSymbols:
            symbols_path = '%(symbols_url)s'
        else:
            symbols_path = 'symbols'
        for suite in self.test_suites:
            leak_threshold = self.leak_thresholds.get(suite, None)
            if suite.startswith('mobile-mochitest'):
                # Mobile specific mochitests need a couple things to be
                # set differently compared to non-mobile specific tests
                real_suite = suite[len('mobile-'):]
                # Unpack the tests
                self.addStep(UnpackTest(
                 filename=WithProperties('%(tests_filename)s'),
                 testtype='mochitest',
                 haltOnFailure=True,
                 name='unpack mochitest tests',
                 ))

                variant = real_suite.split('-', 1)[1]
                self.addStep(unittest_steps.MozillaPackagedMochitests(
                 variant=variant,
                 env=self.env,
                 symbols_path=symbols_path,
                 testPath='mobile',
                 leakThreshold=leak_threshold,
                 chunkByDir=self.chunkByDir,
                 totalChunks=self.totalChunks,
                 thisChunk=self.thisChunk,
                 maxTime=90*60, # One and a half hours, to allow for slow minis
                ))
            elif suite.startswith('mochitest'):
                # Unpack the tests
                self.addStep(UnpackTest(
                 filename=WithProperties('%(tests_filename)s'),
                 testtype='mochitest',
                 haltOnFailure=True,
                 name='unpack mochitest tests',
                 ))

                variant = suite.split('-', 1)[1]
                self.addStep(unittest_steps.MozillaPackagedMochitests(
                 variant=variant,
                 env=self.env,
                 symbols_path=symbols_path,
                 leakThreshold=leak_threshold,
                 chunkByDir=self.chunkByDir,
                 totalChunks=self.totalChunks,
                 thisChunk=self.thisChunk,
                 maxTime=90*60, # One and a half hours, to allow for slow minis
                ))
            elif suite == 'xpcshell':
                # Unpack the tests
                self.addStep(UnpackTest(
                 filename=WithProperties('%(tests_filename)s'),
                 testtype='xpcshell',
                 haltOnFailure=True,
                 name='unpack xpcshell tests',
                 ))

                self.addStep(unittest_steps.MozillaPackagedXPCShellTests(
                 env=self.env,
                 platform=self.platform,
                 symbols_path=symbols_path,
                 maxTime=120*60, # Two Hours
                ))
            elif suite in ('jsreftest', ):
                # Specialized runner for jsreftest because they take so long to unpack and clean up
                # Unpack the tests
                self.addStep(UnpackTest(
                 filename=WithProperties('%(tests_filename)s'),
                 testtype='jsreftest',
                 haltOnFailure=True,
                 name='unpack jsreftest tests',
                 ))

                self.addStep(unittest_steps.MozillaPackagedReftests(
                 suite=suite,
                 env=self.env,
                 leakThreshold=leak_threshold,
                 symbols_path=symbols_path,
                 maxTime=2*60*60, # Two Hours
                ))
            elif suite == 'jetpack':
                # Unpack the tests
                self.addStep(UnpackTest(
                 filename=WithProperties('%(tests_filename)s'),
                 testtype='jetpack',
                 haltOnFailure=True,
                 name='unpack jetpack tests',
                 ))

                self.addStep(unittest_steps.MozillaPackagedJetpackTests(
                  suite=suite,
                  env=self.env,
                  leakThreshold=leak_threshold,
                  symbols_path=symbols_path,
                  maxTime=120*60, # Two Hours
                 ))
            elif suite in ('reftest', 'reftest-ipc', 'reftest-d2d', 'crashtest', \
                           'crashtest-ipc', 'direct3D', 'opengl', 'opengl-no-accel', \
                           'reftest-no-d2d-d3d'):
                if suite in ('direct3D', 'opengl'):
                    self.env.update({'MOZ_ACCELERATED':'11'})
                if suite in ('reftest-ipc', 'crashtest-ipc'):
                    self.env.update({'MOZ_LAYERS_FORCE_SHMEM_SURFACES':'1'})
                # Unpack the tests
                self.addStep(UnpackTest(
                 filename=WithProperties('%(tests_filename)s'),
                 testtype='reftest',
                 haltOnFailure=True,
                 name='unpack reftest tests',
                 ))
                self.addStep(unittest_steps.MozillaPackagedReftests(
                 suite=suite,
                 env=self.env,
                 leakThreshold=leak_threshold,
                 symbols_path=symbols_path,
                 maxTime=2*60*60, # Two Hours
                ))
            elif suite == 'mozmill':

                # Unpack the tests
                self.addStep(UnpackTest(
                 filename=WithProperties('%(tests_filename)s'),
                 testtype='mozmill',
                 haltOnFailure=True,
                 name='unpack mochitest tests',
                 ))

                # install mozmill into its virtualenv
                self.addStep(ShellCommand(
                    name='install mozmill',
                    command=['python',
                             'mozmill/installmozmill.py'],
                    flunkOnFailure=True,
                    haltOnFailure=True,
                    ))

                # run the mozmill tests
                self.addStep(unittest_steps.MozillaPackagedMozmillTests(
                    name="run_mozmill",
                    tests_dir='tests/firefox',
                    binary='../%(exepath)s',
                    platform=self.platform,
                    workdir='build/mozmill',
                    timeout=10*60,
                    flunkOnFailure=True
                    ))
                self.addStep(unittest_steps.MozillaPackagedMozmillTests(
                    name="run_mozmill_restart",
                    tests_dir='tests/firefox/restartTests',
                    binary='../%(exepath)s',
                    platform=self.platform,
                    restart=True,
                    workdir='build/mozmill',
                    timeout=5*60,
                    flunkOnFailure=True
                    ))
        if self.platform.startswith('macosx64'):
            self.addStep(resolution_step())


class RemoteUnittestFactory(MozillaTestFactory):
    def __init__(self, platform, suites, hostUtils, productName='fennec',
                 downloadSymbols=False, downloadTests=True,
                 posixBinarySuffix='', remoteExtras=None,
                 branchName=None, stackwalk_cgi=None, **kwargs):
        self.suites = suites
        self.hostUtils = hostUtils
        self.stackwalk_cgi = stackwalk_cgi
        self.env = {}

        if stackwalk_cgi and kwargs.get('downloadSymbols'):
            self.env['MINIDUMP_STACKWALK_CGI'] = stackwalk_cgi
        else:
            self.env['MINIDUMP_STACKWALK'] = getPlatformMinidumpPath(platform)
        self.env['MINIDUMP_SAVE_PATH'] = WithProperties('%(basedir:-)s/minidumps')

        if remoteExtras is not None:
            self.remoteExtras = remoteExtras
        else:
            self.remoteExtras = {}

        exePaths = self.remoteExtras.get('processName', {})
        if branchName in exePaths:
            self.remoteProcessName = exePaths[branchName]
        else:
            if 'default' in exePaths:
                self.remoteProcessName = exePaths['default']
            else:
                self.remoteProcessName = 'org.mozilla.fennec'

        MozillaTestFactory.__init__(self, platform, productName=productName,
                                    downloadSymbols=downloadSymbols,
                                    downloadTests=downloadTests,
                                    posixBinarySuffix=posixBinarySuffix,
                                    stackwalk_cgi=stackwalk_cgi,
                                    **kwargs)

    def addCleanupSteps(self):
        '''Clean up the relevant places before starting a build'''
        #On windows, we should try using cmd's attrib and native rmdir
        self.addStep(ShellCommand(
            name='rm_builddir',
            command=['rm', '-rfv', 'build'],
            workdir='.'
        ))

    def addInitialSteps(self):
        self.addStep(SetProperty(
             command=['bash', '-c', 'echo $SUT_IP'],
             property='sut_ip'
        ))
        MozillaTestFactory.addInitialSteps(self)

    def addSetupSteps(self):
        self.addStep(DownloadFile(
            url=self.hostUtils,
            filename_property='hostutils_filename',
            url_property='hostutils_url',
            haltOnFailure=True,
            ignore_certs=self.ignoreCerts,
            name='download_hostutils',
        ))
        self.addStep(UnpackFile(
            filename=WithProperties('../%(hostutils_filename)s'),
            scripts_dir='../tools/buildfarm/utils',
            haltOnFailure=True,
            workdir='build/hostutils',
            name='unpack_hostutils',
        ))
        self.addStep(ShellCommand(
            name='cleanup device',
            workdir='.',
            description="Cleanup Device",
            command=['python', '/builds/sut_tools/cleanup.py',
                     WithProperties("%(sut_ip)s"),
                    ],
            haltOnFailure=True)
        )
        self.addStep(ShellCommand(
            name='install app on device',
            workdir='.',
            description="Install App on Device",
            command=['python', '/builds/sut_tools/installApp.py',
                     WithProperties("%(sut_ip)s"),
                     WithProperties("build/%(build_filename)s"),
                     self.remoteProcessName,
                    ],
            haltOnFailure=True)
        )

    def addPrepareBuildSteps(self):
        def get_build_url(build):
            '''Make sure that there is at least one build in the file list'''
            assert len(build.source.changes[-1].files) > 0, 'Unittest sendchange has no files'
            return parse_sendchange_files(build, exclude_substrs=['.crashreporter-symbols.',
                                                   '.tests.'])
        self.addStep(DownloadFile(
            url_fn=get_build_url,
            filename_property='build_filename',
            url_property='build_url',
            haltOnFailure=True,
            ignore_certs=self.ignoreCerts,
            name='download_build',
        ))
        self.addStep(UnpackFile(
            filename=WithProperties('../%(build_filename)s'),
            scripts_dir='../tools/buildfarm/utils',
            haltOnFailure=True,
            workdir='build/%s' % self.productName,
            name='unpack_build',
        ))
        self.addStep(SetBuildProperty(
         property_name="exedir",
         value=self.productName
        ))

    def addRunTestSteps(self):
        if self.stackwalk_cgi and self.downloadSymbols:
            symbols_path = '%(symbols_url)s'
        else:
            symbols_path = 'symbols'

        for suite in self.suites:
            name = suite['suite']

            self.addStep(ShellCommand(
                name='configure device',
                workdir='.',
                description="Configure Device",
                command=['python', '/builds/sut_tools/config.py',
                         WithProperties("%(sut_ip)s"),
                         name,
                        ],
                haltOnFailure=True)
            )
            if name.startswith('mochitest'):
                self.addStep(UnpackTest(
                 filename=WithProperties('../%(tests_filename)s'),
                 testtype='mochitest',
                 workdir='build/tests',
                 haltOnFailure=True,
                ))
                variant = name.split('-', 1)[1]
                if 'browser-chrome' in name:
                    stepProc = unittest_steps.RemoteMochitestBrowserChromeStep
                else:
                    stepProc = unittest_steps.RemoteMochitestStep
                if suite.get('testPaths', None):
                    for tp in suite.get('testPaths', []):
                        self.addStep(stepProc(
                         variant=variant,
                         symbols_path=symbols_path,
                         testPath=tp,
                         workdir='build/tests',
                         timeout=2400,
                         app=self.remoteProcessName,
                         env=self.env,
                        ))
                else:
                    totalChunks = suite.get('totalChunks', None)
                    thisChunk = suite.get('thisChunk', None)
                    self.addStep(stepProc(
                     variant=variant,
                     symbols_path=symbols_path,
                     testManifest=suite.get('testManifest', None),
                     workdir='build/tests',
                     timeout=2400,
                     app=self.remoteProcessName,
                     env=self.env,
                     totalChunks=totalChunks,
                     thisChunk=thisChunk,
                    ))
            elif name.startswith('reftest') or name == 'crashtest':
                totalChunks = suite.get('totalChunks', None)
                thisChunk = suite.get('thisChunk', None)
                # Unpack the tests
                self.addStep(UnpackTest(
                 filename=WithProperties('../%(tests_filename)s'),
                 testtype='reftest',
                 workdir='build/tests',
                 haltOnFailure=True,
                 ))
                self.addStep(unittest_steps.RemoteReftestStep(
                 suite=name,
                 symbols_path=symbols_path,
                 totalChunks=totalChunks,
                 thisChunk=thisChunk,
                 workdir='build/tests',
                 timeout=2400,
                 app=self.remoteProcessName,
                 env=self.env,
                 cmdOptions=self.remoteExtras.get('cmdOptions'),
                ))
            elif name == 'jsreftest':
                totalChunks = suite.get('totalChunks', None)
                thisChunk = suite.get('thisChunk', None)
                self.addStep(UnpackTest(
                 filename=WithProperties('../%(tests_filename)s'),
                 testtype='jsreftest',
                 workdir='build/tests',
                 haltOnFailure=True,
                 ))
                self.addStep(unittest_steps.RemoteReftestStep(
                 suite=name,
                 symbols_path=symbols_path,
                 totalChunks=totalChunks,
                 thisChunk=thisChunk,
                 workdir='build/tests',
                 timeout=2400,
                 app=self.remoteProcessName,
                 env=self.env,
                 cmdOptions=self.remoteExtras.get('cmdOptions'),
                ))

    def addTearDownSteps(self):
        self.addCleanupSteps()
        self.addStep(ShellCommand(
            name='reboot device',
            workdir='.',
            alwaysRun=True,
            warnOnFailure=False,
            flunkOnFailure=False,
            timeout=60*30,
            description='Reboot Device',
            command=['python', '/builds/sut_tools/reboot.py',
                      WithProperties("%(sut_ip)s"),
                     ],
        ))

class TalosFactory(RequestSortingBuildFactory):
    extName = 'addon.xpi'
    """Create working talos build factory"""
    def __init__(self, OS, supportUrlBase, envName, buildBranch, branchName,
            configOptions, talosCmd, customManifest=None, customTalos=None,
            workdirBase=None, fetchSymbols=False, plugins=None, pagesets=[],
            remoteTests=False, productName="firefox", remoteExtras=None,
            talosAddOns=[], addonTester=False, releaseTester=False,
            talosBranch=None, branch=None, talos_from_source_code=False):

        BuildFactory.__init__(self)

        if workdirBase is None:
            workdirBase = "."

        self.workdirBase = workdirBase
        self.OS = OS
        self.supportUrlBase = supportUrlBase
        self.buildBranch = buildBranch
        self.branchName = branchName
        self.ignoreCerts = False
        if self.branchName.lower().startswith('shadow'):
            self.ignoreCerts = True
        self.remoteTests = remoteTests
        self.configOptions = configOptions['suites'][:]
        self.talosCmd = talosCmd
        self.customManifest = customManifest
        self.customTalos = customTalos
        self.fetchSymbols = fetchSymbols
        self.plugins = plugins
        self.pagesets = pagesets[:]
        self.talosAddOns = talosAddOns[:]
        self.exepath = None
        self.env = MozillaEnvironments[envName]
        self.addonTester = addonTester
        self.releaseTester = releaseTester
        self.productName = productName
        self.remoteExtras = remoteExtras
        self.talos_from_source_code = talos_from_source_code
        if talosBranch is None:
            self.talosBranch = branchName
        else:
            self.talosBranch = talosBranch

        if self.remoteExtras is not None:
            exePaths = self.remoteExtras.get('processName', {})
        else:
            exePaths = {}
        if branch in exePaths:
            self.remoteProcessName = exePaths[branch]
        else:
            if 'default' in exePaths:
                self.remoteProcessName = exePaths['default']
            else:
                self.remoteProcessName = 'org.mozilla.fennec'

        self.addInfoSteps()
        self.addCleanupSteps()
        self.addDmgInstaller()
        self.addDownloadBuildStep()
        self.addUnpackBuildSteps()
        self.addGetBuildInfoStep()
        if fetchSymbols:
            self.addDownloadSymbolsStep()
        if self.addonTester:
            self.addDownloadExtensionStep()
        self.addSetupSteps()
        self.addPluginInstallSteps()
        self.addPagesetInstallSteps()
        self.addAddOnInstallSteps()
        if self.remoteTests:
            self.addPrepareDeviceStep()
        self.addUpdateConfigStep()
        self.addRunTestStep()
        self.addRebootStep()

    def pythonWithSimpleJson(self, platform):
        if (platform in ("fedora", "fedora64", "leopard", "snowleopard", "lion")):
            return "/tools/buildbot/bin/python"
        elif (platform in ('w764', 'win7', 'xp')):
            return "C:\\mozilla-build\\python25\\python.exe"

    def _propertyIsSet(self, step, prop):
        return step.build.getProperties().has_key(prop)

    def addInfoSteps(self):
        self.addStep(OutputStep(
         name='tinderboxprint_slavename',
         data=WithProperties('TinderboxPrint: s: %(slavename)s'),
        ))
        if self.remoteTests:
            self.addStep(SetProperty(
                 command=['bash', '-c', 'echo $SUT_IP'],
                 property='sut_ip'
            ))

    def addCleanupSteps(self):
        if self.OS in ('xp', 'vista', 'win7', 'w764'):
            #required step due to long filename length in tp4
            self.addStep(ShellCommand(
             name='mv tp4',
             workdir=os.path.join(self.workdirBase),
             flunkOnFailure=False,
             warnOnFailure=False,
             description="move tp4 out of talos dir to tp4-%random%",
             command=["if", "exist", "talos\\page_load_test\\tp4", "mv", "talos\\page_load_test\\tp4", "tp4-%random%"],
             env=self.env)
            )
            #required step due to long filename length in tp5
            self.addStep(ShellCommand(
             name='mv tp5',
             workdir=os.path.join(self.workdirBase),
             flunkOnFailure=False,
             warnOnFailure=False,
             description="move tp5 out of talos dir to tp5-%random%",
             command=["if", "exist", "talos\\page_load_test\\tp5", "mv", "talos\\page_load_test\\tp5", "tp5-%random%"],
             env=self.env)
            )
            self.addStep(ShellCommand(
             name='chmod_files',
             workdir=self.workdirBase,
             flunkOnFailure=False,
             warnOnFailure=False,
             description="chmod files (see msys bug)",
             command=["chmod", "-v", "-R", "a+rwx", "."],
             env=self.env)
            )
            #on windows move the whole working dir out of the way, saves us trouble later
            self.addStep(ShellCommand(
             name='move old working dir out of the way',
             workdir=os.path.dirname(self.workdirBase),
             description="move working dir",
             command=["if", "exist", os.path.basename(self.workdirBase), "mv", os.path.basename(self.workdirBase), "t-%random%"],
             env=self.env)
            )
            self.addStep(ShellCommand(
             name='remove any old working dirs',
             workdir=os.path.dirname(self.workdirBase),
             description="remove old working dirs",
             command='if exist t-* nohup rm -vrf t-*',
             env=self.env)
            )
            self.addStep(ShellCommand(
             name='create new working dir',
             workdir=os.path.dirname(self.workdirBase),
             description="create new working dir",
             command='mkdir ' + os.path.basename(self.workdirBase),
             env=self.env)
            )
        else:
            self.addStep(ShellCommand(
             name='cleanup',
             workdir=self.workdirBase,
             description="Cleanup",
             command='nohup rm -vrf *',
             env=self.env)
            )
        if 'fed' in self.OS:
            self.addStep(ShellCommand(
                name='disable_screensaver',
                command=['xset', 's', 'reset']))
        self.addStep(ShellCommand(
         name='create talos dir',
         workdir=self.workdirBase,
         description="talos dir creation",
         command='mkdir talos',
         env=self.env)
        )
        if self.remoteTests:
            self.addStep(ShellCommand(
                name='cleanup device',
                workdir=self.workdirBase,
                description="Cleanup Device",
                command=['python', '/builds/sut_tools/cleanup.py',
                         WithProperties("%(sut_ip)s"),
                        ],
                env=self.env,
                haltOnFailure=True)
            )
        if not self.remoteTests:
            self.addStep(DownloadFile(
             url=WithProperties("%s/tools/buildfarm/maintenance/count_and_reboot.py" % self.supportUrlBase),
             workdir=self.workdirBase,
             haltOnFailure=True,
            ))


    def addDmgInstaller(self):
        if self.OS in ('leopard', 'tiger', 'snowleopard', 'lion'):
            self.addStep(DownloadFile(
             url=WithProperties("%s/tools/buildfarm/utils/installdmg.sh" % self.supportUrlBase),
             workdir=self.workdirBase,
             haltOnFailure=True,
            ))

    def addDownloadBuildStep(self):
        def get_url(build):
            url = build.source.changes[-1].files[0]
            url = urllib.unquote(url)
            return url
        self.addStep(DownloadFile(
         url_fn=get_url,
         url_property="fileURL",
         filename_property="filename",
         workdir=self.workdirBase,
         haltOnFailure=True,
         ignore_certs=self.ignoreCerts,
         name="Download build",
        ))

    def addUnpackBuildSteps(self):
        if (self.releaseTester and (self.OS in ('xp', 'vista', 'win7', 'w764'))): 
            #build is packaged in a windows installer 
            self.addStep(DownloadFile( 
             url=WithProperties("%s/tools/buildfarm/utils/firefoxInstallConfig.ini" % self.supportUrlBase),
             haltOnFailure=True,
             workdir=self.workdirBase,
            ))
            self.addStep(SetProperty(
              name='set workdir path',
              command=['pwd'],
              property='workdir_pwd',
              workdir=self.workdirBase,
            ))
            self.addStep(ShellCommand(
             name='install_release_build',
             workdir=self.workdirBase,
             description="install windows release build",
             command=[WithProperties('%(filename)s'), WithProperties('/INI=%(workdir_pwd)s\\firefoxInstallConfig.ini')],
             env=self.env)
            )
        elif self.OS in ('tegra_android',):
            self.addStep(UnpackFile(
             filename=WithProperties("../%(filename)s"),
             workdir="%s/%s" % (self.workdirBase, self.productName),
             name="Unpack build",
             haltOnFailure=True,
            ))
        else:
            self.addStep(UnpackFile(
             filename=WithProperties("%(filename)s"),
             workdir=self.workdirBase,
             name="Unpack build",
             haltOnFailure=True,
            ))
        if self.OS in ('xp', 'vista', 'win7', 'w764'):
            self.addStep(ShellCommand(
             name='chmod_files',
             workdir=os.path.join(self.workdirBase, "%s/" % self.productName),
             flunkOnFailure=False,
             warnOnFailure=False,
             description="chmod files (see msys bug)",
             command=["chmod", "-v", "-R", "a+x", "."],
             env=self.env)
            )
        if self.OS in ('tiger', 'leopard', 'snowleopard', 'lion'):
            self.addStep(FindFile(
             workdir=os.path.join(self.workdirBase, "talos"),
             filename="%s-bin" % self.productName,
             directory="..",
             max_depth=4,
             property_name="exepath",
             name="Find executable",
            ))
        elif self.OS in ('xp', 'vista', 'win7', 'w764'):
            self.addStep(SetBuildProperty(
             property_name="exepath",
             value="../%s/%s" % (self.productName, self.productName)
            ))
        elif self.OS in ('tegra_android',):
            self.addStep(SetBuildProperty(
             property_name="exepath",
             value="../%s/%s" % (self.productName, self.productName)
            ))
        else:
            if self.productName == 'fennec':
                exeName = self.productName
            else:
                exeName = "%s-bin" % self.productName
            self.addStep(SetBuildProperty(
             property_name="exepath",
             value="../%s/%s" % (self.productName, exeName)
            ))
        self.exepath = WithProperties('%(exepath)s')

    def addGetBuildInfoStep(self):
        def get_exedir(build):
            return os.path.dirname(build.getProperty('exepath'))
        self.addStep(SetBuildProperty(
         property_name="exedir",
         value=get_exedir,
        ))

        # Figure out which revision we're running
        def get_build_info(rc, stdout, stderr):
            retval = {'repo_path': None,
                      'revision': None,
                      'buildid': None,
                     }
            stdout = "\n".join([stdout, stderr])
            m = re.search("^BuildID\s*=\s*(\w+)", stdout, re.M)
            if m:
                retval['buildid'] = m.group(1)
            m = re.search("^SourceStamp\s*=\s*(.*)", stdout, re.M)
            if m:
                retval['revision'] = m.group(1).strip()
            m = re.search("^SourceRepository\s*=\s*(\S+)", stdout, re.M)
            if m:
                retval['repo_path'] = m.group(1)
            return retval

        self.addStep(SetProperty(
         command=['cat', WithProperties('%(exedir)s/application.ini')],
         workdir=os.path.join(self.workdirBase, "talos"),
         extract_fn=get_build_info,
         name='get build info',
        ))

        def check_sdk(cmd, step):
            txt = cmd.logs['stdio'].getText()
            m = re.search("MacOSX10\.5\.sdk", txt, re.M)
            if m :
                step.addCompleteLog('sdk-fail', 'TinderboxPrint: Skipping tests; can\'t run 10.5 based build on 10.4 slave')
                return FAILURE
            return SUCCESS
        if self.OS == "tiger":
            self.addStep(ShellCommand(
                command=['bash', '-c',
                         WithProperties('unzip -c %(exedir)s/chrome/toolkit.jar content/global/buildconfig.html | grep sdk')],
                workdir=os.path.join(self.workdirBase, "talos"),
                log_eval_fn=check_sdk,
                haltOnFailure=True,
                flunkOnFailure=False,
                name='check sdk okay'))

    def addSetupSteps(self):
        if self.customManifest:
            self.addStep(FileDownload(
             mastersrc=self.customManifest,
             slavedest="tp3.manifest",
             workdir=os.path.join(self.workdirBase, "talos/page_load_test"),
             haltOnFailure=True,
             ))

        if self.customTalos is None and not self.remoteTests:
            if self.talos_from_source_code:
                self.addStep(DownloadFile(
                    url=WithProperties("%s/tools/scripts/talos/talos_from_code.py" % \
                                       self.supportUrlBase),
                    workdir=self.workdirBase,
                    haltOnFailure=True,
                ))
                self.addStep(ShellCommand(
                    name='download files specified in talos.json',
                    command=[self.pythonWithSimpleJson(self.OS), 'talos_from_code.py', \
                            '--talos-json-url', \
                            WithProperties('%(repo_path)s/raw-file/%(revision)s/testing/talos/talos.json')],
                    workdir=self.workdirBase,
                    haltOnFailure=True,
                ))
            else:
                self.addStep(DownloadFile(
                  url=WithProperties("%s/zips/talos.zip" % self.supportUrlBase),
                  workdir=self.workdirBase,
                  haltOnFailure=True,
                ))
                self.addStep(DownloadFile(
                 url=WithProperties("%s/xpis/pageloader.xpi" % self.supportUrlBase),
                 workdir=os.path.join(self.workdirBase, "talos/page_load_test"),
                 haltOnFailure=True,
                 ))

            self.addStep(UnpackFile(
             filename='talos.zip',
             workdir=self.workdirBase,
             haltOnFailure=True,
            ))
        elif self.remoteTests:
            self.addStep(ShellCommand(
             name='copy_talos',
             command=["cp", "-rv", "/builds/talos-data/talos", "."],
             workdir=self.workdirBase,
             description="copying talos",
             haltOnFailure=True,
             flunkOnFailure=True,
             env=self.env)
            )
            self.addStep(ShellCommand(
             name='copy_fennecmark',
             command=["cp", "-rv", "/builds/talos-data/bench@taras.glek",
                      "talos/mobile_profile/extensions/"],
             workdir=self.workdirBase,
             description="copying fennecmark",
             haltOnFailure=True,
             flunkOnFailure=True,
             env=self.env)
            )
            self.addStep(ShellCommand(
             name='copy_pageloader',
             command=["cp", "-rv", "/builds/talos-data/pageloader@mozilla.org",
                      "talos/mobile_profile/extensions/"],
             workdir=self.workdirBase,
             description="copying pageloader",
             haltOnFailure=True,
             flunkOnFailure=True,
             env=self.env)
            )
        else:
            self.addStep(FileDownload(
             mastersrc=self.customTalos,
             slavedest=self.customTalos,
             workdir=self.workdirBase,
             blocksize=640*1024,
             haltOnFailure=True,
            ))
            self.addStep(UnpackFile(
             filename=self.customTalos,
             workdir=self.workdirBase,
             haltOnFailure=True,
            ))

    def addPluginInstallSteps(self):
        if self.plugins:
            #32 bit (includes mac browsers)
            if self.OS in ('xp', 'vista', 'win7', 'fedora', 'tegra_android', 'leopard', 'snowleopard', 'leopard-o', 'lion'):
                self.addStep(DownloadFile(
                 url=WithProperties("%s/%s" % (self.supportUrlBase, self.plugins['32'])),
                 workdir=os.path.join(self.workdirBase, "talos/base_profile"),
                 haltOnFailure=True,
                ))
                self.addStep(UnpackFile(
                 filename=os.path.basename(self.plugins['32']),
                 workdir=os.path.join(self.workdirBase, "talos/base_profile"),
                 haltOnFailure=True,
                ))
            #64 bit
            if self.OS in ('w764', 'fedora64'):
                self.addStep(DownloadFile(
                 url=WithProperties("%s/%s" % (self.supportUrlBase, self.plugins['64'])),
                 workdir=os.path.join(self.workdirBase, "talos/base_profile"),
                 haltOnFailure=True,
                ))
                self.addStep(UnpackFile(
                 filename=os.path.basename(self.plugins['64']),
                 workdir=os.path.join(self.workdirBase, "talos/base_profile"),
                 haltOnFailure=True,
                ))

    def addPagesetInstallSteps(self):
        for pageset in self.pagesets:
            self.addStep(DownloadFile(
             url=WithProperties("%s/%s" % (self.supportUrlBase, pageset)),
             workdir=os.path.join(self.workdirBase, "talos/page_load_test"),
             haltOnFailure=True,
            ))
            self.addStep(UnpackFile(
             filename=os.path.basename(pageset),
             workdir=os.path.join(self.workdirBase, "talos/page_load_test"),
             haltOnFailure=True,
            ))

    def addAddOnInstallSteps(self):
        for addOn in self.talosAddOns:
            self.addStep(DownloadFile(
             url=WithProperties("%s/%s" % (self.supportUrlBase, addOn)),
             workdir=os.path.join(self.workdirBase, "talos"),
             haltOnFailure=True,
            ))
            self.addStep(UnpackFile(
             filename=os.path.basename(addOn),
             workdir=os.path.join(self.workdirBase, "talos"),
             haltOnFailure=True,
            ))

    def addDownloadSymbolsStep(self):
        def get_symbols_url(build):
            suffixes = ('.tar.bz2', '.dmg', '.zip', '.apk')
            buildURL = build.getProperty('fileURL')

            for suffix in suffixes:
                if buildURL.endswith(suffix):
                    return buildURL[:-len(suffix)] + '.crashreporter-symbols.zip'

        self.addStep(DownloadFile(
         url_fn=get_symbols_url,
         filename_property="symbolsFile",
         workdir=self.workdirBase,
         ignore_certs=self.ignoreCerts,
         name="Download symbols",
        ))
        self.addStep(ShellCommand(
         name="mkdir_symbols",
         command=['mkdir', 'symbols'],
         workdir=self.workdirBase,
        ))
        self.addStep(UnpackFile(
         filename=WithProperties("../%(symbolsFile)s"),
         workdir="%s/symbols" % self.workdirBase,
         name="Unpack symbols",
        ))

    def addDownloadExtensionStep(self):
        def get_addon_url(build):
            import urlparse
            import urllib
            base_url = 'https://addons.mozilla.org/'
            addon_url = build.getProperty('addonUrl')
            #addon_url may be encoded, also we want to ensure that http%3A// becomes http://
            parsed_url = urlparse.urlsplit(urllib.unquote_plus(addon_url).replace('%3A', ':'))
            if parsed_url[1]: #there is a netloc, this is already a full path to a given addon
              return urlparse.urlunsplit(parsed_url)
            else:
              return urlparse.urljoin(base_url, addon_url)

        self.addStep(DownloadFile(
         url_fn=get_addon_url,
         workdir=os.path.join(self.workdirBase, "talos"),
         name="Download extension",
         ignore_certs=True,
         wget_args=['-O', TalosFactory.extName],
         doStepIf=lambda step: self._propertyIsSet(step, 'addonUrl'),
         haltOnFailure=True,
        ))

    def addPrepareDeviceStep(self):
        self.addStep(ShellCommand(
            name='install app on device',
            workdir=self.workdirBase,
            description="Install App on Device",
            command=['python', '/builds/sut_tools/installApp.py',
                     WithProperties("%(sut_ip)s"),
                     WithProperties(self.workdirBase + "/%(filename)s"),
                     self.remoteProcessName,
                    ],
            env=self.env,
            haltOnFailure=True)
        )

    def addUpdateConfigStep(self):
        self.addStep(talos_steps.MozillaUpdateConfig(
         workdir=os.path.join(self.workdirBase, "talos/"),
         branch=self.buildBranch,
         branchName=self.talosBranch,
         remoteTests=self.remoteTests,
         haltOnFailure=True,
         executablePath=self.exepath,
         addOptions=self.configOptions,
         env=self.env,
         extName=TalosFactory.extName,
         addonTester=self.addonTester,
         useSymbols=self.fetchSymbols,
         remoteExtras=self.remoteExtras,
         remoteProcessName=self.remoteProcessName)
        )

    def addRunTestStep(self):
        if self.OS in ('lion', 'snowleopard'):
            self.addStep(resolution_step())
        self.addStep(talos_steps.MozillaRunPerfTests(
         warnOnWarnings=True,
         workdir=os.path.join(self.workdirBase, "talos/"),
         timeout=21600,
         haltOnFailure=False,
         command=self.talosCmd,
         env=self.env)
        )
        if self.OS in ('lion', 'snowleopard'):
            self.addStep(resolution_step())

    def addRebootStep(self):
        if self.OS in ('lion',):
            self.addStep(ShellCommand(
                name="clear_saved_state",
                flunkOnFailure=False,
                warnOnFailure=False,
                haltOnFailure=False,
                workdir='/Users/cltbld',
                command=['bash', '-c', 'rm -rf Library/Saved\ Application\ State/*.savedState']
            ))
        def do_disconnect(cmd):
            try:
                if 'SCHEDULED REBOOT' in cmd.logs['stdio'].getText():
                    return True
            except:
                pass
            return False
        if self.remoteTests:
            self.addStep(ShellCommand(
                         name='reboot device',
                         flunkOnFailure=False,
                         warnOnFailure=False,
                         alwaysRun=True,
                         workdir=self.workdirBase,
                         description="Reboot Device",
                         timeout=60*30,
                         command=['python', '/builds/sut_tools/reboot.py',
                                  WithProperties("%(sut_ip)s"),
                                 ],
                         env=self.env)
            )
        else:
            #the following step is to help the linux running on mac minis reboot cleanly
            #see bug561442
            if 'fedora' in self.OS:
                self.addStep(ShellCommand(
                    name='set_time',
                    description=['set', 'time'],
                    alwaysRun=True,
                    command=['bash', '-c',
                             'sudo hwclock --set --date="$(date +%m/%d/%y\ %H:%M:%S)"'],
                ))

            self.addStep(DisconnectStep(
             name='reboot',
             flunkOnFailure=False,
             warnOnFailure=False,
             alwaysRun=True,
             workdir=self.workdirBase,
             description="reboot after 1 test run",
             command=["python", "count_and_reboot.py", "-f", "../talos_count.txt", "-n", "1", "-z"],
             force_disconnect=do_disconnect,
             env=self.env,
            ))

class RuntimeTalosFactory(TalosFactory):
    def __init__(self, configOptions=None, plugins=None, pagesets=None,
                 supportUrlBase=None, talosAddOns=None, addonTester=True,
                 *args, **kwargs):
        if not configOptions:
            # TalosFactory/MozillaUpdateConfig require this format for this variable
            # MozillaUpdateConfig allows for adding additional options at runtime,
            # which is how this factory is intended to be used.
            configOptions = {'suites': []}
        # For the rest of these, make them overridable with WithProperties
        if not plugins:
            plugins = {'32': '%(plugin)s', '64': '%(plugin)s'}
        if not pagesets:
            pagesets = ['%(pageset1)s', '%(pageset2)s']
        if not supportUrlBase:
            supportUrlBase = '%(supportUrlBase)s'
        if not talosAddOns:
            talosAddOns = ['%(talosAddon1)s', '%(talosAddon2)s']
        TalosFactory.__init__(self, *args, configOptions=configOptions,
                              plugins=plugins, pagesets=pagesets,
                              supportUrlBase=supportUrlBase,
                              talosAddOns=talosAddOns, addonTester=addonTester,
                              **kwargs)

    def addInfoSteps(self):
        pass

    def addPluginInstallSteps(self):
        if self.plugins:
            self.addStep(DownloadFile(
                url=WithProperties("%s/%s" % (self.supportUrlBase, '%(plugin)s')),
                workdir=os.path.join(self.workdirBase, "talos/base_profile"),
                doStepIf=lambda step: self._propertyIsSet(step, 'plugin'),
                filename_property='plugin_base'
            ))
            self.addStep(UnpackFile(
                filename=WithProperties('%(plugin_base)s'),
                workdir=os.path.join(self.workdirBase, "talos/base_profile"),
                doStepIf=lambda step: self._propertyIsSet(step, 'plugin')
            ))

    def addPagesetInstallSteps(self):
        # XXX: This is really hacky, it would be better to extract the property
        # name from the format string.
        n = 1
        for pageset in self.pagesets:
            self.addStep(DownloadFile(
             url=WithProperties("%s/%s" % (self.supportUrlBase, pageset)),
             workdir=os.path.join(self.workdirBase, "talos/page_load_test"),
             doStepIf=lambda step, n=n: self._propertyIsSet(step, 'pageset%d' % n),
             filename_property='pageset%d_base' % n
            ))
            self.addStep(UnpackFile(
             filename=WithProperties('%(pageset' + str(n) + '_base)s'),
             workdir=os.path.join(self.workdirBase, "talos/page_load_test"),
             doStepIf=lambda step, n=n: self._propertyIsSet(step, 'pageset%d' % n)
            ))
            n += 1

    def addAddOnInstallSteps(self):
        n = 1
        for addOn in self.talosAddOns:
            self.addStep(DownloadFile(
             url=WithProperties("%s/%s" % (self.supportUrlBase, addOn)),
             workdir=os.path.join(self.workdirBase, "talos"),
             doStepIf=lambda step, n=n: self._propertyIsSet(step, 'talosAddon%d' % n),
             filename_property='talosAddon%d_base' % n
            ))
            self.addStep(UnpackFile(
             filename=WithProperties('%(talosAddon' + str(n) + '_base)s'),
             workdir=os.path.join(self.workdirBase, "talos"),
             doStepIf=lambda step, n=n: self._propertyIsSet(step, 'talosAddon%d' % n)
            ))
            n += 1

class TryTalosFactory(TalosFactory):
    def addDownloadBuildStep(self):
        def get_url(build):
            url = build.source.changes[-1].files[0]
            return url
        self.addStep(DownloadFile(
         url_fn=get_url,
         url_property="fileURL",
         filename_property="filename",
         workdir=self.workdirBase,
         name="Download build",
         ignore_certs=True,
        ))

        def make_tinderbox_header(build):
            identifier = build.getProperty("filename").rsplit('-', 1)[0]
            # Grab the submitter out of the dir name. CVS and Mercurial builds
            # are a little different, so we need to try fairly hard to find
            # the e-mail address.
            dir = os.path.basename(os.path.dirname(build.getProperty("fileURL")))
            who = ''
            for section in dir.split('-'):
                if '@' in section:
                    who = section
                    break
            msg =  'TinderboxPrint: %s\n' % who
            msg += 'TinderboxPrint: %s\n' % identifier
            return msg
        self.addStep(OutputStep(data=make_tinderbox_header, log='header', name='echo_id'))

    def addDownloadSymbolsStep(self):
        def get_symbols_url(build):
            suffixes = ('.tar.bz2', '.dmg', '.zip')
            buildURL = build.getProperty('fileURL')

            for suffix in suffixes:
                if buildURL.endswith(suffix):
                    return buildURL[:-len(suffix)] + '.crashreporter-symbols.zip'

        self.addStep(DownloadFile(
         url_fn=get_symbols_url,
         filename_property="symbolsFile",
         workdir=self.workdirBase,
         name="Download symbols",
         ignore_certs=True,
         haltOnFailure=False,
         flunkOnFailure=False,
        ))
        self.addStep(ShellCommand(
         command=['mkdir', 'symbols'],
         workdir=self.workdirBase,
        ))
        self.addStep(UnpackFile(
         filename=WithProperties("../%(symbolsFile)s"),
         workdir="%s/symbols" % self.workdirBase,
         name="Unpack symbols",
         haltOnFailure=False,
         flunkOnFailure=False,
        ))


class PartnerRepackFactory(ReleaseFactory):
    def getReleaseTag(self, product, version):
        return product.upper() + '_' + \
               str(version).replace('.','_') + '_' + \
               'RELEASE'

    def __init__(self, productName, version, partnersRepoPath,
                 stagingServer, stageUsername, stageSshKey,
                 buildNumber=1, partnersRepoRevision='default',
                 nightlyDir="nightly", platformList=None, packageDmg=True,
                 partnerUploadDir='unsigned/partner-repacks',
                 baseWorkDir='.', python='python', **kwargs):
        ReleaseFactory.__init__(self, baseWorkDir=baseWorkDir, **kwargs)
        self.productName = productName
        self.version = version
        self.buildNumber = buildNumber
        self.partnersRepoPath = partnersRepoPath
        self.partnersRepoRevision = partnersRepoRevision
        self.stagingServer = stagingServer
        self.stageUsername = stageUsername
        self.stageSshKey = stageSshKey
        self.partnersRepackDir = '%s/partner-repacks' % self.baseWorkDir
        self.partnerUploadDir = partnerUploadDir
        self.packageDmg = packageDmg
        self.python = python
        self.platformList = platformList
        self.candidatesDir = self.getCandidatesDir(productName,
                                                   version,
                                                   buildNumber,
                                                   nightlyDir=nightlyDir)
        self.releaseTag = self.getReleaseTag(productName, version)
        self.extraRepackArgs = []
        if nightlyDir:
            self.extraRepackArgs.extend(['--nightly-dir', '%s/%s' % \
                                        (productName, nightlyDir)])
        if self.packageDmg:
            self.extraRepackArgs.extend(['--pkg-dmg',
                                        WithProperties('%(scriptsdir)s/pkg-dmg')])
        if platformList:
            for platform in platformList:
                self.extraRepackArgs.extend(['--platform', platform])

        self.getPartnerRepackData()
        self.doPartnerRepacks()
        self.uploadPartnerRepacks()

    def getPartnerRepackData(self):
        # We start fresh every time.
        self.addStep(ShellCommand(
            name='rm_partners_repo',
            command=['rm', '-rf', self.partnersRepackDir],
            description=['remove', 'partners', 'repo'],
            workdir=self.baseWorkDir,
        ))
        self.addStep(MercurialCloneCommand(
            name='clone_partners_repo',
            command=['hg', 'clone',
                     'http://%s/%s' % (self.hgHost,
                                          self.partnersRepoPath),
                     self.partnersRepackDir
                    ],
            description=['clone', 'partners', 'repo'],
            workdir=self.baseWorkDir,
            haltOnFailure=True
        ))
        self.addStep(ShellCommand(
            name='update_partners_repo',
            command=['hg', 'update', '-C', '-r', self.partnersRepoRevision],
            description=['update', 'partners', 'repo'],
            workdir=self.partnersRepackDir,
            haltOnFailure=True            
        ))
        if self.packageDmg:
            self.addStep(ShellCommand(
                name='download_pkg-dmg',
                command=['bash', '-c',
                         'wget http://hg.mozilla.org/%s/raw-file/%s/build/package/mac_osx/pkg-dmg' % (self.repoPath, self.releaseTag)],
                description=['download', 'pkg-dmg'],
                workdir='%s/scripts' % self.partnersRepackDir,
                haltOnFailure=True            
            ))
            self.addStep(ShellCommand(
                name='chmod_pkg-dmg',
                command=['chmod', '755', 'pkg-dmg'],
                description=['chmod', 'pkg-dmg'],
                workdir='%s/scripts' % self.partnersRepackDir,
                haltOnFailure=True            
            ))
            self.addStep(SetProperty(
                name='set_scriptsdir',
                command=['bash', '-c', 'pwd'],
                property='scriptsdir',
                workdir='%s/scripts' % self.partnersRepackDir,
            ))

    def doPartnerRepacks(self):
        if self.enableSigning and self.signingServers:
            self.extraRepackArgs.append('--signed')
            self.addGetTokenSteps()
        self.addStep(RepackPartners(
            name='repack_partner_builds',
            command=[self.python, './partner-repacks.py',
                     '--version', str(self.version),
                     '--build-number', str(self.buildNumber),
                     '--staging-server', self.stagingServer,
                     '--dmg-extract-script',
                     WithProperties('%(toolsdir)s/release/common/unpack-diskimage.sh'),
                    ] + self.extraRepackArgs,
            env=self.env,
            description=['repacking', 'partner', 'builds'],
            descriptionDone=['repacked', 'partner', 'builds'],
            workdir='%s/scripts' % self.partnersRepackDir,
            haltOnFailure=True
        ))

    def uploadPartnerRepacks(self):
        self.addStep(ShellCommand(
         name='upload_partner_builds',
         command=['rsync', '-av',
                  '-e', 'ssh -oIdentityFile=~/.ssh/%s' % self.stageSshKey,
                  'build%s/' % str(self.buildNumber),
                  '%s@%s:%s/' % (self.stageUsername,
                                self.stagingServer,
                                self.candidatesDir)
                  ],
         workdir='%s/scripts/repacked_builds/%s' % (self.partnersRepackDir,
                                                    self.version),
         description=['upload', 'partner', 'builds'],
         haltOnFailure=True
        ))

        if not self.enableSigning or not self.signingServers:
            for platform in self.platformList:
                self.addStep(ShellCommand(
                 name='create_partner_build_directory',
                 description=['create', 'partner', 'directory'],
                 command=['bash', '-c',
                    'ssh -oIdentityFile=~/.ssh/%s %s@%s mkdir -p %s/%s/'
                        % (self.stageSshKey, self.stageUsername,
                           self.stagingServer, self.candidatesDir,
                           self.partnerUploadDir),
                     ],
                 workdir='.',
                ))
                self.addStep(ShellCommand(
                 name='upload_partner_build_status',
                 command=['bash', '-c',
                    'ssh -oIdentityFile=~/.ssh/%s %s@%s touch %s/%s/%s'
                        % (self.stageSshKey, self.stageUsername,
                           self.stagingServer, self.candidatesDir,
                           self.partnerUploadDir, 'partner_build_%s' % platform),
                     ],
                 workdir='%s/scripts/repacked_builds/%s/build%s' % (self.partnersRepackDir,
                                                                    self.version,
                                                                    str(self.buildNumber)),
                 description=['upload', 'partner', 'status'],
                 haltOnFailure=True
                ))
def rc_eval_func(exit_statuses):
    def eval_func(cmd, step):
        rc = cmd.rc
        # Temporarily set the rc to 0 so that regex_log_evaluator won't say a
        # command has failed because of non-zero exit code.  We're handing exit
        # codes here.
        try:
            cmd.rc = 0
            regex_status = regex_log_evaluator(cmd, step, global_errors)
        finally:
            cmd.rc = rc

        if cmd.rc in exit_statuses:
            rc_status = exit_statuses[cmd.rc]
        # Use None to specify a default value if you don't want the
        # normal 0 -> SUCCESS, != 0 -> FAILURE
        elif None in exit_statuses:
            rc_status = exit_statuses[None]
        elif cmd.rc == 0:
            rc_status = SUCCESS
        else:
            rc_status = FAILURE

        return worst_status(regex_status, rc_status)
    return eval_func

def extractProperties(rv, stdout, stderr):
    props = {}
    stdout = stdout.strip()
    for l in filter(None, stdout.split('\n')):
        e = filter(None, l.split(':'))
        props[e[0]] = e[1].strip()
    return props

class ScriptFactory(BuildFactory):
    def __init__(self, scriptRepo, scriptName, cwd=None, interpreter=None,
            extra_data=None, extra_args=None,
            script_timeout=1200, script_maxtime=None, log_eval_func=None,
            hg_bin='hg'):
        BuildFactory.__init__(self)
        self.script_timeout = script_timeout
        self.log_eval_func = log_eval_func
        self.script_maxtime = script_maxtime
        if scriptName[0] == '/':
            script_path = scriptName
        else:
            script_path = 'scripts/%s' % scriptName
        if interpreter:
            if isinstance(interpreter, (tuple, list)):
                self.cmd = list(interpreter) + [script_path]
            else:
                self.cmd = [interpreter, script_path]
        else:
            self.cmd = [script_path]

        if extra_args:
            self.cmd.extend(extra_args)

        self.addStep(SetBuildProperty(
            property_name='master',
            value=lambda b: b.builder.botmaster.parent.buildbotURL
        ))
        self.env = {'PROPERTIES_FILE': 'buildprops.json'}
        self.addStep(JSONPropertiesDownload(
            name="download_props",
            slavedest="buildprops.json",
            workdir="."
        ))
        if extra_data:
            self.addStep(JSONStringDownload(
                extra_data,
                name="download_extra",
                slavedest="data.json",
                workdir="."
            ))
            self.env['EXTRA_DATA'] = 'data.json'
        self.addStep(ShellCommand(
            name="clobber_properties",
            command=['rm', '-rf', 'properties'],
            workdir=".",
        ))
        self.addStep(ShellCommand(
            name="clobber_scripts",
            command=['rm', '-rf', 'scripts'],
            workdir=".",
        ))
        self.addStep(ShellCommand(
            name="clone_scripts",
            command=[hg_bin, 'clone', scriptRepo, 'scripts'],
            workdir=".",
            haltOnFailure=True))
        self.addStep(ShellCommand(
            name="update_scripts",
            command=[hg_bin, 'update', '-C', '-r',
                     WithProperties('%(script_repo_revision:-default)s')],
            haltOnFailure=True,
            workdir='scripts'
        ))
        self.runScript()

    def runScript(self):
        self.addStep(ShellCommand(
            name="run_script",
            command=self.cmd,
            env=self.env,
            timeout=self.script_timeout,
            maxTime=self.script_maxtime,
            log_eval_func=self.log_eval_func,
            workdir=".",
            haltOnFailure=True,
            warnOnWarnings=True))

        self.addStep(SetProperty(
            name='set_script_properties',
            command=['bash', '-c', 'for file in `ls -1`; do cat $file; done'],
            workdir='properties',
            extract_fn=extractProperties,
            alwaysRun=True,
            warnOnFailure=False,
            flunkOnFailure=False,
        ))

class SigningScriptFactory(ScriptFactory):

    def __init__(self, signingServers, env, enableSigning=True,
                 **kwargs):
        self.signingServers = signingServers
        self.enableSigning = enableSigning
        self.platform_env = env
        ScriptFactory.__init__(self, **kwargs)

    def runScript(self):

        if self.enableSigning:
            token = "build/token"
            nonce = "build/nonce"
            self.addStep(ShellCommand(
                command=['rm', '-f', nonce],
                workdir='.',
                name='rm_nonce',
                description=['remove', 'old', 'nonce'],
            ))
            self.addStep(SigningServerAuthenication(
                servers=self.signingServers,
                server_cert=SIGNING_SERVER_CERT,
                slavedest=token,
                workdir='.',
                name='download_token',
            ))
            # toolsdir, basedir
            self.addStep(SetProperty(
                name='set_toolsdir',
                command=['bash', '-c', 'pwd'],
                property='toolsdir',
                workdir='scripts',
            ))
            self.addStep(SetProperty(
                name='set_basedir',
                command=['bash', '-c', 'pwd'],
                property='basedir',
                workdir='.',
            ))
            self.env['MOZ_SIGN_CMD'] = WithProperties(get_signing_cmd(
                self.signingServers, self.platform_env.get('PYTHON26')))

        ScriptFactory.runScript(self)
