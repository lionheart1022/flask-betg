from flask import request, jsonify, abort as flask_abort
from flask import g, current_app, copy_current_request_context
from flask.ext.restful.reqparse import RequestParser, Argument
from flask.ext import restful
from flask.ext.restful.utils import http_status_message

from werkzeug.exceptions import HTTPException

import urllib.parse
from datetime import datetime, timedelta
import math
from collections import OrderedDict, namedtuple
import os
import eventlet
import jwt
import hashlib, uuid
import requests
from functools import wraps
import binascii
import apns_clerk

import config
from .models import *
from .main import db, app

### Logging ###
class log_cls:
    """
    Just a handy wrapper for current_app.logger
    """
    def __getattr__(self, name):
        return getattr(current_app.logger, name)
log = log_cls()

### Data returning ###
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


### External APIs ###
def nexmo(endpoint, **kwargs):
    """
    Shorthand for nexmo api calls
    """
    kwargs['api_key'] = config.NEXMO_API_KEY
    kwargs['api_secret'] = config.NEXMO_API_SECRET
    result = requests.post('https://api.nexmo.com/%s/json' % endpoint, data=kwargs)
    return result.json()

def geocode(address):
    # TODO: caching
    # FIXME: error handling
    ret = requests.get('https://maps.googleapis.com/maps/api/geocode/json', params={
        'address': address,
        'sensor': False,
    }).json()
    loc = ret['results'][0]['geometry']['location']
    return loc['lat'], loc['lng']
class IPInfo:
    """
    This is a wrapper for ipinfo.io api with caching.
    """
    iso3 = None # will be filled on first use
    cache = OrderedDict()
    cache_max = 100
    @classmethod
    def country(cls, ip=None, default=None):
        """
        Returns ISO3 country code using country.io for conversion.
        IP defaults to `request.remote_addr`.
        Will return `default` value if no country found (e.g. for localhost ip)
        """
        if not ip:
            ip = request.remote_addr
        if ip in cls.cache:
            # move to end to make it less likely to pop
            cls.cache.move_to_end(ip)
            return cls.cache[ip] or default
        ret = requests.get('http://ipinfo.io/{}/geo'
                           .format(ip))
        try:
            ret = ret.json()
        except ValueError:
            log.warn('No JSON returned by ipinfo.io: '+ret.text)
            ret = {}
        result = None
        if 'country' in ret:
            if not cls.iso3:
                cls.iso3 = requests.get('http://country.io/iso3.json').json()
            result = cls.iso3.get(ret['country'])
            if not result:
                log.warn('couldn\'t convert country code {} to ISO3'.format(
                    ret['country']))
        cls.cache[ip] = result
        if len(cls.cache) > cls.cache_max:
            # remove oldest item
            cls.cache.popitem(last=False)
        return result or default

class PayPal:
    token = None
    token_ttl = None
    base_url = 'https://api.sandbox.paypal.com/v1/'
    @classmethod
    def get_token(cls):
        if cls.token_ttl and cls.token_ttl <= datetime.utcnow():
            cls.token = None # expired
        if not cls.token:
            ret = requests.post(
                cls.base_url+'oauth2/token',
                data={'grant_type': 'client_credentials'},
                auth=(config.PAYPAL_CLIENT, config.PAYPAL_SECRET),
            )
            if ret.status_code != 200:
                log.error('Couldn\'t get token: {}'.format(ret.status_code))
                return
            ret = ret.json()
            cls.token = ret['access_token']
            cls.token_lifetime = (datetime.utcnow() +
                                  timedelta(seconds =
                                            # -10sec to be sure
                                            ret['expires_in'] - 10))
        return cls.token
    @classmethod
    def call(cls, method, url, params=None, json=None):
        if not json:
            json = params
            params = None
        url = cls.base_url + url
        headers = {
            'Authorization': 'Bearer '+cls.get_token(),
            #TODO: 'PayPal-Request-Id': None, # use generated nonce
        }
        ret = requests.request(method, url,
                            params=params,
                            json=json,
                            headers = headers,
                            )
        log.debug('Paypal result: {} {}'.format(ret.status_code, ret.text))
        try:
            jret = ret.json()
        except ValueError:
            log.error('Paypal failure - code %s' % ret.status_code,
                      exc_info=True);
            jret = {}
        jret['_code'] = ret.status_code
        return jret

class Fixer:
    # fixer rates are updated daily, but it should be enough for us
    item = namedtuple('item', ['ttl','rate'])
    cache = OrderedDict()
    cache_max = 100
    cache_ttl = timedelta(minutes=15)
    @classmethod
    def latest(cls, src, dst):
        if src == dst:
            return 1

        now = datetime.now()
        if (src,dst) in cls.cache:
            cls.cache.move_to_end((src,dst))
            if cls.cache[(src,dst)].ttl > now:
                return cls.cache[(src,dst)].rate

        result = requests.get('http://api.fixer.io/latest', params={
            'base': src, 'symbols': dst}).json()
        if 'rates' not in result:
            print('WARNING: failure with Fixer api', result)
            return None
        rate = result.get('rates').get(dst)
        if rate is None:
            raise ValueError('Unknown currency '+dst)

        cls.cache[(src,dst)] = cls.item(now+cls.cache_ttl, rate)
        if len(cls.cache) > cls.cache_max:
            cls.popitem(last=False)

        return rate

def mailsend(user, mtype, **kwargs):
    subjects = dict(
        greeting = 'Welcome to BetGame',
        recover = 'BetGame password recovery',
        win = 'BetGame win notification',
    )
    if mtype not in subjects:
        raise ValueError('Unknown message type {}'.format(mtype))

    kwargs['name'] = user.nickname
    kwargs['email'] = user.email

    subject = subjects[mtype]
    def load(name, ext, values, base=False):
        f = open('{}/templates/{}.{}'.format(
            os.path.dirname(__file__)+'/..', # load from path relative to self
            name, ext
        ), 'r')
        txt = f.read()
        for key,val in values.items():
            txt = txt.replace('{%s}' % key, str(val))
        if not base:
            txt = load('base', ext, dict(
                content = txt,
            ), base=True)
        return txt

    ret = requests.post(
        'https://api.mailgun.net/v3/{}/messages'.format(config.MAIL_DOMAIN),
        auth=('api',config.MAILGUN_KEY),
        params={
            'from': config.MAIL_SENDER,
            'to': '{} <{}>'.format(user.nickname, user.email),
            'subject': subjects[mtype],
            'text': load(mtype, 'txt', kwargs),
            'html': load(mtype, 'html', kwargs),
        },
    )
    try:
        jret = ret.json()
        if 'id' in jret:
            log.info('mail sent: '+jret['message'])
            return True
        else:
            log.error('mail sending failed: '+jret['message'])
            return False
    except Exception:
        return False

class Riot:
    URL = 'https://{region}.api.pvp.net/api/lol/{region}/{version}/{method}'
    REGION = 'ru'

    @classmethod
    def call(cls, version, method, params, data):
        params['api_key'] = config.RIOT_KEY
        ret = requests.get(
            cls.URL.format(
                region=cls.REGION,
                version=version,
                method=method,
            ),
            params = params,
            data = data,
        )
        try:
            return ret.json()
        except Exception:
            log.exception('RIOT api error')
            return {}


### Tokens ###
def validateFederatedToken(service, refresh_token):
    if service == 'google':
        params = dict(
            refresh_token = refresh_token,
            grant_type = 'refresh_token',
            client_id = config.GOOGLE_AUTH_CLIENT_ID,
            client_secret = config.GOOGLE_AUTH_CLIENT_SECRET,
        )
        ret = requests.post('https://www.googleapis.com/oauth2/v3/token',
                            data = params).json()
        success = 'access_token' in ret
    elif service == 'facebook':
        ret = requests.get('https://graph.facebook.com/me/permissions',
                           params = dict(
                               access_token = refresh_token,
                           )).json()
        success = 'data' in ret
    else:
        raise ValueError('Bad service '+service)

    if not success:
        raise ValueError('Invalid or revoked federated token')
def federatedExchangeGoogle(code):
    post_data = dict(
        code = code,
        client_id = config.GOOGLE_AUTH_CLIENT_ID,
        client_secret = config.GOOGLE_AUTH_CLIENT_SECRET,
        redirect_uri = 'postmessage',
        grant_type = 'authorization_code',
        scope = '',
    )
    ret = requests.post('https://accounts.google.com/o/oauth2/token',
                        data=post_data)
    jret = ret.json()
    if 'access_token' in jret:
        if 'refresh_token' in jret:
            return jret['access_token'], jret['refresh_token']
        else:
            abort('Have access token but no refresh token; '
                    'please include approval_prompt=force '
                    'or revoke access and retry.')
    else:
        err = jret.get('error')
        log.error(ret.text)
        abort('Failed to exchange code for tokens: %d %s: %s' %
                (ret.status_code, jret.get('error', ret.reason),
                jret.get('error_description', 'no details')))
def federatedRenewFacebook(refresh_token):
    ret = requests.get('https://graph.facebook.com/oauth/access_token',
                       params=dict(
                           grant_type='fb_exchange_token',
                           client_id=config.FACEBOOK_AUTH_CLIENT_ID,
                           client_secret=config.FACEBOOK_AUTH_CLIENT_SECRET,
                           fb_exchange_token=refresh_token,
                       ))
    # result is in urlencoded form, so convert it to dict
    jret = dict(urllib.parse.parse_qsl(ret.text))
    if 'access_token' in jret:
        return jret['access_token']
    else:
        try:
            err = ret.json().get('error', {})
        except Exception:
            err = {}
        abort('Failed to renew Facebook token: {} {} ({})'.format(
            err.get('code', ret.status_code),
            err.get('type', ret.reason),
            err.get('message', 'no info')))
def makeToken(user, service=None, refresh_token=None,
              from_token=None, longterm=False, device=None):
    """
    Generate JWT token for given user.
    That token will allow the user to login.
    :param service: if specified, will generate google- or facebook-based token
    :param refresh_token: refresh token for that service
    :param from_token: if specified, should be longterm token;
        if that token is federated, newly generated will be also federated
        from the same service
    :param longterm: if True, will generate longterm token
    """
    if from_token:
        # should be already checked
        header, payload = jwt.verify_jwt(from_token, config.JWT_SECRET, ['HS256'],
                                         checks_optional=True) # allow longterm
        service = payload.get('svc', service)
        if longterm:
            if service == 'facebook': # for FB will generate new LT token
                refresh_token = federatedRenewFacebook(payload['refresh'])
            else:
                # for plain and Google longterm tokens don't expire
                # so just return an old one
                return from_token
    payload = {
        'sub': user.id,
    }
    if device:
        payload['device'] = device.id
    if service: # federated token
        if not isinstance(user, Client):
            raise ValueError("Cannot generate federated token for "
                             + user.__class__.__name__)
        if longterm and not refresh_token:
            # we don't need it for regular tokens
            raise ValueError('No refresh token provided for service '+service)

        payload['svc'] = service
        if longterm: # store service's refresh token for longterms only
            payload['refresh'] = refresh_token
    slt = binascii.hexlify(user.password[-4:]).decode() # last 4 bytes of salt as hex
    payload['pass'] = slt
    if longterm:
        payload['longterm'] = True
    token = jwt.generate_jwt(payload, config.JWT_SECRET, 'HS256',
                             lifetime=None if longterm else config.JWT_LIFETIME)
    return token
class BadUserId(Exception): pass
class TokenExpired(Exception): pass
def parseToken(token, userid=None, allow_longterm=False):
    """
    Returns a Player object if the token is valid,
    raises an exception otherwise.
    """
    try:
        header, payload = jwt.verify_jwt(token, config.JWT_SECRET, ['HS256'],
                                         checks_optional=allow_longterm)
    except ValueError:
        log.info('error in token parsing', exc_info=1)
        raise ValueError("Invalid token provided")
    except Exception as e:
        if str(e) == 'expired':
            raise TokenExpired
        raise ValueError("Bad token: "+str(e))
    if 'sub' not in payload:
        raise ValueError('Invalid token provided')
    if not allow_longterm and 'longterm' in payload:
        raise ValueError('Longterm token not allowed, use short-living one')
    if not payload['sub']:
        raise ValueError('Invalid userid in token: '+str(payload['sub']))
    if userid and payload['sub'] != userid:
        raise BadUserId
    user = Player.query.get(payload['sub'])
    if not user:
        raise ValueError("No such player")
    slt = binascii.hexlify(user.password[-4:]).decode() # last 4 bytes of salt
    if payload.get('pass') != slt:
        raise ValueError('Your password was changed, please login again')
    if 'svc' in payload and cls == Client and 'longterm' in payload:
        if 'longterm' in payload:
            validateFederatedToken(payload.get('svc'), payload.get('refresh'))

    g.device_id = payload.get('device', None)
    # note that this dev id might be obsolete
    # if login was performed without token and then token was specified

    return user

def check_auth(userid=None,
               allow_nonverified=False,
               allow_nonfilled=False,
               allow_banned=False,
               allow_expired=True,
               allow_longterm=False,
               optional=False):
    """
    Check if auth token is passed,
    validate that token
    and return user object.

    :param allow_expired: if we allow access for vendors with expired subscription.
        This defaults to True, so methods should be manually restricted.
    :param optional: for missing tokens return None without aborting request
    """
    # obtain token
    if ('Authorization' in request.headers
        and request.headers['Authorization'].startswith('Bearer ')):
        token = request.headers['Authorization'][7:]
    elif request.json and 'token' in request.json:
        token = request.json['token']
    elif 'token' in request.values:
        token = request.values['token']
    else:
        if optional:
            return None
        abort('Authorization required', 401)
    # check token
    try:
        user = parseToken(token, userid, allow_longterm)
    except ValueError as e:
        abort(str(e), 401)
    except BadUserId:
        abort('You are not authorized to access this method', 403)
    except TokenExpired:
        abort('Token expired, please obtain new one', 403, expired=True)

    if not allow_nonfilled and not user.complete:
        abort('Profile is incomplete, please fill!', 403)

    return user

def require_auth(_func=None, **params):
    """
    Decorator version of check_auth.
    This decorator checks if auth token is passed,
    validates that token
    and passes user object to the decorated function as a `user` argument.
    """
    def decorator(func):
        @wraps(func)
        def caller(*args, **kwargs):
            user = check_auth(**params)
            g.user = user
            # call function
            return func(*args, user=user, **kwargs)
        return caller
    if hasattr(_func, '__call__'): # used as non-function decorator
        return decorator(_func)
    return decorator

def secure_only(func):
    """
    This decorator prohibits access to method by insecure connection,
    excluding development state.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not request.is_secure and not current_app.debug:
            abort('Please use secure connection', 406)
        return func(*args, **kwargs)
    return wrapper

def sendVerificationCode(user):
    'Creates email verification code for user and mails it'
    if not hasattr(user, 'isEmailVerified'):
        raise ValueError('Invalid user type')
    code = uuid.uuid4().hex[:8] # 8 hex digits
    user.email_verification_code = code
    mailsend(user, 'verification', code=code)
    # db session will be committed after this method called
def checkVerificationCode(user, code):
    """
    Checks previously generated JWT token and marks user as verified on success.
    Will raise ValueError on failure.
    """
    if not hasattr(user, 'isEmailVerified'):
        raise ValueError('Invalid user type')
    if user.isEmailVerified:
        raise ValueError('User already verified')
    if user.email_verification_code.strip().lower() != code.strip().lower():
        raise ValueError('Incorrect code')
    user.email_verification_code = None
    db.session.commit()
    return True

### Field checkers ###
currency_cache = {}
# FIXME: cache currency queries
def currency(val):
    """
    Checks whether the value provided is a valid currency.
    Raises ValueError if not.
    """
    if not val:
        return None
    val = val.upper()
    if val in currency_cache:
        return currency_cache[val]
    currency = Currency.query.filter_by(name=val).first()
    if not currency:
        raise ValueError('Unknown currency %s' % val)
    #currency_cache[val] = {'name':currency.name, 'iso':currency.iso}
    return currency

def country(val):
    if not isinstance(val, str):
        raise ValueError(val)
    if len(val) != 3:
        raise ValueError("Incorrect country code: %s" % val)
    # TODO: check if country is valid
    return val.upper()

def email(val):
    """
    Should be 3 parts separated by @ and .,
    none of these parts can be empty.
    """
    val = val.strip()
    if not '@' in val:
        raise ValueError('Not a valid e-mail')
    user, domain = val.split('@',1)
    if (not user
        or not '.' in domain):
        raise ValueError('Not a valid e-mail')
    a,b = domain.rsplit('.',1)
    if not a or not b:
        raise ValueError('Not a valid e-mail')
    return val

def phone_field(val):
    if not isinstance(val, str):
        raise ValueError('Bad type '+repr(val))
    pnum = ''.join([c for c in val if c in '+0123456789'])
    if not pnum:
        raise ValueError('No digits in phone number '+repr(val))
    return pnum

def boolean_field(val):
    if hasattr(val,'lower'):
        val = val.lower()
    if val in [0,False,'0','off','false','no']:
        return False
    if val in [1,True,'1','on','true','yes']:
        return True
    raise ValueError(str(val)+' is not boolean')

def gamertag_force_field(val):
    val = boolean_field(val)
    if val:
        g.gamertag_force = True
    return val

gamertag_cache = {}
def gamertag_field(nick):
    if nick.lower() in gamertag_cache:
        if gamertag_cache[nick.lower()]:
            return gamertag_cache[nick.lower()]
        raise ValueError('Unknown gamertag: '+nick)

    url = 'https://www.easports.com/fifa/api/'\
        'fifa15-xboxone/match-history/fut/{}'.format(nick)
    try:
        ret = requests.get(url).json()
        if ret.get('code') == 404:
            # don't cache not-registered nicks as they can appear in future
            #gamertag_cache[nick.lower()] = None
            raise ValueError(
                'Gamertag {} seems to be unknown '
                'for FIFA game servers'.format(nick))
        data = ret['data']
        # normalized gamertag (with correct capitalizing)
        # no data so cannot know correct capitalizing; return as is
        goodnick = data[0]['self']['user_info'][0] if data else nick
        gamertag_cache[nick.lower()] = goodnick
        return goodnick
    except ValueError:
        if getattr(g, 'gamertag_force', False):
            return nick
        raise
    except Exception as e: # json failure or missing key
        log.error('Failed to validate gamertag '+nick, exc_info=True)
        log.error('Allowing it...')
        #raise ValueError('Couldn\'t validate this gamertag: {}'.format(nick))
        return nick

def summoner_field(val):
    # TODO
    return val

def encrypt_password(val):
    """
    Check password for weakness, and convert it to its hash.
    If password provided is None, will generate a random salt w/o password
    """
    if val is None:
        # just random salt, 16 bytes
        return uuid.uuid4().bytes # 16 bytes
    if not isinstance(val, str):
        raise ValueError(val)
    if len(val) < 4:
        raise ValueError("Too weak password, minimum is 4 characters")
    if len(val) > 1024:
        # prohibit extremely long passwords
        # because they cause resource eating
        raise ValueError("Too long password")

    salt = uuid.uuid4().bytes # 16 bytes
    crypted = hashlib.pbkdf2_hmac('SHA1', val.encode(), salt, 10000)
    return crypted+salt

def check_password(password, reference):
    salt = reference[-16:] # last 16 bytes = uuid length
    crypted = hashlib.pbkdf2_hmac('SHA1', password.encode(), salt, 10000)
    return crypted+salt == reference

def string_field(field, ftype=None, check_unique=True, allow_empty=False):
    """
    This decorator-like function returns a function
    which will assert that given string is not longer than maxsize.
    Also checks for uniqueness if needed.
    """
    maxsize = field.type.length
    def check(val):
        if not isinstance(val, str):
            raise TypeError
        if len(val) > maxsize:
            raise ValueError('Too long string')
        if not val:
            if allow_empty:
                return None
            raise ValueError('Empty value not allowed')
        # check&convert field type (like email) if provided
        if ftype:
            val = ftype(val)
        # now check for uniqueness, if necessary
        if field.unique and check_unique:
            userid = getattr(g, 'userid', None)
            q = field.class_.query.filter(field==val)
            if userid:
                q = q.filter(field.class_.id != userid)
            exists = db.session.query(q.exists()).scalar()
            if exists:
                raise ValueError('Already used by someone')
        return val
    return check

def bitmask_field(options):
    '''
    Converts comma-separated list of options to bitmask.
    '''
    def check(val):
        if not isinstance(val, str):
            raise ValueError
        mask = 0
        parts = val.split(',')
        for part in parts:
            if part in options:
                mask &= options[part]
            elif part == '': # ignore empty parts
                continue
            else:
                raise ValueError('Unknown option: '+part)
        return mask
    return check
def multival_field(options, allow_empty=False):
    """
    Converts comma-separated list to set of strings,
    checking each item for validity.
    """
    def check(val):
        if not isinstance(val, str):
            raise ValueError
        if not val:
            if allow_empty:
                return []
            raise ValueError('Please choose at least one option')
        parts = set(val.split(','))
        for part in parts:
            if part not in options:
                raise ValueError('Unknown option: '+part)
        return parts
    return check

def hex_field(length):
    def check(val):
        if len(val) != length:
            raise ValueError('Should be %d characters' % length)
        for c in val:
            if c not in 'abcdefABCDEF' and not c.isdigit():
                raise ValueError('Bad character %s' % c)
        return val
    return check


### Extension of RequestParser ###
class MyArgument(Argument):
    def handle_validation_error(self, error, bundle_errors=None):
        help_str = '({}) '.format(self.help) if self.help else ''
        msg = '[{}]: {}{}'.format(self.name, help_str, error)
        abort(msg, problem=self.name)
class MyRequestParser(RequestParser):
    def __init__(self, *args, **kwargs):
        super().__init__(self, *args, **kwargs)
        self.argument_class = MyArgument


# FIXME: cache currency queries
class CurrencyField(restful.fields.Raw):
    def format(self, curr):
        return {'name': curr.name,
                'description': curr.iso,
                }

class InverseBooleanField(restful.fields.Boolean):
    def format(self, val):
        return not bool(val)

class AlternatingNested(restful.fields.Raw):
    """
    Similar to Nested, but allows to pass 2 different structures.
    They are chosen based on result of `condition` proc evaluation
    on surrounding object and value.
    """
    def __init__(self, condition, nested, alternate, **kwargs):
        super().__init__(nested, **kwargs)
        self.condition = condition
        self.alternate = alternate
        self.nested = nested
    def output(self, key, obj):
        value = restful.fields.get_value(
            key if self.attribute is None else self.attribute, obj)
        if value is None:
            return super().output(key, obj)
        return restful.marshal(value, self.nested
                       if self.condition(obj, value) else
                       self.alternate)

class CommaListField(restful.fields.Raw):
    def format(self, val):
        if not isinstance(val, str):
            raise ValueError
        if not val: # empty string?
            return []
        return val.split(',')

### Polling and notification ###
def poll_fifa(gametype, gamemode):
    count_games = 0
    count_ended = 0
    def fetch(nick):
        url = 'https://www.easports.com/fifa/api/'\
            '{}/match-history/{}/{}'.format(
                gametype, gamemode, nick)
        try:
            return requests.get(url).json()['data']
        except Exception as e:
            log.error('Failed to fetch match info '
                      'for player {}, gt {} gm {}'.format(
                          nick, gametype, gamemode),
                      exc_info=True)
            return []
    games = Game.query.filter_by(
        gametype=gametype,
        gamemode=gamemode,
        state = 'accepted',
    )
    log.debug('Found %d games or %r %r' % (games.count(), gametype, gamemode))
    # map gamertags to sets of games related to them
    gamertags = {}
    for game in games:
        count_games += 1
        for gamertag in game.gamertag_creator, game.gamertag_opponent:
            if gamertag in gamertags:
                gamertags[gamertag].add(game)
            else:
                gamertags[gamertag] = set([game])

    # order player names by count of games
    order = list(map(lambda p: p[0],
                     sorted(gamertags.items(),
                            key=lambda p: len(p[1]),
                            reverse=True)))
    games_done = set()
    for gamertag in order:
        log.debug('fetching games for '+gamertag)
        matches = fetch(gamertag)
        for match in reversed(matches): # from oldest to newest
            log.debug('match: {} cr {}, op {}'.format(
                match['timestamp'], *[
                    [match[u]['user_info'], match[u]['stats']['score']]
                    for u in ('self', 'opponent')
                ]
            ))
            for game in gamertags[gamertag]:
                # skip already completed games
                if game.id in games_done:
                    continue
                # skip this game if current match ended before game's start
                if math.floor(game.accept_date.timestamp()) \
                        > match['timestamp'] + 4*3600: # delta of 4 hours
                    log.debug('Skipping game {} because of time'.format(game))
                    continue
                other, who = (
                    (game.gamertag_opponent, 'opponent')
                    if game.gamertag_creator == gamertag else
                    (game.gamertag_creator, 'creator'))
                log.debug('other: '+other)
                if other.lower() in map(lambda t: t.lower(),
                                        match['opponent']['user_info']):
                    log.debug('matched!')
                    # game matched! change its status
                    if match['self']['stats']['score'] > match['opponent']['stats']['score']:
                        # "self" won, "other" lost
                        game.winner = 'creator' if who == 'opponent' else 'opponent'
                    elif match['self']['stats']['score'] < match['opponent']['stats']['score']:
                        # "other" won, "self" lost
                        game.winner = who
                    else:
                        game.winner = 'draw'
                    game.state = 'finished'
                    game.finish_date = datetime.utcfromtimestamp(match['timestamp'])

                    count_ended += 1

                    # move funds...
                    if game.winner == 'creator':
                        game.creator.balance += game.bet
                        game.opponent.balance -= game.bet
                    elif game.winner == 'opponent':
                        game.opponent.balance += game.bet
                        game.creator.balance -= game.bet
                    # and unlock bets
                    # withdrawing them finally from accounts
                    game.creator.locked -= game.bet
                    game.opponent.locked -= game.bet

                    notify_users(game)

                    games_done.add(game.id)
        if count_ended == count_games:
            break # no unended games left, no need to fetch more tags
    db.session.commit()

    return count_games, count_ended
def poll_all():
    log.info('Starting polling')
    for gametype, opts in Game.GAMETYPES.items():
        if not opts['supported']:
            continue
        if 'fifa' in gametype:
            poller = poll_fifa
        else:
            log.error('Unexpected gametype')
            continue
        for gamemode in opts['gamemodes']:
            try:
                games, ended = poller(gametype, gamemode)
                log.info(
                    '{gametype}, {gamemode}: '
                    'ended {ended} of {games} games'.format(**vars()))
            except Exception as e:
                log.error('Couldn\'t poll for gametype {} gamemode {}'.format(
                    gametype, gamemode), exc_info = True)
    log.info('Polling done')


# Notification
apns_session = None
def notify_users(game):
    """
    This method sends PUSH notifications about game state change
    to all interested users.
    It will also send congratulations email to game winner.
    """
    msg = {
        'new': '{} invites you to compete'.format(game.creator.nickname),
        'accepted': '{} accepted your invitation, start playing now!'
            .format(game.opponent.nickname),
        'declined': '{} declined your invitation'.format(game.opponent.nickname),
        'finished': 'Game finished, coins moved',
    }[game.state]

    players = []
    if game.state in ['new', 'finished']:
        players.append(game.opponent)
    if game.state in ['accepted', 'declined', 'finished']:
        players.append(game.creator)
    receivers = []
    for p in players:
        for d in p.devices:
            if d.push_token:
                if len(d.push_token) in (16, 32, 64, 128):
                    # according to err it should be 32, but actually is 64
                    receivers.append(d.push_token)
                else:
                    log.warning('Incorrect push token '+d.push_token)

    from . import routes # for fields list
    message = None
    if receivers:
        message = apns_clerk.Message(receivers, alert=msg, badge='increment',
                                    content_available=1,
                                    game=restful.marshal(
                                        game, routes.GameResource.fields))
        global apns_session
        if not apns_session:
            try:
                apns_session = apns_clerk.Session()
                conn = apns_session.get_connection('push', cert_file=None) # TODO
            except Exception: # import error, OpenSSL error
                log.exception('APNS failure!')
                message = None # will not send PUSH

    def send_push(msg):
        srv = apns_clerk.APNs(conn)
        try:
            ret = srv.send(message)
        except:
            log.error('Failed to connect to APNs', exc_info=True)
        else:
            for token, reason in ret.failed.items():
                log.warning('Device {} failed by {}, removing'.format(token,reason))
                db.session.delete(Device.filter_by(push_token=token).first())

            for code, error in ret.errors:
                log.warning('Error {}: {}'.format(code, error))

            if res.needs_retry():
                do_send(res.retry)

    def send_mail(game):
        if game.state == 'finished':
            if game.winner == 'creator':
                winner = game.creator
            elif game.winner == 'opponent':
                winner = game.opponent
            elif game.winner == 'draw':
                return # will not notify anybody
            else:
                log.error('Internal error: incorrect game winner '+game.winner
                          +' for state '+game.state)
                return
            mailsend(
                winner, 'win',
                date = game.finish_date.strftime('%d.%m.%Y %H:%M:%S UTC'),
                bet = game.bet,
                balance = winner.available,
            )


    if message: # if had any receivers
        send_push(message)
    # and send email if applicable
    send_mail(game)

class classproperty:
    """
    Cached class property; evaluated only once
    """
    def __init__(self, fget):
        self.fget = fget
        self.obj = {}
    def __get__(self, owner, cls):
        if cls not in self.obj:
            self.obj[cls] = self.fget(cls)
        return self.obj[cls]
