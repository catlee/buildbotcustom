# Mozilla schedulers
# Based heavily on buildbot.scheduler
# Contributor(s):
#   Chris AtLee <catlee@mozilla.com>

from twisted.internet import defer, reactor
from twisted.python import log
from twisted.internet.task import LoopingCall
from twisted.web.client import getPage

from buildbot.scheduler import Scheduler
from buildbot.schedulers.base import BaseScheduler
from buildbot.schedulers.timed import Nightly
from buildbot.schedulers.triggerable import Triggerable
from buildbot.sourcestamp import SourceStamp
from buildbot.process.properties import Properties
from buildbot.status.builder import SUCCESS, WARNINGS

from buildbot.util import now

import util.tuxedo
reload(util.tuxedo)
from util.tuxedo import get_release_uptake

import time


class SpecificNightly(Nightly):
    """Subclass of regular Nightly scheduler that allows you to specify a function
    that gets called to generate a sourcestamp
    """
    def __init__(self, ssFunc, *args, **kwargs):
        self.ssFunc = ssFunc

        Nightly.__init__(self, *args, **kwargs)

    def start_HEAD_build(self, t):
        """
        Slightly mis-named, but this function is called when it's time to start
        a build.  We call our ssFunc to get a sourcestamp to build.

        ssFunc is called in a thread with an active database transaction
        running.  It cannot use Deferreds, nor any db.*Now methods.
        """
        #### NOTE: called in a thread!
        ss = self.ssFunc(self, t)

        # if our function returns None, don't create any build
        if ss is None:
            log.msg("%s: No sourcestamp returned from ssfunc; not scheduling a build" % self.name)
            return
        log.msg("%s: Creating buildset with sourcestamp %s" % (
            self.name, ss.getText()))
        db = self.parent.db
        ssid = db.get_sourcestampid(ss, t)
        self.create_buildset(ssid, self.reason, t)


class PersistentScheduler(BaseScheduler):
    """Make sure at least numPending builds are pending on each of builderNames"""

    compare_attrs = ['name', 'numPending', 'pollInterval', 'ssFunc',
                     'builderNames', 'properties']

    def __init__(self, numPending, pollInterval=60, ssFunc=None, properties={},
                 **kwargs):
        self.numPending = numPending
        self.pollInterval = pollInterval
        self.lastCheck = 0
        if ssFunc is None:
            self.ssFunc = self._default_ssFunc
        else:
            self.ssFunc = ssFunc

        BaseScheduler.__init__(self, properties=properties, **kwargs)

    def _default_ssFunc(self, builderName):
        return SourceStamp()

    def run(self):
        if self.lastCheck + self.pollInterval > now():
            # Try again later
            return (self.lastCheck + self.pollInterval + 1)

        db = self.parent.db
        to_create = []
        for builderName in self.builderNames:
            n = len(db.get_pending_brids_for_builder(builderName))
            num_to_create = self.numPending - n
            if num_to_create <= 0:
                continue
            to_create.append((builderName, num_to_create))

        d = db.runInteraction(lambda t: self.create_builds(to_create, t))
        return d

    def create_builds(self, to_create, t):
        db = self.parent.db
        for builderName, count in to_create:
            ss = self.ssFunc(builderName)
            ssid = db.get_sourcestampid(ss, t)
            for i in range(0, count):
                self.create_buildset(
                    ssid, "scheduler", t, builderNames=[builderName])

        # Try again in a bit
        self.lastCheck = now()
        return now() + self.pollInterval


class BuilderChooserScheduler(Scheduler):
    compare_attrs = Scheduler.compare_attrs + (
        'chooserFunc', 'prettyNames',
        'unittestPrettyNames', 'unittestSuites', 'talosSuites', 'buildbotBranch', 'buildersWithSetsMap')

    def __init__(
        self, chooserFunc, prettyNames=None, unittestPrettyNames=None, unittestSuites=None,
            talosSuites=None, buildbotBranch=None, buildersWithSetsMap=None, **kwargs):
        self.chooserFunc = chooserFunc
        self.prettyNames = prettyNames
        self.unittestPrettyNames = unittestPrettyNames
        self.unittestSuites = unittestSuites
        self.talosSuites = talosSuites
        self.buildbotBranch = buildbotBranch
        self.buildersWithSetsMap = buildersWithSetsMap
        Scheduler.__init__(self, **kwargs)

    def run(self):
        db = self.parent.db
        d = db.runInteraction(self.classify_changes)
        d.addCallback(lambda ign: db.runInteraction(self._process_changes))
        d.addCallback(self._maybeRunChooser)
        return d

    def _process_changes(self, t):
        db = self.parent.db
        res = db.scheduler_get_classified_changes(self.schedulerid, t)
        (important, unimportant) = res
        return self._checkTreeStableTimer(important, unimportant)

    def _checkTreeStableTimer(self, important, unimportant):
        """Look at the changes that need to be processed and decide whether
        to queue a BuildRequest or sleep until something changes.

        If I decide that a build should be performed, I will return the list of
        changes to be built.

        If the treeStableTimer has not elapsed, I will return the amount of
        time to wait before trying again.

        Otherwise I will return None.
        """

        if not important:
            # Don't do anything
            return None
        all_changes = important + unimportant
        most_recent = max([c.when for c in all_changes])
        if self.treeStableTimer is not None:
            now = time.time()
            stable_at = most_recent + self.treeStableTimer
            if stable_at > now:
                # Wake up one second late, to avoid waking up too early and
                # looping a lot.
                return stable_at + 1.0

        # ok, do a build for these changes
        return all_changes

    def _maybeRunChooser(self, res):
        if res is None:
            return None
        elif isinstance(res, (int, float)):
            return res
        else:
            assert isinstance(res, list)
            return self._runChooser(res)

    def _runChooser(self, all_changes):
        # Figure out which builders to run
        d = defer.maybeDeferred(self.chooserFunc, self, all_changes)

        def do_add_build_and_remove_changes(t, buildersPerChange):
            log.msg("Adding request for %s" % buildersPerChange)
            if not buildersPerChange:
                return

            db = self.parent.db
            if self.treeStableTimer is None:
                # each Change gets a separate build
                for c in all_changes:
                    if c not in buildersPerChange:
                        continue
                    ss = SourceStamp(changes=[c])
                    ssid = db.get_sourcestampid(ss, t)
                    self.create_buildset(ssid, "scheduler", t, builderNames=buildersPerChange[c])
            else:
                # Grab all builders
                builderNames = set()
                for names in buildersPerChange.values():
                    builderNames.update(names)
                builderNames = list(builderNames)
                ss = SourceStamp(changes=all_changes)
                ssid = db.get_sourcestampid(ss, t)
                self.create_buildset(
                    ssid, "scheduler", t, builderNames=builderNames)

            # and finally retire the changes from scheduler_changes
            changeids = [c.number for c in all_changes]
            db.scheduler_retire_changes(self.schedulerid, changeids, t)
            return None

        d.addCallback(lambda buildersPerChange: self.parent.db.runInteraction(
            do_add_build_and_remove_changes, buildersPerChange))
        return d


class TriggerBouncerCheck(Triggerable):

    compare_attrs = Triggerable.compare_attrs + \
        ('minUptake', 'configRepo', 'checkMARs', 'username',
         'password', 'pollInterval', 'pollTimeout')
    working = False
    loop = None
    release_config = None
    script_repo_revision = None
    configRepo = None

    def __init__(self, minUptake, configRepo, checkMARs=True,
                 username=None, password=None, pollInterval=5 * 60,
                 pollTimeout=12 * 60 * 60, appendBuildNumber=False,
                 checkInstallers=True, **kwargs):
        self.minUptake = minUptake
        self.configRepo = configRepo
        self.checkMARs = checkMARs
        self.checkInstallers = checkInstallers
        self.username = username
        self.password = password
        self.pollInterval = pollInterval
        self.pollTimeout = pollTimeout
        self.ss = None
        self.set_props = None
        self.appendBuildNumber = appendBuildNumber
        Triggerable.__init__(self, **kwargs)

    def trigger(self, ss, set_props=None):
        self.ss = ss
        self.set_props = set_props

        props = Properties()
        props.updateFromProperties(self.properties)
        if set_props:
            props.updateFromProperties(set_props)

        self.script_repo_revision = props.getProperty('script_repo_revision')
        assert self.script_repo_revision, 'script_repo_revision should be set'
        self.release_config = props.getProperty('release_config')
        assert self.release_config, 'release_config should be set'

        def _run_loop(_):
            self.loop = LoopingCall(self.poll)
            reactor.callLater(0, self.loop.start, self.pollInterval)
            reactor.callLater(self.pollTimeout, self.stopLoop,
                              'Timeout after %s' % self.pollTimeout)

        d = self.getReleaseConfig()
        d.addCallback(_run_loop)

    def stopLoop(self, reason=None):
        if reason:
            log.msg('%s: Stopping uptake monitoring: %s' %
                    (self.__class__.__name__, reason))
        if self.loop.running:
            self.loop.stop()
        else:
            log.msg('%s: Loop has been alredy stopped' %
                    self.__class__.__name__)

    def getReleaseConfig(self):
        url = str('%s/raw-file/%s/%s' %
                  (self.configRepo, self.script_repo_revision, self.release_config))
        d = getPage(url)

        def setReleaseConfig(res):
            c = {}
            exec res in c
            self.release_config = c.get('releaseConfig')
            log.msg('%s: release_config loaded' % self.__class__.__name__)

        d.addCallback(setReleaseConfig)
        return d

    def poll(self):
        if self.working:
            log.msg('%s: Not polling because last poll is still working'
                    % self.__class__.__name__)
            return defer.succeed(None)
        self.working = True
        log.msg('%s: polling' % self.__class__.__name__)
        bouncerProductName = self.release_config.get('productName').capitalize()
        version = self.release_config.get('version')
        partialVersions = []
        if self.appendBuildNumber:
            version += 'build%s' % self.release_config.get('buildNumber')
            for partialVersion, info in self.release_config.get("partialUpdates").iteritems():
                partialVersions.append("%sbuild%s" % (partialVersion, info["buildNumber"]))
        if not partialVersions:
            partialVersions = self.release_config.get("partialUpdates").keys()
        d = get_release_uptake(
            tuxedoServerUrl=self.release_config.get('tuxedoServerUrl'),
            bouncerProductName=bouncerProductName,
            version=version,
            platforms=self.release_config.get('enUSPlatforms'),
            partialVersions=partialVersions,
            checkMARs=self.checkMARs,
            checkInstallers=self.checkInstallers,
            username=self.username,
            password=self.password)
        d.addCallback(self.checkUptake)
        d.addCallbacks(self.finished_ok, self.finished_failure)
        return d

    def checkUptake(self, uptake):
        log.msg('%s: uptake is %s' % (self.__class__.__name__, uptake))
        if uptake >= self.minUptake:
            self.stopLoop('Reached required uptake: %s' % uptake)
            Triggerable.trigger(self, self.ss, self.set_props)

    def finished_ok(self, res):
        log.msg('%s: polling finished' % (self.__class__.__name__))
        assert self.working
        self.working = False
        return res

    def finished_failure(self, f):
        log.msg('%s failed:\n%s' % (self.__class__.__name__, f.getTraceback()))
        assert self.working
        self.working = False
        return None  # eat the failure


class AggregatingScheduler(BaseScheduler, Triggerable):
    """This scheduler waits until at least one build of each of
    `upstreamBuilders` completes with a result in `okResults`. Once this
    happens, it triggers builds on `builderNames` with `properties` set.
    Use trigger() method to reset its state.

    `okResults` should be a tuple of acceptable result codes, and defaults to
    (SUCCESS,WARNINGS)."""

    compare_attrs = ('name', 'branch', 'builderNames', 'properties',
                     'upstreamBuilders', 'okResults', 'enable_service')

    def __init__(self, name, branch, builderNames, upstreamBuilders,
                 okResults=(SUCCESS, WARNINGS), properties={}):
        BaseScheduler.__init__(self, name, builderNames, properties)
        self.branch = branch
        self.lock = defer.DeferredLock()
        assert isinstance(upstreamBuilders, (list, tuple))
        self.upstreamBuilders = upstreamBuilders
        self.reason = "AggregatingScheduler(%s)" % name
        self.okResults = okResults
        self.log_prefix = '%s(%s) <id=%s>' % (self.__class__.__name__, name,
                                              id(self))

        # Set this to False to disable the service component of this scheduler
        self.enable_service = True

    def get_initial_state(self, max_changeid):
        log.msg('%s: get_initial_state()' % self.log_prefix)
        # Keep initial state of builders in upstreamBuilders
        # and operate on remainingBuilders to simplify comparison
        # on reconfig
        return {
            "upstreamBuilders": self.upstreamBuilders,
            "remainingBuilders": self.upstreamBuilders,
            "lastCheck": now(),
            "lastReset": now(),
        }

    def startService(self):
        if not self.enable_service:
            return
        self.parent.db.runInteractionNow(self._startService)
        BaseScheduler.startService(self)

    def _startService(self, t):
        state = self.get_state(t)
        old_state = state.copy()
        # Remove deleted/renamed upstream builders to prevent undead schedulers
        for b in list(state['remainingBuilders']):
            if b not in self.upstreamBuilders:
                state['remainingBuilders'].remove(b)
        # Add new upstream builders. New builders shouln't be in
        # state['upstreamBuilders'] which contains old self.upstreamBuilders.
        # Since state['upstreamBuilders'] was introduced after
        # state['remainingBuilders'], it may be absent from the scheduler
        # database.
        for b in self.upstreamBuilders:
            if b not in state.get('upstreamBuilders', []) and \
               b not in state['remainingBuilders']:
                state['remainingBuilders'].append(b)
        state['upstreamBuilders'] = self.upstreamBuilders
        # Previous implentations of AggregatingScheduler didn't always set
        # lastReset. We depend on it now in _run(), so make sure it's set to
        # something
        if 'lastReset' not in state:
            state['lastReset'] = state['lastCheck']
        log.msg('%s: reloaded' % self.log_prefix)
        if old_state != state:
            log.msg('%s: old state: %s' % (self.log_prefix, old_state))
            log.msg('%s: new state: %s' % (self.log_prefix, state))
        self.set_state(t, state)

    def trigger(self, ss, set_props=None):
        """Reset scheduler state"""
        d = self.lock.acquire()
        d.addCallback(
            lambda _: self.parent.db.runInteractionNow(self._trigger))
        d.addBoth(lambda _: self.lock.release())

    def _trigger(self, t):
        state = self.get_initial_state(None)
        state['lastReset'] = state['lastCheck']
        log.msg('%s: reset state: %s' % (self.log_prefix, state))
        self.set_state(t, state)

    def run(self):
        if not self.enable_service:
            return

        if self.lock.locked:
            return

        d = self.lock.acquire()
        d.addCallback(lambda _: self.parent.db.runInteraction(self._run))

        def release(_):
            self.lock.release()
            return _

        d.addBoth(release)
        return d

    def findNewBuilds(self, db, t, lastCheck, lastReset):
        q = """SELECT buildername, id, complete_at FROM
               buildrequests WHERE
               buildername IN %s AND
               buildrequests.complete = 1 AND
               buildrequests.results IN %s AND
               buildrequests.complete_at > ?
            """ % (
            db.parmlist(len(self.upstreamBuilders)),
            db.parmlist(len(self.okResults)),
        )
        q = db.quoteq(q)

        # Take any builds that have finished from the later of 60 seconds before our
        # lastCheck time, or lastReset. Sometimes the SQL updates for finished
        # builds appear out of order according to complete_at. This can be due
        # to clock skew on the masters, network lag, etc.
        # lastCheck is the time of the last build that finished that we're
        # watching. Offset by 60 seconds in the past to make sure we catch
        # builds that finished around the same time, but whose updates arrived
        # to the DB later.
        # Don't look at builds before we last reset.
        # c.f. bug 811708
        cutoff = max(lastCheck - 60, lastReset)
        t.execute(q, tuple(self.upstreamBuilders) + tuple(self.okResults) +
                  (cutoff,))
        newBuilds = t.fetchall()
        if newBuilds:
            log.msg(
                '%s: new builds: %s since %s (lastCheck: %s, lastReset: %s)' %
                (self.log_prefix, newBuilds, cutoff, lastCheck, lastReset))
        return newBuilds

    def _run(self, t):
        db = self.parent.db
        state = self.get_state(t)
        # Check for new builds completed since lastCheck
        lastCheck = state['lastCheck']
        lastReset = state['lastReset']
        remainingBuilders = state['remainingBuilders']

        newBuilds = self.findNewBuilds(db, t, lastCheck, lastReset)

        for builder, brid, complete_at in newBuilds:
            state['lastCheck'] = max(state['lastCheck'], complete_at)
            if builder in remainingBuilders:
                remainingBuilders.remove(builder)

        lastCheck = state['lastCheck']

        if remainingBuilders:
            state['remainingBuilders'] = remainingBuilders
        else:
            ss = SourceStamp(branch=self.branch)
            ssid = db.get_sourcestampid(ss, t)

            # Start a build!
            log.msg(
                '%s: new buildset: branch=%s, ssid=%s, builders: %s'
                % (self.log_prefix, self.branch, ssid,
                   ', '.join(self.builderNames)))
            self.create_buildset(ssid, "downstream", t)

            # Reset the list of builders we're waiting for
            state = self.get_initial_state(None)
            state['lastCheck'] = lastCheck

        self.set_state(t, state)


def makePropertiesScheduler(base_class, propfuncs, *args, **kw):
    """Return a subclass of `base_class` that will call each of `propfuncs` to
    generate a set of properties to attach to new buildsets.

    Each function of propfuncs will be passed (scheduler instance, db
    transaction, sourcestamp id) and must return a Properties instance.  These
    properties will be added to any new buildsets this scheduler creates."""
    pf = propfuncs

    class S(base_class):
        compare_attrs = base_class.compare_attrs + ('propfuncs',)
        propfuncs = pf

        def create_buildset(self, ssid, reason, t, props=None, builderNames=None):
            # We need a fresh set of properties each time since we expect to update
            # the properties below
            my_props = Properties()
            if props is None:
                my_props.updateFromProperties(self.properties)
            else:
                my_props.updateFromProperties(props)

            # Update with our prop functions
            try:
                for func in propfuncs:
                    try:
                        request_props = func(self, t, ssid)
                        log.msg("%s: propfunc returned %s" %
                                (self.name, request_props))
                        my_props.updateFromProperties(request_props)
                    except:
                        log.msg("Error running %s" % func)
                        log.err()
            except:
                log.msg("%s: error calculating properties" % self.name)
                log.err()

            # Call our base class's original, with our new properties.
            return base_class.create_buildset(self, ssid, reason, t, my_props, builderNames)

    # Copy the original class' name so that buildbot's ComparableMixin works
    S.__name__ = base_class.__name__ + "-props"

    return S


class EveryNthScheduler(Scheduler):
    """
    Triggers jobs every Nth change, or after idleTimeout seconds have elapsed
    since the most recent change. Set idleTimeout to None to wait forever for n changes.
    """

    compare_attrs = Scheduler.compare_attrs + ('n', 'idleTimeout')

    def __init__(self, name, n, idleTimeout=None, **kwargs):
        self.n = n
        self.idleTimeout = idleTimeout

        Scheduler.__init__(self, name=name, **kwargs)

    def decide_and_remove_changes(self, t, important, unimportant):
        """
        Based on Scheduler.decide_and_remove_changes.

        If we have n or more important changes, we should trigger jobs.

        If more than idleTimeout has elapsed since the last change, we should trigger jobs.
        """
        if not important:
            return None

        nImportant = len(important)
        if nImportant < self.n:
            if not self.idleTimeout:
                log.msg("%s: skipping with %i/%i important changes since no idle timeout" %
                        (self.name, nImportant, self.n))
                return

            oldest = min([c.when for c in important])
            elapsed = int(now() - oldest)

            if self.idleTimeout and elapsed < self.idleTimeout:
                # Haven't hit the timeout yet, so let's wait more
                log.msg("%s: skipping with %i/%i important changes since only %i/%is have elapsed" %
                        (self.name, nImportant, self.n, elapsed, self.idleTimeout))
                return now() + (self.idleTimeout - elapsed)
            log.msg("%s: triggering with %i/%i important changes since %is have elapsed" % (self.name, nImportant, self.n, elapsed))
        else:
            log.msg("%s: triggering since we have %i/%i important changes" % (self.name, nImportant, self.n))

        return Scheduler.decide_and_remove_changes(self, t, important, unimportant)
