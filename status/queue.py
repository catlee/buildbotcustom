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
    # How long before things are considered to be "old"
    # Also how long between cleanup jobs
    cleanup_time = 300 # 5 minutes

    # Should the producer do cleanup?
    producer_cleanup = True

    # Mapping of names to QueueDir instances
    _objects = {}

    def __init__(self, name, queue_dir):
        self._objects[name] = self
        self.queue_dir = queue_dir

        self.pid = os.getpid()
        self.started = int(time.time())
        self.count = 0
        self.last_cleanup = 0

        self.tmp_dir = os.path.join(self.queue_dir, 'tmp')
        self.new_dir = os.path.join(self.queue_dir, 'new')
        self.cur_dir = os.path.join(self.queue_dir, 'cur')
        self.log_dir = os.path.join(self.queue_dir, 'logs')
        self.dead_dir = os.path.join(self.queue_dir, 'dead')

        self.setup()

    @classmethod
    def getQueue(cls, name):
        return cls._objects[name]

    def setup(self):
        for d in (self.tmp_dir, self.new_dir, self.cur_dir, self.log_dir, self.dead_dir):
            if not os.path.exists(d):
                os.makedirs(d, 0700)
            else:
                os.chmod(d, 0700)

        self.cleanup()

    def cleanup(self):
        """
        Removes old items from tmp
        Removes old logs from log_dir
        Moves old items from cur into new

        'old' is defined by the cleanup_time property
        """
        now = time.time()
        if now - self.last_cleanup < self.cleanup_time:
            return
        self.last_cleanup = now
        dirs = [self.tmp_dir, self.log_dir]
        for d in dirs:
            for f in os.listdir(d):
                fn = os.path.join(d, f)
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
        # write data to tmp
        fd, tmp_name = tempfile.mkstemp(prefix="%i-%i-%i.0" % (self.started, self.count, self.pid),
                dir=self.tmp_dir)
        os.write(fd, data)
        os.close(fd)

        dst_name = os.path.join(self.new_dir, os.path.basename(tmp_name))
        os.rename(tmp_name, dst_name)
        self.count += 1

        if self.producer_cleanup:
            self.cleanup()

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
            items.sort(key=lambda f: os.path.getmtime(os.path.join(self.new_dir, f)))
        for item in items:
            try:
                dst_name = os.path.join(self.cur_dir, item)
                os.rename(os.path.join(self.new_dir, item), dst_name)
                os.utime(dst_name, None)
                return item, open(dst_name, 'rb')
            except OSError:
                pass
        return None

    def touch(self, item_id):
        """
        Indicate that we're still working on this item
        """
        fn = os.path.join(self.cur_dir, item_id)
        os.utime(fn, None)

    def getcount(self, item_id):
        """
        Returns how many times this item has been run
        """
        try:
            return int(item_id.split(".")[1])
        except:
            return 0

    def getlogname(self, item_id):
        if "." in item_id:
            item_id = item_id.split(".")[0]
        fn = os.path.join(self.log_dir, "%s.log" % item_id)
        return fn

    def getlog(self, item_id):
        """
        Creates and returns a file object for a log file for this item
        """
        return open(self.getlogname(item_id), "a+")

    def log(self, item_id, msg):
        self.getlog(item_id).write(msg)

    def remove(self, item_id):
        """
        Removes item_id from cur
        """
        os.unlink(os.path.join(self.cur_dir, item_id))

    def requeue(self, item_id):
        """
        Moves item_id from cur back into new, incrementing the counter at the
        end
        """
        try:
            core_item_id, count = item_id.split(".")
            count = int(count)+1
        except:
            core_item_id = item_id
            count = 1
        dst_name = os.path.join(self.new_dir, "%s.%i" % (core_item_id, count))
        os.rename(os.path.join(self.cur_dir, item_id), dst_name)
        os.utime(dst_name, None)

    def murder(self, item_id):
        """
        Moves item_id and log from cur into dead for future inspection
        """
        dst_name = os.path.join(self.dead_dir, item_id)
        os.rename(os.path.join(self.cur_dir, item_id), dst_name)
        if os.path.exists(self.getlogname(item_id)):
            dst_name = os.path.join(self.dead_dir, "%s.log" % item_id)
            os.rename(self.getlogname(item_id), dst_name)

    if pyinotify:
        def wait(self, timeout=None):
            """
            Waits for new items to arrive in new, call cb when we have something
            """
            wm = pyinotify.WatchManager()
            try:
                wm.add_watch(self.new_dir, pyinotify.IN_MOVED_TO)

                notifier = pyinotify.Notifier(wm, _MovedHandler())
                notifier.check_events(timeout)
                notifier.process_events()
            finally:
                wm.close()
