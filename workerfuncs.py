from functools import wraps
from twisted.python import log
import random
import inspect
from buildbot.util import now

import buildbotcustom.jacuzzi
reload(buildbotcustom.jacuzzi)
from buildbotcustom.jacuzzi import JacuzziAllocator

J = JacuzziAllocator()

def _getLastTimeOnBuilder(builder, slavename):
    # New builds are at the end of the buildCache, so
    # examine it backwards
    buildNumbers = reversed(sorted(builder.builder_status.buildCache.keys()))
    for buildNumber in buildNumbers:
        try:
            build = builder.builder_status.buildCache[buildNumber]
            # Skip non-successful builds
            if build.getResults() != 0:
                continue
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


def safeNextSlave(func):
    """Wrapper around nextSlave functions that catch exceptions , log them, and
    choose a random slave instead"""
    @wraps(func)
    def _nextSlave(builder, available_slaves):
        try:
            return func(builder, available_slaves)
        except Exception:
            log.msg("Error choosing next slave for builder '%s', choosing"
                    " randomly instead" % builder.name)
            log.err()
            if available_slaves:
                return random.choice(available_slaves)
            return None
    return _nextSlave


def _get_pending(builder):
    """Returns the pending build requests for this builder"""
    frame = inspect.currentframe()
    # Walk up the stack until we find 't', a db transaction object. It allows
    # us to make synchronous calls to the db from this thread.
    # We need to commit this horrible crime because
    # a) we're running in a thread
    # b) so we can't use the db's existing sync query methods since they use a
    # db connection created in another thread
    # c) nor can we use deferreds (threads and deferreds don't play well
    # together)
    # d) there's no other way to get a db connection
    while 't' not in frame.f_locals:
        frame = frame.f_back
    t = frame.f_locals['t']
    del frame

    return builder._getBuildable(t, None)


def is_spot(name):
    return "-spot-" in name


def _classifyAWSSlaves(slaves):
    """
    Partitions slaves into three groups: inhouse, ondemand, spot according to
    their name. Returns three lists:
        inhouse, ondemand, spot
    """
    inhouse = []
    ondemand = []
    spot = []
    for s in slaves:
        if not s.slave:
            continue
        name = s.slave.slavename
        if is_spot(name):
            spot.append(s)
        elif 'ec2' in name:
            ondemand.append(s)
        else:
            inhouse.append(s)

    return inhouse, ondemand, spot


def _nextAWSSlave(aws_wait=None, recentSort=False):
    """
    Returns a nextSlave function that pick the next available slave, with some
    special consideration for AWS instances:
        - If the request is very new, wait for an inhouse instance to pick it
          up. Set aws_wait to the number of seconds to wait before using an AWS
          instance. Set to None to disable this behaviour.

        - Otherwise give the job to a spot instance

    If recentSort is True then pick slaves that most recently did this type of
    build. Otherwise pick randomly.

    """
    log.msg("nextAWSSlave: start")

    if recentSort:
        def sorter(slaves, builder):
            if not slaves:
                return None
            return sorted(slaves, _recentSort(builder))[-1]
    else:
        def sorter(slaves, builder):
            if not slaves:
                return None
            return random.choice(slaves)

    def _nextSlave(builder, available_slaves):
        # Partition the slaves into 3 groups:
        # - inhouse slaves
        # - ondemand slaves
        # - spot slaves
        # We always prefer to run on inhouse. We'll wait up to aws_wait
        # seconds for one to show up!

        # Easy! If there are no available slaves, don't return any!
        if not available_slaves:
            return None

        inhouse, ondemand, spot = _classifyAWSSlaves(available_slaves)

        # Always prefer inhouse slaves
        if inhouse:
            log.msg("nextAWSSlave: Choosing inhouse because it's the best!")
            return sorter(inhouse, builder)

        # We need to look at our build requests if we need to know # of
        # retries, or if we're going to be waiting for an inhouse slave to come
        # online.
        if aws_wait or spot:
            requests = _get_pending(builder)
            if requests:
                oldestRequestTime = sorted(requests, key=lambda r:
                                           r.submittedAt)[0].submittedAt
            else:
                oldestRequestTime = 0

        if aws_wait and now() - oldestRequestTime < aws_wait:
            log.msg("nextAWSSlave: Waiting for inhouse slaves to show up")
            return None

        if spot:
            log.msg("nextAWSSlave: Choosing spot since there aren't any retries")
            return sorter(spot, builder)
        elif ondemand:
            log.msg("nextAWSSlave: Choosing ondemand since there aren't any spot available")
            return sorter(ondemand, builder)
        else:
            log.msg("nextAWSSlave: No slaves - returning None")
            return None
    return _nextSlave

_nextAWSSlave_sort = safeNextSlave(J(_nextAWSSlave(aws_wait=0, recentSort=True)))
_nextAWSSlave_nowait = safeNextSlave(_nextAWSSlave())


@safeNextSlave
def _nextSlave(builder, available_slaves):
    # Choose the slave that was most recently on this builder
    if available_slaves:
        return sorted(available_slaves, _recentSort(builder))[-1]
    else:
        return None


def _nextIdleSlave(nReserved):
    """Return a nextSlave function that will only return a slave to run a build
    if there are at least nReserved slaves available."""
    @safeNextSlave
    @J
    def _nextslave(builder, available_slaves):
        if len(available_slaves) <= nReserved:
            return None
        return sorted(available_slaves, _recentSort(builder))[-1]
    return _nextslave

