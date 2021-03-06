"""Web site."""
from __future__ import print_function


import logging
import sys
import os
import socket
import struct
import random
import json

import zmq
from zmq.eventloop import ioloop
from zmq.eventloop.zmqstream import ZMQStream

ioloop.install()

import tornado.ioloop
import tornado.web
import tornado.template
import tornado.gen

import sockjs.tornado

import orwell.messages.controller_pb2 as pb_controller
import orwell.messages.server_game_pb2 as pb_server_game

RANDOM = random.Random()
RANDOM.seed(42)


class MainHandler(tornado.web.RequestHandler):
    handler = None

    def initialize(self):
        # import ipdb; ipdb.set_trace()
        print(os.getcwd())
        self._loader = tornado.template.Loader("data")
        broadcast = Broadcast()
        print(broadcast.push_address + " / " + broadcast.subscribe_address)
        self._push_address = broadcast.push_address
        self._subscribe_address = broadcast.subscribe_address
        self._context = zmq.Context.instance()
        push_socket = self._context.socket(zmq.PUSH)
        push_socket.connect(self._push_address)
        self._push_stream = ZMQStream(push_socket)
        subscribe_socket = self._context.socket(zmq.SUB)
        subscribe_socket.connect(self._subscribe_address)
        subscribe_socket.setsockopt(zmq.SUBSCRIBE, "")
        self._subscribe_stream = ZMQStream(subscribe_socket)
        self._subscribe_stream.on_recv(self._handle_message_parts)
        self._routing_id = "temporary_id_" + str(RANDOM.randint(0, 32768))
        MainHandler.handler = self

    @tornado.web.asynchronous
    def get(self):
        content = self._loader.load("index.html").generate(
                videofeed="/test",
                status="well let's say pending")
        self.write(content)
        hello = self._build_hello()
        print("Send Hello: " + repr(hello))
        # self._subscribe_stream.send(hello)
        self._push_stream.send(hello)

    @tornado.web.asynchronous
    def _handle_message_parts(self, message_parts):
        # print('message received: %s' % map(repr, message_parts))
        for message in message_parts:
            recipient, _, typed_payload = message.partition(' ')
            message_type, _, payload = typed_payload.partition(' ')
            self._handle_message(recipient, message_type, payload)

    def _handle_message(self, recipient, message_type, payload):
        if (self._destination_matches(recipient)):
            if ("Welcome" == message_type):
                self._handle_welcome(payload)
            elif ("Goodbye" == message_type):
                self._handle_goodbye(payload)
            elif ("GameState" == message_type):
                self._handle_game_state(payload)
            else:
                print("Message ignored: " + message_type)

    def _destination_matches(self, recipient):
        return True  # for now

    def _handle_welcome(self, payload):
        message = pb_server_game.Welcome()
        message.ParseFromString(payload)
        print(
            "Welcome ; id = " + str(message.id) +
            " ; video_address = " + message.video_address +
            " ; video_port = " + str(message.video_port))
        self._routing_id = str(message.id)
        if (message.game_state):
            print("playing ? " + str(message.game_state.playing))
            print("time left: " + str(message.game_state.seconds))
            for team in message.game_state.teams:
                print(team.name + " (" + str(team.num_players) +
                      ") -> " + str(team.score))
        videofeed = message.video_address + ":" + str(message.video_port)
        print("videofeed =", videofeed,
              ";", len(OrwellConnection.all_connections))
        video_url = "/video?address={}&port={}".format(
            message.video_address,
            str(message.video_port))
        # video_url = "/test?address={}&port={}".format(
            # message.video_address,
            # str(message.video_port))
        json_str = json.dumps({"videofeed": video_url})
        OrwellConnection.data_to_send.append(json_str)
        # for connection in OrwellConnection.all_connections:
            # print("send videofeed(" + json_str + ") to", connection)
            # connection.send(json_str)
        print("_handle_welcome - finish")
        self.finish()

    def _handle_goodbye(self, payload):
        message = pb_server_game.Goodbye()
        message.ParseFromString(payload)
        print("Goodbye ...")
        print("_handle_goodbye - finish")
        self.finish()

    def _handle_game_state(self, payload):
        message = pb_server_game.GameState()
        message.ParseFromString(payload)
        if (message.HasField("winner")):
            status = "Game won by team " + message.winner
        else:
            if (message.playing):
                status = "Game running"
                if (message.HasField("seconds")):
                    status += " ({} second(s) left)".format(message.seconds)
            else:
                status = "Game NOT running"
        print(status)
        for connection in OrwellConnection.all_connections:
            connection.send(json.dumps({"status": status}))

    def _build_hello(self):
        pb_message = pb_controller.Hello()
        name = "JAMBON"
        pb_message.name = name
        payload = pb_message.SerializeToString()
        return self._routing_id + ' Hello ' + payload

    def send_input(self, data):
        factor = 0.5
        left = 0
        right = 0
        fire_weapon1 = False
        fire_weapon2 = False
        if ("LEFT" == data):
            left = -1 * factor
            right = 1 * factor
        elif ("FORWARD" == data):
            left = 1 * factor
            right = 1 * factor
        elif ("RIGHT" == data):
            left = 1 * factor
            right = -1 * factor
        elif ("BACKWARD" == data):
            left = -1 * factor
            right = -1 * factor
        elif ("FIRE1" == data):
            fire_weapon1 = True
        elif ("FIRE2" == data):
            fire_weapon2 = True
        pb_input = pb_controller.Input()
        pb_input.move.left = left
        pb_input.move.right = right
        pb_input.fire.weapon1 = fire_weapon1
        pb_input.fire.weapon2 = fire_weapon2
        payload = pb_input.SerializeToString()
        message = self._routing_id + ' Input ' + payload
        # self._subscribe_stream.send(hello)
        self._push_stream.send(message)


class VideoHandler(tornado.web.RequestHandler):
    handler = None

    def initialize(self):
        self._data_chunk_size = 10000
        self._stop = False

    @tornado.web.asynchronous
    @tornado.gen.coroutine
    def get(self):
        address = self.get_argument('address')
        port = self.get_argument('port')
        print("address =", address, "; port =", port)
        # address = "192.168.0.17"
        # port = 5000
        self.set_header(
            "content-type",
            "multipart/x-mixed-replace; boundary=--ThisRandomString")
        command = "nc {address} {port}".format(address=address, port=port)
        command += ' | gst-launch-1.0 filesrc location=/dev/fd/0'
        command += ' ! h264parse'
        command += ' ! avdec_h264'
        command += ' ! jpegenc'
        command += ' ! multipartmux'
        command += ' ! filesink location=/dev/stdout'
        print("command =", command)
        import subprocess
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            bufsize=-1,
            shell=True)
        print("starting polling loop.")
        while (not self._stop):
            # import datetime
            # sys.stdout.write(" " + datetime.datetime.now().isoformat())
            chars = self._process.stdout.read(self._data_chunk_size)
            self.write(chars)
            if (self._process.poll() is not None):
                print("stopping polling loop")
                self._stop = True
            yield tornado.gen.Task(self.flush)
        self._process.terminate()
        print("TestHandler::get - finish")
        self.finish()

    def on_connection_close(self):
        print("on_connection_close")
        self._stop = True
        super(self.__class__, self).on_connection_close()


class TestHandler(tornado.web.RequestHandler):
    handler = None

    def initialize(self):
        self._data_chunk_size = 10000
        self._stop = False

    @tornado.web.asynchronous
    @tornado.gen.coroutine
    def get(self):
        self.set_header(
            "content-type",
            "video/ogg")
        command = 'echo "--video boundary--" ;'
        command += 'gst-launch-1.0 -e -q videotestsrc is-live=true' \
            + ' ! video/x-raw, framerate=5/1, width=1024, height=768' \
            + ' ! clockoverlay shaded-background=true font-desc="Sans 38"' \
            + ' ! theoraenc' \
            + ' ! oggmux max-delay=0' \
            + ' ! filesink location=/dev/stdout'
        import subprocess
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            bufsize=-1,
            shell=True)
        print("starting polling loop.")
        while (not self._stop):
            # import datetime
            # sys.stdout.write(" " + datetime.datetime.now().isoformat())
            chars = self._process.stdout.read(self._data_chunk_size)
            self.write(chars)
            if (self._process.poll() is not None):
                print("stopping polling loop")
                self._stop = True
            yield tornado.gen.Task(self.flush)
        self._process.terminate()
        print("TestHandler::get - finish")
        self.finish()

    def on_connection_close(self):
        print("on_connection_close")
        self._stop = True
        super(self.__class__, self).on_connection_close()


class OrwellConnection(sockjs.tornado.SockJSConnection):
    all_connections = set()
    data_to_send = []

    def on_open(self, info):
        print("on_open - info = " + str(info))
        print("on_open - info.ip = " + str(info.ip))
        print("on_open - info.cookies = " + str(info.cookies))
        print("on_open - info.arguments = " + str(info.arguments))
        print("on_open - info.headers = " + str(info.headers))
        print("on_open - info.path = " + str(info.path))
        OrwellConnection.all_connections.add(self)
        for data in OrwellConnection.data_to_send:
            self.send(data)

    def on_message(self, message):
        print("on_message - message = " + str(message))
        if (MainHandler.handler is not None):
            MainHandler.handler.send_input(message)

    def on_close(self):
        print("on_close")
        OrwellConnection.all_connections.remove(self)


def make_app():
    router = sockjs.tornado.SockJSRouter(OrwellConnection, '/orwell')
    static_path = os.path.join(os.getcwd(), 'data', 'static')
    return tornado.web.Application(
        [(r"/", MainHandler),
         (r'/static/', tornado.web.StaticFileHandler),
         (r'/video', VideoHandler),
         (r'/test', TestHandler),
         ] + router.urls,
        debug=True,
        static_path=static_path)


class Broadcast(object):
    def __init__(self, port=9080, retries=5, timeout=10):
        self._size = 512
        self._retries = retries
        self._group = ('225.0.0.42', port)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.settimeout(timeout)
        ttl = struct.pack('b', 1)
        self._socket.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        self._received = False
        self._data = None
        self._sender = None
        self._decding_successful = False
        self.send_all_broadcast_messages()

    def send_all_broadcast_messages(self):
        tries = 0
        while ((tries < self._retries) and (not self._received)):
            self.send_one_broadcast_message()
            tries += 1
        if (self._received):
            self.decode_data()

    def send_one_broadcast_message(self):
        try:
            sent = self._socket.sendto(
                    "<broadcast>".encode("ascii"), self._group)
            while not self._received:
                try:
                    self._data, self._sender = self._socket.recvfrom(
                            self._size)
                    self._received = True
                except socket.timeout:
                    print('timed out, no more responses', file=sys.stderr)
                    break
                else:
                    print(
                        'received "%s" from %s'
                        % (repr(self._data), self._sender),
                        file=sys.stderr)
        finally:
            print('closing socket', file=sys.stderr)
            self._socket.close()

    def decode_data(self):
        # data (split on multiple lines for clarity):
        # 0xA0
        # size on 8 bytes
        # Address of puller
        # 0xA1
        # size on 8 bytes
        # Address of publisher
        # 0x00
        import struct
        to_char = lambda x: struct.unpack('B', x)[0]
        to_str = lambda x: x.decode("ascii")
        assert(self._data[0] == '\xa0')
        puller_size = to_char(self._data[1])
        # print("puller_size = " + str(puller_size))
        end_puller = 2 + puller_size
        puller_address = to_str(self._data[2:end_puller])
        # print("puller_address = " + puller_address)
        assert(self._data[end_puller] == '\xa1')
        publisher_size = to_char(self._data[end_puller + 1])
        # print("publisher_size = " + str(publisher_size))
        end_publisher = end_puller + 2 + publisher_size
        publisher_address = to_str(self._data[end_puller + 2:end_publisher])
        # print("publisher_address = " + publisher_address)
        assert(self._data[end_publisher] == '\x00')
        sender_ip, _ = self._sender
        self._push_address = puller_address.replace('*', sender_ip)
        self._subscribe_address = publisher_address.replace('*', sender_ip)
        self._decding_successful = True

    @property
    def push_address(self):
        return self._push_address

    @property
    def subscribe_address(self):
        return self._subscribe_address


def main(argv=sys.argv[1:]):
    """Entry point for the tests and program."""
    app = make_app()
    app.listen(5000)
    # tornado.ioloop.IOLoop.current().start()
    try:
        ioloop.IOLoop.instance().start()
    except KeyboardInterrupt:
        print('Interrupted')


if ("__main__" == __name__):
    sys.exit(main(sys.argv[1:]))  # pragma: no coverage
