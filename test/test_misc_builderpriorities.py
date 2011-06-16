from __future__ import with_statement
import time
import tempfile

import mock

from twisted.trial import unittest

import buildbotcustom.misc
from buildbotcustom.misc import prioritizeBuilders, builderPriority

class FakeBuilder:
    def __init__(self, branch, slaves=None):
        self.name = branch
        self.builder_status = mock.Mock()
        self.builder_status.category = branch
        self.properties = {'branch': branch}
        self.slaves = []
        if slaves:
            for slavename in slaves:
                s = mock.Mock()
                s.slave.slavename = slavename
                self.slaves.append(s)

    def __repr__(self):
        return "<FakeBuilder %s>" % self.name

class TestBuilderPriority(unittest.TestCase):
    def testRelease(self):
        self.assertEquals(builderPriority(FakeBuilder('release-foo'), {'foo': 2}, (0,0,0)), (0,0))

    def testDefault(self):
        self.assertEquals(builderPriority(FakeBuilder('foo'), {'bar': 2, None: 3}, (0,0,0)), (3,0))

    def testPriority(self):
        self.assertEquals(builderPriority(FakeBuilder('foo'), {'foo': 2, None: 3}, (0,0,0)), (2,0))

class TestPrioritizeBuilders(unittest.TestCase):
    def setUp(self):
        self.botmaster = mock.Mock()

    def testRelease(self):
        builders = [
                FakeBuilder('bar', ['s1', 's2']),
                FakeBuilder('release-foo', ['s1', 's2']),
                ]
        branch_priorities = {None: 1}
        requests = [
                ('release-foo', 0, 0),
                ('bar', 0, 0),
                ]
        self.botmaster.db.runQueryNow.return_value = requests
        sorted_builders = prioritizeBuilders(self.botmaster, builders, branch_priorities)
        self.assertEquals(sorted_builders, [builders[1]])

    def testReleaseOverlappingSlaves(self):
        builders = [
                FakeBuilder('release-foo', ['s1', 's2', 's3']),
                FakeBuilder('bar', ['s2', 's3', 's4']),
                ]
        branch_priorities = {None: 1}
        requests = [
                ('release-foo', 0, 0),
                ('bar', 0, 0),
                ]
        self.botmaster.db.runQueryNow.return_value = requests
        sorted_builders = prioritizeBuilders(self.botmaster, builders, branch_priorities)
        self.assertEquals(sorted_builders, [builders[0], builders[1]])

    def testMultipleReleases(self):
        builders = [
                FakeBuilder('release-foo', ['s1', 's2']),
                FakeBuilder('release-bar', ['s1', 's2']),
                FakeBuilder('bar', ['s2', 's1']),
                ]
        branch_priorities = {None: 1}
        requests = [
                ('release-foo', 0, 0),
                ('bar', 0, 0),
                ('release-bar', 0, 1),
                ]
        self.botmaster.db.runQueryNow.return_value = requests
        sorted_builders = prioritizeBuilders(self.botmaster, builders, branch_priorities)
        self.assertEquals(sorted_builders, [builders[0], builders[1]])

    def testNormalRequests(self):
        builders = [
                FakeBuilder('foo', ['s1', 's2']),
                FakeBuilder('bar', ['s1', 's2']),
                ]
        branch_priorities = {'foo': 1, 'bar': 2}
        requests = [
                ('foo', 0, 0),
                ('bar', 0, 0),
                ]
        self.botmaster.db.runQueryNow.return_value = requests
        sorted_builders = prioritizeBuilders(self.botmaster, builders, branch_priorities)
        self.assertEquals(sorted_builders, [builders[0]])

    def testPrioritizedRequests(self):
        builders = [
                FakeBuilder('foo', ['s1', 's2']),
                FakeBuilder('bar', ['s1', 's2']),
                ]
        branch_priorities = {'foo': 1, 'bar': 2}
        requests = [
                ('foo', 0, 0),
                ('bar', 11, 0),
                ]
        self.botmaster.db.runQueryNow.return_value = requests
        sorted_builders = prioritizeBuilders(self.botmaster, builders, branch_priorities)
        self.assertEquals(sorted_builders, [builders[1]])
        # TODO: Busted because bar request is being removed
