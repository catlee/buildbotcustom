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

import buildbotcustom.status.db.model as model

class PostRunner(object):
    def __init__(self, config):
        self.config = config

    def uploadLog(self, build):
        """Uploads the build log, and returns the URL to it"""
        builder = build.builder
        branch = build.getProperty('branch')
        product = build.getProperty('product')
        platform = build.getProperty('platform')

        upload_args = []
        if "nightly" in builder.name:
            upload_args.append("--nightly")
        if builder.name.startswith("release-"):
            upload_args.append("--release")

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

        # TODO:
        # --user
        # --identity

        upload_args.extend([builder.basedir, build.number])

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

    def getBuild(self, build_path):
        if not os.path.exists(build_path):
            raise ValueError("Couldn't find %s" % build_path)

        builder_path = os.path.dirname(build_path)

        class FakeBuilder:
            basedir = builder_path
            name = os.path.basename(builder_path)

        build = pickle.load(open(build_path))
        build.builder = FakeBuilder()
        return build

    def writePulseMessage(self, build):
        pass

    def mailUser(self, build):
        builder = build.builder
        mailer_args = []
        mailer_args.extend(["-f", self.config.get('mail', 'from_addr')])
        mailer_args.extend(["--logurl", build.getProperty('log_url')])
        # TODO: check config to see if we should mail this user, or somebody else
        mailer_args.append("--to-author")

        mailer_args.extend([builder.basedir, build.number])
        my_dir = os.path.abspath(os.path.dirname(__file__))
        cmd = [sys.executable, "%s/try_mailer.py" % my_dir] + mailer_args
        devnull = open(os.devnull)
        proc = subprocess.Popen(cmd, stdin=devnull)
        proc.wait()

    def updateStatusDB(self, build):
        session = model.connect(self.config.get('database', 'url'))()
        master = model.Master.get(session, self.config.get('master', 'url'))
        master.name = unicode(self.config.get('master', 'name'))

        builder_name = build.builder.name
        db_builder = model.Builder.get(session, builder_name, master.id)
        db_builder.category = unicode(build.getProperty('branch'))

        starttime = None
        if build.started:
            starttime = datetime.utcfromtimestamp(build.started)

        q = session.query(model.Build).filter_by(
                master_id=master.id,
                builder=db_builder,
                buildnumber=build.number,
                starttime=starttime,
                )
        db_build = q.first()
        if not db_build:
            db_build = model.Build.fromBBBuild(session, build, builder_name, master.id)
        else:
            db_build.updateFromBBBuild(session, build)
        session.commit()

    def shouldEmailUser(self, build):
        branch = build.getProperty('branch')
        # TODO: check config to see if we should mail this user at all

    def processBuild(self, f):
        build = self.getBuild(f)
        log_url, retcode = self.uploadLog(build)
        build.properties['log_url'] = log_url
        if retcode != 0:
            # TODO
            continue

        self.writePulseMessage(build)

        if self.shouldEmailUser(build):
            self.mailUser(build)

        self.updateStatusDB(build)

def main():
    from optparse import OptionParser
    parser = OptionParser()

    options, args = parser.parse_args()

    post_runner = PostRunner()

    for f in args:
        post_runner.processBuild(f)

if __name__ == '__main__':
    main()
