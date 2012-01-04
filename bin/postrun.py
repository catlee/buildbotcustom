#!/usr/bin/env python
"""
postrun.py [options] /path/to/build/pickle

post-job tasks
- upload logs
- (optionally) mail users about try results
- send pulse message about log being uploaded
- update statusdb with job info (including log url)
"""
import os, sys, subprocess
import re
import cPickle as pickle
from datetime import datetime
try:
    import simplejson as json
except ImportError:
    import json

import logging
log = logging.getLogger(__name__)

import buildbotcustom.status.db.model as model
from mozilla_buildtools.queuedir import QueueDir

class PostRunner(object):
    def __init__(self, config):
        self.config = config

    def uploadLog(self, build):
        log.info("Uploading log")
        """Uploads the build log, and returns the URL to it"""
        builder = build.builder

        info = self.getBuildInfo(build)
        branch = info['branch']
        product = info['product']
        platform = info['platform']

        upload_args = []
        if "nightly" in builder.name:
            upload_args.append("--nightly")
        if builder.name.startswith("release-"):
            upload_args.append("--release")
            # TODO version/buildnumber

        if branch == 'try':
            upload_args.append("--try")
        elif branch == 'shadow-central':
            upload_args.append("--shadow")

        if 'l10n' in builder.name:
            upload_args.append("--l10n")

        if product:
            upload_args.extend(["--product", product])
        if platform:
            upload_args.extend(["--platform", platform])
        if branch:
            upload_args.extend(["--branch", branch])

        upload_args.extend(self.getUploadArgs(build))
        upload_args.extend([builder.basedir, str(build.number)])

        my_dir = os.path.abspath(os.path.dirname(__file__))
        cmd = [sys.executable, "%s/log_uploader.py" % my_dir] + upload_args
        devnull = open(os.devnull)

        print "Running", cmd

        proc = subprocess.Popen(cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=devnull)

        retcode = proc.wait()
        output = proc.stdout.read().strip()
        print output

        # Look for URLs
        url = re.search("http://\S+", output)
        if url:
            return url.group(), retcode
        return None, retcode

    def isPrivate(self, build):
        for pat in self.config.get('pvt_upload_patterns', []):
            if re.search(pat, build.builder.name):
                return True
        return False

    def getUploadArgs(self, build):
        if self.isPrivate(build):
            retval = ["--user", self.config['pvt_upload_user']]
            if "pvt_upload_sshkey" in self.config:
                retval.extend(["-i", self.config['pvt_upload_sshkey']])
            retval.append(self.config['pvt_upload_host'])
        else:
            retval = ["--user", self.config['upload_user']]
            if "upload_sshkey" in self.config:
                retval.extend(["-i", self.config['upload_sshkey']])
            retval.append(self.config['upload_host'])
        return retval

    def getBuild(self, build_path):
        log.info("Loading build pickle")
        if not os.path.exists(build_path):
            raise ValueError("Couldn't find %s" % build_path)

        builder_path = os.path.dirname(build_path)
        class FakeBuilder:
            basedir = builder_path
            name = os.path.basename(builder_path)

        build = pickle.load(open(build_path))
        build.builder = FakeBuilder()
        return build

    def getBuildInfo(self, build):
        """
        Returns a dictionary with
        'branch', 'platform', 'product'
        set as appropriate
        """
        props = build.getProperties()
        retval = {}
        if props.getProperty('stage_platform') is not None:
            retval['platform'] = props['stage_platform']
        elif props.getProperty('platform') is not None:
            retval['platform'] = props['platform']
        else:
            retval['platform'] = None

        if props.getProperty('stage_product') is not None:
            retval['product'] = props['stage_product']
        elif props.getProperty('product') is not None:
            retval['product'] = props['product']
        else:
            retval['product'] = None

        if props.getProperty('branch') is not None:
            retval['branch'] = props['branch']
        else:
            retval['branch'] = None

        log.debug("Build info: %s", retval)
        return retval

    def writePulseMessage(self, build):
        msg = {
                # TODO: Fix routing key to build.$builder.$number.log_uploaded
                'event': 'build.%s.log_uploaded' % build.getProperty('statusdb_id'),
                'payload': {"build": build.asDict()},

                'master_name': self.config['master_name'], # TODO
                'master_incarnation': None, #TODO
                'id': None, #TODO
            }
        self.config['pulse_queue'].add(json.dumps([msg]))

    def updateStatusDB(self, build):
        log.info("Updating statusdb")
        session = model.connect(self.config['database'])()
        master = model.Master.get(session, self.config['master_url'])
        master.name = unicode(self.config['master_name'])

        if not master.id:
            log.debug("added master")
            session.add(master)
            session.commit()

        builder_name = build.builder.name
        db_builder = model.Builder.get(session, builder_name, master.id)
        db_builder.category = unicode(build.getProperty('branch'))

        starttime = None
        if build.started:
            starttime = datetime.utcfromtimestamp(build.started)

        log.debug("searching for build")
        q = session.query(model.Build).filter_by(
                master_id=master.id,
                builder=db_builder,
                buildnumber=build.number,
                starttime=starttime,
                )
        db_build = q.first()
        if not db_build:
            log.debug("creating new build")
            db_build = model.Build.fromBBBuild(session, build, builder_name, master.id)
        else:
            log.debug("updating old build")
            db_build.updateFromBBBuild(session, build)
        session.commit()
        log.debug("committed")
        return db_build.id

    def processBuild(self, options, build_path, request_ids):
        build = self.getBuild(build_path)
        if not options.log_url:
            log.info("uploading log")
            log_url, retcode = self.uploadLog(build)
            assert retcode == 0
            if log_url is None:
                log_url = 'null'
            cmd = [sys.executable] + sys.argv + ["--log-url", log_url]
            self.config['command_queue'].add(json.dumps(cmd))
        elif not options.statusdb_id:
            log.info("adding to statusdb")
            log_url = options.log_url
            if log_url == 'null':
                log_url = None
            build.properties.setProperty('log_url', log_url, 'postrun.py')
            build.properties.setProperty('request_ids', [int(i) for i in request_ids], 'postrun.py')
            build_id = self.updateStatusDB(build)
            for i in request_ids:
                if not model.schedulerdb_requests.select().where(model.schedulerdb_requests.c.build_id==build_id).where(model.schedulerdb_requests.c.request_id==i).limit(1).execute().fetchone():
                    model.schedulerdb_requests.insert().execute(build_id=build_id, request_id=i)
            cmd = [sys.executable] + sys.argv + ["--statusdb-id", str(build_id)]
            self.config['command_queue'].add(json.dumps(cmd))
        else:
            log_url = options.log_url
            build_id = options.statusdb_id
            build.properties.setProperty('log_url', log_url, 'postrun.py')
            build.properties.setProperty('statusdb_id', build_id, 'postrun.py')
            build.properties.setProperty('request_ids', [int(i) for i in request_ids], 'postrun.py')
            self.writePulseMessage(build)

def main():
    from optparse import OptionParser
    parser = OptionParser()
    parser.set_defaults(
            loglevel=logging.INFO,
            log_url=None,
            statusdb_id=None,
            )
    parser.add_option("-v", "--verbose", dest="loglevel", const=logging.DEBUG, action="store_const")
    parser.add_option("-q", "--quiet", dest="loglevel", const=logging.WARNING, action="store_const")
    parser.add_option("--log-url", dest="log_url")
    parser.add_option("--statusdb-id", dest="statusdb_id", type="int")

    options, args = parser.parse_args()

    logging.basicConfig(level=options.loglevel)

    config = {
            'database': 'mysql://buildbot@localhost/buildbot',
            'master_url': 'http://localhost:9010',
            'master_name': 'fooie',
            'upload_host': 'localhost',
            'upload_user': 'catlee',

            'pvt_upload_host': 'localhost',
            'pvt_upload_user': 'catlee',
            'pvt_upload_patterns': ['fuzzer', 'shadow-central'],

            'command_queue': QueueDir('commands', '/dev/shm/queue/commands/'),
            'pulse_queue': QueueDir('pulse', '/dev/shm/queue/pulse/'),
            }

    post_runner = PostRunner(config)

    build_path, request_ids = args[0], args[1:]
    post_runner.processBuild(options, build_path, request_ids)

if __name__ == '__main__':
    main()
