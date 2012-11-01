import json
import lame
import time
import base64
import Queue
import logging
import threading
import soundcloud
import scwaveform
import tornado.web
import tornado.ioloop
import tornado.template
import tornadio2.server
import multiprocessing
from capsule import Mixer
from random import shuffle
from metadata import Metadata
from operator import attrgetter
from assetcompiler import compiled
from sockethandler import SocketHandler

client = soundcloud.Client(client_id="b08793cf5964f5571db86e3ca9e5378f")
Metadata.client = client


def good_track(track):
    return track.streamable and track.duration < 360000 and track.duration > 90000


logging.basicConfig(format="%(asctime)s P%(process)-5d (%(levelname)8s) %(module)16s%(lineno)5d: %(uid)32s %(message)s")
log = logging.getLogger(__name__)

import sys
test = 'test' in sys.argv
frontend = 'frontend' in sys.argv
stream = not frontend


frame_seconds = lame.SAMPLES_PER_FRAME / 44100.0
LAG_LIMIT = 2  # seconds. If we're lagging by this much, drop packets and try again.

template_dir = "templates/"
templates = tornado.template.Loader(template_dir)
templates.autoescape = None


class Listeners(list):
    def __init__(self, queue, name):
        self.__queue = queue
        self.__name = name
        self.__packet = None
        self.__last_send = None
        self.__lag = 0
        list.__init__(self)

    def append(self, listener):
        if self.__packet:
            listener.write(self.__packet)
            listener.flush()
        list.append(self, listener)

    def broadcast(self):
        try:
            if self.__lag > LAG_LIMIT:
                log.error("Lag (%s) exceeds limit - dropping frames!",
                          self.__lag)
                self.__lag = 0

            self.__broadcast()
            if self.__last_send:
                self.__lag += (time.time() - self.__last_send) - frame_seconds
            else:
                log.info("Sending first frame for %s.", self.__name)
            self.__last_send = time.time()

            if self.__lag > 0:   # TODO: Doesn't this make this "leading?"
                log.warning("Queue %s lag detected. (%2.2f ms)",
                            self.__name, self.__lag * 1000)
                while self.__lag > 0 and not self.__queue.empty():
                    self.__broadcast()
                    self.__lag -= frame_seconds

            self.__last_send = time.time()
        except Queue.Empty:
            if self.__packet and not self.__starving:
                self.__starving = True
                log.warning("Dropping frames! Queue %s is starving!", self.__name)
            pass

    def __broadcast(self):
        self.__packet = self.__queue.get_nowait()
        self.__starving = False
        for listener in self:
            listener.write(self.__packet)
            listener.flush()


class MainHandler(tornado.web.RequestHandler):
    def get(self):
        debug = self.get_argument('__debug', None)
        if debug is None:
            debug = self.request.host.startswith("localhost")
        else:
            debug = debug in ["True", 1, "on"]
        kwargs = {
            'debug': debug,
            'compiled': compiled,
        }
        try:
            if test:
                s = open(template_dir + 'index.html').read()
                content = tornado.template.Template(s).generate(**kwargs)
            else:
                content = templates.load('index.html').generate(**kwargs)

            self.write(content)
        except Exception, e:
            log.error(e)
            tornado.web.RequestHandler.send_error(self, 500)


class InfoHandler(tornado.web.RequestHandler):
    actions = []
    offset = 60  # extra seconds to save info for

    @classmethod
    def add(self, data):
        self.clean()
        self.actions.append(data)
        SocketHandler.on_data(data)

    @classmethod
    def clean(cls):
        now = time.time()
        while cls.actions and cls.actions[0]['time'] \
                     + cls.actions[0]['duration'] + cls.offset < now:
            cls.actions.pop(0)

    def get(self):
        self.write(json.dumps(self.actions))


class StreamHandler(tornado.web.RequestHandler):
    __subclasses = []
    listeners = []

    @classmethod
    def stream_frames(cls):
        for klass in cls.__subclasses:
            klass.listeners.broadcast()

    @classmethod
    def init_streams(cls, streams):
        routes = []
        for endpoint, name, q in streams:
            klass = type(
                name + "Handler",
                (StreamHandler,),
                {"listeners": Listeners(q, name)}
            )
            cls.__subclasses.append(klass)
            routes.append((endpoint, klass))
        return routes

    @tornado.web.asynchronous
    def get(self):
        log.info("Added new listener at %s", self.request.remote_ip)
        self.set_header("Content-Type", "audio/mpeg")
        self.listeners.append(self)

    def on_finish(self):
        log.info("Removed listener at %s", self.request.remote_ip)
        self.listeners.remove(self)


class BufferedReadQueue(Queue.Queue):
    def __init__(self, lim=None):
        self.raw = multiprocessing.Queue(lim)
        self.__listener = threading.Thread(target=self.listen)
        self.__listener.setDaemon(True)
        self.__listener.start()
        Queue.Queue.__init__(self, lim)

    def listen(self):
        try:
            while True:
                self.put(self.raw.get())
        except:
            pass

    @property
    def buffered(self):
        return self.qsize()


def start():
    track_queue = multiprocessing.Queue(1)
    v2_queue = BufferedReadQueue()
    info_queue = multiprocessing.Queue()

    mixer = Mixer(iqueue=track_queue,
                  oqueues=(v2_queue.raw,),
                  infoqueue=info_queue)
    mixer.start()

    at = threading.Thread(target=add_tracks, args=(track_queue,))
    at.daemon = True
    if stream:
        at.start()

    pi = threading.Thread(target=parse_info, args=(info_queue,))
    pi.daemon = True
    pi.start()

    class SocketConnection(tornadio2.conn.SocketConnection):
        __endpoints__ = {"/info.websocket": SocketHandler}

    stream_routes = StreamHandler.init_streams([
        (r"/all.mp3", "All", v2_queue)
    ])

    application = tornado.web.Application(
        tornadio2.TornadioRouter(SocketConnection).apply_routes([
            # Static assets for local development
            (r"/(favicon.ico)", tornado.web.StaticFileHandler, {"path": "static/img/"}),
            (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": "static/"}),
            (r"/all.json", InfoHandler),
            (r"/", MainHandler)
        ] + stream_routes),
        socket_io_port=8193,
        enabled_protocols=['websocket', 'xhr-multipart', 'xhr-polling', 'jsonp-polling']
    )

    frame_sender = tornado.ioloop.PeriodicCallback(
        StreamHandler.stream_frames, frame_seconds * 1000
    )
    frame_sender.start()

    cleaner = tornado.ioloop.PeriodicCallback(
        InfoHandler.clean, 5 * 1000
    )
    cleaner.start()

    application.listen(8192)
    tornadio2.server.SocketServer(application)


def add_tracks(track_queue):
    sent = 0
    try:
        while test:
            tracks = client.get('/tracks', q='sobot', license='cc-by', limit=4)
            for track in tracks:
                if track.title != "Bright Night":
                    track_queue.put(track.obj)

        l = 1000

        offsets = range(0, 4) * l
        shuffle(offsets)
        for i in offsets:
            tracks = client.get('/tracks', order='hotness', limit=l, offset=i)
            tracks = filter(good_track, tracks)
            for track in tracks:
                if track.bpm:
                    print "track", track.title, "has bpm", track.bpm

            for track in sorted(tracks, key=attrgetter('bpm')):
                if track.bpm:
                    print "track", track.title, "has bpm", track.bpm

            shuffle(tracks)

            for track in tracks:
                log.info("Adding new track.")
                track_queue.put(track.obj)
                sent += 1
                log.info("Added new track.")
    finally:
        pass


def parse_info(info_queue):
    """
    Listen to streams of info from the remixer process and generate proper
    metadata about it. I.e.: Waveform images, colours, and the frontend data.

    TODO: Make this stream-agnostic (ideally, move info parsing into each stream.)
    """
    while True:
        action = info_queue.get()
        if len(action['tracks']) == 2:
            m1 = Metadata(action['tracks'][0]['metadata'])
            s1 = action['tracks'][0]['start']
            e1 = action['tracks'][0]['end']

            m2 = Metadata(action['tracks'][1]['metadata'])
            s2 = action['tracks'][1]['start']
            e2 = action['tracks'][1]['end']

            log.info("Processing metadata for %s -> %s, (%2.2fs %2.2fs) -> (%2.2fs, %2.2fs).",
                        m1.title, m2.title, s1, s2, e1, e2)

            a = scwaveform.generate([s1, s2], [e1, e2],
                                    [m1.color, m2.color],
                                    [m1.waveform_url, m2.waveform_url],
                                    [m1.duration, m2.duration],
                                    action['duration'])
        else:
            for track in action['tracks']:
                metadata = Metadata(track['metadata'])
                start = track['start']
                end = track['end']

                log.info("Processing metadata for %s, %2.2fs -> %2.2fs.",
                            metadata.title, start, end)
                a = scwaveform.generate(start, end, metadata.color,
                                        metadata.waveform_url,
                                        metadata.duration,
                                        action['duration'])
        action['waveform'] = "data:image/png;base64,%s" % \
                              base64.encodestring(a)
        action['width'] = int(action['duration'] * scwaveform.DEFAULT_SPEED)
        InfoHandler.add(action)

if __name__ == "__main__":
    start()