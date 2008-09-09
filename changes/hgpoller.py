import time
from calendar import timegm
from xml.dom import minidom, Node

from twisted.python import log, failure
from twisted.internet import defer, reactor
from twisted.internet.task import LoopingCall
from twisted.web.client import getPage

from buildbot.changes import base, changes

class ThrottleGetPage(object):
    """A helper class which can limit the number of simultaneous calls to
    getPage"""

    def __init__(self, limit, timeout=120):
        """
        @param limit   The number of simultaneous connections allowed
        @param timeout A timeout passed to the underlying getPage method
        """
        self.limit = limit
        self.timeout = timeout

        # This will be a list of (url, deferred)
        self.list = []

    def getPage(self, url):
        d = defer.Deferred()
        d.addBoth(self._finish)
        self.list.append((url, d))
        self._go()
        return d

    def _finish(self, r):
        self.limit += 1
        self._go()
        return r

    def _go(self):
        if len(self.list) and self.limit > 0:
            url, d = self.list.pop(0)
            getd = getPage(url, timeout=self.timeout)
            getd.chainDeferred(d)
            self.limit -= 1

pollThrottler = ThrottleGetPage(4)

# From pyiso8601 module,
#  http://code.google.com/p/pyiso8601/source/browse/trunk/iso8601/iso8601.py
#   Revision 22

# Required license header:

# Copyright (c) 2007 Michael Twomey
# 
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
# 
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""ISO 8601 date time string parsing

Basic usage:
>>> import iso8601
>>> iso8601.parse_date("2007-01-25T12:00:00Z")
datetime.datetime(2007, 1, 25, 12, 0, tzinfo=<iso8601.iso8601.Utc ...>)
>>>

"""

from datetime import datetime, timedelta, tzinfo
import re

__all__ = ["parse_date", "ParseError"]

# Adapted from http://delete.me.uk/2005/03/iso8601.html
ISO8601_REGEX = re.compile(r"(?P<year>[0-9]{4})(-(?P<month>[0-9]{1,2})(-(?P<day>[0-9]{1,2})"
    r"((?P<separator>.)(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})(:(?P<second>[0-9]{2})(\.(?P<fraction>[0-9]+))?)?"
    r"(?P<timezone>Z|(([-+])([0-9]{2}):([0-9]{2})))?)?)?)?"
)
TIMEZONE_REGEX = re.compile("(?P<prefix>[+-])(?P<hours>[0-9]{2}).(?P<minutes>[0-9]{2})")

class ParseError(Exception):
    """Raised when there is a problem parsing a date string"""

# Yoinked from python docs
ZERO = timedelta(0)
class Utc(tzinfo):
    """UTC
    
    """
    def utcoffset(self, dt):
        return ZERO

    def tzname(self, dt):
        return "UTC"

    def dst(self, dt):
        return ZERO
UTC = Utc()

class FixedOffset(tzinfo):
    """Fixed offset in hours and minutes from UTC
    
    """
    def __init__(self, offset_hours, offset_minutes, name):
        self.__offset = timedelta(hours=offset_hours, minutes=offset_minutes)
        self.__name = name

    def utcoffset(self, dt):
        return self.__offset

    def tzname(self, dt):
        return self.__name

    def dst(self, dt):
        return ZERO
    
    def __repr__(self):
        return "<FixedOffset %r>" % self.__name

def parse_timezone(tzstring, default_timezone=UTC):
    """Parses ISO 8601 time zone specs into tzinfo offsets
    
    """
    if tzstring == "Z":
        return default_timezone
    # This isn't strictly correct, but it's common to encounter dates without
    # timezones so I'll assume the default (which defaults to UTC).
    # Addresses issue 4.
    if tzstring is None:
        return default_timezone
    m = TIMEZONE_REGEX.match(tzstring)
    prefix, hours, minutes = m.groups()
    hours, minutes = int(hours), int(minutes)
    if prefix == "-":
        hours = -hours
        minutes = -minutes
    return FixedOffset(hours, minutes, tzstring)

def parse_date(datestring, default_timezone=UTC):
    """Parses ISO 8601 dates into datetime objects
    
    The timezone is parsed from the date string. However it is quite common to
    have dates without a timezone (not strictly correct). In this case the
    default timezone specified in default_timezone is used. This is UTC by
    default.
    """
    if not isinstance(datestring, basestring):
        raise ParseError("Expecting a string %r" % datestring)
    m = ISO8601_REGEX.match(datestring)
    if not m:
        raise ParseError("Unable to parse date string %r" % datestring)
    groups = m.groupdict()
    tz = parse_timezone(groups["timezone"], default_timezone=default_timezone)
    if groups["fraction"] is None:
        groups["fraction"] = 0
    else:
        groups["fraction"] = int(float("0.%s" % groups["fraction"]) * 1e6)
    return datetime(int(groups["year"]), int(groups["month"]), int(groups["day"]),
        int(groups["hour"]), int(groups["minute"]), int(groups["second"]),
        int(groups["fraction"]), tz)

# End of iso8601.py

def parse_date_string(dateString):
    return timegm(parse_date(dateString).utctimetuple())

def _parse_changes(query, lastChange):
    dom = minidom.parseString(query)

    items = dom.getElementsByTagName("entry")
    changes = []
    for i in items:
        d = {}
        for k in ["title", "updated"]:
            d[k] = i.getElementsByTagName(k)[0].firstChild.wholeText
        d["updated"] = parse_date_string(d["updated"])
        d["changeset"] = d["title"].split(" ")[1]
        nameNode = i.getElementsByTagName("author")[0].childNodes[1]
        d["author"] = nameNode.firstChild.wholeText
        d["link"] = i.getElementsByTagName("link")[0].getAttribute("href")
        if d["updated"] > lastChange:
            changes.append(d)
    changes.reverse() # want them in chronological order
    return changes
    
class BaseHgPoller(object):
    """Common base of HgPoller, HgLocalePoller, and HgAllLocalesPoller.

    Subclasses should implement getData, processData, and __str__"""
    working = False
    
    def poll(self):
        if self.working:
            log.msg("Not polling %s because last poll is still working" % self)
        else:
            self.working = True
            d = self.getData()
            d.addCallback(self.processData)
            d.addCallbacks(self.dataFinished, self.dataFailed)

    def dataFinished(self, res):
        assert self.working
        self.working = False
        return res

    def dataFailed(self, res):
        assert self.working
        self.working = False
        log.msg("%s: polling failed, result %s" % (self, res))
        return None

class HgPoller(base.ChangeSource, BaseHgPoller):
    """This source will poll a Mercurial server over HTTP using
    the built-in RSS feed for changes and submit them to the
    change master."""

    compare_attrs = ['hgURL', 'branch', 'pollInterval']
    parent = None
    loop = None
    volatile = ['loop']
    
    def __init__(self, hgURL, branch, pushlogUrlOverride=None,
                 tipsOnly=False, pollInterval=30):
        """
        @type   hgURL:          string
        @param  hgURL:          The base URL of the Hg repo
                                (e.g. http://hg.mozilla.org/)
        @type   branch:         string
        @param  branch:         The branch to check (e.g. mozilla-central)
        @type   pollInterval:   int
        @param  pollInterval:   The time (in seconds) between queries for
                                changes
        @type  tipsOnly:        bool
        @param tipsOnly:        Make the pushlog only show the tips of pushes.
                                With this enabled every push will only show up
                                as *one* changeset
        """
        
        self.hgURL = hgURL
        self.branch = branch
        self.pushlogUrlOverride = pushlogUrlOverride
        self.tipsOnly = tipsOnly
        self.pollInterval = pollInterval
        self.lastChange = time.time()

    def startService(self):
        self.loop = LoopingCall(self.poll)
        base.ChangeSource.startService(self)
        reactor.callLater(0, self.loop.start, self.pollInterval)

    def stopService(self):
        self.loop.stop()
        return base.ChangeSource.stopService(self)
    
    def describe(self):
        return "Getting changes from: %s" % self._make_url()
    
    def _make_url(self):
        url = None
        if self.pushlogUrlOverride:
            url = self.pushlogUrlOverride
        else:
            url = "%s%s/pushlog" % (self.hgURL, self.branch)

        if self.tipsOnly:
            url += '?tipsonly=1'

        return url
    

    def getData(self):
        url = self._make_url()
        log.msg("Polling Hg server at %s" % url)
        return pollThrottler.getPage(url)

    def processData(self, query):
        change_list = _parse_changes(query, self.lastChange)
        for change in change_list:
            adjustedChangeTime = change["updated"]
            c = changes.Change(who = change["author"],
                               files = [], # sucks
                               revision = change["changeset"],
                               comments = change["link"],
                               when = adjustedChangeTime,
                               branch = self.branch)
            self.parent.addChange(c)
        if len(change_list) > 0:
            self.lastChange = max(self.lastChange, *[c["updated"]
                                                     for c in change_list])

    def __str__(self):
        return "<HgPoller for %s%s>" % (self.hgURL, self.branch)

class HgLocalePoller(BaseHgPoller):
    """This helper class for HgAllLocalesPoller polls a single locale and
    submits changes if necessary."""

    def __init__(self, locale, parent, branch, url):
        self.locale = locale
        self.parent = parent
        self.branch = branch
        self.url = url
        self.lastChange = time.time()

    def getData(self):
        log.msg("Polling l10n Hg server at %s" % self.url)
        return pollThrottler.getPage(self.url)

    def processData(self, query):
        change_list = _parse_changes(query, self.lastChange)
        for change in change_list:
            adjustedChangeTime = change["updated"]
            c = changes.Change(who = change["author"],
                               files = [], # sucks
                               revision = change["changeset"],
                               comments = change["link"],
                               when = adjustedChangeTime,
                               branch = self.branch)
            c.locale = self.locale
            self.parent.addChange(c)
        if len(change_list) > 0:
            self.lastChange = max(self.lastChange, *[c["updated"]
                                                     for c in change_list])

    def __str__(self):
        return "<HgLocalePoller for %s>" % self.url

class HgAllLocalesPoller(base.ChangeSource, BaseHgPoller):
    """Poll every locale from an all-locales file."""

    compare_attrs = ['allLocalesURL', 'pollInterval',
                     'localePushlogURL', 'branch']
    parent = None
    loop = None
    volatile = ['loop']

    def __init__(self, allLocalesURL, localePushlogURL, branch=None,
                 pollInterval=120):
        """
        @type  allLocalesURL:      string
        @param allLocalesURL:      The URL of the all-locales file
        @type  localePushlogURL:   string
        @param localePushlogURL:   The URL of the localized pushlogs.
                                   %(locale)s will be substituted.
        @type  pollInterval        int
        @param pollInterval        The time (in seconds) between queries for
                                   changes
        @type  branch              string or None
        @param branch              The name of the branch to report changes on.
                                   This only affects the Change, it doesn't
                                   affect the polling URLs at all!
        """

        self.allLocalesURL = allLocalesURL
        self.localePushlogURL = localePushlogURL
        self.branch = branch
        self.pollInterval = pollInterval
        self.lastChange = time.time()
        self.localePollers = {}

    def startService(self):
        self.loop = LoopingCall(self.poll)
        base.ChangeSource.startService(self)
        reactor.callLater(0, self.loop.start, self.pollInterval)

    def stopService(self):
        self.loop.stop()
        return base.ChangeSource.stopService(self)

    def describe(self):
        return "Getting changes from all-locales at %s for repositories at %s" % (self.allLocalesURL, self.localePushlogURL)

    def getData(self):
        log.msg("Polling all-locales at %s" % self.allLocalesURL)
        return pollThrottler.getPage(self.allLocalesURL)

    def getLocalePoller(self, locale):
        if locale not in self.localePollers:
            self.localePollers[locale] = \
                HgLocalePoller(locale, self.parent, self.branch,
                               self.localePushlogURL % {'locale': locale})
        return self.localePollers[locale]

    def processData(self, data):
        for l in data.splitlines():
            l = l.strip()
            if l == '': continue
            self.getLocalePoller(l).poll()

    def __str__(self):
        return "<HgAllLocalesPoller for %s>" % self.allLocalesURL
