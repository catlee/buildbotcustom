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
    cleanup_time = 300 # 5 minutes
    _objects = {}
    def __init__(self, name, queue_dir):
        assert name not in self._objects
        self._objects[name] = self
        self.queue_dir = queue_dir

        self.pid = os.getpid()
        self.started = int(time.time())
        self.count = 0

        self.tmp_dir = os.path.join(self.queue_dir, 'tmp')
        self.new_dir = os.path.join(self.queue_dir, 'new')
        self.cur_dir = os.path.join(self.queue_dir, 'cur')

        self.setup()

    @classmethod
    def getQueue(cls, name)
        return cls._objects[name]

    def setup(self):
        for d in (self.tmp_dir, self.new_dir, self.cur_dir):
            if not os.path.exists(d):
                os.makedirs(d, 0700)
            else:
                os.chmod(d, 0700)

        self.cleanup()

    def cleanup(self):
        """
        Removes old items from tmp
        Moves old items from cur into new

        'old' is defined by the cleanup_time property
        """
        now = time.time()
        for f in os.listdir(self.tmp_dir):
            fn = os.path.join(self.tmp_dir, f)
            try:
                if os.path.getmtime(fn) < now - self.cleanup_time:
                    os.unlink(fn)
            except OSError:
                pass

        for f in os.listdir(self.cur_dir):
            fn = os.path.join(self.cur_dir, f)
            try:
                if os.path.getmtime(fn) < now - self.cleanup_time:
                    self.requeue(f)
            except OSError:
                pass

    ###
    # For producers
    ###
    def add(self, data):
        """
        Adds a new item to the queue
        """
        self.cleanup()
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
        self.cleanup()
        items = os.listdir(self.new_dir)
        if sorted:
            items.sort(key=_intkeys)
        for item in items:
            try:
                dst_name = os.path.join(self.cur_dir, item)
                os.rename(os.path.join(self.new_dir, item), dst_name)
                os.utime(dst_name, None)
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
        os.utime(dst_name, None)

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
