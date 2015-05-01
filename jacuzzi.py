import urllib2
import time
import inspect
try:
    import json
    assert json  # pyflakes
except:
    import simplejson as json

from functools import wraps

from twisted.python import log


class JacuzziAllocator(object):
    """Class for contacting slave allocator service

    The service assigns slaves into builder-specific pools (aka jacuzzis)

    Caching is done per JacuzziAllocator instance. The instance is meant to be
    used as a decorator for buildbot nextSlave functions. e.g.
    >>> J = JacuzziAllocator()
    >>> builder['nextSlave'] = J(my_next_slave_func)

    Attributes:
        BASE_URL (str): Base URL to use for the service
        CACHE_MAXAGE (int): Time in seconds to cache results from service,
            defaults to 300
        CACHE_FAIL_MAXAGE (int): Time in seconds to cache failures from
            service, defaults to 30
        MAX_TRIES (int): Maximum number of times to try to contact service,
            defaults to 3
        SLEEP_TIME (int): How long to sleep between tries, in seconds, defaults
            to 10
        HTTP_TIMEOUT (int): How long to wait for a response from the service,
            in seconds, defaults to 10
        ESCAPE_TIMEOUT (int): How long to wait for an allocated slave to take a
            job before allowing other slaves to take it. Defaults to 1800 (30 minutes)
    """
    BASE_URL = "http://jacuzzi-allocator.pub.build.mozilla.org/v1"
    CACHE_MAXAGE = 300  # 5 minutes
    CACHE_FAIL_MAXAGE = 30  # Cache failures for 30 seconds
    MAX_TRIES = 3  # Try up to 3 times
    SLEEP_TIME = 10  # Wait 10s between tries
    HTTP_TIMEOUT = 10  # Timeout http fetches in 10s
    ESCAPE_TIMEOUT = 10  # How long to wait for an allocated slave to take the job

    def __init__(self):
        # Cache of builder name -> (timestamp, set of slavenames)
        self.cache = {}

        # (timestamp, set of slavenames)
        self.allocated_cache = None

        # Cache of builder name -> timestamp
        self.missing_cache = {}

        self.jacuzzi_enabled = True

        self.log("created")

    def log(self, msg, exc_info=False):
        """
        Output stuff into twistd.log

        Args:
            msg (str): message to log
            exc_info (bool, optional): include traceback info, defaults to
                False
        """
        if exc_info:
            log.err()
            log.msg("JacuzziAllocator %i: %s" % (id(self), msg))
        else:
            log.msg("JacuzziAllocator %i: %s" % (id(self), msg))

    def get_unallocated_slaves(self, available_slaves):
        """Filters available_slaves by the list of slaves not currently
        allocated to a jacuzzi.

        This can return cached results.
        """
        if not self.jacuzzi_enabled:
            return available_slaves

        self.log("checking cache allocated slaves")
        if self.allocated_cache:
            cache_expiry_time, slaves = self.allocated_cache
            if cache_expiry_time > time.time():
                # TODO: This could get spammy
                self.log("fresh cache: %s" % slaves)
                return [s for s in available_slaves if s.slave.slavename not in slaves]
            else:
                self.log("expired cache")
        else:
            self.log("cache miss")

        url = "%s/allocated/all" % self.BASE_URL
        self.log("fetching %s" % url)
        data = json.load(urllib2.urlopen(url, timeout=self.HTTP_TIMEOUT))
        slaves = set(data['machines'])  # use sets for moar speed!
        # TODO: This could get spammy
        self.log("already allocated: %s" % slaves)
        self.allocated_cache = (time.time() + self.CACHE_MAXAGE, slaves)
        return [s for s in available_slaves if s.slave.slavename not in slaves]

    def get_allocated_slaves(self, buildername):
        """Returns the set of allocated slavenames for the builder, or None if
        there are no allocated slaves for the builder."""

        # Check the cache for this builder
        self.log("checking cache for builder %s" % str(buildername))
        c = self.cache.get(buildername)
        if c:
            cache_expiry_time, slaves = c
            # If the cache is still fresh, use the builder's allocated slaves
            # to filter our list of available slaves
            if cache_expiry_time > time.time():
                self.log("cache hit for %s" % buildername)
                # TODO: This could get spammy
                self.log("fresh cache: %s" % slaves)
                return slaves
            else:
                self.log("expired cache")
        else:
            self.log("cache miss")

        url = "%s/builders/%s" % (self.BASE_URL, urllib2.quote(buildername, ""))
        for i in range(self.MAX_TRIES):
            try:
                self.log("fetching %s" % url)
                data = json.load(urllib2.urlopen(url, timeout=self.HTTP_TIMEOUT))
                slaves = set(data['machines'])  # use sets for moar speed!
                # TODO: This could get spammy
                self.log("slaves: %s" % slaves)
                self.cache[buildername] = (time.time() + self.CACHE_MAXAGE, slaves)
                # Filter the list of available slaves by the set the service
                # returned to us
                return slaves
            except urllib2.HTTPError, e:
                # We couldn't find an allocation for this builder
                # Fetch the list of all allocated slaves, and filter them out
                # of our list of available slaves
                if e.code == 404:
                    self.cache[buildername] = (time.time() + self.CACHE_MAXAGE, None)
                    return None
                else:
                    self.log("unhandled http error %s" % e.code, exc_info=True)
            except Exception:
                # Ignore other exceptions for now
                self.log("unhandled exception", exc_info=True)

            if i < self.MAX_TRIES:
                self.log("try %i/%i; sleeping %i and trying again" % (i + 1, self.MAX_TRIES, self.SLEEP_TIME))
                time.sleep(self.SLEEP_TIME)

        # We couldn't get a good answer. Cache the failure so we're not
        # hammering the service all the time, and then return None
        self.log("gave up, returning None")
        self.cache[buildername] = (time.time() + self.CACHE_FAIL_MAXAGE, None)
        return None

    def _get_requests_from_stack(self):
        frame = inspect.currentframe().f_back
        while frame:
            if (inspect.getframeinfo(frame).function == '_claim_buildreqs'
                    and 'requests' in frame.f_locals):
                return frame.f_locals['requests']
            frame = frame.f_back

    def waiting_too_long(self, builder):
        # MOAR STACK WALKING!
        # We need to walk up the stack to find the list of requests we're
        # processing, so we can find the oldest one.
        # We can't call builder.getBuildable(), because we're running in a
        # thread, and getBuildable() uses the dedicated DB connection from the
        # main thread
        requests = self._get_requests_from_stack()
        if requests:
            oldest = min(req.submittedAt for req in requests)
            self.log('oldest request submitted at %s' % oldest)
            if oldest + self.ESCAPE_TIMEOUT < time.time():
                return True
        return False

    def get_slaves(self, builder, available_slaves):
        """Returns which slaves are suitable for building this builder

        Args:
            builder (buildbot Builder object): builder to get slaves for
            available_slaves (list of buildbot Slave objects): slaves that are
                currently available on this master

        Returns:
            None if no slaves are suitable for building this builder, otherwise
            returns a list of slaves to use
        """

        if not self.jacuzzi_enabled:
            return available_slaves

        # Basic flow we want to do here:
        # if waiting_too_long:
        #   use any slave
        # if allocated slaves:
        #   use an allocated jacuzzi slave
        # else:
        #   use an unallocated jacuzzi slave

        buildername = builder.name

        if self.waiting_too_long(builder):
            self.log("waiting too long for %s; using any free slave" % buildername)
            return available_slaves

        # Get a list of available slaves allocated for this builder
        # NB we get an empty list if there is an allocation, but no slaves
        # available; we get None if there is no allocation
        allocated_slaves = self.get_allocated_slaves(buildername)
        if allocated_slaves is not None:
            return [s for s in available_slaves if s.slave.name in allocated_slaves]
        else:
            return self.get_unallocated_slaves(available_slaves)

    def __call__(self, func):
        """
        Decorator for nextSlave functions that will contact the allocator
        thingy and trim list of available slaves
        """
        @wraps(func)
        def _nextSlave(builder, available_slaves):
            my_available_slaves = self.get_slaves(builder, available_slaves)
            # Something went wrong; fallback to using any available machine
            if my_available_slaves is None:
                return func(builder, available_slaves)
            return func(builder, my_available_slaves)
        return _nextSlave
