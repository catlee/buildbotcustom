#!/usr/bin/env python
"""hgpoller.py [-f|--config-file configfile] [-v|--verbose] [branch ...]"""
import urlparse, urllib, time
try:
    import json
except:
    import simplejson as json

import xml.etree.ElementTree as etree
import httplib, urllib2, socket, ssl

import subprocess
from buildbotcustom.changes.hgpoller import _parse_changes
import logging as log

import os, fcntl

def lockfile(filename):
    fd = os.open(filename, os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    # Touch the file
    os.utime(filename, None)

def buildValidatingOpener(ca_certs):
    class VerifiedHTTPSConnection(httplib.HTTPSConnection):
        def connect(self):
            # overrides the version in httplib so that we do
            #    certificate verification
            sock = socket.create_connection((self.host, self.port),
                                            self.timeout)
            if self._tunnel_host:
                self.sock = sock
                self._tunnel()

            # wrap the socket using verification with the root
            #    certs in trusted_root_certs
            self.sock = ssl.wrap_socket(sock,
                                        self.key_file,
                                        self.cert_file,
                                        cert_reqs=ssl.CERT_REQUIRED,
                                        ca_certs=ca_certs,
                                        )

    # wraps https connections with ssl certificate verification
    class VerifiedHTTPSHandler(urllib2.HTTPSHandler):
        def __init__(self, connection_class=VerifiedHTTPSConnection):
            self.specialized_conn_class = connection_class
            urllib2.HTTPSHandler.__init__(self)

        def https_open(self, req):
            return self.do_open(self.specialized_conn_class, req)

    https_handler = VerifiedHTTPSHandler()
    url_opener = urllib2.build_opener(https_handler)

    return url_opener

def validating_https_open(url, ca_certs, username=None, password=None):
    url_opener = buildValidatingOpener(ca_certs)
    req = urllib2.Request(url)
    if username and password:
        # Basic HTTP auth
        # The username/password aren't sent if the cert validation fails
        pw = ("%s:%s" % (username, password)).encode("base64").strip()
        req.add_header("Authorization", "Basic %s" % pw)
    return url_opener.open(req)

def getChanges(base_url, last_changeset=None, tips_only=False, ca_certs=None,
        username=None, password=None, mirror_url=None):
    bits = urlparse.urlparse(base_url)
    if bits.scheme == 'https':
        assert ca_certs, "you must specify ca_certs"

    # TODO: Handle the repo being reset

    to_changeset = None

    # If we have a mirror_url, we first need to check the mirror's atom feed
    if mirror_url:
        mirror_url += "/atom-log"
        u = urllib2.urlopen(mirror_url)
        log.debug("Fetching %s", mirror_url)
        tree = etree.parse(u)
        latest_entry = tree.find("{http://www.w3.org/2005/Atom}entry")
        if not latest_entry:
            return []
        latest_rev = latest_entry.find("{http://www.w3.org/2005/Atom}id").text.split("-")[-1]
        to_changeset = latest_rev
        log.debug("Mirror has up to %s", to_changeset)

    if last_changeset and to_changeset:
        if last_changeset == to_changeset:
            log.debug("Mirror has our latest changeset, nothing else to do")
            return []

    params = [('full', '1')]
    if last_changeset:
        params.append( ('fromchange', last_changeset) )
    if to_changeset:
        params.append( ('tochange', to_changeset) )
    if tips_only:
        params.append( ('tipsonly', '1') )
    url = "%s/json-pushes?%s" % (base_url, urllib.urlencode(params))

    log.debug("Fetching %s", url)

    if bits.scheme == 'https':
        handle = validating_https_open(url, ca_certs, username, password)
    else:
        handle = urllib2.urlopen(url)

    data = handle.read()
    return _parse_changes(data)

def sendchange(master, branch, change):
    log.info("Sendchange %s to %s on branch %s", change['changeset'], master, branch)
    cmd = ['retry.py', '-r', '5', '-s', '5', '-t', '30',
            '--stdout-regexp', 'change sent successfully']
    cmd.extend(
          ['buildbot', 'sendchange',
            '--master', master,
            '--branch', branch,
            '--comments', change['comments'].encode('ascii', 'replace'),
            '--revision', change['changeset'],
            '--user', change['author'].encode('ascii', 'replace'),
            '--when', str(change['updated']),
            ])

    if change.get('revlink'):
        cmd.extend(['--revlink', change['revlink']])

    # Buildbot sendchange requires actual files to have changed. Normally we
    # have those, but sometimes no files change on a revision (e.g. try
    # pushes). In those cases we use a dummy filename.
    if change['files']:
        cmd.extend(change['files'])
    else:
        cmd.append('dummy')

    try:
        subprocess.check_call(cmd)
    except:
        log.error("Couldn't run %s", cmd)
        raise

def processBranch(branch, state, config, force=False):
    master = config.get('main', 'master')
    if branch not in state:
        state[branch] = {'last_run': 0, 'last_changeset': None}
    log.debug("Processing %s (last changeset: %s)", branch, state[branch]['last_changeset'])
    branch_state = state[branch]
    interval = config.getint(branch, 'interval')
    if not force and time.time() < (branch_state['last_run'] + interval):
        log.debug("Skipping %s, too soon since last run", branch)
        return

    branch_state['last_run'] = time.time()

    url = config.get(branch, 'url')
    if config.has_option(branch, 'mirror_url'):
        mirror_url = config.get(branch, 'mirror_url')
    else:
        mirror_url = None
    ca_certs = config.get(branch, 'ca_certs')
    tips_only = config.getboolean(branch, 'tips_only')
    username = config.get(branch, 'username')
    password = config.get(branch, 'password')
    last_changeset = branch_state['last_changeset']

    try:
        changes = getChanges(url, tips_only=tips_only,
                last_changeset=last_changeset, ca_certs=ca_certs,
                username=username, password=password, mirror_url=mirror_url)
        # Do sendchanges!
        for c in changes:
            # Ignore off-default branches
            if c['branch'] != 'default' and config.getboolean(branch, 'default_branch_only'):
                log.info("Skipping %s on branch %s", c['changeset'], c['branch'])
                continue

            # Update revlink with our mirror url if appropriate
            if c.get('revlink') and mirror_url:
                c['revlink'] = c['revlink'].replace(url, mirror_url)

            # Change the comments to include the url to the revision
            c['comments'] += ' %s/rev/%s' % (mirror_url or url, c['changeset'])
            log.info("%s %s %s", branch, c['changeset'], c['files'])
            sendchange(master, branch, c)

    except urllib2.HTTPError, e:
        msg = e.fp.read()
        if e.code == 500 and 'unknown revision' in msg:
            log.info("%s Repo was reset, resetting last_changeset", branch)
            branch_state['last_changeset'] = None
            return
        else:
            raise

    if not changes:
        # Empty repo, or no new changes; nothing to do
        return

    last_change = changes[-1]
    branch_state['last_changeset'] = last_change['changeset']

if __name__ == '__main__':
    from ConfigParser import RawConfigParser
    from optparse import OptionParser
    import os

    parser = OptionParser(__doc__)
    parser.set_defaults(
            config_file="hgpoller.ini",
            verbosity=log.INFO,
            force=False,
            )
    parser.add_option("-f", "--config-file", dest="config_file")
    parser.add_option("-v", "--verbose", dest="verbosity",
            action="store_const", const=log.DEBUG)
    parser.add_option("--force", dest="force", action="store_true",
            help="force to run even if it's too early")

    options, args = parser.parse_args()

    if not os.path.exists(options.config_file):
        parser.error("%s doesn't exist" % options.config_file)

    log.basicConfig(format="%(asctime)s %(message)s", level=options.verbosity)

    config = RawConfigParser({
        'tips_only': 'yes',
        'username': None,
        'password': None,
        'ca_certs': None,
        'lockfile': 'hgpoller.lock',
        'interval': 300,
        'state_file': 'state.json',
        'default_branch_only': "yes",
        })
    config.read(options.config_file)

    lockfile(config.get('main', 'lockfile'))

    try:
        state = json.load(open(config.get('main', 'state_file')))
    except (IOError, ValueError):
        state = {}

    branches = [s for s in config.sections() if s != 'main']
    for a in args:
        if a not in branches:
            parser.error("Invalid branch name: %s" % a)

    errors = False
    for branch in branches:
        if args and branch not in args:
            continue
        try:
            processBranch(branch, state, config, force=options.force)
        except:
            log.exception("Couldn't handle branch %s", branch)
            errors = True

    # Save state
    json.dump(state, open(config.get('main', 'state_file'), 'w'))

    if errors:
        raise SystemExit(1)
