"""
Implement an on-disk queue for stuff
"""
import os, tempfile, time, re

try:
    import pyinotify
    assert pyinotify
except ImportError:
    pyinotify = None

def _maybeint(i):
    try:
        return int(i)
    except:
        return i

def _intkeys(s):
    parts = [p for p in re.split("(\d+)", s) if p != '']
    return [_maybeint(p) for p in parts]

class _MovedHandler(pyinotify.ProcessEvent):
    def process_IN_MOVED_TO(self, event):
        pass

class QueueDir(object):
    def __init__(self, queue_dir):
        self.queue_dir = queue_dir

        self.pid = os.getpid()
        self.started = int(time.time())
        self.count = 0

        self.tmp_dir = os.path.join(self.queue_dir, 'tmp')
        self.new_dir = os.path.join(self.queue_dir, 'new')
        self.cur_dir = os.path.join(self.queue_dir, 'cur')

        self.setup()

    def setup(self):
        for d in (self.tmp_dir, self.new_dir, self.cur_dir):
            if not os.path.exists(d):
                os.makedirs(d, 0700)
            else:
                os.chmod(d, 0700)

        # Clean out stuff in tmp.
        # TODO: This assumes we're the only producer. Limit this by mtime?
        for f in os.listdir(self.tmp_dir):
            os.unlink(os.path.join(self.tmp_dir, f))

        # TODO: What about cur? Probably should look in cur and move back into
        # new on some interval if stuff gets too old

    ###
    # For producers
    ###
    def add(self, data):
        """
        Adds a new item to the queue
        """
        # write data to tmp
        fd, tmp_name = tempfile.mkstemp(prefix="%i-%i-%i" % (self.started, self.count, self.pid),
                dir=self.tmp_dir)
        os.write(fd, data)
        os.close(fd)

        dst_name = os.path.join(self.new_dir, os.path.basename(tmp_name))
        os.rename(tmp_name, dst_name)
        self.count += 1

    ###
    # For consumers
    ###
    def pop(self, sorted=True):
        """
        Moves an item from new into cur
        Returns item_id, file handle
        Returns None if queue is empty
        If sorted is True, then the earliest item is returned
        """
        items = os.listdir(self.new_dir)
        if sorted:
            items.sort(key=_intkeys)
        for item in items:
            try:
                dst_name = os.path.join(self.cur_dir, item)
                os.rename(os.path.join(self.new_dir, item), dst_name)
                return item, open(dst_name, 'rb')
            except OSError:
                pass
        return None

    def remove(self, item_id):
        """
        Removes item_id from cur
        """
        os.unlink(os.path.join(self.cur_dir, item_id))

    def requeue(self, item_id):
        """
        Moves item_id from cur back into new
        """
        dst_name = os.path.join(self.new_dir, item_id)
        os.rename(os.path.join(self.cur_dir, item_id), dst_name)

    def wait(self, timeout=None):
        """
        Waits for new items to arrive in new, call cb when we have something
        """
        assert pyinotify
        wm = pyinotify.WatchManager()
        try:
            wm.add_watch(self.new_dir, pyinotify.IN_MOVED_TO)

            notifier = pyinotify.Notifier(wm, _MovedHandler())
            notifier.check_events(timeout)
            # TODO: need to call these?
            #notifier.read_events()
            notifier.process_events()
        finally:
            wm.close()
