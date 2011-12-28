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

import logging
log = logging.getLogger(__name__)

import buildbotcustom.status.db.model as model

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

    def getUploadArgs(self, build):
        # TODO
        return ["--user", "catlee", "localhost"]

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
        pass

    def updateStatusDB(self, build):
        log.info("Updating statusdb")
        session = model.connect(self.config.get('database', 'url'))()
        master = model.Master.get(session, self.config.get('master', 'url'))
        master.name = unicode(self.config.get('master', 'name'))

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

    def processBuild(self, build_path, request_ids):
        build = self.getBuild(build_path)
        log_url, retcode = self.uploadLog(build)
        build.properties.setProperty('log_url', log_url, 'postrun.py')
        # TODO: save this in a separate table
        build.properties.setProperty('request_ids', [int(i) for i in request_ids], 'postrun.py')
        if retcode != 0:
            # TODO
            return

        build_id = self.updateStatusDB(build)
        # TODO: save this in a separate table
        build.properties.setProperty('statusdb_id', build_id, 'postrun.py')
        self.writePulseMessage(build)

def main():
    from optparse import OptionParser
    parser = OptionParser()
    parser.set_defaults(
            loglevel=logging.INFO,
            )
    parser.add_option("-v", "--verbose", dest="loglevel", const=logging.DEBUG, action="store_const")
    parser.add_option("-q", "--quiet", dest="loglevel", const=logging.WARNING, action="store_const")

    options, args = parser.parse_args()

    logging.basicConfig(level=options.loglevel)

    config = {
            'database': 'mysql://buildbot@localhost/buildbot',
            }

    post_runner = PostRunner(config)

    build_path, request_ids = args[0], args[1:]
    post_runner.processBuild(build_path, request_ids)

if __name__ == '__main__':
    main()
