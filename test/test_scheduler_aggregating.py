# Tests for the aggregating scheduler
import os
import shutil
import json

from buildbot.db import dbspec, connector
from buildbot.db.schema.manager import DBSchemaManager
from twisted.trial import unittest

import mock

from buildbotcustom.scheduler import AggregatingScheduler


class TestAggregatingScheduler(unittest.TestCase):
    basedir = "test_scheduler_aggregating"

    def setUp(self):
        if os.path.exists(self.basedir):
            shutil.rmtree(self.basedir)
        os.makedirs(self.basedir)
        spec = dbspec.DBSpec.from_url("sqlite:///state.sqlite", self.basedir)
        manager = DBSchemaManager(spec, self.basedir)
        manager.upgrade()

        self.dbc = connector.DBConnector(spec)
        self.dbc.start()
        self._patcher = mock.patch("buildbotcustom.scheduler.now")
        self._time = self._patcher.start()
        self._time.return_value = 123

    def tearDown(self):
        self.dbc.stop()
        shutil.rmtree(self.basedir)
        self._patcher.stop()

    def testCreate(self):
        s = AggregatingScheduler(name='s1', branch='b1', builderNames=['d1', 'd2'], upstreamBuilders=['u1', 'u2'])
        s.parent = mock.Mock()
        s.parent.db = self.dbc

        d = self.dbc.addSchedulers([s])

        def checkState(_):
            schedulers = self.dbc.runQueryNow("SELECT name, state FROM schedulers")
            self.assertEquals(len(schedulers), 1)
            state = json.loads(schedulers[0][1])
            self.assertEquals(state, {"remainingBuilders": ["u1", "u2"], "upstreamBuilders": ["u1", "u2"], "lastCheck": 123})

        d.addCallback(checkState)
        return d

    def testTriggerDownstreams(self):
        s = AggregatingScheduler(name='s1', branch='b1', builderNames=['d1', 'd2'], upstreamBuilders=['u1'])
        s.parent = mock.Mock()
        s.parent.db = self.dbc

        d = self.dbc.addSchedulers([s])

        def check(_):
            requests = self.dbc.runQueryNow("SELECT * FROM buildrequests")
            self.assertEquals(len(requests), 0)
        d.addCallback(check)

        def addFinishedBuild(_):
            self.dbc.runQueryNow("""
                    INSERT into buildrequests
                    (buildsetid, buildername, complete, complete_at, submitted_at, results) VALUES
                    (0, 'u1', 1, 124, 0, 0)
            """)

            # Now run the scheduler
            self._time.return_value = 200
            return s.run()
        d.addCallback(addFinishedBuild)

        def checkRequests(_):
            requests = self.dbc.runQueryNow("SELECT buildername FROM buildrequests WHERE complete=0")
            self.assertEquals(len(requests), 2)
            self.assertEquals(sorted([r[0] for r in requests]), ['d1', 'd2'])
        d.addCallback(checkRequests)

        return d
