#!/usr/bin/env python
import time

from mozilla_buildtools.queuedir import QueueDir
import requests

import sqlalchemy as sa

try:
    import simplejson as json
except ImportError:
    import json

queuedir = QueueDir("scheduler", "/dev/shm/queue/scheduler")

def get_builds(change, scheduler_data, scheduler_type):
    builds = []
    change = change['change']
    # TODO: generate buildid / builduid?
    for job_type in scheduler_data[scheduler_type]:
        job_data = scheduler_data[scheduler_type][job_type]
        builds.append({
            "job_type": job_type,
            "platform": job_data["platform"],
            "product": job_data["product"],
            "args": job_data.get("args", []),
            "branch": change['branch'],
            "revision": change['revision'],
            "cmd_url": job_data['cmd_url'] % change,
        })
    return builds

def process(change):
    if 'revlink' in change['change']:
        url = change['change']['revlink']
        file_url = url.replace("/rev/", "/raw-file/")
        file_url += "/build/build.json"

        file_url = "http://localhost/~catlee/build.json"
        # XXX: Manual override!
        print "Getting", file_url
        r = requests.get(file_url)
        if r.status_code != 200:
            print "Error", r.status_code, r.headers
            return

        builds = get_builds(change, r.json, "on-push")
        return builds

def trigger_builds(change, builds):
    db = sa.create_engine("sqlite:////home/catlee/mozilla/buildbot-configs/build-master/state.sqlite")
    for build in builds:
        print "triggering", build

        now = time.time()

        # Create a sourcestamp
        q = sa.text("""INSERT INTO sourcestamps
                (`branch`, `revision`, `patchid`, `repository`, `project`)
                VALUES
                (:branch, :revision, NULL, '', '')
                """)
        print q
        r = db.execute(q, branch=build['branch'], revision=build['revision'])
        ssid = r.lastrowid

        # TODO: Associate with change

        # Create a new buildset
        q = sa.text("""INSERT INTO buildsets
            (`external_idstring`, `reason`, `sourcestampid`, `submitted_at`, `complete`, `complete_at`, `results`)
            VALUES
            (:idstring, :reason, :sourcestampid, :submitted_at, 0, NULL, NULL)""")
        print q

        r = db.execute(q,
                idstring=None,
                reason="created by external_scheduler by change %i" % change['change']['number'],
                sourcestampid=ssid,
                submitted_at=now,
                )
        buildsetid = r.lastrowid

        # Create buildset properties
        for k, v in build.items():
            q = sa.text("""INSERT INTO buildset_properties
                    (`buildsetid`, `property_name`, `property_value`)
                    VALUES
                    (:buildsetid, :key, :value)""")
            print q
            r = db.execute(q,
                    buildsetid=buildsetid,
                    key=k,
                    value=json.dumps((v,'external_scheduler')),
                    )

        # Create a new build request
        q = sa.text("""INSERT INTO buildrequests
                (`buildsetid`, `buildername`, `submitted_at`, `priority`, `claimed_at`, `claimed_by_name`, `claimed_by_incarnation`, `complete`, `results`, `complete_at`)
                VALUES
                (:buildsetid, :buildername, :submitted_at, 0, 0, NULL, NULL, 0, NULL, NULL)""")
        print q

        buildername = "%s_%s_generic" % (build['branch'], build['platform'])
        r = db.execute(q,
                buildsetid=buildsetid,
                buildername=buildername,
                submitted_at=now,
                )

        new_brid = r.lastrowid

while True:
    while True:
        item = queuedir.pop()
        if not item:
            break
        item_id, fp = item
        change = json.load(fp)
        fp.close()
        builds = process(change)
        trigger_builds(change, builds)
        queuedir.remove(item_id)
    queuedir.wait(30)
