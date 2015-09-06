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
import itertools
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
# allow 507 error code
class InsufficientStorage(HTTPException):
    code = 507
    description = 'No observers available'
    # FIXME: for some reason this exception gets propagated to stdout
    # rather than handled by make_json_error
flask_abort.mapping[507] = InsufficientStorage
def make_json_error(ex):
    code = getattr(ex, 'code', 500)
    if hasattr(ex, 'data'):
        response = jsonify(**ex.data)
    else:
        response = jsonify(error_code = code, error = http_status_message(code))
    response.status_code = code
    return response
for code in flask_abort.mapping.keys():
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

    # now restart all active streams
    # and remove stale records
    with app.test_request_context():
        for stream in Stream.query:
            log.info('restarting stream '+stream.handle)
            if stream.state in ('waiting', 'watching'):
                add_stream(stream)
            elif stream.state in ('found', 'failed'):
                # was not yet deleted - delete now
                # FIXME: maybe send result (again)?
                db.session.delete(stream)
                db.session.commit()
            else:
                log.warning('Unexpected stream state '+stream.state)

    return app


# declare model
class Stream(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # Twitch stream handle
    handle = db.Column(db.String(64), nullable=False)

    # Which child handles this stream? None if self
    child = db.Column(db.String(64), default=None)

    # Gametype and other metadata goes below
    gametype = db.Column(db.String(64), default=None)

    # this is an ID of the primary Game object for this stream.
    # We don't use foreign key because we may reside on separate server
    # and use separate db.
    game_id = db.Column(db.Integer, unique=True)
    # supplementary game IDs - ones which follow the same stream.
    # Some of them might be reversed (winner for them should be inverted).
    # Format: 10,-20,15,3,-17
    # -n means reversed game
    game_ids_supplementary = db.Column(db.String, default='')

    state = db.Column(db.Enum('waiting', 'watching', 'found', 'failed'),
                      default='waiting')

    creator = db.Column(db.String(128))
    opponent = db.Column(db.String(128))

    __table_args__ = (
        db.UniqueConstraint('handle', 'gametype', name='_handle_gametype_uc'),
    )

    @classmethod
    def find(cls, id, gametype=None):
        q = cls.query.filter_by(handle=id)
        if gametype:
            q = q.filter_by(gametype=gametype)
        return q.first()


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
        self.handle = stream.handle

    def start(self):
        log.info('spawning handler')
        self.thread = eventlet.spawn(self.watch_tc)
        pool[self.handle, self.gametype] = self

    def abort(self):
        self.thread.kill()
        # this will automatically execute `finally` clause in `watch_tc`
        # and remove us from pool

        # if subprocess is still alive, kill it
        if hasattr(self, 'sub') and self.sub.poll() is None: # still running?
            log.info('Killing subprocess')
            self.sub.terminate()
            eventlet.spawn_after(3, self.sub.kill)

    def watch_tc(self):
        log.info('watch_tc started')
        try:
            result = self.watch()
            waits = 0
            while result == 'offline':
                # re-add stream to session if needed
                # to avoid DetachedInstanceError
                # FIXME: why is it detached after watch() but not before?
                if not db.session.object_session(self.stream):
                    db.session.add(self.stream)

                if waits > WAIT_MAX:
                    # will be caught below
                    raise Exception('We waited for too long, '
                                    'abandoning stream '+self.stream.handle)
                log.info('Stream {} is offline, waiting'
                            .format(self.stream.handle))
                self.stream.state = 'waiting'
                db.session.commit()
                # wait & retry
                eventlet.sleep(WAIT_DELAY)
                result = self.watch()
                waits += 1
            return result
        except Exception: # will not catch GreenletExit
            log.exception('Watching failed')

            self.stream.state = 'failed'
            db.session.commit()
            # mark it as Done anyway
            self.done('failed', datetime.utcnow().timestamp())
        except eventlet.greenlet.GreenletExit:
            # do nothing (just perform `finally` block) but don't print traceback
            log.info('Watcher aborted for handle '+self.handle)
        finally:
            # mark that this stream has stopped
            # stream may be already deleted from db, so use saved handle
            del pool[self.handle, self.gametype]

    def watch(self):
        # start subprocess and watch its output

        # first, chdir to this script's directory
        os.chdir(ROOT)

        # then, if required, chdir handler's requested dir (relative to script's)
        if self.path:
            os.chdir(self.path)
        cmd = 'exec ' + self.process.format(handle = self.stream.handle)
        if self.env:
            cmd = 'VIRTUAL_ENV_DISABLE_PROMPT=1 . {}/bin/activate; {}'.format(self.env, cmd)
        log.info('starting process...')
        sub = self.sub = subprocess.Popen(
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
        log.debug('results list: '+str(results))

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
    process = 'python2 -u fifa_streamer.py "http://twitch.tv/{handle}"'

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

            log.info('Got score data. Nicks {} / {}, scores {} / {}'.format(
                nick1, nick2, score1, score2))

            if score1 == score2:
                log.info('draw detected')
                return 'draw'

            cl = self.stream.creator.lower()
            ol = self.stream.opponent.lower()
            log.debug('cl: {}, ol: {}'.format(cl, ol))
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
    if (stream.handle, stream.gametype) not in pool:
        return False
    pool[stream.handle, stream.gametype].abort()
    # will remove itself
    return True

def stream_done(stream, winner, timestamp):
    """
    Runs on master node only.
    Marks given stream as done, and notifies clients etc.
    """
    from v1.polling import Poller
    from v1.models import Game

    for gid in itertools.chain(
        [stream.game_id],
        map(
            int,
            filter(
                None,
                stream.game_ids_supplementary.split(','),
            ),
        ),
    ):
        if gid < 0:
            gid = -gid
            reverse = True
        else:
            reverse = False
        game = Game.query.get(gid)
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
                if winner in ['creator','opponent'] and reverse:
                    winner = 'creator' if winner == 'opponent' else 'opponent'
                Poller.gameDone(game, winner, int(timestamp))
        else:
            log.error('Invalid game ID: %d' % stream.game_id)

    # and anyway issue DELETE request, because this stream is unneeded anymore

    # no need to remove from pool, because we are on master
    # and it was already removed from pool anyway
    # but now let's delete it from DB

    # Notice: this is DELETE request to ourselves.
    # But we are still handling PATCH request, so it will hang.
    # So launch it as a green thread immediately after we finish
    eventlet.spawn(requests.delete,
                   '{}/streams/{}'.format(SELF_URL, stream.handle))

    return True

def current_load():
    streams = len(pool)
    maximum = MAX_STREAMS
    # TODO: use load average as a base, and add some cap on it
    load = streams / maximum
    return load, streams, maximum


# now define our endpoints
def child_url(cname, sid=None, gametype=None):
    if cname in CHILDREN:
        return '{host}/streams/{sid}'.format(
            host = CHILDREN[cname],
            sid = '{}/{}'.format(sid, gametype) if sid else '',
        )
    return None

@api.resource(
    '/streams',
    '/streams/',
    '/streams/<id>/<gametype>',
)
class StreamResource(restful.Resource):
    fields = dict(
        handle = fields.String,
        gametype = fields.String,
        game_id = fields.Integer,
        game_ids_supplementary = fields.String,
        state = fields.String,
        creator = fields.String,
        opponent = fields.String,
    )

    def get(self, id=None, gametype=None):
        """
        Returns details (current state) for certain stream.
        """
        if not id:
            # at least for debugging
            q = Stream.query
            return dict(
                streams = fields.List(fields.Nested(self.fields)).format(q),
            )

        log.info('Stream queried with id '+id)

        stream = Stream.find(id, gametype)
        if not stream:
            raise NotFound

        if stream.child:
            # forward request
            return requests.get(child_url(stream.child, stream.handle, stream.gametype)).json()

        return marshal(stream, self.fields)

    def put(self, id=None, gametype=None):
        """
        Returns 409 if stream with this handle exists with different parameters.
        Returns 507 if no slots are available.
        Returns newly created stream id otherwise.
        Will return 201 code if new stream was added
        or 200 code if game was added to existing stream.
        """
        if not id or not gametype:
            raise MethodNotAllowed

        log.info('Stream put with id {}, gt {}'.format(id, gametype))

        parser = RequestParser(bundle_errors=True)
        parser.add_argument('game_id', type=int, required=True)
        parser.add_argument('creator', required=True)
        parser.add_argument('opponent', required=True)
        # TODO...
        args = parser.parse_args()

        if Stream.query.filter_by(game_id = args.game_id).first():
            # FIXME: handle dup ID in supplementaries?
            abort('This game ID is already watched in some another stream')

        stream = Stream.find(id, gametype)
        if stream:
            # stream already exists; add this game to it as a supplementary game
            new = False

            if args.creator.lower() == stream.creator.lower():
                if args.opponent.lower() != stream.opponent.lower():
                    abort('Duplicate stream ID with wrong opponent nickname', 409)
                game = args.game_id
            elif args.opponent.lower() == stream.creator.lower():
                if args.creator.lower() != stream.opponent.lower():
                    abort('Duplicate stream ID with wrong reverse oppo nickname', 409)
                game = -args.game_id # reversed result
            else:
                abort('Duplicate stream ID with different players', 409)

            # now add game id
            if stream.game_ids_supplementary:
                stream.game_ids_supplementary += ','
            else:
                stream.game_ids_supplementary = ''
            stream.game_ids_supplementary += str(game)

        else:
            # new stream
            new = True

            stream = Stream()
            stream.handle = id
            stream.gametype = gametype
            for k, v in args.items():
                setattr(stream, k, v)

        ret = None
        # now find the child who will handle this stream
        for child, host in CHILDREN.items():
            # try to delegate this stream to that child
            # FIXME: implement some load balancing
            result = requests.put('{}/streams/{}/{}'.format(host, id, gametype),
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

        if new:
            db.session.add(stream)
        db.session.commit()

        if ret:
            return ret
        return marshal(stream, self.fields), 201 if new else 200

    def patch(self, id=None, gametype=None):
        """
        Used to propagate stream result (or status update) from child to parent.
        """
        if not id or not gametype:
            raise MethodNotAllowed

        log.info('Stream patched with id {}, gt {}'.format(id, gametype))

        # this is called from child to parent
        stream = Stream.find(id, gametype)
        if not stream:
            raise NotFound

        parser = RequestParser(bundle_errors=True)
        parser.add_argument('winner', required=True)
        parser.add_argument('timestamp', type=float, required=True)
        args = parser.parse_args()

        if PARENT:
            # send this request upstream
            return requests.patch('{}/streams/{}/{}'.format(PARENT[1], id, gametype),
                                  data = args).json()
        else:
            stream_done(stream, args.winner, args.timestamp)

        return jsonify(success = True)

    def delete(self, id=None, gametype=None):
        """
        Deletes all records for given stream.
        Also aborts watching if stream is still watched.
        """
        if not id or not gametype:
            raise MethodNotAllowed
        log.info('Stream delete for id {}, gt {}'.format(id, gametype))
        stream = Stream.find(id, gametype)
        if not stream:
            raise NotFound
        if stream.child:
            ret = requests.delete(child_url(stream.child, id, stream.gametype))
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
    load, streams, maximum = current_load()
    for child in CHILDREN.values():
        ret = requests.get(child+'/load').json()
        load += ret.get('total', 0)
        streams += ret.get('current_streams', 0)
        maximum += ret.get('max_streams', 0)
    return jsonify(
        total = load / (len(CHILDREN)+1),
        current_streams = streams,
        max_streams = maximum,
    )


if __name__ == '__main__':
    init_app()
    app.run(port=8021, debug=False, use_debugger=False, use_reloader=False)
