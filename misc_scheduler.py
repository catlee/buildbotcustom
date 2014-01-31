# Additional Scheduler functions
# Contributor(s):
#   Chris AtLee <catlee@mozilla.com>
#   Lukas Blakk <lsblakk@mozilla.com>
import re
import time
from twisted.python import log
from twisted.internet import defer
from twisted.web.client import getPage

from buildbot.sourcestamp import SourceStamp

import buildbotcustom.try_parser
reload(buildbotcustom.try_parser)

from buildbotcustom.try_parser import TryParser
from buildbotcustom.common import genBuildID, genBuildUID, incrementBuildID

from buildbot.process.properties import Properties
from buildbot.util import json


def tryChooser(s, all_changes):
    log.msg("Looking at changes: %s" % all_changes)

    buildersPerChange = {}

    dl = []

    def getJSON(data):
        push = json.loads(data)
        log.msg("Looking at the push json data for try comments")
        for p in push:
            pd = push[p]
            changes = pd['changesets']
            for change in reversed(changes):
                match = re.search("try:", change['desc'])
                if match:
                    return change['desc'].encode("utf8", "replace")

    def parseData(comments, c):
        if not comments:
            # still need to parse a comment string to get the default set
            log.msg("No comments, passing empty string which will result in default set")
            comments = ""
        customBuilders = TryParser(
            comments, s.builderNames, s.prettyNames, s.unittestPrettyNames,
            s.unittestSuites, s.talosSuites, s.buildbotBranch, s.buildersWithSetsMap)
        buildersPerChange[c] = customBuilders

    def parseDataError(failure, c):
        log.msg(
            "Couldn't parse data: Requesting default try set. %s" % failure)
        parseData("", c)

    for c in all_changes:
        try:
            match = re.search("try", c.branch)
            if not match:
                log.msg("Ignoring off-branch %s" % c.branch)
                continue
            # Look in comments first for try: syntax
            match = re.search("try:", c.comments)
            if match:
                log.msg("Found try message in the change comments, ignoring push comments")
                d = defer.succeed(c.comments)
            # otherwise getPage from hg.m.o
            else:
                d = getPage(str("https://hg.mozilla.org/try/json-pushes?full=1&changeset=%s" % c.revision))
                d.addCallback(getJSON)
        except:
            log.msg("Error in all_changes loop: sending default try set")
            d = defer.succeed("")
        d.addCallback(parseData, c)
        d.addErrback(parseDataError, c)
        dl.append(d)
    d = defer.DeferredList(dl)
    d.addCallback(lambda res: buildersPerChange)
    return d


def buildIDSchedFunc(sched, t, ssid):
    """Generates a unique buildid for this change.

    Returns a Properties instance with 'buildid' set to the buildid to use.

    scheduler `sched`'s state is modified as a result."""
    state = sched.get_state(t)

    # Get the last buildid we scheduled from the database
    lastid = state.get('last_buildid', '19700101000000')

    incrementedid = incrementBuildID(lastid)
    nowid = genBuildID()

    # Our new buildid will be the highest of the last buildid incremented or
    # the buildid based on the current date
    newid = str(max(int(nowid), int(incrementedid)))

    # Save it in the scheduler's state so we don't generate the same one again.
    state['last_buildid'] = newid
    sched.set_state(t, state)

    props = Properties()
    props.setProperty('buildid', newid, 'buildIDSchedFunc')
    return props


def buildUIDSchedFunc(sched, t, ssid):
    """Return a Properties instance with 'builduid' set to a randomly generated
    id."""
    props = Properties()
    props.setProperty('builduid', genBuildUID(), 'buildUIDSchedFunc')
    return props

# A version of changeEventGenerator that can be used within a db connector
# thread.  Copied from buildbot/db/connector.py.


def changeEventGeneratorInTransaction(dbconn, t, branches=[],
                                      categories=[], committers=[], minTime=0):
    q = "SELECT changeid FROM changes"
    args = []
    if branches or categories or committers:
        q += " WHERE "
        pieces = []
        if branches:
            pieces.append("branch IN %s" % dbconn.parmlist(len(branches)))
            args.extend(list(branches))
        if categories:
            pieces.append("category IN %s" % dbconn.parmlist(len(categories)))
            args.extend(list(categories))
        if committers:
            pieces.append("author IN %s" % dbconn.parmlist(len(committers)))
            args.extend(list(committers))
        if minTime:
            pieces.append("when_timestamp > %d" % minTime)
        q += " AND ".join(pieces)
    q += " ORDER BY changeid DESC"
    t.execute(q, tuple(args))
    for (changeid,) in t.fetchall():
        yield dbconn._txn_getChangeNumberedNow(t, changeid)


def lastChange(db, t, branch):
    """Returns the revision for the last changeset on the given branch"""
    #### NOTE: called in a thread!
    for c in changeEventGeneratorInTransaction(db, t, branches=[branch]):
        # Ignore DONTBUILD changes
        if c.comments and "DONTBUILD" in c.comments:
            continue
        # Ignore changes which didn't come from the poller
        if not c.revlink:
            continue
        return c
    return None


def lastGoodRev(db, t, branch, builderNames, starttime, endtime):
    """Returns the revision for the latest green build among builders.  If no
    revision is all green, None is returned."""

    # Get a list of branch, revision, buildername tuples from builds on
    # `branch` that completed successfully or with warnings within [starttime,
    # endtime] (a closed interval)
    q = db.quoteq("""SELECT branch, revision, buildername FROM
                sourcestamps,
                buildsets,
                buildrequests

            WHERE
                buildsets.sourcestampid = sourcestamps.id AND
                buildrequests.buildsetid = buildsets.id AND
                buildrequests.complete = 1 AND
                buildrequests.results IN (0,1) AND
                sourcestamps.revision IS NOT NULL AND
                buildrequests.buildername in %s AND
                sourcestamps.branch = ? AND
                buildrequests.complete_at >= ? AND
                buildrequests.complete_at <= ?

            ORDER BY
                buildsets.id DESC
        """ % db.parmlist(len(builderNames)))
    t.execute(q, tuple(builderNames) + (branch, starttime, endtime))
    builds = t.fetchall()

    builderNames = set(builderNames)

    # Map of (branch, revision) to set of builders that passed
    good_sourcestamps = {}

    # Go through the results and group them by branch,revision.
    # When we find a revision where all our required builders are listed, we've
    # found a good revision!
    count = 0
    for (branch, revision, name) in builds:
        count += 1
        key = branch, revision
        good_sourcestamps.setdefault(key, set()).add(name)

        if good_sourcestamps[key] == builderNames:
            # Looks like a winner!
            log.msg("lastGood: ss %s good for everyone!" % (key,))
            log.msg("lastGood: looked at %i builds" % count)
            return revision
    return None


def getLatestRev(db, t, branch, revs):
    """Returns whichever of revs has the latest when_timestamp"""
    # Strip out duplicates
    short_revs = set(r[:12] for r in revs)
    if len(short_revs) == 1:
        return list(revs)[0]

    if 'sqlite' in db._spec.dbapiName:
        rev_clause = " OR ".join(["revision LIKE (? || '%')"] * len(short_revs))
    else:
        rev_clause = " OR ".join(["revision LIKE CONCAT(?, '%%')"] * len(short_revs))

    # Get the when_timestamp for these two revisions
    q = db.quoteq("""SELECT revision FROM changes
                     WHERE
                        branch = ? AND
                        (%s)
                     ORDER BY
                        when_timestamp DESC
                     LIMIT 1""" % rev_clause)

    t.execute(q, (branch,) + tuple(short_revs))
    latest = t.fetchone()[0]
    log.msg("getLatestRev: %s is latest of %s" % (latest, revs))
    return latest


def getLastBuiltRevisions(db, t, branch, builderNames, limit=5):
    """Returns the latest revision that was built on builderNames"""
    # Find the latest revision we built on any one of builderNames.
    q = db.quoteq("""SELECT sourcestamps.revision FROM
                buildrequests, buildsets, sourcestamps
            WHERE
                buildrequests.buildsetid = buildsets.id AND
                buildsets.sourcestampid = sourcestamps.id AND
                sourcestamps.branch = ? AND
                buildrequests.buildername IN %s
            ORDER BY
                buildsets.submitted_at DESC
            LIMIT ?""" % db.parmlist(len(builderNames)))

    t.execute(q, (branch,) + tuple(builderNames) + (limit,))
    retval = []
    for row in t.fetchall():
        retval.append(row[0])
    return retval


def lastGoodFunc(branch, builderNames, triggerBuildIfNoChanges=True, l10nBranch=None):
    """Returns a function that returns the latest revision on branch that was
    green for all builders in builderNames.

    If unable to find an all green build, fall back to the latest known
    revision on this branch, or the tip of the default branch if we don't know
    anything about this branch.

    Also check that we don't schedule a build for a revision that is older that
    the latest revision built on the scheduler's builders.
    """
    def ssFunc(scheduler, t):
        #### NOTE: called in a thread!
        db = scheduler.parent.db

        # Look back 24 hours for a good revision to build
        start = time.time()
        rev = lastGoodRev(
            db, t, branch, builderNames, start - (24 * 3600), start)
        end = time.time()
        log.msg("lastGoodRev: took %.2f seconds to run; returned %s" %
                (end - start, rev))

        if rev is None:
            # Check if there are any recent l10n changes
            if l10nBranch:
                lastL10nChange = lastChange(db, t, l10nBranch)
                if lastL10nChange:
                    lastL10nChange = lastL10nChange.when
                else:
                    lastL10nChange = 0
            else:
                lastL10nChange = 0

            # If there are no recent l10n changes, and we don't want to trigger
            # builds if nothing has changed in the past 24 hours, then return
            # None, indicating that no build should be scheduled
            if not triggerBuildIfNoChanges:
                if l10nBranch:
                    if (start - lastL10nChange) > (24 * 3600):
                        return None
                else:
                    return None

            # Couldn't find a good revision.  Fall back to using the latest
            # revision on this branch
            c = lastChange(db, t, branch)
            if c:
                rev = c.revision
            log.msg("lastChange returned %s" % (rev))

        # Find the last revisions our scheduler's builders have built.  This can
        # include forced builds.
        last_built_revs = getLastBuiltRevisions(db, t, branch,
                                                scheduler.builderNames)
        log.msg("lastNightlyRevisions: %s" % last_built_revs)

        if last_built_revs:
            # Make sure that rev is newer than the last revision we built.
            later_rev = getLatestRev(db, t, branch, [rev] + last_built_revs)
            if later_rev != rev:
                log.msg("lastGoodRev: Building %s since it's newer than %s" %
                        (later_rev, rev))
                rev = later_rev
        return SourceStamp(branch=scheduler.branch, revision=rev)
    return ssFunc


def lastRevFunc(branch, triggerBuildIfNoChanges=True):
    """Returns a function that returns the latest revision on branch."""
    def ssFunc(scheduler, t):
        #### NOTE: called in a thread!
        db = scheduler.parent.db

        c = lastChange(db, t, branch)
        if not c:
            return None

        rev = c.revision
        log.msg("lastChange returned %s" % (rev))

        # Find the last revisions our scheduler's builders have built.  This can
        # include forced builds.
        last_built_revs = getLastBuiltRevisions(db, t, branch,
                                                scheduler.builderNames)
        log.msg("lastBuiltRevisions: %s" % last_built_revs)

        if last_built_revs:
            # Make sure that rev is newer than the last revision we built.
            later_rev = getLatestRev(db, t, branch, [rev] + last_built_revs)
            if later_rev in last_built_revs and not triggerBuildIfNoChanges:
                log.msg("lastGoodRev: Skipping %s since we've already built it" % rev)
                return None
        return SourceStamp(branch=scheduler.branch, revision=rev)
    return ssFunc
