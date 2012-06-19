from buildbot.schedulers.base import BaseScheduler
from buildbot.steps.shell import WithProperties
from buildbot.util import json
from buildbot.schedulers.filter import ChangeFilter

from buildbotcustom.process.factory import ScriptFactory

def makeGenericBuilder(branch, platform, slaves):
    factory = ScriptFactory(
            #scriptRepo="http://hg.mozilla.org/build/tools",
            scriptRepo="http://hg.mozilla.org/catlee_mozilla.com/buildtools",
            scriptName="scripts/generic.py",
            extra_args=[WithProperties("%(cmd_url)s")],
            # TODO: windows support!
            reboot_command="sudo reboot",
            platform=platform,
            )

    builder = {
            'name': '%s_%s_generic' % (branch, platform),
            'slavenames': slaves,
            'factory': factory,
            'category': branch,
            'properties': {
                'branch': branch,
                'platform': platform,
            },
    }
    return builder

def canMergeGenericRequests(builder, req1, req2):
    p1 = req1.properties()
    p2 = req2.properties()

    if p1['job_type'] != p2['job_type']:
        return False

    if p1['branch'] != p2['branch']:
        return False

    if p1['platform'] != p2['platform']:
        return False

    if not p1.get('mergeable', True) or not p2.get('mergeable', True):
        return False

    return req1.canBeMergedWith(req2)

class ExternalScheduler(BaseScheduler):
    """Scheduler that uses a QueueDir to process new changes. External
    processes are required to handle new items in the queue and insert the
    ppropriate build requests directly into the database."""
    fileIsImportant = None
    def __init__(self, name, queuedir, settings, fileIsImportant=None, **kwargs):
        self.queuedir = queuedir
        self.settings = settings
        self.change_filter = ChangeFilter()
        self.fileIsImportant = fileIsImportant
        BaseScheduler.__init__(self, name=name, builderNames=[], properties={}, **kwargs)

    def run(self):
        d = self.parent.db.runInteraction(self.process_changes)
        return d

    def get_initial_state(self, max_changeid):
        return {"last_processed": max_changeid}

    def process_changes(self, t):
        cm = self.parent.change_svc
        state = self.get_state(t)
        state_changed = False
        last_processed = state.get("last_processed", None)

        if last_processed is None:
            # Is get_initial_state working?
            raise ValueError("last_processed is None")

        changes = cm.getChangesGreaterThan(last_processed, t)
        important_changes = []
        for c in changes:
            if self.change_filter.filter_change(c):
                important = True
                if self.fileIsImportant:
                    important = self.fileIsImportant(c)
                if important:
                    important_changes.append(c)

        # Write out to the queue
        for c in important_changes:
            data = {
                    'master_name': self.parent.parent.botmaster.master_name,
                    'master_incarnation': self.parent.parent.botmaster.master_incarnation,
                    'change': c.asDict(),
                    'settings': self.settings,
                    }
            self.queuedir.add(json.dumps(data))

        # now that we've handled each change, we can update the
        # last_processed record
        if changes:
            max_changeid = max([c.number for c in changes])
            state["last_processed"] = max_changeid # retain other keys
            state_changed = True

        if state_changed:
            self.set_state(t, state)

class GenericTimedScheduler(BaseScheduler):
    """Handle timed stuff, like PGO and nightlies"""
    def __init__(self, **kwargs):
        BaseScheduler.__init__(self, **kwargs)

class GenericTriggeredScheduler(BaseScheduler):
    """Handle triggered stuff, like builds finishing and kicking off tests"""
    def __init__(self, **kwargs):
        BaseScheduler.__init__(self, **kwargs)
