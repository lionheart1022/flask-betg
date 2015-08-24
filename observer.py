#!/usr/bin/env python3

# Observer daemon:
# listens on some port/path,
# may have some children configured,
# knows how many streams can it handle.
# Also each host accepts connections only from its known siblings.
#
# Messaging flow:
#
# Client -> Master: Please watch the stream with URL ... for game ...
# Master: checks if this stream is already watched
# Master -> Slave1: Can you watch one more stream? (details)
# Slave1 -> Master: no
# Master -> Slave2: Can you watch one more stream? (details)
# Slave2 -> Master: yes
# (or if none agreed - tries to watch itself)
# ...
# Slave2 -> Master: stream X finished, result is Xres
# Master -> Poller: stream X done

# API:
# PUT /streams/id - watch the stream (client->master->slave)
# GET /streams/id - check stream status (master->slave)

import eventlet
eventlet.monkey_patch() # before loading flask

from flask import Flask, jsonify, request, abort as flask_abort
from flask.ext import restful
from flask.ext.sqlalchemy import SQLAlchemy
from flask.ext.restful import fields, marshal
from flask.ext.restful.reqparse import RequestParser
from flask.ext.restful.utils import http_status_message
from werkzeug.exceptions import default_exceptions
from werkzeug.exceptions import HTTPException, BadRequest, MethodNotAllowed, Forbidden, NotImplemented, NotFound

import os
from datetime import datetime, timedelta
from eventlet.green import subprocess
import requests
import logging

import config
from observer_conf import SELF_URL, PARENT, CHILDREN, MAX_STREAMS

# if stream happens to be online, wait some time...
WAIT_DELAY = 30 # seconds between retries
WAIT_MAX = 360 # 3 hours

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = config.DB_URL
app.config['ERROR_404_HELP'] = False # disable this flask_restful feature
db = SQLAlchemy(app)
api = restful.Api(app)

# Fix request context's remote_addr property to respect X-Real-IP header
from flask import Request
from werkzeug.utils import cached_property
class MyRequest(Request):
    @cached_property
    def remote_addr(self):
        """The remote address of the client, with respect to X-Real-IP header"""
        return self.headers.get('X-Real-IP') or super().remote_addr
app.request_class = MyRequest


# JSONful error handling
log = app.logger
def make_json_error(ex):
    code = getattr(ex, 'code', 500)
    if hasattr(ex, 'data'):
        response = jsonify(**ex.data)
    else:
        response = jsonify(error_code = code, error = http_status_message(code))
    response.status_code = code
    return response
for code in default_exceptions.keys():
    app.error_handler_spec[None][code] = make_json_error

def abort(message, code=400, **kwargs):
    data = {'error_code': code, 'error': message}
    if kwargs:
        data.update(kwargs)

    log.warning('Aborting request {} /{}: {}'.format(
        # GET /v1/smth
        request.method,
        request.base_url.split('//',1)[-1].split('/',1)[-1],
        ', '.join(['{}: {}'.format(*i) for i in data.items()])))

    try:
        flask_abort(code)
    except HTTPException as e:
        e.data = data
        raise
restful.abort = lambda code,message: abort(message,code) # monkey-patch to use our approach to aborting
restful.utils.error_data = lambda code: {
    'error_code': code,
    'error': http_status_message(code)
}


# Restrict list of allowed hosts
def getsiblings():
    import socket
    ret = set()
    for host in list(CHILDREN.values()) + ([PARENT[1], 'localhost']
                                           if PARENT else
                                           ['localhost']):
        if not host:
            continue # skip empty addrs, e.g. parent for master node
        host = host.split('://',1)[-1].split(':',1)[0] # cut off protocol and port
        h, a, ips = socket.gethostbyname_ex(host)
        ret.update(ips)
    return ret
NEIGHBOURS = getsiblings()
@app.before_request
def restrict_siblings():
    if request.remote_addr not in NEIGHBOURS:
        log.debug('Attempt to request from unknown address '+request.remote_addr)
        raise Forbidden


def init_app(logfile=None):
    app.logger.setLevel(logging.DEBUG)

    logger = logging.FileHandler(logfile) if logfile else logging.StreamHandler()
    logger.setFormatter(logging.Formatter('[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s'))
    logger.setLevel(logging.DEBUG)
    app.logger.addHandler(logger)

    return app


# declare model
class Stream(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # Twitch stream handle
    handle = db.Column(db.String(64), nullable=False, unique=True)

    # Which child handles this stream? None if self
    child = db.Column(db.String(64), default=None)

    # Gametype and other metadata goes below
    gametype = db.Column(db.String(64), default=None)

    # this is an ID of Game object.
    # We don't use foreign key because we may reside on separate server
    # and use separate db.
    game_id = db.Column(db.Integer, unique=True)

    state = db.Column(db.Enum('waiting', 'watching', 'found', 'failed'),
                      default='waiting')

    creator = db.Column(db.String(128))
    opponent = db.Column(db.String(128))

    @classmethod
    def find(cls, id):
        return cls.query.filter_by(handle=id).first()


# Main logic
ROOT = os.path.dirname(os.path.abspath(__file__))
class Handler:
    """
    This hierarchy is similar to Poller's one
    """
    gametypes = []
    path = None
    env = None
    process = None
    quorum = 5 # min results, or
    maxdelta = timedelta(seconds=10)

    @classmethod
    def find(cls, gametype):
        if gametype in cls.gametypes:
            return cls
        for sub in cls.__subclasses__():
            ret = sub.find(gametype)
            if ret:
                return ret

    def __init__(self, stream):
        self.stream = stream

    def start(self):
        log.info('spawning handler')
        eventlet.spawn(self.watch_tc)
        pool[self.stream.handle] = self

    def abort(self):
        pass

    def watch_tc(self):
        log.info('watch_tc started')
        try:
            result = self.watch()
            waits = 0
            while result == 'offline':
                if waits > WAIT_MAX:
                    # will be caught below
                    raise Exception('We waited for too long, '
                                    'abandoning stream '+self.stream.handle)
                log.info('Stream {} is offline, waiting'
                            .format(self.stream.handle))
                self.stream.state = 'waiting'
                #db.session.commit()
                # wait & retry
                eventlet.sleep(WAIT_DELAY)
                result = self.watch()
                waits += 1
            return result
        except Exception:
            log.exception('Watching failed')

            self.stream.state = 'failed'
            #db.session.commit()
            # mark it as Done anyway
            self.done('failed', datetime.utcnow().timestamp())
        finally:
            # mark that this stream has stopped
            del pool[self.stream.handle]

    def watch(self):
        # start subprocess and watch its output

        # first, chdir to this script's directory
        os.chdir(ROOT)

        # then, if required, chdir handler's requested dir (relative to script's)
        if self.path:
            os.chdir(self.path)
        cmd = self.process.format(handle = self.stream.handle)
        if self.env:
            cmd = '. {}/bin/activate; {}'.format(self.env, cmd)
        log.info('starting process...')
        sub = subprocess.Popen(
            cmd,
            bufsize = 1, # line buffered
            universal_newlines = True, # text mode
            shell = True, # interpret ';'-separated commands
            stdout = subprocess.PIPE, # intercept it!
            stderr = subprocess.STDOUT, # intercept it as well
        )
        log.info('process started')

        self.stream.state = 'watching'
        db.session.commit()

        # and now the main loop starts
        results = []
        first_res = None
        log.info('waiting for output')
        for line in sub.stdout:
            log.info('got line '+str(line))
            line = line.strip().decode()
            result = self.check(line)

            if result == 'offline':
                # handle it specially:
                # force stop this process and retry in 30 seconds
                # (will be done in watch_tc)
                return 'offline'

            if result is not None:
                self.stream.state = 'found'
                results.append(result)
                if not first_res:
                    first_res = datetime.utcnow()

            # consider game done when either got quorum results
            # or maxdelta passed since first result
            if results and (len(results) >= self.quorum or
                            datetime.utcnow() > first_res + self.maxdelta):
                # FIXME: maybe don't rely on first result
                # as it might be errorneous

                # kill the process as we don't need more results
                sub.terminate()
                # will kill() later, after done() - to avoid delaying it

                break # don't handle remaining output

        # now that process is stopped, handle results found
        if not results:
            log.warning('process failed with status %d, considering draw' % sub.poll())
            results = ['failed']
            self.stream.state = 'failed'
            first_res = datetime.utcnow()

        # calculate most trusted result
        freqs = {}
        for r in results:
            if r in freqs:
                freqs[r] += 1
            else:
                freqs[r] = 1
        pairs = sorted(freqs.items(), key=lambda p: p[1])
        # use most frequently occuring result
        result = pairs[0][0]

        log.debug('got result: %s' % result)
        # handle result
        db.session.commit()
        self.done(result, first_res.timestamp())

        # if process didn't stop itself yet, kill it as we don't need it anymore
        if sub.poll() is None:
            eventlet.sleep(3)
            sub.kill()

        # TODO: clean sub?

    def done(self, result, timestamp):
        # determine winner and propagate result to master
        requests.patch(
            '{}/streams/{}'.format(SELF_URL, self.stream.handle),
            data = dict(
                winner = result,
                timestamp = timestamp,
            ),
        )

class FifaHandler(Handler):
    gametypes = [
        'fifa14-xboxone',
        'fifa15-xboxone',
    ]
    path = 'fifastreamer'
    env = '../../env2'
    process = 'python2 fifa_streamer.py "http://twitch.tv/{handle}"'

    def check(self, line):
        log.debug('checking line: '+line)

        if 'Stream is offline' in line:
            return 'offline'

        if 'Impossible to recognize who won' in line:
            log.warning('Couldn\'t get result, skipping')
            return None #'draw'
        if 'Score:' in line:
            nick1, nick2 = line.split('Players:',1)[1].strip().split('\t\t',1)
            score1, score2 = [p for p in line.split('Score: ',1)[1]
                              .split('Players:',1)[0]
                              .split() if '-' in p and p[0].isdigit()][0].split('-')
            nick1, nick2 = map(lambda x: x.lower(), (nick1, nick2))
            score1, score2 = map(int, (score1, score2))

            if score1 == score2:
                log.info('draw detected')
                return 'draw'

            cl = self.stream.creator.lower()
            ol = self.stream.opponent.lower()
            creator = opponent = None
            if cl == nick1:
                creator = 1
            elif cl == nick2:
                creator = 2
            if ol == nick1:
                opponent = 1
            elif ol == nick2:
                opponent = 2
            if not creator and not opponent:
                log.warning('defaulting to creator! '+line)
                creator = 1
                opponent = 2
            if not creator:
                creator = 1 if opponent == 2 else 2

            if score1 > score2:
                winner = 1
            else:
                winner = 2
            return 'creator' if winner == creator else 'opponent'
        return None
class TestHandler(Handler):
    gametypes = [
        'test',
    ]
    process = './test.sh'

    def check(self, line):
        print('line')


pool = {}
def add_stream(stream):
    """
    Tries to append given stream object (which is not yet committed) to watchlist.
    Returns string on failure (e.g. if list is full).
    """
    if len(pool) >= MAX_STREAMS:
        return 'busy'

    handler = Handler.find(stream.gametype)
    if not handler:
        return 'unsupported'

    log.info('Adding stream')
    handler(stream).start() # will add stream to pool

    return True

def abort_stream(stream):
    """
    If stream is running, abort it. Else do nothing.
    """
    if stream.handle not in pool:
        return False
    pool[stream.handle].abort()
    # will remove itself
    return True

def stream_done(stream, winner, timestamp):
    """
    Runs on master node only.
    Marks given stream as done, and notifies clients etc.
    """
    from v1.helpers import Poller
    from v1.models import Game

    game = Game.query.get(stream.game_id)
    if game:
        poller = Poller.findPoller(stream.gametype)
        if winner == 'failed':
            if poller.twitch == 2: # mandatory
                log.warning('Watching failed, considering it a draw')
                winner = 'draw'
            elif poller.twitch == 1: # optional
                log.warning('Watching failed, not updating game')
                winner = None # will be fetched by Polling later
        if winner:
            Poller.gameDone(game, winner, int(timestamp))
    else:
        log.error('Invalid game ID: %d' % stream.game_id)

    # and anyway issue DELETE request, because this stream is unneeded anymore

    # no need to remove from pool, because we are on master
    # and it was already removed anyway
    # but now let's delete it from DB

    # Notice: this is DELETE request to ourselves.
    # But we are still handling PATCH request, so it will hang.
    # So launch it as a green thread immediately after we finish
    eventlet.spawn(requests.delete,
                   '{}/streams/{}'.format(SELF_URL, stream.handle))

    return True

def current_load():
    # TODO: use load average as a base, and add some cap on it
    return len(pool) / MAX_STREAMS


# now define our endpoints
def child_url(cname, sid=''):
    if cname in CHILDREN:
        return '{host}/streams/{sid}'.format(
            host = CHILDREN[cname],
            sid = sid,
        )
    return None

@api.resource(
    '/streams',
    '/streams/',
    '/streams/<id>',
)
class StreamResource(restful.Resource):
    fields = dict(
        handle = fields.String,
        gametype = fields.String,
        game_id = fields.Integer,
        state = fields.String,
        creator = fields.String,
        opponent = fields.String,
    )

    def get(self, id=None):
        """
        Returns details (current state) for certain stream.
        """
        if not id:
            # TODO?
            raise NotImplemented

        log.info('Stream queried with id '+id)

        stream = Stream.find(id)
        if not stream:
            raise NotFound

        if stream.child:
            # forward request
            return requests.get(child_url(stream.child, stream.handle)).json()

        return marshal(stream, self.fields)

    def put(self, id=None):
        """
        Returns 409 on duplicate twitch id.
        Returns 507 if no slots are available.
        Returns newly created stream id otherwise.
        """
        if not id:
            raise MethodNotAllowed

        log.info('Stream put with id '+id)

        # id should be stream handle
        if Stream.find(id):
            # FIXME: instead of failing, append game to existing stream
            abort('Duplicate stream handle', 409) # 409 Conflict

        parser = RequestParser(bundle_errors=True)
        parser.add_argument('gametype', required=True)
        parser.add_argument('game_id', type=int, required=True)
        parser.add_argument('creator', required=True)
        parser.add_argument('opponent', required=True)
        # TODO...
        args = parser.parse_args()

        stream = Stream()
        stream.handle = id
        for k, v in args.items():
            setattr(stream, k, v)

        ret = None
        # now find the child who will handle this stream
        for child, host in CHILDREN.items():
            # try to delegate this stream to that child
            # FIXME: implement some load balancing
            result = requests.put('{}/streams/{}'.format(host, id),
                                  data = args)
            if result.status_code == 200: # accepted?
                ret = result.json()
                # remember which child accepted this stream
                stream.child = child
                break
        else:
            # nobody accepted? try to handle ourself
            try:
                result = add_stream(stream)
            except Exception as e:
                abort('Error adding stream: '+str(e))
            if result == True:
                stream.child = None
            elif result == 'busy':
                abort('All observers are busy', 507) # 507 Insufficient Stroage
            elif result == 'unsupported':
                abort('Gametype not supported')
            else:
                abort('Unknown error '+result, 500)

        db.session.add(stream)
        db.session.commit()

        if ret:
            return ret
        return marshal(stream, self.fields)

    def patch(self, id=None):
        """
        Used to propagate stream result (or status update) from child to parent.
        """
        if not id:
            raise MethodNotAllowed

        log.info('Stream patched with id '+id)

        # this is called from child to parent
        stream = Stream.find(id)
        if not stream:
            raise NotFound

        parser = RequestParser(bundle_errors=True)
        parser.add_argument('winner', required=True)
        parser.add_argument('timestamp', type=float, required=True)
        args = parser.parse_args()

        if PARENT:
            # send this request upstream
            return requests.patch('{}/streams/{}'.format(*PARENT),
                                  data = args).json()
        else:
            stream_done(stream, args.winner, args.timestamp)

        return jsonify(success = True)

    def delete(self, id=None):
        """
        Deletes all records for given stream.
        Also aborts watching if stream is still watched.
        """
        if not id:
            raise MethodNotAllowed
        log.info('Stream delete for id '+id)
        stream = Stream.find(id)
        if not stream:
            raise NotFound
        if stream.child:
            ret = requests.delete(child_url(stream.child, stream.handle))
            if ret.status_code != 200:
                abort('Couldn\'t delete stream', ret.status_code, details=ret)
        else: # watching ourself:
            abort_stream(stream)
        db.session.delete(stream)
        db.session.commit()
        return jsonify(deleted=True)

@app.route('/load')
def load_ep():
    # TODO: allow querying `load average` of each child
    load = current_load()
    for child in CHILDREN.values():
        load += requests.get(child+'/load').json()['load']
    return jsonify(total = load / (len(CHILDREN)+1))


if __name__ == '__main__':
    init_app()
    app.run(port=8021, debug=False, use_debugger=False, use_reloader=False)
