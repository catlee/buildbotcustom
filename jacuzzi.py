import urllib2
import time
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
    """
    BASE_URL = "http://jacuzzi-allocator.pub.build.mozilla.org/v1"
    CACHE_MAXAGE = 300  # 5 minutes
    CACHE_FAIL_MAXAGE = 30  # Cache failures for 30 seconds
    MAX_TRIES = 3  # Try up to 3 times
    SLEEP_TIME = 10  # Wait 10s between tries
    HTTP_TIMEOUT = 10  # Timeout http fetches in 10s

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

    def get_slaves(self, buildername, available_slaves):
        """Returns which slaves are suitable for building this builder

        Args:
            buildername (str): which builder to get slaves for
            available_slaves (list of buildbot Slave objects): slaves that are
                currently available on this master

        Returns:
            None if no slaves are suitable for building this builder, otherwise
            returns a list of slaves to use
        """
        if not self.jacuzzi_enabled:
            return available_slaves

        # Check the cache for this builder
        self.log("checking cache for builder %s" % str(buildername))
        c = self.cache.get(buildername)
        if c:
            cache_expiry_time, slaves = c
            # If the cache is still fresh, use the builder's allocated slaves
            # to filter our list of available slaves
            if cache_expiry_time > time.time():
                self.log("cache hit")
                # TODO: This could get spammy
                self.log("fresh cache: %s" % slaves)
                if slaves:
                    return [s for s in available_slaves if s.slave.slavename in slaves]
                return None
            else:
                self.log("expired cache")
        else:
            self.log("cache miss")

        url = "%s/builders/%s" % (self.BASE_URL, urllib2.quote(buildername, ""))
        for i in range(self.MAX_TRIES):
            try:
                if self.missing_cache.get(buildername, 0) > time.time():
                    self.log("skipping %s since we 404'ed last time" % url)
                    # Use unallocted slaves instead
                    return self.get_unallocated_slaves(available_slaves)

                self.log("fetching %s" % url)
                data = json.load(urllib2.urlopen(url, timeout=self.HTTP_TIMEOUT))
                slaves = set(data['machines'])  # use sets for moar speed!
                # TODO: This could get spammy
                self.log("slaves: %s" % slaves)
                self.cache[buildername] = (time.time() + self.CACHE_MAXAGE, slaves)
                # Filter the list of available slaves by the set the service
                # returned to us
                return [s for s in available_slaves if s.slave.slavename in slaves]
            except urllib2.HTTPError, e:
                # We couldn't find an allocation for this builder
                # Fetch the list of all allocated slaves, and filter them out
                # of our list of available slaves
                if e.code == 404:
                    try:
                        slaves = self.get_unallocated_slaves(available_slaves)
                        self.log("slaves: %s" % [s.slave.slavename for s in slaves])
                        # We hit a 404 error, so we should remember
                        # this for next time. We'll avoid doing the
                        # per-builder lookup for CACHE_MAXAGE seconds, and
                        # fall back to looking at all the allocated slaves
                        self.log("remembering 404 result for %s seconds" % self.CACHE_MAXAGE)
                        self.missing_cache[buildername] = time.time() + self.CACHE_MAXAGE
                        return slaves
                    except Exception:
                        self.log("unhandled exception getting unallocated slaves", exc_info=True)
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

    def __call__(self, func):
        """
        Decorator for nextSlave functions that will contact the allocator
        thingy and trim list of available slaves
        """
        @wraps(func)
        def _nextSlave(builder, available_slaves):
            my_available_slaves = self.get_slaves(builder.name, available_slaves)
            # Something went wrong; fallback to using any available machine
            if my_available_slaves is None:
                return func(builder, available_slaves)
            return func(builder, my_available_slaves)
        return _nextSlave

