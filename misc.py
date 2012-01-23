from urlparse import urljoin
try:
    import json
    assert json # pyflakes
except:
    import simplejson as json
import collections
import random
import re
import sys, os, time

from copy import deepcopy

from twisted.python import log

from buildbot.scheduler import Nightly, Scheduler, Triggerable
from buildbot.status.tinderbox import TinderboxMailNotifier
from buildbot.steps.shell import WithProperties
from buildbot.status.builder import WARNINGS, FAILURE, EXCEPTION, RETRY
from buildbot.process.buildstep import regex_log_evaluator

import buildbotcustom.common
import buildbotcustom.changes.hgpoller
import buildbotcustom.process.factory
import buildbotcustom.log
import buildbotcustom.l10n
import buildbotcustom.scheduler
import buildbotcustom.status.mail
import buildbotcustom.status.generators
import buildbotcustom.status.queued_command
import buildbotcustom.status.log_handlers
import buildbotcustom.misc_scheduler
import build.paths
import mozilla_buildtools.queuedir
reload(buildbotcustom.changes.hgpoller)
reload(buildbotcustom.process.factory)
reload(buildbotcustom.log)
reload(buildbotcustom.l10n)
reload(buildbotcustom.scheduler)
reload(buildbotcustom.status.mail)
reload(buildbotcustom.status.generators)
reload(buildbotcustom.status.queued_command)
reload(buildbotcustom.status.log_handlers)
reload(buildbotcustom.misc_scheduler)
reload(build.paths)
reload(mozilla_buildtools.queuedir)

from buildbotcustom.common import reallyShort
from buildbotcustom.changes.hgpoller import HgPoller, HgAllLocalesPoller
from buildbotcustom.process.factory import NightlyBuildFactory, \
  NightlyRepackFactory, UnittestBuildFactory, CodeCoverageFactory, \
  UnittestPackagedBuildFactory, TalosFactory, CCNightlyBuildFactory, \
  CCNightlyRepackFactory, CCUnittestBuildFactory, TryBuildFactory, \
  TryUnittestBuildFactory, ScriptFactory, rc_eval_func
from buildbotcustom.process.factory import RemoteUnittestFactory
from buildbotcustom.scheduler import MultiScheduler, BuilderChooserScheduler, \
    PersistentScheduler, makePropertiesScheduler, SpecificNightly
from buildbotcustom.l10n import TriggerableL10n
from buildbotcustom.status.mail import MercurialEmailLookup, ChangeNotifier
from buildbotcustom.status.generators import buildTryChangeMessage
from buildbotcustom.env import MozillaEnvironments
from buildbotcustom.misc_scheduler import tryChooser, buildIDSchedFunc, \
    buildUIDSchedFunc, lastGoodFunc
from buildbotcustom.status.queued_command import QueuedCommandHandler
from buildbotcustom.status.log_handlers import SubprocessLogHandler
from build.paths import getRealpath
from mozilla_buildtools.queuedir import QueueDir

# This file contains misc. helper function that don't make sense to put in
# other files. For example, functions that are called in a master.cfg

def get_l10n_repositories(file, l10nRepoPath, relbranch):
    """Reads in a list of locale names and revisions for their associated
       repository from 'file'.
    """
    if not l10nRepoPath.endswith('/'):
        l10nRepoPath = l10nRepoPath + '/'
    repositories = {}
    for localeLine in open(file).readlines():
        locale, revision = localeLine.rstrip().split()
        if revision == 'FIXME':
            raise Exception('Found FIXME in %s for locale "%s"' % \
                           (file, locale))
        locale = urljoin(l10nRepoPath, locale)
        repositories[locale] = {
            'revision': revision,
            'relbranchOverride': relbranch,
            'bumpFiles': []
        }

    return repositories

def get_locales_from_json(jsonFile, l10nRepoPath, relbranch):
    if not l10nRepoPath.endswith('/'):
        l10nRepoPath = l10nRepoPath + '/'

    l10nRepositories = {}
    platformLocales = collections.defaultdict(dict)

    file = open(jsonFile)
    localesJson = json.load(file)
    for locale in localesJson.keys():
        revision = localesJson[locale]['revision']
        if revision == 'FIXME':
            raise Exception('Found FIXME in %s for locale "%s"' % \
                           (jsonFile, locale))
        localeUrl = urljoin(l10nRepoPath, locale)
        l10nRepositories[localeUrl] = {
            'revision': revision,
            'relbranchOverride': relbranch,
            'bumpFiles': []
        }
        for platform in localesJson[locale]['platforms']:
            platformLocales[platform][locale] = localesJson[locale]['platforms']

    return (l10nRepositories, platformLocales)

# This function is used as fileIsImportant parameter for Buildbots that do both
# dep/nightlies and release builds. Because they build the same "branch" this
# allows us to have the release builder ignore HgPoller triggered changse
# and the dep builders only obey HgPoller/Force Build triggered ones.

def isHgPollerTriggered(change, hgUrl):
    if (change.revlink and hgUrl in change.revlink) or \
       change.comments.find(hgUrl) > -1:
        return True

def shouldBuild(change):
    """check for commit message disabling build for this change"""
    return "DONTBUILD" not in change.comments

def isImportantL10nFile(change, l10nModules):
    for f in change.files:
        for basepath in l10nModules:
            if f.startswith(basepath):
                return True
    return False

def changeContainsProduct(change, productName):
    products = change.properties.getProperty("products")
    if isinstance(products, basestring) and \
        productName in products.split(','):
            return True
    return False

def changeContainsProperties(change, props={}):
    for prop, value in props.iteritems():
        if change.properties.getProperty(prop) != value:
            return False
    return True

def generateTestBuilderNames(name_prefix, suites_name, suites):
    test_builders = []
    if isinstance(suites, dict) and "totalChunks" in suites:
        totalChunks = suites['totalChunks']
        for i in range(totalChunks):
            test_builders.append('%s %s-%i/%i' % \
                    (name_prefix, suites_name, i+1, totalChunks))
    else:
        test_builders.append('%s %s' % (name_prefix, suites_name))

    return test_builders

fastRegexes = []
nReservedFastSlaves = 0
nReservedSlowSlaves = 0

def _partitionSlaves(slaves):
    """Partitions the list of slaves into 'fast' and 'slow' slaves, according
    to fastRegexes.
    Returns two lists, 'fast' and 'slow'."""
    fast = []
    slow = []
    for s in slaves:
        name = s.slave.slavename
        for e in fastRegexes:
            if re.search(e, name):
                fast.append(s)
                break
        else:
            slow.append(s)
    return fast, slow

def _partitionUnreservedSlaves(slaves):
    fast, slow = _partitionSlaves(slaves)
    return fast[nReservedFastSlaves:], slow[nReservedSlowSlaves:]

def _readReservedFile(filename, fast=True):
    if not filename or not os.path.exists(filename):
        n = 0
    else:
        try:
            data = open(filename).read().strip()
            if data == '':
                n = 0
            else:
                n = int(data)
        except IOError:
            log.msg("Unable to open '%s' for reading" % filename)
            log.err()
            return
        except ValueError:
            log.msg("Unable to read '%s' as an integer" % filename)
            log.err()
            return

    global nReservedSlowSlaves, nReservedFastSlaves
    if fast:
        if n != nReservedFastSlaves:
            log.msg("Setting nReservedFastSlaves to %i (was %i)" % (n, nReservedFastSlaves))
            nReservedFastSlaves = n
    else:
        if n != nReservedSlowSlaves:
            log.msg("Setting nReservedSlowSlaves to %i (was %i)" % (n, nReservedSlowSlaves))
            nReservedSlowSlaves = n

def _getLastTimeOnBuilder(builder, slavename):
    # New builds are at the end of the buildCache, so
    # examine it backwards
    buildNumbers = reversed(sorted(builder.builder_status.buildCache.keys()))
    for buildNumber in buildNumbers:
        try:
            build = builder.builder_status.buildCache[buildNumber]
            if build.slavename == slavename:
                return build.finished
        except KeyError:
            continue
    return None

def _recentSort(builder):
    def sortfunc(s1, s2):
        t1 = _getLastTimeOnBuilder(builder, s1.slave.slavename)
        t2 = _getLastTimeOnBuilder(builder, s2.slave.slavename)
        return cmp(t1, t2)
    return sortfunc

def _nextSlowSlave(builder, available_slaves):
    try:
        fast, slow = _partitionUnreservedSlaves(available_slaves)
        # Choose the slow slave that was most recently on this builder
        # If there aren't any slow slaves, choose the slow slave that was most
        # recently on this builder
        if slow:
            return sorted(slow, _recentSort(builder))[-1]
        elif fast:
            return sorted(fast, _recentSort(builder))[-1]
        else:
            return None
    except:
        log.msg("Error choosing next slow slave for builder '%s', choosing randomly instead" % builder.name)
        log.err()
        return random.choice(available_slaves)

def _nextFastSlave(builder, available_slaves, only_fast=False, reserved=False):
    # Check if our reserved slaves count needs updating
    global _checkedReservedSlaveFile, _reservedFileName
    if int(time.time() - _checkedReservedSlaveFile) > 60:
        _readReservedFile(_reservedFileName)
        _checkedReservedSlaveFile = int(time.time())

    try:
        if only_fast:
            # Check that the builder has some fast slaves configured.  We do
            # this because some machines classes don't have a fast/slow
            # distinction, and so they default to 'slow'
            # We should look at the full set of slaves here regardless of if
            # we're only supposed to be returning unreserved slaves so we get
            # the full set of slaves on the builder.
            fast, slow = _partitionSlaves(builder.slaves)
            if not fast:
                log.msg("Builder '%s' has no fast slaves configured, but only_fast is enabled; disabling only_fast" % builder.name)
                only_fast = False

        if reserved:
            # We have access to the full set of slaves!
            fast, slow = _partitionSlaves(available_slaves)
        else:
            # We only have access to unreserved slaves
            fast, slow = _partitionUnreservedSlaves(available_slaves)

        # Choose the fast slave that was most recently on this builder
        # If there aren't any fast slaves, choose the slow slave that was most
        # recently on this builder if only_fast is False
        if not fast and only_fast:
            return None
        elif fast:
            return sorted(fast, _recentSort(builder))[-1]
        elif slow and not only_fast:
            return sorted(slow, _recentSort(builder))[-1]
        else:
            return None
    except:
        log.msg("Error choosing next fast slave for builder '%s', choosing randomly instead" % builder.name)
        log.err()
        return random.choice(available_slaves)

_checkedReservedSlaveFile = 0
_reservedFileName = None
def setReservedFileName(filename):
    global _reservedFileName
    _reservedFileName = filename

def _nextFastReservedSlave(builder, available_slaves, only_fast=True):
    return _nextFastSlave(builder, available_slaves, only_fast, reserved=True)

def _nextL10nSlave(n=8):
    """Return a nextSlave function that restricts itself to choosing amongst
    the first n connnected slaves.  If there aren't enough slow slaves,
    fallback to using fast slaves."""
    def _nextslave(builder, available_slaves):
        try:
            # Determine our list of the first n connected slaves, preferring to use slow slaves
            # if available.
            connected_slaves = [s for s in builder.slaves if s.slave.slave_status.isConnected()]
            # Sort the list so we're stable across reconfigs
            connected_slaves.sort(key=lambda s: s.slave.slavename)
            fast, slow = _partitionUnreservedSlaves(connected_slaves)
            slow = slow[:n]
            # Choose enough fast slaves so that we're considering a total of n slaves
            fast = fast[:n-(len(slow))]

            # Now keep only those that are in available_slaves
            slow = [s for s in slow if s in available_slaves]
            fast = [s for s in fast if s in available_slaves]

            # Now prefer slaves that most recently did this repack
            if slow:
                return sorted(slow, _recentSort(builder))[-1]
            elif fast:
                return sorted(fast, _recentSort(builder))[-1]
            else:
                # That's ok!
                return None
        except:
            log.msg("Error choosing l10n slave for builder '%s', choosing randomly instead" % builder.name)
            log.err()
            return random.choice(available_slaves)
    return _nextslave

def _nextSlowIdleSlave(nReserved):
    """Return a nextSlave function that will only return a slave to run a build
    if there are at least nReserved slaves available."""
    def _nextslave(builder, available_slaves):
        fast, slow = _partitionUnreservedSlaves(available_slaves)
        if len(slow) <= nReserved:
            return None
        return sorted(slow, _recentSort(builder))[-1]
    return _nextslave

nomergeBuilders = []
def mergeRequests(builder, req1, req2):
    if builder.name in nomergeBuilders:
        return False
    return req1.canBeMergedWith(req2)

def mergeBuildObjects(d1, d2):
    retval = d1.copy()
    keys = ['builders', 'status', 'schedulers', 'change_source']

    for key in keys:
        retval.setdefault(key, []).extend(d2.get(key, []))

    return retval

def generateTestBuilder(config, branch_name, platform, name_prefix,
                        build_dir_prefix, suites_name, suites,
                        mochitestLeakThreshold, crashtestLeakThreshold,
                        slaves=None, resetHwClock=False, category=None,
                        stagePlatform=None, stageProduct=None,
                        mozharness=False, mozharness_python=None):
    builders = []
    pf = config['platforms'].get(platform, {})
    if slaves == None:
        slavenames = config['platforms'][platform]['slaves']
    else:
        slavenames = slaves
    if not category:
        category = branch_name
    productName = pf['product_name']
    branchProperty = branch_name
    posixBinarySuffix = '' if 'mobile' in name_prefix else '-bin'
    properties = {'branch': branchProperty, 'platform': platform,
                  'slavebuilddir': 'test', 'stage_platform': stagePlatform,
                  'product': stageProduct}
    if pf.get('is_remote', False):
        hostUtils = pf['host_utils_url']
        factory = RemoteUnittestFactory(
            platform=platform,
            productName=productName,
            hostUtils=hostUtils,
            suites=suites,
            hgHost=config['hghost'],
            repoPath=config['repo_path'],
            buildToolsRepoPath=config['build_tools_repo_path'],
            branchName=branch_name,
            remoteExtras=pf.get('remote_extras'),
            downloadSymbols=pf.get('download_symbols', True),
        )
        builder = {
            'name': '%s %s' % (name_prefix, suites_name),
            'slavenames': slavenames,
            'builddir': '%s-%s' % (build_dir_prefix, suites_name),
            'slavebuilddir': 'test',
            'factory': factory,
            'category': category,
            'properties': properties,
        }
        builders.append(builder)
    elif mozharness:
        # suites is a dict!
        extra_args = suites.get('extra_args', [])
        factory = ScriptFactory(
            interpreter=mozharness_python,
            scriptRepo=suites['mozharness_repo'],
            scriptName=suites['script_path'],
            hg_bin=suites['hg_bin'],
            extra_args=suites.get('extra_args', []),
            log_eval_func=lambda c,s: regex_log_evaluator(c, s, (
             (re.compile('# TBPL WARNING #'), WARNINGS),
             (re.compile('# TBPL FAILURE #'), FAILURE),
             (re.compile('# TBPL EXCEPTION #'), EXCEPTION),
             (re.compile('# TBPL RETRY #'), RETRY),
            ))
        )
        builder = {
            'name': '%s %s' % (name_prefix, suites_name),
            'slavenames': slavenames,
            'builddir': '%s-%s' % (build_dir_prefix, suites_name),
            'slavebuilddir': 'test',
            'factory': factory,
            'category': category,
            'properties': properties,
        }
        builders.append(builder)
    else:
        if isinstance(suites, dict) and "totalChunks" in suites:
            totalChunks = suites['totalChunks']
            for i in range(totalChunks):
                factory = UnittestPackagedBuildFactory(
                    platform=platform,
                    test_suites=[suites['suite']],
                    mochitest_leak_threshold=mochitestLeakThreshold,
                    crashtest_leak_threshold=crashtestLeakThreshold,
                    hgHost=config['hghost'],
                    repoPath=config['repo_path'],
                    productName=productName,
                    posixBinarySuffix=posixBinarySuffix,
                    buildToolsRepoPath=config['build_tools_repo_path'],
                    buildSpace=1.0,
                    buildsBeforeReboot=config['platforms'][platform]['builds_before_reboot'],
                    totalChunks=totalChunks,
                    thisChunk=i+1,
                    chunkByDir=suites.get('chunkByDir'),
                    env=pf.get('unittest-env', {}),
                    downloadSymbols=pf.get('download_symbols', True),
                    resetHwClock=resetHwClock,
                    stackwalk_cgi=config.get('stackwalk_cgi'),
                )
                builder = {
                    'name': '%s %s-%i/%i' % (name_prefix, suites_name, i+1, totalChunks),
                    'slavenames': slavenames,
                    'builddir': '%s-%s-%i' % (build_dir_prefix, suites_name, i+1),
                    'slavebuilddir': 'test',
                    'factory': factory,
                    'category': category,
                    'nextSlave': _nextSlowSlave,
                    'properties': properties,
                    'env' : MozillaEnvironments.get(config['platforms'][platform].get('env_name'), {}),
                }
                builders.append(builder)
        else:
            factory = UnittestPackagedBuildFactory(
                platform=platform,
                test_suites=suites,
                mochitest_leak_threshold=mochitestLeakThreshold,
                crashtest_leak_threshold=crashtestLeakThreshold,
                hgHost=config['hghost'],
                repoPath=config['repo_path'],
                productName=productName,
                posixBinarySuffix=posixBinarySuffix,
                buildToolsRepoPath=config['build_tools_repo_path'],
                buildSpace=1.0,
                buildsBeforeReboot=config['platforms'][platform]['builds_before_reboot'],
                downloadSymbols=pf.get('download_symbols', True),
                env=pf.get('unittest-env', {}),
                resetHwClock=resetHwClock,
                stackwalk_cgi=config.get('stackwalk_cgi'),
            )
            builder = {
                'name': '%s %s' % (name_prefix, suites_name),
                'slavenames': slavenames,
                'builddir': '%s-%s' % (build_dir_prefix, suites_name),
                'slavebuilddir': 'test',
                'factory': factory,
                'category': category,
                'properties': properties,
                'env' : MozillaEnvironments.get(config['platforms'][platform].get('env_name'), {}),
            }
            builders.append(builder)
    return builders

def generateCCTestBuilder(config, branch_name, platform, name_prefix,
                          build_dir_prefix, suites_name, suites,
                          mochitestLeakThreshold, crashtestLeakThreshold,
                          slaves=None, resetHwClock=False, category=None):
    builders = []
    pf = config['platforms'].get(platform, {})
    if slaves == None:
        slavenames = config['platforms'][platform]['slaves']
    else:
        slavenames = slaves
    if not category:
        category = branch_name
    productName = pf['product_name']
    posixBinarySuffix = '-bin'
    if isinstance(suites, dict) and "totalChunks" in suites:
        totalChunks = suites['totalChunks']
        for i in range(totalChunks):
            factory = UnittestPackagedBuildFactory(
                platform=platform,
                test_suites=[suites['suite']],
                mochitest_leak_threshold=mochitestLeakThreshold,
                crashtest_leak_threshold=crashtestLeakThreshold,
                hgHost=config['hghost'],
                repoPath=config['repo_path'],
                productName=productName,
                posixBinarySuffix=posixBinarySuffix,
                buildToolsRepoPath=config['build_tools_repo_path'],
                buildSpace=1.0,
                buildsBeforeReboot=config['platforms'][platform]['builds_before_reboot'],
                totalChunks=totalChunks,
                thisChunk=i+1,
                chunkByDir=suites.get('chunkByDir'),
                env=pf.get('unittest-env', {}),
                downloadSymbols=pf.get('download_symbols', True),
                resetHwClock=resetHwClock,
            )
            builder = {
                'name': '%s %s-%i/%i' % (name_prefix, suites_name, i+1, totalChunks),
                'slavenames': slavenames,
                'builddir': '%s-%s-%i' % (build_dir_prefix, suites_name, i+1),
                'slavebuilddir': 'test',
                'factory': factory,
                'category': category,
                'properties': {'branch': branch_name, 'platform': platform,
                    'build_platform': platform, 'slavebuilddir': 'test'},
                'env' : MozillaEnvironments.get(config['platforms'][platform].get('env_name'), {}),
            }
            builders.append(builder)
    else:
        factory = UnittestPackagedBuildFactory(
            platform=platform,
            test_suites=suites,
            mochitest_leak_threshold=mochitestLeakThreshold,
            crashtest_leak_threshold=crashtestLeakThreshold,
            hgHost=config['hghost'],
            repoPath=config['repo_path'],
            productName=productName,
            posixBinarySuffix=posixBinarySuffix,
            buildToolsRepoPath=config['build_tools_repo_path'],
            buildSpace=1.0,
            buildsBeforeReboot=config['platforms'][platform]['builds_before_reboot'],
            downloadSymbols=pf.get('download_symbols', True),
            env=pf.get('unittest-env', {}),
            resetHwClock=resetHwClock,
        )
        builder = {
            'name': '%s %s' % (name_prefix, suites_name),
            'slavenames': slavenames,
            'builddir': '%s-%s' % (build_dir_prefix, suites_name),
            'slavebuilddir': 'test',
            'factory': factory,
            'category': category,
            'properties': {'branch': branch_name, 'platform': platform, 'build_platform': platform, 'slavebuilddir': 'test'},
            'env' : MozillaEnvironments.get(config['platforms'][platform].get('env_name'), {}),
        }
        builders.append(builder)
    return builders


def generateBranchObjects(config, name, secrets=None):
    """name is the name of branch which is usually the last part of the path
       to the repository. For example, 'mozilla-central', 'mozilla-aurora', or
       'mozilla-1.9.1'.
       config is a dictionary containing all of the necessary configuration
       information for a branch. The required keys depends greatly on what's
       enabled for a branch (unittests, xulrunner, l10n, etc). The best way
       to figure out what you need to pass is by looking at existing configs
       and using 'buildbot checkconfig' to verify.
    """
    # We return this at the end
    branchObjects = {
        'builders': [],
        'change_source': [],
        'schedulers': [],
        'status': []
    }
    if secrets is None:
        secrets = {}
    builders = []
    unittestBuilders = []
    triggeredUnittestBuilders = []
    nightlyBuilders = []
    xulrunnerNightlyBuilders = []
    periodicPgoBuilders = [] # Only used for the 'periodic' strategy. rename to perodicPgoBuilders?
    debugBuilders = []
    weeklyBuilders = []
    coverageBuilders = []
    # prettyNames is a mapping to pass to the try_parser for validation
    PRETTY_NAME = '%s build'
    prettyNames = {}
    unittestPrettyNames = {}
    unittestSuites = []
    # These dicts provides mapping between en-US dep and nightly scheduler names
    # to l10n dep and l10n nightly scheduler names. It's filled out just below here.
    l10nBuilders = {}
    l10nNightlyBuilders = {}
    pollInterval = config.get('pollInterval', 60)
    l10nPollInterval = config.get('l10nPollInterval', 5*60)

    # We only understand a couple PGO strategies
    assert config['pgo_strategy'] in ('per-checkin', 'periodic', None), \
            "%s is not an understood PGO strategy" % config['pgo_strategy']

    # This section is to make it easier to disable certain products.
    # Ideally we could specify a shorter platforms key on the branch,
    # but that doesn't work
    enabled_platforms = []
    for platform in sorted(config['platforms'].keys()):
        pf = config['platforms'][platform]
        if pf['stage_product'] in config['enabled_products']:
            enabled_platforms.append(platform)

    # generate a list of builders, nightly builders (names must be different)
    # for easy access
    for platform in enabled_platforms:
        pf = config['platforms'][platform]
        base_name = pf['base_name']
        pretty_name = PRETTY_NAME % base_name
        if platform.endswith("-debug"):
            debugBuilders.append(pretty_name)
            prettyNames[platform] = pretty_name
            # Debug unittests
            if pf.get('enable_unittests'):
                test_builders = []
                if 'opt_base_name' in config['platforms'][platform]:
                    base_name = config['platforms'][platform]['opt_base_name']
                else:
                    base_name = config['platforms'][platform.replace("-debug", "")]['base_name']
                for suites_name, suites in config['unittest_suites']:
                    unittestPrettyNames[platform] = '%s debug test' % base_name
                    test_builders.extend(generateTestBuilderNames('%s debug test' % base_name, suites_name, suites))
                triggeredUnittestBuilders.append(('%s-%s-unittest' % (name, platform), test_builders, config.get('enable_merging', True)))
            # Skip l10n, unit tests
            # Skip nightlies for debug builds unless requested  
            if not pf.has_key('enable_nightly'):
                continue
        elif pf.get('enable_dep', True):
            builders.append(pretty_name)
            prettyNames[platform] = pretty_name

        # Fill the l10n dep dict
        if config['enable_l10n'] and platform in config['l10n_platforms'] and \
           config['enable_l10n_onchange']:
                l10nBuilders[base_name] = {}
                l10nBuilders[base_name]['tree'] = config['l10n_tree']
                l10nBuilders[base_name]['l10n_builder'] = \
                    '%s %s %s l10n dep' % (pf['product_name'].capitalize(),
                                       name, platform)
                l10nBuilders[base_name]['platform'] = platform
        # Check if branch wants nightly builds
        if config['enable_nightly']:
            if pf.has_key('enable_nightly'):
                do_nightly = pf['enable_nightly']
            else:
                do_nightly = True
        else:
            do_nightly = False

        # Check if platform as a PGO builder
        if config['pgo_strategy'] == 'periodic' and platform in config['pgo_platforms']:
            periodicPgoBuilders.append('%s pgo-build' % pf['base_name'])

        if do_nightly:
            builder = '%s nightly' % base_name
            nightlyBuilders.append(builder)
            # Fill the l10nNightly dict
            if config['enable_l10n'] and platform in config['l10n_platforms']:
                l10nNightlyBuilders[builder] = {}
                l10nNightlyBuilders[builder]['tree'] = config['l10n_tree']
                l10nNightlyBuilders[builder]['l10n_builder'] = \
                    '%s %s %s l10n nightly' % (pf['product_name'].capitalize(),
                                       name, platform)
                l10nNightlyBuilders[builder]['platform'] = platform
            if config['enable_shark'] and pf.get('enable_shark'):
                nightlyBuilders.append('%s shark' % base_name)
            if config['enable_valgrind'] and \
               platform in config['valgrind_platforms']:
                nightlyBuilders.append('%s valgrind' % base_name)
        # Regular unittest builds
        if pf.get('enable_unittests'):
            unittestBuilders.append('%s unit test' % base_name)
            test_builders = []
            for suites_name, suites in config['unittest_suites']:
                test_builders.extend(generateTestBuilderNames('%s test' % base_name, suites_name, suites))
                unittestPrettyNames[platform] = '%s test' % base_name
            triggeredUnittestBuilders.append(('%s-%s-unittest' % (name, platform), test_builders, config.get('enable_merging', True)))
        # Optimized unittest builds
        if pf.get('enable_opt_unittests'):
            test_builders = []
            for suites_name, suites in config['unittest_suites']:
                unittestPrettyNames[platform] = '%s opt test' % base_name
                test_builders.extend(generateTestBuilderNames('%s opt test' % base_name, suites_name, suites))
            triggeredUnittestBuilders.append(('%s-%s-opt-unittest' % (name, platform), test_builders, config.get('enable_merging', True)))
        if config['enable_codecoverage'] and platform in ('linux',):
            coverageBuilders.append('%s code coverage' % base_name)
        if config.get('enable_blocklist_update', False) and platform in ('linux',):
            weeklyBuilders.append('%s blocklist update' % base_name)
        if pf.get('enable_xulrunner', config['enable_xulrunner']):
            xulrunnerNightlyBuilders.append('%s xulrunner' % base_name)
    if config['enable_weekly_bundle']:
        weeklyBuilders.append('%s hg bundle' % name)

    logUploadCmd = makeLogUploadCommand(name, config, is_try=config.get('enable_try'),
            is_shadow=bool(name=='shadow-central'), platform_prop='stage_platform',product_prop='product')

    # this comment is for grepping! SubprocessLogHandler
    branchObjects['status'].append(QueuedCommandHandler(
        logUploadCmd,
        QueueDir.getQueue('commands'),
        builders=builders + unittestBuilders + debugBuilders + periodicPgoBuilders,
    ))

    if nightlyBuilders:
        branchObjects['status'].append(QueuedCommandHandler(
            logUploadCmd + ['--nightly'],
            QueueDir.getQueue('commands'),
            builders=nightlyBuilders,
        ))

    # Currently, each branch goes to a different tree
    # If this changes in the future this may have to be
    # moved out of the loop
    if not config.get('disable_tinderbox_mail'):
        branchObjects['status'].append(TinderboxMailNotifier(
            fromaddr="mozilla2.buildbot@build.mozilla.org",
            tree=config['tinderbox_tree'],
            extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
            relayhost="mail.build.mozilla.org",
            builders=builders + nightlyBuilders + unittestBuilders + debugBuilders,
            logCompression="gzip",
            errorparser="unittest"
        ))
        # XULRunner builds
        branchObjects['status'].append(TinderboxMailNotifier(
            fromaddr="mozilla2.buildbot@build.mozilla.org",
            tree=config['xulrunner_tinderbox_tree'],
            extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
            relayhost="mail.build.mozilla.org",
            builders=xulrunnerNightlyBuilders,
            logCompression="gzip"
        ))
        # Code coverage builds go to a different tree
        branchObjects['status'].append(TinderboxMailNotifier(
            fromaddr="mozilla2.buildbot@build.mozilla.org",
            tree=config['weekly_tinderbox_tree'],
            extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
            relayhost="mail.build.mozilla.org",
            builders=coverageBuilders,
            logCompression="gzip",
            errorparser="unittest"
        ))

    # Try Server notifier
    if config.get('enable_mail_notifier'):
        packageUrl = config['package_url']
        packageDir = config['package_dir']

        if config.get('notify_real_author'):
            extraRecipients = []
            sendToInterestedUsers = True
        else:
            extraRecipients = config['email_override']
            sendToInterestedUsers = False

        # This notifies users as soon as we receive their push, and will let them
        # know where to find builds/logs
        branchObjects['status'].append(ChangeNotifier(
            fromaddr="tryserver@build.mozilla.org",
            lookup=MercurialEmailLookup(),
            relayhost="mail.build.mozilla.org",
            sendToInterestedUsers=sendToInterestedUsers,
            extraRecipients=extraRecipients,
            branches=[config['repo_path']],
            messageFormatter=lambda c: buildTryChangeMessage(c,
                '/'.join([packageUrl, packageDir])),
            ))

    if config['enable_l10n']:
        l10n_builders = []
        for b in l10nBuilders:
            if config['enable_l10n_onchange']:
                l10n_builders.append(l10nBuilders[b]['l10n_builder'])
            l10n_builders.append(l10nNightlyBuilders['%s nightly' % b]['l10n_builder'])
        l10n_binaryURL = config['enUS_binaryURL']
        if l10n_binaryURL.endswith('/'):
            l10n_binaryURL = l10n_binaryURL[:-1]
        l10n_binaryURL += "-l10n"
        nomergeBuilders.extend(l10n_builders)

        branchObjects['status'].append(TinderboxMailNotifier(
            fromaddr="bootstrap@mozilla.com",
            tree=config['l10n_tinderbox_tree'],
            extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
            relayhost="mail.build.mozilla.org",
            logCompression="gzip",
            builders=l10n_builders,
            binaryURL=l10n_binaryURL
        ))

        # We only want the builds from the specified builders
        # since their builds have a build property called "locale"
        branchObjects['status'].append(TinderboxMailNotifier(
            fromaddr="bootstrap@mozilla.com",
            tree=WithProperties(config['l10n_tinderbox_tree'] + "-%(locale)s"),
            extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
            relayhost="mail.build.mozilla.org",
            logCompression="gzip",
            builders=l10n_builders,
            binaryURL=l10n_binaryURL
        ))

        # Log uploads for dep l10n repacks
        branchObjects['status'].append(QueuedCommandHandler(
            logUploadCmd + ['--l10n'],
            QueueDir.getQueue('commands'),
            builders=[l10nBuilders[b]['l10n_builder'] for b in l10nBuilders],
        ))
        # and for nightly repacks
        branchObjects['status'].append(QueuedCommandHandler(
            logUploadCmd + ['--l10n', '--nightly'],
            QueueDir.getQueue('commands'),
            builders=[l10nNightlyBuilders['%s nightly' % b]['l10n_builder'] for b in l10nBuilders]
        ))

    # Skip https repos until bug 592060 is fixed and we have a https-capable HgPoller
    if config['hgurl'].startswith('https:'):
        pass
    else:
        if config.get('enable_try', False):
            tipsOnly = True
            # Pay attention to all branches for pushes to try
            repo_branch = None
        else:
            tipsOnly = True
            # Other branches should only pay attention to the default branch
            repo_branch = "default"

        branchObjects['change_source'].append(HgPoller(
            hgURL=config['hgurl'],
            branch=config['repo_path'],
            tipsOnly=tipsOnly,
            repo_branch=repo_branch,
            pollInterval=pollInterval,
        ))

    if config['enable_l10n'] and config['enable_l10n_onchange']:
        hg_all_locales_poller = HgAllLocalesPoller(hgURL = config['hgurl'],
                            repositoryIndex = config['l10n_repo_path'],
                            pollInterval=l10nPollInterval)
        hg_all_locales_poller.parallelRequests = 1
        branchObjects['change_source'].append(hg_all_locales_poller)

    # schedulers
    # this one gets triggered by the HG Poller
    # for Try we have a custom scheduler that can accept a function to read commit comments
    # in order to know what to schedule
    extra_args = {}
    if config.get('enable_try'):
        scheduler_class = makePropertiesScheduler(BuilderChooserScheduler, [buildUIDSchedFunc])
        extra_args['chooserFunc'] = tryChooser
        extra_args['numberOfBuildsToTrigger'] = 1
        extra_args['prettyNames'] = prettyNames
    else:
        scheduler_class = makePropertiesScheduler(Scheduler, [buildIDSchedFunc, buildUIDSchedFunc])

    if not config.get('enable_merging', True):
        nomergeBuilders.extend(builders + unittestBuilders + debugBuilders)
    nomergeBuilders.extend(periodicPgoBuilders) # these should never, ever merge
    extra_args['treeStableTimer'] = None

    branchObjects['schedulers'].append(scheduler_class(
        name=name,
        branch=config['repo_path'],
        builderNames=builders + unittestBuilders + debugBuilders,
        fileIsImportant=lambda c: isHgPollerTriggered(c, config['hgurl']) and shouldBuild(c),
        **extra_args
    ))

    if config['enable_l10n']:
        l10n_builders = []
        for b in l10nBuilders:
            l10n_builders.append(l10nBuilders[b]['l10n_builder'])
        # This L10n scheduler triggers only the builders of its own branch
        branchObjects['schedulers'].append(Scheduler(
            name="%s l10n" % name,
            branch=config['l10n_repo_path'],
            treeStableTimer=None,
            builderNames=l10n_builders,
            fileIsImportant=lambda c: isImportantL10nFile(c, config['l10n_modules']),
            properties={
                'app': 'browser',
                'en_revision': 'default',
                'l10n_revision': 'default',
                }
        ))

    for scheduler_branch, test_builders, merge in triggeredUnittestBuilders:
        scheduler_name = scheduler_branch
        for test in test_builders:
            unittestSuites.append(test.split(' ')[-1])
        if not merge:
            nomergeBuilders.extend(test_builders)
        extra_args = {}
        if config.get('enable_try'):
            scheduler_class = BuilderChooserScheduler
            extra_args['chooserFunc'] = tryChooser
            extra_args['numberOfBuildsToTrigger'] = 1
            extra_args['prettyNames'] = prettyNames
            extra_args['unittestSuites'] = unittestSuites
            extra_args['unittestPrettyNames'] = unittestPrettyNames
        else:
            scheduler_class = Scheduler
        branchObjects['schedulers'].append(scheduler_class(
            name=scheduler_name,
            branch=scheduler_branch,
            builderNames=test_builders,
            treeStableTimer=None,
            **extra_args
        ))

        if not config.get('disable_tinderbox_mail'):
            branchObjects['status'].append(TinderboxMailNotifier(
                fromaddr="mozilla2.buildbot@build.mozilla.org",
                tree=config['packaged_unittest_tinderbox_tree'],
                extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
                relayhost="mail.build.mozilla.org",
                builders=test_builders,
                logCompression="gzip",
                errorparser="unittest"
            ))

        branchObjects['status'].append(QueuedCommandHandler(
            logUploadCmd,
            QueueDir.getQueue('commands'),
            builders=test_builders,
        ))

    # Now, setup the nightly en-US schedulers and maybe,
    # their downstream l10n ones
    if nightlyBuilders or xulrunnerNightlyBuilders:
        goodFunc = lastGoodFunc(
                branch=config['repo_path'],
                builderNames=builders,
                triggerBuildIfNoChanges=False,
                l10nBranch=config.get('l10n_repo_path')
                )

        nightly_scheduler = makePropertiesScheduler(
                SpecificNightly,
                [buildIDSchedFunc, buildUIDSchedFunc])(
                    ssFunc=goodFunc,
                    name="%s nightly" % name,
                    branch=config['repo_path'],
                    # bug 482123 - keep the minute to avoid problems with DST
                    # changes
                    hour=config['start_hour'], minute=config['start_minute'],
                    builderNames=nightlyBuilders + xulrunnerNightlyBuilders,
        )
        branchObjects['schedulers'].append(nightly_scheduler)

    if len(periodicPgoBuilders) > 0:
        pgo_scheduler = makePropertiesScheduler(
                            Nightly,
                            [buildIDSchedFunc, buildUIDSchedFunc])(
                            name="%s pgo" % name,
                            branch=config['repo_path'],
                            builderNames=periodicPgoBuilders,
                            hour=range(0,24,config['periodic_pgo_interval']),
                        )
        branchObjects['schedulers'].append(pgo_scheduler)

    for builder in nightlyBuilders + xulrunnerNightlyBuilders:
        if config['enable_l10n'] and \
                config['enable_nightly'] and builder in l10nNightlyBuilders:
            l10n_builder = l10nNightlyBuilders[builder]['l10n_builder']
            platform = l10nNightlyBuilders[builder]['platform']
            branchObjects['schedulers'].append(TriggerableL10n(
                                   name=l10n_builder,
                                   platform=platform,
                                   builderNames=[l10n_builder],
                                   branch=config['repo_path'],
                                   baseTag='default',
                                   localesURL=config.get('localesURL', None)
                                  ))

    weekly_scheduler = Nightly(
            name='weekly-%s' % name,
            branch=config['repo_path'],
            dayOfWeek=5, # Saturday
            hour=[3], minute=[02],
            builderNames=coverageBuilders + weeklyBuilders,
            )
    branchObjects['schedulers'].append(weekly_scheduler)

    # We iterate throught the platforms a second time, so we need
    # to ensure that disabled platforms aren't configured the second time
    enabled_platforms = []
    for platform in sorted(config['platforms'].keys()):
        pf = config['platforms'][platform]
        if pf['stage_product'] in config['enabled_products']:
            enabled_platforms.append(platform)

    for platform in enabled_platforms:
        # shorthand
        pf = config['platforms'][platform]

        # The stage platform needs to be used by the factory __init__ methods
        # as well as the log handler status target.  Instead of repurposing the
        # platform property on each builder, we will create a new property
        # on the needed builders
        stage_platform = pf.get('stage_platform', platform)

        uploadPackages = True
        uploadSymbols = False
        packageTests = False
        talosMasters = pf['talos_masters']
        unittestBranch = "%s-%s-opt-unittest" % (name, platform)
        # Generate the PGO branch even if it isn't on for dep builds
        # because we will still use it for nightlies... maybe
        pgoUnittestBranch = "%s-%s-pgo-unittest" % (name, platform)
        tinderboxBuildsDir = None
        if platform.find('-debug') > -1:
            # Some platforms can't run on the build host
            leakTest = pf.get('enable_leaktests', True)
            codesighs = False
            if not pf.get('enable_unittests'):
                uploadPackages = pf.get('packageTests', False)
            else:
                packageTests = True
            talosMasters = None
            # Platform already has the -debug suffix
            unittestBranch = "%s-%s-unittest" % (name, platform)
            tinderboxBuildsDir = "%s-%s" % (name, platform)
        else:
            if pf.get('enable_opt_unittests'):
                packageTests=True
            codesighs = pf.get('enable_codesighs', True)
            leakTest = False

        # Allow for test packages on platforms that can't be tested
        # on the same master.
        packageTests = pf.get('packageTests', packageTests)

        if platform.find('win') > -1:
            codesighs = False

        doBuildAnalysis = pf.get('enable_build_analysis', False)

        buildSpace = pf.get('build_space', config['default_build_space'])
        l10nSpace = config['default_l10n_space']
        clobberTime = pf.get('clobber_time', config['default_clobber_time'])
        mochitestLeakThreshold = pf.get('mochitest_leak_threshold', None)
        crashtestLeakThreshold = pf.get('crashtest_leak_threshold', None)
        checkTest = pf.get('enable_checktests', False)
        valgrindCheck = pf.get('enable_valgrind_checktests', False)

        extra_args = {}
        if config.get('enable_try'):
            factory_class = TryBuildFactory
            extra_args['packageUrl'] = config['package_url']
            extra_args['packageDir'] = config['package_dir']
            extra_args['branchName'] = name
            uploadSymbols = pf.get('upload_symbols', False)
        else:
            factory_class = NightlyBuildFactory
            uploadSymbols = False

        stageBasePath = '%s/%s' % (config['stage_base_path'],
                                       pf['stage_product'])

        # For the 'per-checkin' pgo strategy, we want PGO
        # enabled on what would be 'opt' builds.
        if platform in config['pgo_platforms']:
            if config['pgo_strategy'] == 'periodic' or config['pgo_strategy'] == None:
                per_checkin_build_uses_pgo = False
            elif config['pgo_strategy'] == 'per-checkin':
                per_checkin_build_uses_pgo = True
        else:
            # All platforms that can't do PGO... shouldn't do PGO.
            per_checkin_build_uses_pgo = False

        if per_checkin_build_uses_pgo:
            per_checkin_unittest_branch = pgoUnittestBranch
        else:
            per_checkin_unittest_branch = unittestBranch

        # Some platforms shouldn't do dep builds (i.e. RPM)
        if pf.get('enable_dep', True):
            factory_kwargs = {
                'env': pf['env'],
                'objdir': pf['platform_objdir'],
                'platform': platform,
                'hgHost': config['hghost'],
                'repoPath': config['repo_path'],
                'buildToolsRepoPath': config['build_tools_repo_path'],
                'configRepoPath': config['config_repo_path'],
                'configSubDir': config['config_subdir'],
                'profiledBuild': per_checkin_build_uses_pgo,
                'productName': pf['product_name'],
                'mozconfig': pf['mozconfig'],
                'srcMozconfig': pf.get('src_mozconfig'),
                'use_scratchbox': pf.get('use_scratchbox'),
                'stageServer': config['stage_server'],
                'stageUsername': config['stage_username'],
                'stageGroup': config['stage_group'],
                'stageSshKey': config['stage_ssh_key'],
                'stageBasePath': stageBasePath,
                'stageLogBaseUrl': config.get('stage_log_base_url', None),
                'stagePlatform': pf['stage_platform'],
                'stageProduct': pf['stage_product'],
                'graphServer': config['graph_server'],
                'graphSelector': config['graph_selector'],
                'graphBranch': config.get('graph_branch', config['tinderbox_tree']),
                'doBuildAnalysis': doBuildAnalysis,
                'baseName': pf['base_name'],
                'leakTest': leakTest,
                'checkTest': checkTest,
                'valgrindCheck': valgrindCheck,
                'codesighs': codesighs,
                'uploadPackages': uploadPackages,
                'uploadSymbols': uploadSymbols,
                'buildSpace': buildSpace,
                'clobberURL': config['base_clobber_url'],
                'clobberTime': clobberTime,
                'buildsBeforeReboot': pf['builds_before_reboot'],
                'talosMasters': talosMasters,
                'packageTests': packageTests,
                'unittestMasters': pf.get('unittest_masters', config['unittest_masters']),
                'unittestBranch': per_checkin_unittest_branch,
                'tinderboxBuildsDir': tinderboxBuildsDir,
                'enable_ccache': pf.get('enable_ccache', False),
                'useSharedCheckouts': pf.get('enable_shared_checkouts', False),
                'testPrettyNames': pf.get('test_pretty_names', False),
                'l10nCheckTest': pf.get('l10n_check_test', False),
                'android_signing': pf.get('android_signing', False),
                'post_upload_include_platform': pf.get('post_upload_include_platform', False),
                'signingServers': secrets.get(pf.get('dep_signing_servers')),
                'baseMirrorUrls': config.get('base_mirror_urls'),
                'baseBundleUrls': config.get('base_bundle_urls'),
                'mozillaDir': config.get('mozilla_dir', None),
            }
            factory_kwargs.update(extra_args)

            if name in ('mozilla-1.9.1', 'mozilla-1.9.2', 'mozilla-2.0'):
                # We force profiledBuild off here because its meaning has changed
                # We deal with turning on PGO for these old branches in the actual factory
                factory_kwargs['profiledBuild'] = False

            mozilla2_dep_factory = factory_class(**factory_kwargs)
            mozilla2_dep_builder = {
                'name': '%s build' % pf['base_name'],
                'slavenames': pf['slaves'],
                'builddir': '%s-%s' % (name, platform),
                'slavebuilddir': reallyShort('%s-%s' % (name, platform)),
                'factory': mozilla2_dep_factory,
                'category': name,
                'nextSlave': _nextFastSlave,
                # Uncomment to enable only fast slaves for dep builds.
                #'nextSlave': lambda b, sl: _nextFastSlave(b, sl, only_fast=True),
                'properties': {'branch': name,
                               'platform': platform,
                               'stage_platform': stage_platform,
                               'product': pf['stage_product'],
                               'slavebuilddir' : reallyShort('%s-%s' % (name, platform))},
            }
            branchObjects['builders'].append(mozilla2_dep_builder)

            # We have some platforms which need to be built every X hours with PGO.
            # These builds are as close to regular dep builds as we can make them, 
            # other than PGO
            if config['pgo_strategy'] == 'periodic' and platform in config['pgo_platforms']:
                pgo_kwargs = factory_kwargs.copy()
                pgo_kwargs['profiledBuild'] = True
                pgo_kwargs['stagePlatform'] += '-pgo'
                pgo_kwargs['unittestBranch'] = pgoUnittestBranch
                pgo_factory = factory_class(**pgo_kwargs)
                pgo_builder = {
                    'name': '%s pgo-build' % pf['base_name'],
                    'slavenames': pf['slaves'],
                    'builddir':  '%s-%s-pgo' % (name, platform),
                    'slavebuilddir': reallyShort('%s-%s-pgo' % (name, platform)),
                    'factory': pgo_factory,
                    'category': name,
                    'nextSlave': _nextFastSlave,
                    'properties': {'branch': name,
                               'platform': platform,
                               'stage_platform': stage_platform + '-pgo',
                               'product': pf['stage_product'],
                               'slavebuilddir' : reallyShort('%s-%s-pgo' % (name, platform))},
                }
                branchObjects['builders'].append(pgo_builder)

        # skip nightlies for debug builds unless requested at platform level
        if platform.find('debug') > -1:
            if pf.get('enable_unittests'):
                for suites_name, suites in config['unittest_suites']:
                    if "macosx" in platform and 'mochitest-a11y' in suites:
                        suites = suites[:]
                        suites.remove('mochitest-a11y')

                    if 'opt_base_name' in config['platforms'][platform]:
                        base_name = config['platforms'][platform]['opt_base_name']
                    else:
                        base_name = config['platforms'][platform.replace("-debug", "")]['base_name']

                    branchObjects['builders'].extend(generateTestBuilder(
                        config, name, platform, "%s debug test" % base_name,
                        "%s-%s-unittest" % (name, platform),
                        suites_name, suites, mochitestLeakThreshold,
                        crashtestLeakThreshold, stagePlatform=stage_platform,
                        stageProduct=pf['stage_product']))
            if not pf.has_key('enable_nightly'):
                continue

        if config['enable_nightly']:
            if pf.has_key('enable_nightly'):
                do_nightly = pf['enable_nightly']
            else:
                do_nightly = True
        else:
            do_nightly = False

        if do_nightly:
            nightly_builder = '%s nightly' % pf['base_name']

            platform_env = pf['env'].copy()
            if 'update_channel' in config and config.get('create_snippet'):
                platform_env['MOZ_UPDATE_CHANNEL'] = config['update_channel']

            triggeredSchedulers=None
            if config['enable_l10n'] and pf.get('is_mobile_l10n') and pf.get('l10n_chunks'):
                mobile_l10n_scheduler_name = '%s-%s-l10n' % (name, platform)
                builder_env = platform_env.copy()
                builder_env.update({
                    'BUILDBOT_CONFIGS': '%s%s' % (config['hgurl'],
                                                  config['config_repo_path']),
                    'CLOBBERER_URL': config['base_clobber_url'],
                })
                mobile_l10n_builders = []
                for n in range(1, int(pf['l10n_chunks']) + 1):
                    builddir='%s-%s-l10n_%s' % (name, platform, str(n))
                    builderName = "%s l10n nightly %s/%s" % \
                        (pf['base_name'], n, pf['l10n_chunks'])
                    mobile_l10n_builders.append(builderName)
                    factory = ScriptFactory(
                        scriptRepo='%s%s' % (config['hgurl'],
                                              config['build_tools_repo_path']),
                        interpreter='bash',
                        scriptName='scripts/l10n/nightly_mobile_repacks.sh',
                        extra_args=[platform, stage_platform,
                                    getRealpath('localconfig.py'),
                                    str(pf['l10n_chunks']), str(n)]
                    )
                    slavebuilddir = reallyShort(builddir)
                    branchObjects['builders'].append({
                        'name': builderName,
                        'slavenames': pf.get('slaves'),
                        'builddir': builddir,
                        'slavebuilddir': slavebuilddir,
                        'factory': factory,
                        'category': name,
                        'nextSlave': _nextL10nSlave(),
                        'properties': {'branch': '%s' % config['repo_path'],
                                       'builddir': '%s-l10n_%s' % (builddir, str(n)),
                                       'stage_platform': stage_platform,
                                       'product': pf['stage_product'],
                                       'slavebuilddir': slavebuilddir},
                        'env': builder_env
                    })

                branchObjects["schedulers"].append(Triggerable(
                    name=mobile_l10n_scheduler_name,
                    builderNames=mobile_l10n_builders
                ))
                triggeredSchedulers=[mobile_l10n_scheduler_name]

            else:  # Non-mobile l10n is done differently at this time
                if config['enable_l10n'] and platform in config['l10n_platforms'] and \
                   nightly_builder in l10nNightlyBuilders:
                    triggeredSchedulers=[l10nNightlyBuilders[nightly_builder]['l10n_builder']]


            multiargs = {}
            if config.get('enable_l10n') and config.get('enable_multi_locale') and pf.get('multi_locale'):
                multiargs['multiLocale'] = True
                multiargs['multiLocaleMerge'] = config['multi_locale_merge']
                multiargs['compareLocalesRepoPath'] = config['compare_locales_repo_path']
                multiargs['compareLocalesTag'] = config['compare_locales_tag']
                multiargs['mozharnessRepoPath'] = config['mozharness_repo_path']
                multiargs['mozharnessTag'] = config['mozharness_tag']
                multi_config_name = 'multi_locale/%s_%s.json' % (name, platform)
                if 'android' in platform:
                    multiargs['multiLocaleScript'] = 'scripts/multil10n.py'
                elif 'maemo' in platform:
                    multiargs['multiLocaleScript'] = 'scripts/maemo_multi_locale_build.py'
                multiargs['multiLocaleConfig'] = multi_config_name

            create_snippet = config['create_snippet']
            if pf.has_key('create_snippet') and config['create_snippet']:
                create_snippet = pf.get('create_snippet')
            if create_snippet and 'android' in platform:
                # Ideally, this woud use some combination of product name and
                # stage_platform, but that can be done in a follow up.
                # Android doesn't create updates for all the branches that
                # Firefox desktop does.
                if config.get('create_mobile_snippet'):
                    ausargs = {
                        'downloadBaseURL': config['mobile_download_base_url'],
                        'downloadSubdir': '%s-%s' % (name, pf.get('stage_platform', platform)),
                        'ausBaseUploadDir': config['aus2_mobile_base_upload_dir'],
                    }
                else:
                    create_snippet = False
                    ausargs = {}
            else:
                ausargs = {
                    'downloadBaseURL': config['download_base_url'],
                    'downloadSubdir': '%s-%s' % (name, pf.get('stage_platform', platform)),
                    'ausBaseUploadDir': config['aus2_base_upload_dir'],
                }


            nightly_kwargs = {}
            nightly_kwargs.update(multiargs)
            nightly_kwargs.update(ausargs)

            # We make the assumption that *all* nightly builds
            # are to be done with PGO.  This is to ensure that all
            # branches get some PGO coverage
            # We do not stick '-pgo' in the stage_platform for
            # nightlies because it'd be ugly and break stuff
            if platform in config['pgo_platforms']:
                nightly_pgo = True
                nightlyUnittestBranch = pgoUnittestBranch
            else:
                nightlyUnittestBranch = unittestBranch
                nightly_pgo = False

            # More 191,192,20 special casing
            if name in ('mozilla-1.9.1', 'mozilla-1.9.2', 'mozilla-2.0'):
                nightlyUnittestBranch = unittestBranch
                nightly_pgo = False

            mozilla2_nightly_factory = NightlyBuildFactory(
                env=platform_env,
                objdir=pf['platform_objdir'],
                platform=platform,
                hgHost=config['hghost'],
                repoPath=config['repo_path'],
                buildToolsRepoPath=config['build_tools_repo_path'],
                configRepoPath=config['config_repo_path'],
                configSubDir=config['config_subdir'],
                profiledBuild=nightly_pgo,
                productName=pf['product_name'],
                mozconfig=pf['mozconfig'],
                srcMozconfig=pf.get('src_mozconfig'),
                use_scratchbox=pf.get('use_scratchbox'),
                stageServer=config['stage_server'],
                stageUsername=config['stage_username'],
                stageGroup=config['stage_group'],
                stageSshKey=config['stage_ssh_key'],
                stageBasePath=stageBasePath,
                stageLogBaseUrl=config.get('stage_log_base_url', None),
                stagePlatform=pf['stage_platform'],
                stageProduct=pf['stage_product'],
                codesighs=False,
                doBuildAnalysis=doBuildAnalysis,
                uploadPackages=uploadPackages,
                uploadSymbols=pf.get('upload_symbols', False),
                nightly=True,
                createSnippet=create_snippet,
                createPartial=pf.get('create_partial', config['create_partial']),
                updatePlatform=pf['update_platform'],
                ausUser=config['aus2_user'],
                ausSshKey=config['aus2_ssh_key'],
                ausHost=config['aus2_host'],
                hashType=config['hash_type'],
                buildSpace=buildSpace,
                clobberURL=config['base_clobber_url'],
                clobberTime=clobberTime,
                buildsBeforeReboot=pf['builds_before_reboot'],
                talosMasters=talosMasters,
                packageTests=packageTests,
                unittestMasters=pf.get('unittest_masters', config['unittest_masters']),
                unittestBranch=nightlyUnittestBranch,
                triggerBuilds=config['enable_l10n'],
                triggeredSchedulers=triggeredSchedulers,
                tinderboxBuildsDir=tinderboxBuildsDir,
                enable_ccache=pf.get('enable_ccache', False),
                useSharedCheckouts=pf.get('enable_shared_checkouts', False),
                testPrettyNames=pf.get('test_pretty_names', False),
                l10nCheckTest=pf.get('l10n_check_test', False),
                android_signing=pf.get('android_signing', False),
                post_upload_include_platform=pf.get('post_upload_include_platform', False),
                signingServers=secrets.get(pf.get('nightly_signing_servers')),
                baseMirrorUrls=config.get('base_mirror_urls'),
                baseBundleUrls=config.get('base_bundle_urls'),
                mozillaDir=config.get('mozilla_dir', None),
                **nightly_kwargs
            )

            mozilla2_nightly_builder = {
                'name': nightly_builder,
                'slavenames': pf['slaves'],
                'builddir': '%s-%s-nightly' % (name, platform),
                'slavebuilddir': reallyShort('%s-%s-nightly' % (name, platform)),
                'factory': mozilla2_nightly_factory,
                'category': name,
                'nextSlave': lambda b, sl: _nextFastSlave(b, sl, only_fast=True),
                'properties': {'branch': name,
                               'platform': platform,
                               'stage_platform': stage_platform,
                               'product': pf['stage_product'],
                               'nightly_build': True,
                               'slavebuilddir': reallyShort('%s-%s-nightly' % (name, platform))},
            }
            branchObjects['builders'].append(mozilla2_nightly_builder)

            if config['enable_l10n']:
                if platform in config['l10n_platforms']:
                    # TODO Linux and mac are not working with mozconfig at this point
                    # and this will disable it for now. We will fix this in bug 518359.
                    objdir = ''
                    mozconfig = None

                    mozilla2_l10n_nightly_factory = NightlyRepackFactory(
                        env=platform_env,
                        objdir=objdir,
                        platform=platform,
                        hgHost=config['hghost'],
                        tree=config['l10n_tree'],
                        project=pf['product_name'],
                        appName=pf['app_name'],
                        enUSBinaryURL=config['enUS_binaryURL'],
                        nightly=True,
                        configRepoPath=config['config_repo_path'],
                        configSubDir=config['config_subdir'],
                        mozconfig=mozconfig,
                        l10nNightlyUpdate=config['l10nNightlyUpdate'],
                        l10nDatedDirs=config['l10nDatedDirs'],
                        createPartial=config['create_partial_l10n'],
                        ausBaseUploadDir=config['aus2_base_upload_dir_l10n'],
                        updatePlatform=pf['update_platform'],
                        downloadBaseURL=config['download_base_url'],
                        ausUser=config['aus2_user'],
                        ausSshKey=config['aus2_ssh_key'],
                        ausHost=config['aus2_host'],
                        hashType=config['hash_type'],
                        stageServer=config['stage_server'],
                        stageUsername=config['stage_username'],
                        stageSshKey=config['stage_ssh_key'],
                        repoPath=config['repo_path'],
                        l10nRepoPath=config['l10n_repo_path'],
                        buildToolsRepoPath=config['build_tools_repo_path'],
                        compareLocalesRepoPath=config['compare_locales_repo_path'],
                        compareLocalesTag=config['compare_locales_tag'],
                        buildSpace=l10nSpace,
                        clobberURL=config['base_clobber_url'],
                        clobberTime=clobberTime,
                        signingServers=secrets.get(pf.get('nightly_signing_servers')),
                        baseMirrorUrls=config.get('base_mirror_urls'),
                        extraConfigureArgs=config.get('l10n_extra_configure_args', []),
                    )
                    mozilla2_l10n_nightly_builder = {
                        'name': l10nNightlyBuilders[nightly_builder]['l10n_builder'],
                        'slavenames': config['l10n_slaves'][platform],
                        'builddir': '%s-%s-l10n-nightly' % (name, platform),
                        'slavebuilddir': reallyShort('%s-%s-l10n-nightly' % (name, platform)),
                        'factory': mozilla2_l10n_nightly_factory,
                        'category': name,
                        'nextSlave': _nextL10nSlave(),
                        'properties': {'branch': name,
                                       'platform': platform,
                                       'product': pf['stage_product'],
                                       'stage_platform': stage_platform,
                                       'slavebuilddir': reallyShort('%s-%s-l10n-nightly' % (name, platform)),},
                    }
                    branchObjects['builders'].append(mozilla2_l10n_nightly_builder)

            if config['enable_shark'] and pf.get('enable_shark'):
                if name in ('mozilla-1.9.1','mozilla-1.9.2'):
                    shark_objdir = config['objdir']
                else:
                    shark_objdir = pf['platform_objdir']
                mozilla2_shark_factory = NightlyBuildFactory(
                    env=platform_env,
                    objdir=shark_objdir,
                    platform=platform,
                    stagePlatform=stage_platform,
                    hgHost=config['hghost'],
                    repoPath=config['repo_path'],
                    buildToolsRepoPath=config['build_tools_repo_path'],
                    configRepoPath=config['config_repo_path'],
                    configSubDir=config['config_subdir'],
                    profiledBuild=False,
                    productName=pf['product_name'],
                    mozconfig='%s/%s/shark' % (platform, name),
                    srcMozconfig=pf.get('src_shark_mozconfig'),
                    stageServer=config['stage_server'],
                    stageUsername=config['stage_username'],
                    stageGroup=config['stage_group'],
                    stageSshKey=config['stage_ssh_key'],
                    stageBasePath=stageBasePath,
                    stageLogBaseUrl=config.get('stage_log_base_url', None),
                    stageProduct=pf.get('stage_product'),
                    codesighs=False,
                    uploadPackages=uploadPackages,
                    uploadSymbols=False,
                    nightly=True,
                    createSnippet=False,
                    buildSpace=buildSpace,
                    clobberURL=config['base_clobber_url'],
                    clobberTime=clobberTime,
                    buildsBeforeReboot=pf['builds_before_reboot'],
                    post_upload_include_platform=pf.get('post_upload_include_platform', False),
                )
                mozilla2_shark_builder = {
                    'name': '%s shark' % pf['base_name'],
                    'slavenames': pf['slaves'],
                    'builddir': '%s-%s-shark' % (name, platform),
                    'slavebuilddir': reallyShort('%s-%s-shark' % (name, platform)),
                    'factory': mozilla2_shark_factory,
                    'category': name,
                    'nextSlave': _nextSlowSlave,
                    'properties': {'branch': name,
                                   'platform': platform,
                                   'stage_platform': stage_platform,
                                   'product': pf['stage_product'],
                                   'slavebuilddir': reallyShort('%s-%s-shark' % (name, platform))},
                }
                branchObjects['builders'].append(mozilla2_shark_builder)
            if config['enable_valgrind'] and \
               platform in config['valgrind_platforms']:
                valgrind_env=platform_env.copy()
                valgrind_env['REVISION'] = WithProperties("%(revision)s")
                mozilla2_valgrind_factory = ScriptFactory(
                    "%s%s" % (config['hgurl'],config['build_tools_repo_path']),
                    'scripts/valgrind/valgrind.sh',
                )
                mozilla2_valgrind_builder = {
                    'name': '%s valgrind' % pf['base_name'],
                    'slavenames': pf['slaves'],
                    'builddir': '%s-%s-valgrind' % (name, platform),
                    'slavebuilddir': reallyShort('%s-%s-valgrind' % (name, platform)),
                    'factory': mozilla2_valgrind_factory,
                    'category': name,
                    'env': valgrind_env,
                    'nextSlave': _nextSlowSlave,
                    'properties': {'branch': name,
                                   'platform': platform,
                                   'stage_platform': stage_platform,
                                   'product': pf['stage_product'],
                                   'slavebuilddir': reallyShort('%s-%s-valgrind' % (name, platform))},
                }
                branchObjects['builders'].append(mozilla2_valgrind_builder)

        # We still want l10n_dep builds if nightlies are off
        if config['enable_l10n'] and platform in config['l10n_platforms'] and \
           config['enable_l10n_onchange']:
            mozilla2_l10n_dep_factory = NightlyRepackFactory(
                env=platform_env,
                platform=platform,
                hgHost=config['hghost'],
                tree=config['l10n_tree'],
                project=pf['product_name'],
                appName=pf['app_name'],
                enUSBinaryURL=config['enUS_binaryURL'],
                nightly=False,
                l10nDatedDirs=config['l10nDatedDirs'],
                stageServer=config['stage_server'],
                stageUsername=config['stage_username'],
                stageSshKey=config['stage_ssh_key'],
                repoPath=config['repo_path'],
                l10nRepoPath=config['l10n_repo_path'],
                buildToolsRepoPath=config['build_tools_repo_path'],
                compareLocalesRepoPath=config['compare_locales_repo_path'],
                compareLocalesTag=config['compare_locales_tag'],
                buildSpace=l10nSpace,
                clobberURL=config['base_clobber_url'],
                clobberTime=clobberTime,
                signingServers=secrets.get(pf.get('dep_signing_servers')),
                baseMirrorUrls=config.get('base_mirror_urls'),
                extraConfigureArgs=config.get('l10n_extra_configure_args', []),
            )
            mozilla2_l10n_dep_builder = {
                'name': l10nBuilders[pf['base_name']]['l10n_builder'],
                'slavenames': config['l10n_slaves'][platform],
                'builddir': '%s-%s-l10n-dep' % (name, platform),
                'slavebuilddir': reallyShort('%s-%s-l10n-dep' % (name, platform)),
                'factory': mozilla2_l10n_dep_factory,
                'category': name,
                'nextSlave': _nextL10nSlave(),
                'properties': {'branch': name,
                               'platform': platform,
                               'stage_platform': stage_platform,
                               'product': pf['stage_product'],
                               'slavebuilddir': reallyShort('%s-%s-l10n-dep' % (name, platform))},
            }
            branchObjects['builders'].append(mozilla2_l10n_dep_builder)

        if pf.get('enable_unittests'):
            runA11y = True
            if platform.startswith('macosx'):
                runA11y = config['enable_mac_a11y']

            extra_args = {}
            if config.get('enable_try'):
                factory_class = TryUnittestBuildFactory
                extra_args['branchName'] = name
            else:
                factory_class = UnittestBuildFactory

            unittest_factory = factory_class(
                env=pf.get('unittest-env', {}),
                platform=platform,
                productName=pf['product_name'],
                config_repo_path=config['config_repo_path'],
                config_dir=config['config_subdir'],
                objdir=config['objdir_unittests'],
                hgHost=config['hghost'],
                repoPath=config['repo_path'],
                buildToolsRepoPath=config['build_tools_repo_path'],
                buildSpace=config['unittest_build_space'],
                clobberURL=config['base_clobber_url'],
                clobberTime=clobberTime,
                buildsBeforeReboot=pf['builds_before_reboot'],
                run_a11y=runA11y,
                mochitest_leak_threshold=mochitestLeakThreshold,
                crashtest_leak_threshold=crashtestLeakThreshold,
                stageServer=config['stage_server'],
                stageUsername=config['stage_username'],
                stageSshKey=config['stage_ssh_key'],
                unittestMasters=config['unittest_masters'],
                unittestBranch="%s-%s-unittest" % (name, platform),
                uploadPackages=True,
                **extra_args
            )
            unittest_builder = {
                'name': '%s unit test' % pf['base_name'],
                'slavenames': pf['slaves'],
                'builddir': '%s-%s-unittest' % (name, platform),
                'slavebuilddir': reallyShort('%s-%s-unittest' % (name, platform)),
                'factory': unittest_factory,
                'category': name,
                'nextSlave': _nextFastSlave,
                'properties': {'branch': name,
                               'platform': platform,
                               'stage_platform': stage_platform,
                               'product': pf['stage_product'],
                               'slavebuilddir': reallyShort('%s-%s-unittest' % (name, platform))},
            }
            branchObjects['builders'].append(unittest_builder)

        for suites_name, suites in config['unittest_suites']:
            runA11y = True
            if platform.startswith('macosx'):
                runA11y = config['enable_mac_a11y']

            # For the regular unittest build, run the a11y suite if
            # enable_mac_a11y is set on mac
            if not runA11y and 'mochitest-a11y' in suites:
                suites = suites[:]
                suites.remove('mochitest-a11y')

            if pf.get('enable_unittests'):
                branchObjects['builders'].extend(generateTestBuilder(
                    config, name, platform, "%s test" % pf['base_name'],
                    "%s-%s-unittest" % (name, platform),
                    suites_name, suites, mochitestLeakThreshold,
                    crashtestLeakThreshold, stagePlatform=stage_platform,
                    stageProduct=pf['stage_product']))

            # Remove mochitest-a11y from other types of builds, since they're not
            # built with a11y enabled
            if platform.startswith("macosx") and 'mochitest-a11y' in suites:
                # Create a new factory that doesn't have mochitest-a11y
                suites = suites[:]
                suites.remove('mochitest-a11y')

            if pf.get('enable_opt_unittests'):
                branchObjects['builders'].extend(generateTestBuilder(
                    config, name, platform, "%s opt test" % pf['base_name'],
                    "%s-%s-opt-unittest" % (name, platform),
                    suites_name, suites, mochitestLeakThreshold,
                    crashtestLeakThreshold, stagePlatform=stage_platform,
                    stageProduct=pf['stage_product']))

        if config['enable_codecoverage']:
            # We only do code coverage builds on linux right now
            if platform == 'linux':
                codecoverage_factory = CodeCoverageFactory(
                    platform=platform,
                    productName=pf['product_name'],
                    config_repo_path=config['config_repo_path'],
                    config_dir=config['config_subdir'],
                    objdir=config['objdir_unittests'],
                    hgHost=config['hghost'],
                    repoPath=config['repo_path'],
                    buildToolsRepoPath=config['build_tools_repo_path'],
                    buildSpace=7,
                    clobberURL=config['base_clobber_url'],
                    clobberTime=clobberTime,
                    buildsBeforeReboot=pf['builds_before_reboot'],
                    mochitest_leak_threshold=mochitestLeakThreshold,
                    crashtest_leak_threshold=crashtestLeakThreshold,
                    stageServer=config['stage_server'],
                    stageUsername=config['stage_username'],
                    stageSshKey=config['stage_ssh_key'],
                )
                codecoverage_builder = {
                    'name': '%s code coverage' % pf['base_name'],
                    'slavenames': pf['slaves'],
                    'builddir': '%s-%s-codecoverage' % (name, platform),
                    'slavebuilddir': reallyShort('%s-%s-codecoverage' % (name, platform)),
                    'factory': codecoverage_factory,
                    'category': name,
                    'nextSlave': _nextSlowSlave,
                    'properties': {'branch': name, 'platform': platform, 'slavebuilddir': reallyShort('%s-%s-codecoverage' % (name, platform))},
                }
                branchObjects['builders'].append(codecoverage_builder)

        if config.get('enable_blocklist_update', False):
            if platform == 'linux':
                blocklistBuilder = generateBlocklistBuilder(config, name, platform, pf['base_name'], pf['slaves'])
                branchObjects['builders'].append(blocklistBuilder)

        if pf.get('enable_xulrunner', config['enable_xulrunner']):
             xr_env = pf['env'].copy()
             xr_env['SYMBOL_SERVER_USER'] = config['stage_username_xulrunner']
             xr_env['SYMBOL_SERVER_PATH'] = config['symbol_server_xulrunner_path']
             xr_env['SYMBOL_SERVER_SSH_KEY'] = \
                 xr_env['SYMBOL_SERVER_SSH_KEY'].replace(config['stage_ssh_key'], config['stage_ssh_xulrunner_key'])
             if pf.has_key('xr_mozconfig'):
                 mozconfig = pf['xr_mozconfig']
             else:
                 mozconfig = '%s/%s/xulrunner' % (platform, name)
             xulrunnerStageBasePath = '%s/xulrunner' % config['stage_base_path']
             mozilla2_xulrunner_factory = NightlyBuildFactory(
                 env=xr_env,
                 objdir=pf['platform_objdir'],
                 platform=platform,
                 hgHost=config['hghost'],
                 repoPath=config['repo_path'],
                 buildToolsRepoPath=config['build_tools_repo_path'],
                 configRepoPath=config['config_repo_path'],
                 configSubDir=config['config_subdir'],
                 profiledBuild=False,
                 productName='xulrunner',
                 mozconfig=mozconfig,
                 srcMozconfig=pf.get('src_xulrunner_mozconfig'),
                 stageServer=config['stage_server'],
                 stageUsername=config['stage_username_xulrunner'],
                 stageGroup=config['stage_group'],
                 stageSshKey=config['stage_ssh_xulrunner_key'],
                 stageBasePath=xulrunnerStageBasePath,
                 codesighs=False,
                 uploadPackages=uploadPackages,
                 uploadSymbols=True,
                 nightly=True,
                 createSnippet=False,
                 buildSpace=buildSpace,
                 clobberURL=config['base_clobber_url'],
                 clobberTime=clobberTime,
                 buildsBeforeReboot=pf['builds_before_reboot'],
                 packageSDK=True,
                 signingServers=secrets.get(pf.get('nightly_signing_servers')),
             )
             mozilla2_xulrunner_builder = {
                 'name': '%s xulrunner' % pf['base_name'],
                 'slavenames': pf['slaves'],
                 'builddir': '%s-%s-xulrunner' % (name, platform),
                 'slavebuilddir': reallyShort('%s-%s-xulrunner' % (name, platform)),
                 'factory': mozilla2_xulrunner_factory,
                 'category': name,
                 'nextSlave': _nextSlowSlave,
                 'properties': {'branch': name, 'platform': platform, 'slavebuilddir': reallyShort('%s-%s-xulrunner' % (name, platform))},
             }
             branchObjects['builders'].append(mozilla2_xulrunner_builder)

        # -- end of per-platform loop --

    if config['enable_weekly_bundle']:
        stageBasePath = '%s/%s' % (config['stage_base_path'],
                                   pf['stage_product'])
        bundle_factory = ScriptFactory(
            config['hgurl'] + config['build_tools_repo_path'],
            'scripts/bundle/hg-bundle.sh',
            interpreter='bash',
            script_timeout=3600,
            script_maxtime=3600,
            extra_args=[
                name,
                config['repo_path'],
                config['stage_server'],
                config['stage_username'],
                stageBasePath,
                config['stage_ssh_key'],
                ],
        )
        slaves = set()
        for p in sorted(config['platforms'].keys()):
            slaves.update(set(config['platforms'][p]['slaves']))
        bundle_builder = {
            'name': '%s hg bundle' % name,
            'slavenames': list(slaves),
            'builddir': '%s-bundle' % (name,),
            'slavebuilddir': reallyShort('%s-bundle' % (name,)),
            'factory': bundle_factory,
            'category': name,
            'nextSlave': _nextSlowSlave,
            'properties': {'slavebuilddir': reallyShort('%s-bundle' % (name,))}
        }
        branchObjects['builders'].append(bundle_builder)

    return branchObjects

def generateCCBranchObjects(config, name):
    """name is the name of branch which is usually the last part of the path
       to the repository. For example, 'comm-central-trunk', or 'comm-1.9.1'.
       config is a dictionary containing all of the necessary configuration
       information for a branch. The required keys depends greatly on what's
       enabled for a branch (unittests, l10n, etc). The best way to figure out
       what you need to pass is by looking at existing configs and using
       'buildbot checkconfig' to verify.
    """
    # We return this at the end
    branchObjects = {
        'builders': [],
        'change_source': [],
        'schedulers': [],
        'status': []
    }
    builders = []
    unittestBuilders = []
    triggeredUnittestBuilders = []
    nightlyBuilders = []
    debugBuilders = []
    weeklyBuilders = []
    coverageBuilders = []
    # prettyNames is a mapping to pass to the try_parser for validation
    PRETTY_NAME = '%s build'
    prettyNames = {}
    unittestPrettyNames = {}
    unittestSuites = []
    # These dicts provides mapping between en-US dep and nightly scheduler names
    # to l10n dep and l10n nightly scheduler names. It's filled out just below here.
    l10nBuilders = {}
    l10nNightlyBuilders = {}
    pollInterval = config.get('pollInterval', 60)
    l10nPollInterval = config.get('l10nPollInterval', 5*60)
    # generate a list of builders, nightly builders (names must be different)
    # for easy access
    for platform in config['platforms'].keys():
        pf = config['platforms'][platform]
        base_name = pf['base_name']
        pretty_name = PRETTY_NAME % base_name
        if platform.endswith("-debug"):
            debugBuilders.append(pretty_name)
            prettyNames[platform] = pretty_name
            # Debug unittests
            if pf.get('enable_unittests'):
                test_builders = []
                if 'opt_base_name' in config['platforms'][platform]:
                    base_name = config['platforms'][platform]['opt_base_name']
                else:
                    base_name = config['platforms'][platform.replace("-debug", "")]['base_name']
                for suites_name, suites in config['unittest_suites']:
                    unittestPrettyNames[platform] = '%s debug test' % base_name
                    test_builders.extend(generateTestBuilderNames('%s debug test' % base_name, suites_name, suites))
                triggeredUnittestBuilders.append(('%s-%s-unittest' % (name, platform), test_builders, config.get('enable_merging', True)))
            # Skip l10n, unit tests and nightlies for debug builds
            continue
        else:
            builders.append(pretty_name)
            prettyNames[platform] = pretty_name

        # Fill the l10n dep dict
        if config['enable_l10n'] and platform in config['l10n_platforms'] and \
           config['enable_l10n_onchange']:
                l10nBuilders[base_name] = {}
                l10nBuilders[base_name]['tree'] = config['l10n_tree']
                l10nBuilders[base_name]['l10n_builder'] = \
                    '%s %s %s l10n dep' % (pf['product_name'].capitalize(),
                                       name, platform)
                l10nBuilders[base_name]['platform'] = platform
        # Check if branch wants nightly builds
        if config['enable_nightly']:
            if pf.has_key('enable_nightly'):
                do_nightly = pf['enable_nightly']
            else:
                do_nightly = True
        else:
            do_nightly = False

        if do_nightly:
            builder = '%s nightly' % base_name
            nightlyBuilders.append(builder)
            # Fill the l10nNightly dict
            if config['enable_l10n'] and platform in config['l10n_platforms']:
                l10nNightlyBuilders[builder] = {}
                l10nNightlyBuilders[builder]['tree'] = config['l10n_tree']
                l10nNightlyBuilders[builder]['l10n_builder'] = \
                    '%s %s %s l10n nightly' % (pf['product_name'].capitalize(),
                                       name, platform)
                l10nNightlyBuilders[builder]['platform'] = platform
            if config['enable_shark'] and platform.startswith('macosx'):
                nightlyBuilders.append('%s shark' % base_name)
        # Regular unittest builds
        if pf.get('enable_unittests'):
            unittestBuilders.append('%s unit test' % base_name)
            test_builders = []
            for suites_name, suites in config['unittest_suites']:
                test_builders.extend(generateTestBuilderNames('%s test' % base_name, suites_name, suites))
                unittestPrettyNames[platform] = '%s test' % base_name
            triggeredUnittestBuilders.append(('%s-%s-unittest' % (name, platform), test_builders, config.get('enable_merging', True)))
        # Optimized unittest builds
        if pf.get('enable_opt_unittests'):
            test_builders = []
            for suites_name, suites in config['unittest_suites']:
                unittestPrettyNames[platform] = '%s opt test' % base_name
                test_builders.extend(generateTestBuilderNames('%s opt test' % base_name, suites_name, suites))
            triggeredUnittestBuilders.append(('%s-%s-opt-unittest' % (name, platform), test_builders, config.get('enable_merging', True)))
        if config['enable_codecoverage'] and platform in ('linux',):
            coverageBuilders.append('%s code coverage' % base_name)
        if config.get('enable_blocklist_update', False) and platform in ('linux',):
            weeklyBuilders.append('%s blocklist update' % base_name)
    if config['enable_weekly_bundle']:
        weeklyBuilders.append('%s hg bundle' % name)

    logUploadCmd = makeLogUploadCommand(name, config, is_try=config.get('enable_try'),
            is_shadow=bool(name=='shadow-central'), product=pf['product_name'])

    branchObjects['status'].append(SubprocessLogHandler(
        logUploadCmd,
        builders=builders + unittestBuilders + debugBuilders,
    ))

    if nightlyBuilders:
        branchObjects['status'].append(SubprocessLogHandler(
            logUploadCmd + ['--nightly'],
            builders=nightlyBuilders,
        ))

    # Currently, each branch goes to a different tree
    # If this changes in the future this may have to be
    # moved out of the loop
    branchObjects['status'].append(TinderboxMailNotifier(
        fromaddr="comm.buildbot@build.mozilla.org",
        tree=config['tinderbox_tree'],
        extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
        relayhost="mail.build.mozilla.org",
        builders=builders + nightlyBuilders + unittestBuilders + debugBuilders,
        logCompression="gzip",
        errorparser="unittest"
    ))
    # Code coverage builds go to a different tree
    branchObjects['status'].append(TinderboxMailNotifier(
        fromaddr="comm.buildbot@build.mozilla.org",
        tree=config['weekly_tinderbox_tree'],
        extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
        relayhost="mail.build.mozilla.org",
        builders=coverageBuilders,
        logCompression="gzip",
        errorparser="unittest"
    ))

    # Try Server notifier
    if config.get('enable_mail_notifier'):
        packageUrl = config['package_url']
        packageDir = config['package_dir']

        if config.get('notify_real_author'):
            extraRecipients = []
            sendToInterestedUsers = True
        else:
            extraRecipients = config['email_override']
            sendToInterestedUsers = False

        # This notifies users as soon as we receive their push, and will let them
        # know where to find builds/logs
        branchObjects['status'].append(ChangeNotifier(
            fromaddr="tryserver@build.mozilla.org",
            lookup=MercurialEmailLookup(),
            relayhost="mail.build.mozilla.org",
            sendToInterestedUsers=sendToInterestedUsers,
            extraRecipients=extraRecipients,
            branches=[config['repo_path']],
            messageFormatter=lambda c: buildTryChangeMessage(c,
                '/'.join([packageUrl, packageDir])),
            ))

    if config['enable_l10n']:
        l10n_builders = []
        for b in l10nBuilders:
            if config['enable_l10n_onchange']:
                l10n_builders.append(l10nBuilders[b]['l10n_builder'])
            l10n_builders.append(l10nNightlyBuilders['%s nightly' % b]['l10n_builder'])
        l10n_binaryURL = config['enUS_binaryURL']
        if l10n_binaryURL.endswith('/'):
            l10n_binaryURL = l10n_binaryURL[:-1]
        l10n_binaryURL += "-l10n"
        nomergeBuilders.extend(l10n_builders)

        # This notifies all l10n related build objects to Mozilla-l10n
        branchObjects['status'].append(TinderboxMailNotifier(
            fromaddr="comm.buildbot@build.mozilla.org",
            tree=config['l10n_tinderbox_tree'],
            extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
            relayhost="mail.build.mozilla.org",
            logCompression="gzip",
            builders=l10n_builders,
            binaryURL=l10n_binaryURL
        ))

        # We only want the builds from the specified builders
        # since their builds have a build property called "locale"
        branchObjects['status'].append(TinderboxMailNotifier(
            fromaddr="comm.buildbot@build.mozilla.org",
            tree=WithProperties(config['l10n_tinderbox_tree'] + "-%(locale)s"),
            extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
            relayhost="mail.build.mozilla.org",
            logCompression="gzip",
            builders=l10n_builders,
            binaryURL=l10n_binaryURL
        ))

        # Log uploads for dep l10n repacks
        branchObjects['status'].append(SubprocessLogHandler(
            logUploadCmd + ['--l10n'],
            builders=[l10nBuilders[b]['l10n_builder'] for b in l10nBuilders],
        ))
        # and for nightly repacks
        branchObjects['status'].append(SubprocessLogHandler(
            logUploadCmd + ['--l10n', '--nightly'],
            builders=[l10nNightlyBuilders['%s nightly' % b]['l10n_builder'] for b in l10nBuilders]
        ))

    # change sources - if try is enabled, tipsOnly will be true which makes
    # every push only show up as one changeset
    # Skip https repos until bug 592060 is fixed and we have a https-capable HgPoller
    if config['hgurl'].startswith('https:'):
        pass
    else:
        branchObjects['change_source'].append(HgPoller(
            hgURL=config['hgurl'],
            branch=config['repo_path'],
            tipsOnly=config.get('enable_try', False),
            pollInterval=pollInterval,
            storeRev="polled_comm_revision",
        ))
        # for Mozilla tree, need valid branch, so override pushlog URL
        branchObjects['change_source'].append(HgPoller(
            hgURL=config['hgurl'],
            branch=config['repo_path'],
            pushlogUrlOverride='%s/%s/json-pushes?full=1' % (config['hgurl'],
                                                  config['mozilla_repo_path']),
            tipsOnly=config.get('enable_try', False),
            pollInterval=pollInterval,
            storeRev="polled_moz_revision",
        ))

    if config['enable_l10n'] and config['enable_l10n_onchange']:
        hg_all_locales_poller = HgAllLocalesPoller(hgURL = config['hgurl'],
                            repositoryIndex = config['l10n_repo_path'],
                            pollInterval=l10nPollInterval)
        hg_all_locales_poller.parallelRequests = 1
        branchObjects['change_source'].append(hg_all_locales_poller)

    # schedulers
    # this one gets triggered by the HG Poller
    # for Try we have a custom scheduler that can accept a function to read commit comments
    # in order to know what to schedule
    extra_args = {}
    if config.get('enable_try'):
        scheduler_class = makePropertiesScheduler(BuilderChooserScheduler, [buildUIDSchedFunc])
        extra_args['chooserFunc'] = tryChooser
        extra_args['numberOfBuildsToTrigger'] = 1
        extra_args['prettyNames'] = prettyNames
    else:
        scheduler_class = makePropertiesScheduler(Scheduler, [buildIDSchedFunc, buildUIDSchedFunc])

    if not config.get('enable_merging', True):
        nomergeBuilders.extend(builders + unittestBuilders + debugBuilders)
        extra_args['treeStableTimer'] = None
    else:
        extra_args['treeStableTimer'] = 3*60

    branchObjects['schedulers'].append(scheduler_class(
        name=name,
        branch=config['repo_path'],
        builderNames=builders + unittestBuilders + debugBuilders,
        fileIsImportant=lambda c: isHgPollerTriggered(c, config['hgurl']) and shouldBuild(c),
        **extra_args
    ))

    if config['enable_l10n']:
        l10n_builders = []
        for b in l10nBuilders:
            l10n_builders.append(l10nBuilders[b]['l10n_builder'])
        # This L10n scheduler triggers only the builders of its own branch
        branchObjects['schedulers'].append(Scheduler(
            name="%s l10n" % name,
            branch=config['l10n_repo_path'],
            treeStableTimer=None,
            builderNames=l10n_builders,
            fileIsImportant=lambda c: isImportantL10nFile(c, config['l10n_modules']),
            properties={
                'app': pf['app_name'],
                'en_revision': 'default',
                'l10n_revision': 'default',
                }
        ))

    for scheduler_branch, test_builders, merge in triggeredUnittestBuilders:
        scheduler_name = scheduler_branch
        for test in test_builders:
            unittestSuites.append(test.split(' ')[-1])
        if not merge:
            nomergeBuilders.extend(test_builders)
        extra_args = {}
        if config.get('enable_try'):
            scheduler_class = BuilderChooserScheduler
            extra_args['chooserFunc'] = tryChooser
            extra_args['numberOfBuildsToTrigger'] = 1
            extra_args['prettyNames'] = prettyNames
            extra_args['unittestSuites'] = unittestSuites
            extra_args['unittestPrettyNames'] = unittestPrettyNames
        else:
            scheduler_class = Scheduler
        branchObjects['schedulers'].append(scheduler_class(
            name=scheduler_name,
            branch=scheduler_branch,
            builderNames=test_builders,
            treeStableTimer=None,
            **extra_args
        ))

        branchObjects['status'].append(TinderboxMailNotifier(
            fromaddr="comm.buildbot@build.mozilla.org",
            tree=config['packaged_unittest_tinderbox_tree'],
            extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
            relayhost="mail.build.mozilla.org",
            builders=test_builders,
            logCompression="gzip",
            errorparser="unittest"
        ))

        branchObjects['status'].append(SubprocessLogHandler(
            logUploadCmd,
            builders=test_builders,
        ))

    # Now, setup the nightly en-US schedulers and maybe,
    # their downstream l10n ones
    if nightlyBuilders:
        nightly_scheduler = makePropertiesScheduler(
                SpecificNightly,
                [buildIDSchedFunc, buildUIDSchedFunc])(
                    ssFunc=lastGoodFunc(config['repo_path'],
                        builderNames=builders),
                    name="%s nightly" % name,
                    branch=config['repo_path'],
                    # bug 482123 - keep the minute to avoid problems with DST
                    # changes
                    hour=config['start_hour'], minute=config['start_minute'],
                    builderNames=nightlyBuilders,
        )
        branchObjects['schedulers'].append(nightly_scheduler)

    for builder in nightlyBuilders:
        if config['enable_l10n'] and \
                config['enable_nightly'] and builder in l10nNightlyBuilders:
            l10n_builder = l10nNightlyBuilders[builder]['l10n_builder']
            platform = l10nNightlyBuilders[builder]['platform']
            branchObjects['schedulers'].append(TriggerableL10n(
                                   name=l10n_builder,
                                   platform=platform,
                                   builderNames=[l10n_builder],
                                   branch=config['repo_path'],
                                   baseTag='default',
                                   localesFile=config['allLocalesFile']
                                  ))

    weekly_scheduler=Nightly(
            name='weekly-%s' % name,
            branch=config['repo_path'],
            dayOfWeek=5, # Saturday
            hour=[3], minute=[02],
            builderNames=coverageBuilders + weeklyBuilders,
            )
    branchObjects['schedulers'].append(weekly_scheduler)

    for platform in sorted(config['platforms'].keys()):
        # shorthand
        pf = config['platforms'][platform]

        leakTest = False
        codesighs = config.get('enable_codesighs',True)
        uploadPackages = True
        uploadSymbols = False
        packageTests = False
        talosMasters = pf['talos_masters']
        unittestBranch = "%s-%s-opt-unittest" % (name, platform)
        tinderboxBuildsDir = None
        if platform.find('-debug') > -1:
            leakTest = True
            codesighs = False
            if not pf.get('enable_unittests'):
                uploadPackages = pf.get('packageTests', False)
            else:
                packageTests = True
            talosMasters = None
            # Platform already has the -debug suffix
            unittestBranch = "%s-%s-unittest" % (name, platform)
            tinderboxBuildsDir = "%s-%s" % (name, platform)
        elif pf.get('enable_opt_unittests'):
            packageTests = True

        # Allow for test packages on platforms that can't be tested
        # on the same master.
        packageTests = pf.get('packageTests', packageTests)

        if platform.find('win') > -1:
            codesighs = False

        buildSpace = pf.get('build_space', config['default_build_space'])
        l10nSpace = config['default_l10n_space']
        clobberTime = pf.get('clobber_time', config['default_clobber_time'])
        mochitestLeakThreshold = pf.get('mochitest_leak_threshold', None)
        # -chrome- and -browser- are only used by CCUnittestBuildFactory
        mochichromeLeakThreshold = pf.get('mochichrome_leak_threshold', None)
        mochibrowserLeakThreshold = pf.get('mochibrowser_leak_threshold', None)
        crashtestLeakThreshold = pf.get('crashtest_leak_threshold', None)
        checkTest = pf.get('enable_checktests', False)
        valgrindCheck = pf.get('enable_valgrind_checktests', False)

        extra_args = {}
        if config.get('enable_try'):
            factory_class = TryBuildFactory
            extra_args['packageUrl'] = config['package_url']
            extra_args['packageDir'] = config['package_dir']
            extra_args['branchName'] = name
            uploadSymbols = pf.get('upload_symbols', False)
        else:
            factory_class = CCNightlyBuildFactory
            uploadSymbols = pf.get('upload_symbols', False)

        mozilla2_dep_factory = factory_class(env=pf['env'],
            objdir=pf['platform_objdir'],
            platform=platform,
            hgHost=config['hghost'],
            repoPath=config['repo_path'],
            mozRepoPath=config['mozilla_repo_path'],
            buildToolsRepoPath=config['build_tools_repo_path'],
            configRepoPath=config['config_repo_path'],
            configSubDir=config['config_subdir'],
            profiledBuild=pf['profiled_build'],
            productName=pf['product_name'],
            mozconfig=pf['mozconfig_dep'],
            branchName=name,
            stageServer=config['stage_server'],
            stageUsername=config['stage_username'],
            stageGroup=config['stage_group'],
            stageSshKey=config['stage_ssh_key'],
            stageBasePath=config['stage_base_path'],
            stageLogBaseUrl=config.get('stage_log_base_url', None),
            graphServer=config['graph_server'],
            graphSelector=config['graph_selector'],
            graphBranch=config.get('graph_branch', config['tinderbox_tree']),
            baseName=pf['base_name'],
            leakTest=leakTest,
            checkTest=checkTest,
            valgrindCheck=valgrindCheck,
            codesighs=codesighs,
            uploadPackages=uploadPackages,
            uploadSymbols=uploadSymbols,
            buildSpace=buildSpace,
            clobberURL=config['base_clobber_url'],
            clobberTime=clobberTime,
            buildsBeforeReboot=pf['builds_before_reboot'],
            talosMasters=talosMasters,
            packageTests=packageTests,
            unittestMasters=config['unittest_masters'],
            unittestBranch=unittestBranch,
            tinderboxBuildsDir=tinderboxBuildsDir,
            enable_ccache=pf.get('enable_ccache', False),
            useSharedCheckouts=pf.get('enable_shared_checkouts', False),
            **extra_args
        )
        mozilla2_dep_builder = {
            'name': '%s build' % pf['base_name'],
            'slavenames': pf['slaves'],
            'builddir': '%s-%s' % (name, platform),
            'slavebuilddir': reallyShort('%s-%s' % (name, platform)),
            'factory': mozilla2_dep_factory,
            'category': name,
            'properties': {'branch': name, 'platform': platform, 'slavebuilddir': reallyShort('%s-%s' % (name, platform))},
        }
        branchObjects['builders'].append(mozilla2_dep_builder)

        # skip nightlies for debug builds
        if platform.find('debug') > -1:
            if pf.get('enable_unittests'):
                for suites_name, suites in config['unittest_suites']:
                    if "macosx" in platform and 'mochitest-a11y' in suites:
                        suites = suites[:]
                        suites.remove('mochitest-a11y')

                    if 'opt_base_name' in config['platforms'][platform]:
                        base_name = config['platforms'][platform]['opt_base_name']
                    else:
                        base_name = config['platforms'][platform.replace("-debug", "")]['base_name']

                    branchObjects['builders'].extend(generateCCTestBuilder(
                        config, name, platform, "%s debug test" % base_name,
                        "%s-%s-unittest" % (name, platform),
                        suites_name, suites, mochitestLeakThreshold,
                        crashtestLeakThreshold))
            continue

        if config['enable_nightly']:
            if pf.has_key('enable_nightly'):
                do_nightly = pf['enable_nightly']
            else:
                do_nightly = True
        else:
            do_nightly = False

        if do_nightly:
            nightly_builder = '%s nightly' % pf['base_name']

            triggeredSchedulers=None
            if config['enable_l10n'] and platform in config['l10n_platforms'] and \
               nightly_builder in l10nNightlyBuilders:
                triggeredSchedulers=[l10nNightlyBuilders[nightly_builder]['l10n_builder']]

            mozilla2_nightly_factory = CCNightlyBuildFactory(
                env=pf['env'],
                objdir=pf['platform_objdir'],
                platform=platform,
                hgHost=config['hghost'],
                repoPath=config['repo_path'],
                mozRepoPath=config['mozilla_repo_path'],
                buildToolsRepoPath=config['build_tools_repo_path'],
                configRepoPath=config['config_repo_path'],
                configSubDir=config['config_subdir'],
                profiledBuild=pf['profiled_build'],
                productName=pf['product_name'],
                mozconfig=pf['mozconfig'],
                branchName=name,
                stageServer=config['stage_server'],
                stageUsername=config['stage_username'],
                stageGroup=config['stage_group'],
                stageSshKey=config['stage_ssh_key'],
                stageBasePath=config['stage_base_path'],
                stageLogBaseUrl=config.get('stage_log_base_url', None),
                codesighs=False,
                uploadPackages=uploadPackages,
                uploadSymbols=pf.get('upload_symbols', False),
                nightly=True,
                createSnippet=config['create_snippet'],
                createPartial=config['create_partial'],
                ausBaseUploadDir=config['aus2_base_upload_dir'],
                updatePlatform=pf['update_platform'],
                downloadBaseURL=config['download_base_url'],
                ausUser=config['aus2_user'],
                ausSshKey=config['aus2_ssh_key'],
                ausHost=config['aus2_host'],
                hashType=config['hash_type'],
                buildSpace=buildSpace,
                clobberURL=config['base_clobber_url'],
                clobberTime=clobberTime,
                buildsBeforeReboot=pf['builds_before_reboot'],
                talosMasters=talosMasters,
                packageTests=packageTests,
                unittestMasters=config['unittest_masters'],
                unittestBranch=unittestBranch,
                triggerBuilds=config['enable_l10n'],
                triggeredSchedulers=triggeredSchedulers,
                tinderboxBuildsDir=tinderboxBuildsDir,
                enable_ccache=pf.get('enable_ccache', False),
                useSharedCheckouts=pf.get('enable_shared_checkouts', False),
            )

            mozilla2_nightly_builder = {
                'name': nightly_builder,
                'slavenames': pf['slaves'],
                'builddir': '%s-%s-nightly' % (name, platform),
                'slavebuilddir': reallyShort('%s-%s-nightly' % (name, platform)),
                'factory': mozilla2_nightly_factory,
                'category': name,
                'properties': {'branch': name, 'platform': platform,
                    'nightly_build': True, 'slavebuilddir': reallyShort('%s-%s-nightly' % (name, platform))},
            }
            branchObjects['builders'].append(mozilla2_nightly_builder)

            if config['enable_l10n']:
                if platform in config['l10n_platforms']:
                    # TODO Linux and mac are not working with mozconfig at this point
                    # and this will disable it for now. We will fix this in bug 518359.
                    env = {}
                    objdir = ''
                    mozconfig = None

                    mozilla2_l10n_nightly_factory = CCNightlyRepackFactory(
                        env=env,
                        objdir=objdir,
                        platform=platform,
                        hgHost=config['hghost'],
                        tree=config['l10n_tree'],
                        project=pf['product_name'],
                        appName=pf['app_name'],
                        enUSBinaryURL=config['enUS_binaryURL'],
                        nightly=True,
                        configRepoPath=config['config_repo_path'],
                        configSubDir=config['config_subdir'],
                        mozconfig=mozconfig,
                        branchName=name,
                        l10nNightlyUpdate=config['l10nNightlyUpdate'],
                        l10nDatedDirs=config['l10nDatedDirs'],
                        createPartial=config['create_partial_l10n'],
                        ausBaseUploadDir=config['aus2_base_upload_dir_l10n'],
                        updatePlatform=pf['update_platform'],
                        downloadBaseURL=config['download_base_url'],
                        ausUser=config['aus2_user'],
                        ausSshKey=config['aus2_ssh_key'],
                        ausHost=config['aus2_host'],
                        hashType=config['hash_type'],
                        stageServer=config['stage_server'],
                        stageUsername=config['stage_username'],
                        stageSshKey=config['stage_ssh_key'],
                        repoPath=config['repo_path'],
                        mozRepoPath=config['mozilla_repo_path'],
                        l10nRepoPath=config['l10n_repo_path'],
                        buildToolsRepoPath=config['build_tools_repo_path'],
                        compareLocalesRepoPath=config['compare_locales_repo_path'],
                        compareLocalesTag=config['compare_locales_tag'],
                        buildSpace=l10nSpace,
                        clobberURL=config['base_clobber_url'],
                        clobberTime=clobberTime,
                    )
                    mozilla2_l10n_nightly_builder = {
                        'name': l10nNightlyBuilders[nightly_builder]['l10n_builder'],
                        'slavenames': config['l10n_slaves'][platform],
                        'builddir': '%s-%s-l10n-nightly' % (name, platform),
                        'slavebuilddir': reallyShort('%s-%s-l10n-nightly' % (name, platform)),
                        'factory': mozilla2_l10n_nightly_factory,
                        'category': name,
                        'properties': {'branch': name, 'platform': platform, 'slavebuilddir': reallyShort('%s-%s-l10n-nightly' % (name, platform))},
                    }
                    branchObjects['builders'].append(mozilla2_l10n_nightly_builder)

            if config['enable_shark'] and platform.startswith('macosx'):
                mozilla2_shark_factory = CCNightlyBuildFactory(
                    env= pf['env'],
                    objdir=config['objdir'],
                    platform=platform,
                    stagePlatform=stage_platform,
                    hgHost=config['hghost'],
                    repoPath=config['repo_path'],
                    mozRepoPath=config['mozilla_repo_path'],
                    buildToolsRepoPath=config['build_tools_repo_path'],
                    configRepoPath=config['config_repo_path'],
                    configSubDir=config['config_subdir'],
                    profiledBuild=False,
                    productName=pf['product_name'],
                    mozconfig='%s/%s/shark' % (platform, name),
                    branchName=name,
                    stageServer=config['stage_server'],
                    stageUsername=config['stage_username'],
                    stageGroup=config['stage_group'],
                    stageSshKey=config['stage_ssh_key'],
                    stageBasePath=config['stage_base_path'],
                    stageLogBaseUrl=config.get('stage_log_base_url', None),
                    codesighs=False,
                    uploadPackages=uploadPackages,
                    uploadSymbols=False,
                    nightly=True,
                    createSnippet=False,
                    buildSpace=buildSpace,
                    clobberURL=config['base_clobber_url'],
                    clobberTime=clobberTime,
                    buildsBeforeReboot=pf['builds_before_reboot'],
                    post_upload_include_platform=pf.get('post_upload_include_platform', False),
                )
                mozilla2_shark_builder = {
                    'name': '%s shark' % pf['base_name'],
                    'slavenames': pf['slaves'],
                    'builddir': '%s-%s-shark' % (name, platform),
                    'slavebuilddir': reallyShort('%s-%s-shark' % (name, platform)),
                    'factory': mozilla2_shark_factory,
                    'category': name,
                    'properties': {'branch': name, 'platform': platform, 'slavebuilddir': reallyShort('%s-%s-shark' % (name, platform))},
                }
                branchObjects['builders'].append(mozilla2_shark_builder)

        # We still want l10n_dep builds if nightlies are off
        if config['enable_l10n'] and platform in config['l10n_platforms'] and \
           config['enable_l10n_onchange']:
            mozilla2_l10n_dep_factory = CCNightlyRepackFactory(
                platform=platform,
                hgHost=config['hghost'],
                tree=config['l10n_tree'],
                project=pf['product_name'],
                appName=pf['app_name'],
                enUSBinaryURL=config['enUS_binaryURL'],
                nightly=False,
                branchName=name,
                l10nDatedDirs=config['l10nDatedDirs'],
                stageServer=config['stage_server'],
                stageUsername=config['stage_username'],
                stageSshKey=config['stage_ssh_key'],
                repoPath=config['repo_path'],
                mozRepoPath=config['mozilla_repo_path'],
                l10nRepoPath=config['l10n_repo_path'],
                buildToolsRepoPath=config['build_tools_repo_path'],
                compareLocalesRepoPath=config['compare_locales_repo_path'],
                compareLocalesTag=config['compare_locales_tag'],
                buildSpace=l10nSpace,
                clobberURL=config['base_clobber_url'],
                clobberTime=clobberTime,
            )
            mozilla2_l10n_dep_builder = {
                'name': l10nBuilders[pf['base_name']]['l10n_builder'],
                'slavenames': config['l10n_slaves'][platform],
                'builddir': '%s-%s-l10n-dep' % (name, platform),
                'slavebuilddir': reallyShort('%s-%s-l10n-dep' % (name, platform)),
                'factory': mozilla2_l10n_dep_factory,
                'category': name,
                'properties': {'branch': name, 'platform': platform, 'slavebuilddir': reallyShort('%s-%s-l10n-dep' % (name, platform))},
            }
            branchObjects['builders'].append(mozilla2_l10n_dep_builder)

        if pf.get('enable_unittests'):
            runA11y = True
            if platform.startswith('macosx'):
                runA11y = config['enable_mac_a11y']

            extra_args = {}
            if config.get('enable_try'):
                factory_class = TryUnittestBuildFactory
                extra_args['branchName'] = name
            else:
                factory_class = CCUnittestBuildFactory

            unittest_factory = factory_class(
                env=pf.get('unittest-env', {}),
                platform=platform,
                productName=pf['product_name'],
                branchName=name,
                brandName=pf['brand_name'],
                config_repo_path=config['config_repo_path'],
                config_dir=config['config_subdir'],
                objdir=config['objdir_unittests'],
                hgHost=config['hghost'],
                repoPath=config['repo_path'],
                mozRepoPath=config['mozilla_repo_path'],
                buildToolsRepoPath=config['build_tools_repo_path'],
                buildSpace=config['unittest_build_space'],
                clobberURL=config['base_clobber_url'],
                clobberTime=clobberTime,
                buildsBeforeReboot=pf['builds_before_reboot'],
                exec_xpcshell_suites = config['unittest_exec_xpcshell_suites'],
                exec_reftest_suites = config['unittest_exec_reftest_suites'],
                exec_mochi_suites = config['unittest_exec_mochi_suites'],
                exec_mozmill_suites = config['unittest_exec_mozmill_suites'],
                run_a11y=runA11y,
                mochitest_leak_threshold=mochitestLeakThreshold,
                mochichrome_leak_threshold=mochichromeLeakThreshold,
                mochibrowser_leak_threshold=mochibrowserLeakThreshold,
                crashtest_leak_threshold=crashtestLeakThreshold,
                stageServer=config['stage_server'],
                stageUsername=config['stage_username'],
                stageSshKey=config['stage_ssh_key'],
                unittestMasters=config['unittest_masters'],
                unittestBranch="%s-%s-unittest" % (name, platform),
                uploadPackages=True,
                **extra_args
            )
            unittest_builder = {
                'name': '%s unit test' % pf['base_name'],
                'slavenames': pf['slaves'],
                'builddir': '%s-%s-unittest' % (name, platform),
                'slavebuilddir': reallyShort('%s-%s-unittest' % (name, platform)),
                'factory': unittest_factory,
                'category': name,
                'properties': {'branch': name, 'platform': platform, 'slavebuilddir': reallyShort('%s-%s-unittest' % (name, platform))},
            }
            branchObjects['builders'].append(unittest_builder)

        for suites_name, suites in config['unittest_suites']:
            runA11y = True
            if platform.startswith('macosx'):
                runA11y = config['enable_mac_a11y']

            # For the regular unittest build, run the a11y suite if
            # enable_mac_a11y is set on mac
            if not runA11y and 'mochitest-a11y' in suites:
                suites = suites[:]
                suites.remove('mochitest-a11y')

            if pf.get('enable_unittests'):
                branchObjects['builders'].extend(generateCCTestBuilder(
                    config, name, platform, "%s test" % pf['base_name'],
                    "%s-%s-unittest" % (name, platform),
                    suites_name, suites, mochitestLeakThreshold,
                    crashtestLeakThreshold))

            # Remove mochitest-a11y from other types of builds, since they're not
            # built with a11y enabled
            if platform.startswith("macosx") and 'mochitest-a11y' in suites:
                # Create a new factory that doesn't have mochitest-a11y
                suites = suites[:]
                suites.remove('mochitest-a11y')

            if pf.get('enable_opt_unittests'):
                branchObjects['builders'].extend(generateCCTestBuilder(
                    config, name, platform, "%s opt test" % pf['base_name'],
                    "%s-%s-opt-unittest" % (name, platform),
                    suites_name, suites, mochitestLeakThreshold,
                    crashtestLeakThreshold))

        if config['enable_codecoverage']:
            # We only do code coverage builds on linux right now
            if platform == 'linux':
                codecoverage_factory = CodeCoverageFactory(
                    platform=platform,
                    productName=pf['product_name'],
                    config_repo_path=config['config_repo_path'],
                    config_dir=config['config_subdir'],
                    objdir=config['objdir_unittests'],
                    hgHost=config['hghost'],
                    repoPath=config['repo_path'],
                    buildToolsRepoPath=config['build_tools_repo_path'],
                    buildSpace=5,
                    clobberURL=config['base_clobber_url'],
                    clobberTime=clobberTime,
                    buildsBeforeReboot=pf['builds_before_reboot'],
                    mochitest_leak_threshold=mochitestLeakThreshold,
                    crashtest_leak_threshold=crashtestLeakThreshold,
                    stageServer=config['stage_server'],
                    stageUsername=config['stage_username'],
                    stageSshKey=config['stage_ssh_key'],
                )
                codecoverage_builder = {
                    'name': '%s code coverage' % pf['base_name'],
                    'slavenames': pf['slaves'],
                    'builddir': '%s-%s-codecoverage' % (name, platform),
                    'slavebuilddir': reallyShort('%s-%s-codecoverage' % (name, platform)),
                    'factory': codecoverage_factory,
                    'category': name,
                    'properties': {'branch': name, 'platform': platform, 'slavebuilddir': reallyShort('%s-%s-codecoverage' % (name, platform))},
                }
                branchObjects['builders'].append(codecoverage_builder)

        if config.get('enable_blocklist_update', False):
            if platform == 'linux':
                blocklistBuilder = generateBlocklistBuilder(config, name, platform, pf['base_name'], pf['slaves'])
                branchObjects['builders'].append(blocklistBuilder)

        # -- end of per-platform loop --

    if config['enable_weekly_bundle']:
        bundle_factory = ScriptFactory(
            config['hgurl'] + config['build_tools_repo_path'],
            'scripts/bundle/hg-bundle.sh',
            interpreter='bash',
            script_timeout=3600,
            script_maxtime=3600,
            extra_args=[
                name,
                config['repo_path'],
                config['stage_server'],
                config['stage_username'],
                config['stage_base_path'],
                config['stage_ssh_key'],
                ],
        )
        slaves = set()
        for p in sorted(config['platforms'].keys()):
            slaves.update(set(config['platforms'][p]['slaves']))
        bundle_builder = {
            'name': '%s hg bundle' % name,
            'slavenames': list(slaves),
            'builddir': '%s-bundle' % (name,),
            'slavebuilddir': reallyShort('%s-bundle' % (name,)),
            'factory': bundle_factory,
            'category': name,
            'properties' : { 'slavebuilddir': reallyShort('%s-bundle' % (name,)) }
        }
        branchObjects['builders'].append(bundle_builder)

    return branchObjects


def generateTalosBranchObjects(branch, branch_config, PLATFORMS, SUITES,
        ACTIVE_UNITTEST_PLATFORMS, factory_class=TalosFactory):
    branchObjects = {'schedulers': [], 'builders': [], 'status': [], 'change_source': []}
    branch_builders = {}
    all_test_builders = {}
    all_builders = []
    # prettyNames is a mapping to pass to the try_parser for validation
    prettyNames = {}

    # We only understand a couple PGO strategies
    assert branch_config['pgo_strategy'] in ('per-checkin', 'periodic', None), \
            "%s is not an understood PGO strategy" % branch_config['pgo_strategy']

    buildBranch = branch_config['build_branch']
    talosCmd = branch_config['talos_command']

    for platform, platform_config in PLATFORMS.items():
        if platform_config.get('is_mobile', False):
            branchName = branch_config['mobile_branch_name']
            tinderboxTree = branch_config['mobile_tinderbox_tree']
            talosBranch = branch_config.get('mobile_talos_branch', branch_config['mobile_tinderbox_tree'])
        else:
            branchName = branch_config['branch_name']
            tinderboxTree = branch_config['tinderbox_tree']
            talosBranch = branch_config['tinderbox_tree']

        if tinderboxTree not in branch_builders:
            branch_builders[tinderboxTree] = []
        if tinderboxTree not in all_test_builders:
            all_test_builders[tinderboxTree] = []

        branchProperty = branch

        stage_platform = platform_config.get('stage_platform', platform)
        stage_product = platform_config['stage_product']

        # Decide whether this platform should have PGO builders created
        if branch_config['pgo_strategy'] and platform in branch_config['pgo_platforms']:
            create_pgo_builders = True
        else:
            create_pgo_builders = False

        # if platform is in the branch config check for overriding slave_platforms at the branch level
        # before creating the builders & schedulers
        if branch_config['platforms'].get(platform):
            slave_platforms = branch_config['platforms'][platform].get('slave_platforms', platform_config.get('slave_platforms', []))

            # Map of # of test runs to builder names
            talos_builders = {}
            talos_pgo_builders = {}

            for slave_platform in slave_platforms:
                platform_name = platform_config[slave_platform]['name']
                # this is to handle how a platform has more than one slave platform
                if prettyNames.has_key(platform):
                    prettyNames[platform].append(platform_name)
                else:
                    prettyNames[platform] = [platform_name]
                for suite, talosConfig in SUITES.items():
                    tests, merge, extra, platforms = branch_config['%s_tests' % suite]
                    if tests == 0 or slave_platform not in platforms:
                        continue

                    # We only want to append '-Non-PGO' to platforms that
                    # also have PGO builds.
                    if create_pgo_builders:
                        opt_branch_name = branchName + '-Non-PGO'
                        opt_talos_branch = talosBranch + '-Non-PGO'
                    else:
                        opt_branch_name = branchName
                        opt_talos_branch = talosBranch

                    factory_kwargs = {
                        "OS": slave_platform.split('-')[0],
                        "supportUrlBase": branch_config['support_url_base'],
                        "envName": platform_config['env_name'],
                        "workdirBase": "../talos-data",
                        "buildBranch": buildBranch,
                        "branchName": opt_branch_name,
                        "branch": branch,
                        "talosBranch": opt_talos_branch,
                        "configOptions": talosConfig,
                        "talosCmd": talosCmd,
                        "fetchSymbols": branch_config['fetch_symbols'] and
                          platform_config[slave_platform].get('download_symbols',True),
                        "talos_from_source_code": branch_config.get('talos_from_source_code', False)
                    }

                    if extra and extra.get('remoteTests', False) and 'xul' in platform:
                        myextra      = deepcopy(extra)
                        remoteExtras = myextra.get('remoteExtras', {})
                        reOptions    = remoteExtras.get('options', [])
                        reOptions.append('--nativeUI')
                        remoteExtras['options'] = reOptions
                        myextra['remoteExtras'] = remoteExtras
                        factory_kwargs.update(myextra)
                    else:
                        factory_kwargs.update(extra)

                    builddir = "%s_%s_test-%s" % (branch, slave_platform, suite)
                    slavebuilddir= 'test'
                    factory = factory_class(**factory_kwargs)
                    builder = {
                        'name': "%s %s talos %s" % (platform_name, branch, suite),
                        'slavenames': platform_config[slave_platform]['slaves'],
                        'builddir': builddir,
                        'slavebuilddir': slavebuilddir,
                        'factory': factory,
                        'category': branch,
                        'properties': {
                            'branch': branchProperty,
                            'platform': slave_platform,
                            'stage_platform': stage_platform,
                            'product': stage_product,
                            'builddir': builddir,
                            'slavebuilddir': slavebuilddir,
                        },
                    }

                    if not merge:
                        nomergeBuilders.append(builder['name'])

                    talos_builders.setdefault(tests, []).append(builder['name'])
                    branchObjects['builders'].append(builder)
                    branch_builders[tinderboxTree].append(builder['name'])
                    all_builders.append(builder['name'])

                    if create_pgo_builders:
                        pgo_factory_kwargs = factory_kwargs.copy()
                        pgo_factory_kwargs['branchName'] = branchName
                        pgo_factory_kwargs['talosBranch'] = talosBranch
                        pgo_factory = factory_class(**pgo_factory_kwargs)
                        pgo_builder = {
                            'name': "%s %s pgo talos %s" % (platform_name, branch, suite),
                            'slavenames': platform_config[slave_platform]['slaves'],
                            'builddir': builddir + '-pgo',
                            'slavebuilddir': slavebuilddir + '-pgo',
                            'factory': pgo_factory,
                            'category': branch,
                            'properties': {
                                'branch': branchProperty,
                                'platform': slave_platform,
                                'stage_platform': stage_platform + '-pgo',
                                'product': stage_product,
                                'builddir': builddir,
                                'slavebuilddir': slavebuilddir,
                            },
                        }

                        if not merge:
                            nomergeBuilders.append(pgo_builder['name'])
                        branchObjects['builders'].append(pgo_builder)
                        talos_pgo_builders.setdefault(tests, []).append(pgo_builder['name'])
                        branch_builders[tinderboxTree].append(pgo_builder['name'])
                        all_builders.append(pgo_builder['name'])


                if platform in ACTIVE_UNITTEST_PLATFORMS.keys() and branch_config.get('enable_unittests', True):
                    testTypes = []
                    # unittestSuites are gathered up for each platform from config.py
                    unittestSuites = []
                    if branch_config['platforms'][platform].get('enable_opt_unittests'):
                        testTypes.append('opt')
                    if branch_config['platforms'][platform].get('enable_debug_unittests'):
                        testTypes.append('debug')
                    if branch_config['platforms'][platform].get('enable_mobile_unittests'):
                        testTypes.append('mobile')

                    merge_tests = branch_config.get('enable_merging', True)

                    for test_type in testTypes:
                        test_builders = []
                        pgo_builders = []
                        triggeredUnittestBuilders = []
                        pgoUnittestBuilders = []
                        unittest_suites = "%s_unittest_suites" % test_type
                        if test_type == "debug":
                            slave_platform_name = "%s-debug" % slave_platform
                        elif test_type == "mobile":
                            slave_platform_name = "%s-mobile" % slave_platform
                        else:
                            slave_platform_name = slave_platform

                        # create builder names for TinderboxMailNotifier
                        for suites_name, suites in branch_config['platforms'][platform][slave_platform][unittest_suites]:
                            test_builders.extend(generateTestBuilderNames(
                                '%s %s %s test' % (platform_name, branch, test_type), suites_name, suites))
                            if create_pgo_builders and test_type == 'opt':
                                pgo_builders.extend(generateTestBuilderNames(
                                '%s %s pgo test' % (platform_name, branch), suites_name, suites))
                        # Collect test builders for the TinderboxMailNotifier
                        all_test_builders[tinderboxTree].extend(test_builders + pgo_builders)
                        all_builders.extend(test_builders + pgo_builders)

                        triggeredUnittestBuilders.append(('tests-%s-%s-%s-unittest' % (branch, slave_platform, test_type),
                                                         test_builders, merge_tests))
                        if create_pgo_builders and test_type == 'opt':
                            pgoUnittestBuilders.append(('tests-%s-%s-pgo-unittest' % (branch, slave_platform),
                                                       pgo_builders, merge_tests))

                        for suites_name, suites in branch_config['platforms'][platform][slave_platform][unittest_suites]:
                            # create the builders
                            test_builder_kwargs = {
                                "config": branch_config,
                                "branch_name": branch,
                                "platform": platform,
                                "name_prefix": "%s %s %s test" % (platform_name, branch, test_type),
                                "build_dir_prefix": "%s_%s_test" % (branch, slave_platform_name),
                                "suites_name": suites_name,
                                "suites": suites,
                                "mochitestLeakThreshold": branch_config.get('mochitest_leak_threshold', None),
                                "crashtestLeakThreshold": branch_config.get('crashtest_leak_threshold', None),
                                "slaves": platform_config[slave_platform]['slaves'],
                                "resetHwClock": branch_config['platforms'][platform][slave_platform].get('reset_hw_clock', False),
                                "stagePlatform": stage_platform,
                                "stageProduct": stage_product
                            }
                            if isinstance(suites, dict) and "mozharness_repo" in suites:
                                test_builder_kwargs['mozharness'] = True
                                test_builder_kwargs['mozharness_python'] = platform_config['mozharness_python']
                            branchObjects['builders'].extend(generateTestBuilder(**test_builder_kwargs))
                            if create_pgo_builders and test_type == 'opt':
                                pgo_builder_kwargs = test_builder_kwargs.copy()
                                pgo_builder_kwargs['name_prefix'] = "%s %s pgo test" % (platform_name, branch)
                                pgo_builder_kwargs['build_dir_prefix'] += '_pgo'
                                pgo_builder_kwargs['stagePlatform'] += '-pgo'
                                branchObjects['builders'].extend(generateTestBuilder(**pgo_builder_kwargs))

                        for scheduler_name, test_builders, merge in triggeredUnittestBuilders:
                            for test in test_builders:
                                unittestSuites.append(test.split(' ')[-1])
                            scheduler_branch = ('%s-%s-%s-unittest' % (branch, platform, test_type))
                            if not merge:
                                nomergeBuilders.extend(test_builders)
                            extra_args = {}
                            if branch == "try":
                                scheduler_class = BuilderChooserScheduler
                                extra_args['chooserFunc'] = tryChooser
                                extra_args['numberOfBuildsToTrigger'] = 1
                                extra_args['prettyNames'] = prettyNames
                                extra_args['unittestSuites'] = unittestSuites
                            else:
                                scheduler_class = Scheduler
                            branchObjects['schedulers'].append(scheduler_class(
                                name=scheduler_name,
                                branch=scheduler_branch,
                                builderNames=test_builders,
                                treeStableTimer=None,
                                **extra_args
                            ))
                        for scheduler_name, test_builders, merge in pgoUnittestBuilders:
                            for test in test_builders:
                                unittestSuites.append(test.split(' ')[-1])
                            scheduler_branch = '%s-%s-pgo-unittest' % (branch, platform)
                            if not merge:
                                nomergeBuilders.extend(pgo_builders)
                            extra_args = {}
                            if branch == "try":
                                scheduler_class = BuilderChooserScheduler
                                extra_args['chooserFunc'] = tryChooser
                                extra_args['numberOfBuildsToTrigger'] = 1
                                extra_args['prettyNames'] = prettyNames
                                extra_args['unittestSuites'] = unittestSuites
                            else:
                                scheduler_class = Scheduler
                            branchObjects['schedulers'].append(scheduler_class(
                                name=scheduler_name,
                                branch=scheduler_branch,
                                builderNames=pgo_builders,
                                treeStableTimer=None,
                                **extra_args
                            ))

            # Create one scheduler per # of tests to run
            for tests, builder_names in talos_builders.items():
                extra_args = {}
                if tests == 1:
                    scheduler_class = Scheduler
                    name='tests-%s-%s-talos' % (branch, platform)
                else:
                    scheduler_class = MultiScheduler
                    name='tests-%s-%s-talos-x%s' % (branch, platform, tests)
                    extra_args['numberOfBuildsToTrigger'] = tests

                if branch == "try":
                    scheduler_class = BuilderChooserScheduler
                    extra_args['chooserFunc'] = tryChooser
                    extra_args['prettyNames'] = prettyNames
                    extra_args['talosSuites'] = SUITES.keys()
                    extra_args['numberOfBuildsToTrigger'] = tests

                s = scheduler_class(
                        name=name,
                        branch='%s-%s-talos' % (branch, platform),
                        treeStableTimer=None,
                        builderNames=builder_names,
                        **extra_args
                        )
                branchObjects['schedulers'].append(s)
            # PGO Schedulers
            for tests, builder_names in talos_pgo_builders.items():
                extra_args = {}
                if tests == 1:
                    scheduler_class = Scheduler
                    name='tests-%s-%s-pgo-talos' % (branch, platform)
                else:
                    scheduler_class = MultiScheduler
                    name='tests-%s-%s-pgo-talos-x%s' % (branch, platform, tests)
                    extra_args['numberOfBuildsToTrigger'] = tests

                if branch == "try":
                    scheduler_class = BuilderChooserScheduler
                    extra_args['chooserFunc'] = tryChooser
                    extra_args['prettyNames'] = prettyNames
                    extra_args['talosSuites'] = SUITES.keys()
                    extra_args['numberOfBuildsToTrigger'] = tests

                s = scheduler_class(
                        name=name,
                        branch='%s-%s-pgo-talos' % (branch, platform),
                        treeStableTimer=None,
                        builderNames=builder_names,
                        **extra_args
                        )
                branchObjects['schedulers'].append(s)

    if not branch_config.get('disable_tinderbox_mail'):
        for tinderboxTree in branch_builders.keys():
            if len(branch_builders[tinderboxTree]):
                branchObjects['status'].append(TinderboxMailNotifier(
                               fromaddr="talos.buildbot@build.mozilla.org",
                               tree=tinderboxTree,
                               extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org",],
                               relayhost="mail.build.mozilla.org",
                               builders=branch_builders[tinderboxTree],
                               useChangeTime=False,
                               logCompression="gzip"))
        ###  Unittests need specific errorparser
        for tinderboxTree in all_test_builders.keys():
            if len(all_test_builders[tinderboxTree]):
                branchObjects['status'].append(TinderboxMailNotifier(
                               fromaddr="talos.buildbot@build.mozilla.org",
                               tree=tinderboxTree,
                               extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org",],
                               relayhost="mail.build.mozilla.org",
                               builders=all_test_builders[tinderboxTree],
                               useChangeTime=False,
                               errorparser="unittest",
                               logCompression="gzip"))

    logUploadCmd = makeLogUploadCommand(branch, branch_config,
            is_try=bool(branch=='try'),
            is_shadow=bool(branch=='shadow-central'),
            platform_prop='stage_platform',
            product_prop='product')

    branchObjects['status'].append(QueuedCommandHandler(
        logUploadCmd,
        QueueDir.getQueue('commands'),
        builders=all_builders,
    ))

    if branch_config.get('release_tests'):
        releaseObjects = generateTalosReleaseBranchObjects(branch,
                branch_config, PLATFORMS, SUITES, ACTIVE_UNITTEST_PLATFORMS, factory_class)
        for k,v in releaseObjects.items():
            branchObjects[k].extend(v)
    return branchObjects

def generateTalosReleaseBranchObjects(branch, branch_config, PLATFORMS, SUITES,
        ACTIVE_UNITTEST_PLATFORMS, factory_class=TalosFactory):
    branch_config = branch_config.copy()
    release_tests = branch_config['release_tests']

    # Update the # of tests to run with our release_tests number
    # Force no merging
    for suite, talosConfig in SUITES.items():
        tests, merge, extra, platforms = branch_config['%s_tests' % suite]
        if tests > 0:
            branch_config['%s_tests' % suite] = (release_tests, False, extra, platforms)


    # Update the TinderboxTree and the branch_name
    branch_config['tinderbox_tree'] += '-Release'
    branch_config['branch_name'] += '-Release'
    branch = "release-" + branch

    # Remove the release_tests key so we don't call ourselves again
    del branch_config['release_tests']

    # Don't fetch symbols
    branch_config['fetch_symbols'] = branch_config['fetch_release_symbols']
    return generateTalosBranchObjects(branch, branch_config, PLATFORMS, SUITES,
        ACTIVE_UNITTEST_PLATFORMS, factory_class)


def generateBlocklistBuilder(config, branch_name, platform, base_name, slaves) :
    pf = config['platforms'].get(platform, {})
    extra_args = ['-b', config['repo_path']]
    if pf['product_name'] is not None:
        extra_args.extend(['-p', pf['product_name']])
    if config['hg_username'] is not None:
        extra_args.extend(['-u', config['hg_username']])
    if config['hg_ssh_key'] is not None:
        extra_args.extend(['-k', config['hg_ssh_key']])
    if config['blocklist_update_on_closed_tree'] is True:
        extra_args.extend(['-c'])
    blocklistupdate_factory = ScriptFactory(
        "%s%s" % (config['hgurl'],
        config['build_tools_repo_path']),
        'scripts/blocklist/sync-hg-blocklist.sh',
        interpreter='bash',
        extra_args=extra_args,
    )
    blocklistupdate_builder = {
        'name': '%s blocklist update' % base_name,
        'slavenames': slaves,
        'builddir': '%s-%s-blocklistupdate' % (branch_name, platform),
        'slavebuilddir': reallyShort('%s-%s-blocklistupdate' % (branch_name, platform)),
        'factory': blocklistupdate_factory,
        'category': branch_name,
        'properties': {'branch': branch_name, 'platform': platform, 'slavebuilddir': reallyShort('%s-%s-blocklistupdate' % (branch_name, platform))},
    }
    return blocklistupdate_builder

def generateFuzzingObjects(config, SLAVES):
    builders = []
    f = ScriptFactory(
            config['scripts_repo'],
            'scripts/fuzzing/fuzzer.sh',
            interpreter='bash',
            script_timeout=1500,
            script_maxtime=1800,
            )
    for platform in config['platforms']:
        env = MozillaEnvironments.get("%s-unittest" % platform, {}).copy()
        env['HG_REPO'] = config['fuzzing_repo']
        env['FUZZ_REMOTE_HOST'] = config['fuzzing_remote_host']
        env['FUZZ_BASE_DIR'] = config['fuzzing_base_dir']
        builder = {'name': 'fuzzer-%s' % platform,
                   'builddir': 'fuzzer-%s' % platform,
                   'slavenames': SLAVES[platform],
                   'nextSlave': _nextSlowIdleSlave(config['idle_slaves']),
                   'factory': f,
                   'category': 'idle',
                   'env': env,
                  }
        builders.append(builder)
        nomergeBuilders.append(builder)
    fuzzing_scheduler = PersistentScheduler(
            name="fuzzer",
            builderNames=[b['name'] for b in builders],
            numPending=2,
            pollInterval=300, # Check every 5 minutes
        )
    return {
            'builders': builders,
            'schedulers': [fuzzing_scheduler],
            }

def generateNanojitObjects(config, SLAVES):
    builders = []
    branch = os.path.basename(config['repo_path'])

    for platform in config['platforms']:
        if 'win' in platform:
            slaves = SLAVES[platform]
            nanojit_script = 'scripts/nanojit/nanojit.sh'
            interpreter = 'bash'
        elif 'arm' in platform:
            slaves = SLAVES['linux']
            nanojit_script = '/builds/slave/nanojit-arm/scripts/scripts/nanojit/nanojit.sh'
            interpreter = ['/scratchbox/moz_scratchbox', '-d', '/builds/slave/nanojit-arm']
        else:
            slaves = SLAVES[platform]
            nanojit_script = 'scripts/nanojit/nanojit.sh'
            interpreter = None

        f = ScriptFactory(
                config['scripts_repo'],
                nanojit_script,
                interpreter=interpreter,
                log_eval_func=rc_eval_func({1: WARNINGS}),
                )

        builder = {'name': 'nanojit-%s' % platform,
                   'builddir': 'nanojit-%s' % platform,
                   'slavenames': slaves,
                   'nextSlave': _nextSlowIdleSlave(config['idle_slaves']),
                   'factory': f,
                   'category': 'idle',
                   'properties': {'branch': branch},
                  }
        builders.append(builder)
        nomergeBuilders.append(builder)

    # Set up polling
    poller = HgPoller(
            hgURL=config['hgurl'],
            branch=config['repo_path'],
            pollInterval=5*60,
            )

    # Set up scheduler
    scheduler = Scheduler(
            name="nanojit",
            branch=config['repo_path'],
            treeStableTimer=None,
            builderNames=[b['name'] for b in builders],
            )

    # Tinderbox notifier
    status = []
    if not config.get("disable_tinderbox_mail"):
        tbox_mailer = TinderboxMailNotifier(
            fromaddr="mozilla2.buildbot@build.mozilla.org",
            tree=config['tinderbox_tree'],
            extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
            relayhost="mail.build.mozilla.org",
            builders=[b['name'] for b in builders],
            logCompression="gzip",
        )
        status = [tbox_mailer]

    return {
            'builders': builders,
            'change_source': [poller],
            'schedulers': [scheduler],
            'status': status,
            }

def generateSpiderMonkeyObjects(config, SLAVES):
    builders = []
    branch = os.path.basename(config['repo_path'])

    for platform, variants in config['platforms'].items():
        base_platform = platform.split('-', 1)[0]
        if 'win' in platform:
            slaves = SLAVES[base_platform]
            interpreter = 'bash'
        elif 'arm' in platform:
            slaves = SLAVES['linux']
            interpreter = ['/scratchbox/moz_scratchbox', '-d',
                    '/builds/slave/%s' % reallyShort('%s_%s_spidermonkey-%s' % (branch, platform, variant))]
        else:
            slaves = SLAVES[base_platform]
            interpreter = None

        env = config['env'][platform].copy()
        env['HG_REPO'] = config['hgurl'] + config['repo_path']

        for variant in variants:
            f = ScriptFactory(
                    config['scripts_repo'],
                    'scripts/spidermonkey_builds/spidermonkey.sh',
                    interpreter=interpreter,
                    log_eval_func=rc_eval_func({1: WARNINGS}),
                    extra_args=(variant,),
                    script_timeout=3600,
                    )

            builder = {'name': '%s_%s_spidermonkey-%s' % (branch, platform, variant),
                    'builddir': '%s_%s_spidermonkey-%s' % (branch, platform, variant),
                    'slavebuilddir': reallyShort('%s_%s_spidermonkey-%s' % (branch, platform, variant)),
                    'slavenames': slaves,
                    'nextSlave': _nextSlowIdleSlave(config['idle_slaves']),
                    'factory': f,
                    'category': branch,
                    'env': env,
                    'properties': {'branch': branch},
                    }
            builders.append(builder)

    def isImportant(change):
        for f in change.files:
            if f.startswith("js/src"):
                return True
        return False

    # Set up scheduler
    scheduler = Scheduler(
            name="%s_spidermonkey" % branch,
            branch=config['repo_path'],
            treeStableTimer=None,
            builderNames=[b['name'] for b in builders],
            fileIsImportant=isImportant,
            )

    # Tinderbox notifier
    status = []
    if not config.get("disable_tinderbox_mail"):
        tbox_mailer = TinderboxMailNotifier(
            fromaddr="mozilla2.buildbot@build.mozilla.org",
            tree=config['tinderbox_tree'],
            extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
            relayhost="mail.build.mozilla.org",
            builders=[b['name'] for b in builders],
            logCompression="gzip",
            errorparser="unittest"
        )
        status = [tbox_mailer]

    return {
            'builders': builders,
            'schedulers': [scheduler],
            'status': status,
            }

def generateJetpackObjects(config, SLAVES):
    builders = []
    project_branch = os.path.basename(config['repo_path'])
    for branch in config['branches']:
        for platform in config['platforms'].keys():
            slaves = SLAVES[platform]
            jetpackTarball = "%s/%s/%s" % (config['hgurl'] , config['repo_path'], config['jetpack_tarball'])
            ftp_url = config['ftp_url']
            types = ['opt','debug']
            for type in types:
                if type == 'debug':
                    ftp_url = ftp_url + "-debug"
                f = ScriptFactory(
                        config['scripts_repo'],
                        'buildfarm/utils/run_jetpack.py',
                        extra_args=("-p", platform, "-t", jetpackTarball, "-b", branch,
                                   "-f", ftp_url, "-e", config['platforms'][platform]['ext'],),
                        interpreter='python',
                        log_eval_func=rc_eval_func({1: WARNINGS}),
                        )
    
                builder = {'name': 'jetpack-%s-%s-%s' % (branch, platform, type),
                           'builddir': 'jetpack-%s-%s-%s' % (branch, platform, type),
                           'slavebuilddir': 'test',
                           'slavenames': slaves,
                           'factory': f,
                           'category': 'jetpack',
                           'properties': {'branch': project_branch},
                           'env': MozillaEnvironments.get("%s" % config['platforms'][platform].get('env'), {}).copy(),
                          }
                builders.append(builder)
                nomergeBuilders.append(builder)

    # Set up polling
    poller = HgPoller(
            hgURL=config['hgurl'],
            branch=config['repo_path'],
            pollInterval=5*60,
            )

    # Set up scheduler
    scheduler = Scheduler(
            name="jetpack",
            branch=config['repo_path'],
            treeStableTimer=None,
            builderNames=[b['name'] for b in builders],
            )

    # Tinderbox notifier
    status = []
    if not config.get("disable_tinderbox_mail"):
        tbox_mailer = TinderboxMailNotifier(
            fromaddr="mozilla2.buildbot@build.mozilla.org",
            tree=config['tinderbox_tree'],
            extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org"],
            relayhost="mail.build.mozilla.org",
            builders=[b['name'] for b in builders],
            logCompression="gzip",
        )
        status = [tbox_mailer]

    return {
            'builders': builders,
            'change_source': [poller],
            'schedulers': [scheduler],
            'status': status,
            }

def generateProjectObjects(project, config, SLAVES):
    builders = []
    schedulers = []
    change_sources = []
    status = []
    buildObjects = {
            'builders': builders,
            'schedulers': schedulers,
            'status': status,
            'change_source': change_sources,
            }

    # Fuzzing
    if project.startswith('fuzzing'):
        fuzzingObjects = generateFuzzingObjects(config, SLAVES)
        buildObjects = mergeBuildObjects(buildObjects, fuzzingObjects)

    # Nanojit
    elif project == 'nanojit':
        nanojitObjects = generateNanojitObjects(config, SLAVES)
        buildObjects = mergeBuildObjects(buildObjects, nanojitObjects)

    # Jetpack
    elif project.startswith('jetpack'):
        jetpackObjects = generateJetpackObjects(config, SLAVES)
        buildObjects = mergeBuildObjects(buildObjects, jetpackObjects)

    # Spidermonkey
    elif project.startswith('spidermonkey'):
        spiderMonkeyObjects = generateSpiderMonkeyObjects(config, SLAVES)
        buildObjects = mergeBuildObjects(buildObjects, spiderMonkeyObjects)

    return buildObjects

def makeLogUploadCommand(branch_name, config, is_try=False, is_shadow=False,
        platform_prop="platform", product_prop=None, product=None):
    extra_args = []
    if config.get('enable_mail_notifier'):
        if config.get('notify_real_author'):
            extraRecipients = []
            sendToAuthor = True
        else:
            extraRecipients = config['email_override']
            sendToAuthor = False

        upload_cmd = 'try_mailer.py'
        extra_args.extend(['-f', 'tryserver@build.mozilla.org'])
        for r in extraRecipients:
            extra_args.extend(['-t', r])
        if sendToAuthor:
            extra_args.append("--to-author")
    else:
        upload_cmd = 'log_uploader.py'

    logUploadCmd = [sys.executable,
         '%s/bin/%s' % (buildbotcustom.__path__[0], upload_cmd),
         config['stage_server'],
         '-u', config['stage_username'],
         '-i', os.path.expanduser("~/.ssh/%s" % config['stage_ssh_key']),
         '-b', branch_name,
         ]

    if platform_prop:
        logUploadCmd += ['-p', WithProperties("%%(%s)s" % platform_prop)]
    logUploadCmd += extra_args

    if product_prop:
        logUploadCmd += ['--product', WithProperties("%%(%s)s" % product_prop)]
        assert not product, 'dont specify static value when using property'
    elif product:
        logUploadCmd.extend(['--product', product])

    if is_try:
        logUploadCmd.append('--try')

    if is_shadow:
        logUploadCmd.append('--shadow')

    return logUploadCmd
