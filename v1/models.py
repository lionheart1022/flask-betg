from datetime import datetime, timedelta

from sqlalchemy import or_, case
from sqlalchemy.sql.expression import func
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from flask import g

from .main import db
from .common import *


class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nickname = db.Column(db.String(64), unique=True)
    email = db.Column(db.String(128), nullable=True, unique=True)
    password = db.Column(db.LargeBinary(36))
    facebook_id = db.Column(db.String(64))
    facebook_token = db.Column(db.String(128))
    twitter_id = db.Column(db.Integer)
    twitter_token = db.Column(db.String(256))
    williamhill_id = db.Column(db.String(128))
    williamhill_token = db.Column(db.String(128))
    # williamhill_currency = db.Column(db.String(3)) # TODO handle&save it?
    create_date = db.Column(db.DateTime, default=datetime.utcnow)
    bio = db.Column(db.Text)

    ea_gamertag = db.Column(db.String(64), unique=True)
    fifa_team = db.Column(db.String(64), unique=False)
    riot_summonerName = db.Column(db.String(64), unique=True)
    # in fact, it is integer, but saved as string for compatibility
    steam_id = db.Column(db.String(64), unique=True)
    starcraft_uid = db.Column(db.String(64), unique=True)
    tibia_character = db.Column(db.String(64), unique=True)

    balance = db.Column(db.Float, default=0)
    locked = db.Column(db.Float, default=0)

    @property
    def available(self):
        return self.balance - self.locked

    @property
    def balance_obj(self):
        return {
            'full': self.balance,
            'locked': self.locked,
            'available': self.available,
        }

    @property
    def complete(self):
        return (self.email != None) & (self.nickname != None)

    @hybrid_property
    def games(self):
        return Game.query.filter(
            (Game.creator_id == self.id) |  # OR
            (Game.opponent_id == self.id))

    @hybrid_method
    def gamecount_impl(self, *filters):
        return fast_count(self.games.filter(*filters))

    @gamecount_impl.expression
    def gamecount_impl(cls, *filters):
        return (
            cls.games
            .filter(*filters)
            .with_entities(func.count('*'))
            .as_scalar()
        )

    @hybrid_property
    def gamecount(self):
        return self.gamecount_impl()

    @hybrid_method
    def winrate_impl(self, *filters):
        count = 0
        wins = 0
        for game in self.games.filter(Game.state == 'finished', *filters):
            count += 1
            whoami = 'creator' if game.creator_id == self.id else 'opponent'
            if game.winner == 'draw':
                wins += 0.5
            elif game.winner == whoami:
                wins += 1
        if count == 0:
            # no finished games, no data
            return None
        return wins / count

    @hybrid_property
    def mygames(cls):
        return (
            db.select([func.count(Game.id)])
            .where(db.and_(
                Game.state == 'finished',
            ))
        )

    @hybrid_property
    def mygamescount(cls):
        return (
            cls.mygames
            .where(cls.id.in_([
                Game.creator_id,
                Game.opponent_id,
            ]))
            .label('cnt')
        )

    @hybrid_property
    def mygameswon(cls):
        return (
            cls.mygames
            .where(
                (
                    (Game.creator_id == cls.id) &
                    (Game.winner == 'creator')
                ) | (
                    (Game.opponent_id == cls.id) &
                    (Game.winner == 'opponent')
                )
            )
            .label('won')
        )

    @hybrid_property
    def mygamesdraw(cls):
        return (
            cls.mygames.with_only_columns([func.count(Game.id) / 2])
            .where(
                (
                    (Game.creator_id == cls.id) |
                    (Game.opponent_id == cls.id)
                ) &
                Game.winner == 'draw',
            )
            .label('draw')
        )

    @winrate_impl.expression
    def winrate_impl(cls, *filters):
        mygames = (
            db.select([func.count(Game.id)])
            .where(db.and_(
                Game.state == 'finished',
                *filters
            ))
        )
        count = (
            mygames
            .where(cls.id.in_([
                Game.creator_id,
                Game.opponent_id,
            ]))
            .label('cnt')
        )
        won = (
            mygames
            .where(
                (
                    (Game.creator_id == cls.id) &
                    (Game.winner == 'creator')
                ) | (
                    (Game.opponent_id == cls.id) &
                    (Game.winner == 'opponent')
                )
            )
            .label('won')
        )
        draw = (
            mygames.with_only_columns([func.count(Game.id) / 2])
            .where(
                (
                    (Game.creator_id == cls.id) |
                    (Game.opponent_id == cls.id)
                ) &
                Game.winner == 'draw',
            )
            .label('draw')
        )
        return case([
            (count == 0, None),  # if count == 0 then NULL else (calc)
        ], else_=
        (won + draw) / count
        )

    @hybrid_property
    def winrate(self):
        if 'winrate_filt' in g and g.winrate_filt:
            log.debug('winrate: using filt ' + ','.join(
                str(f) for f in g.winrate_filt
            ))
            return self.winrate_impl(*g.winrate_filt)
        log.debug('winrate: no filt')
        return self.winrate_impl()

    # @hybrid_method
    def winratehist(self, days=None, weeks=None, months=None):
        count = days or weeks or months
        if not count:
            raise ValueError('Please provide something!')
        # 30.5 is approximate number of days in month
        delta = timedelta(days=1 if days else 7 if weeks else 30.5)
        now = datetime.utcnow()
        ret = []
        for i in range(count):
            prev = now - delta

            count, wins = 0, 0
            for game in self.games.filter(
                            Game.state == 'finished',
                            Game.finish_date > prev,
                            Game.finish_date <= now,
            ):
                count += 1
                whoami = 'creator' if game.creator_id == self.id else 'opponent'
                if game.winner == 'draw':
                    wins += 0.5
                elif game.winner == whoami:
                    wins += 1
            rate = (wins / count) if count else 0

            ret.append((prev, count, wins, rate))
            now = prev
        return ret

    @hybrid_property
    def lastbet(self):
        return self.games.order_by(Game.create_date.desc()).first().create_date

    @lastbet.expression
    def lastbet(cls):
        return (
            db.select([Game.create_date])
            .where(cls.id.in_([
                Game.creator_id,
                Game.opponent_id,
            ]))
            .order_by(Game.create_date.desc())
            .limit(1)
            .label('lastbet')
        )

    @hybrid_method
    def popularity_impl(self, *filters):
        return fast_count(
            self.games.filter(
                Game.state == 'accepted',
                *filters
            )
        )

    @popularity_impl.expression
    def popularity_impl(cls, *filters):
        return (
            db.select([func.count(Game.id)])
            .where(
                db.and_(
                    cls.id.in_([
                        Game.creator_id,
                        Game.opponent_id,
                    ]),
                    Game.state == 'accepted',
                    *filters
                )
            )
            .label('popularity')
        )

    @hybrid_property
    def popularity(self):
        return self.popularity_impl()  # without filters

    _leadercache = {}  # is a class field
    _leadercachetime = None

    @property
    def leaderposition(self):
        if not self._leadercache or self._leadercachetime < datetime.utcnow():
            self._leadercachetime = datetime.utcnow() + \
                                    timedelta(minutes=5)
            self._leadercache = {}
            # This is dirty way, but "db-related" one did not work..
            # http://stackoverflow.com/questions/7057772/get-row-position-in-mysql-query
            # MySQL kept ordering line numbers according to ID,
            # regardless of ORDER BY clause.
            # Maybe because of joins or so.

            # TODO: maybe cache result in memory for e.g. 5min
            q = Player.query.with_entities(
                Player.id,
            ).order_by(
                # FIXME: hardcoded algorithm is not a good thing?
                Player.winrate.desc(),
                Player.gamecount.desc(),
                Player.id.desc(),
            )
            self._leadercache = {
                row[0]: n + 1
                for n, row in enumerate(q)
            }
        return self._leadercache[self.id]

    @hybrid_property
    def recent_opponents(self):
        # last 5 sent and 5 received
        sent, recv = [
            Game.query.filter(field == self.id)
                .order_by(Game.create_date.desc())
                .limit(5).with_entities(other).subquery()
            for field, other in [
                (Game.creator_id, Game.opponent_id),
                (Game.opponent_id, Game.creator_id),
            ]
        ]
        return Player.query.filter(or_(
            Player.id.in_(db.session.query(sent.c.opponent_id)),
            Player.id.in_(db.session.query(recv.c.creator_id)),
        ))

    @property
    def has_userpic(self):
        from .routes import UserpicResource

        return bool(UserpicResource.findfile(self))

    _identities = [
        'nickname',
        'ea_gamertag', 'riot_summonerName', 'steam_id',
    ]

    @classmethod
    def find(cls, key):
        """
        Retrieves user by player id or integer id.
        If id is 'me', will return currently logged in user or None.
        """
        if key == '_':
            from .helpers import MyRequestParser as RequestParser

            parser = RequestParser()
            parser.add_argument('id')
            args = parser.parse_args()
            key = args.id

        if key.lower() == 'me':
            return getattr(g, 'user', None)

        if '@' in key and '.' in key:
            return cls.query.filter_by(email=key).first()

        p = None
        try:
            p = cls.query.get(int(key))
        except ValueError:
            pass
        for identity in cls._identities:
            if p:
                return p
            p = cls.query.filter_by(**{identity: key}).first()
        return p

    @classmethod
    def find_or_fail(cls, key):
        player = cls.find(key)
        if not player:
            raise ValueError('Player {} is not registered on BetGame'.format(key))
        return player

    @classmethod
    def search(cls, filt, operation='like'):
        """
        Filt should be suitable for SQL LIKE statement.
        E.g. "word%" will search anything starting with word.
        """
        if len(filt) < 1:
            return []
        return cls.query.filter(
            or_(*[
                getattr(
                    getattr(cls, identity),
                    operation,
                )(filt)
                for identity in cls._identities
            ])
        )

    def __repr__(self):
        return '<Player id={} nickname={} balance={}>'.format(
            self.id, self.nickname, self.balance)


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('player.id'), index=True)
    player = db.relationship(Player,
                             backref=db.backref('transactions',
                                                lazy='dynamic')  # return query, not list
                             )
    date = db.Column(db.DateTime, default=datetime.utcnow)
    type = db.Column(db.Enum('deposit', 'withdraw', 'won', 'lost', 'other'), nullable=False)
    sum = db.Column(db.Float, nullable=False)
    balance = db.Column(db.Float, nullable=False)  # new balance
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=True)
    game = db.relationship('Game', backref=db.backref('transaction', uselist=False))
    comment = db.Column(db.Text)

    def __repr__(self):
        return '<Transaction id={} sum={}>'.format(self.id, self.sum)


class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('player.id'), index=True)
    player = db.relationship(Player, backref=db.backref('devices',
                                                        lazy='dynamic'))
    push_token = db.Column(db.String(128), nullable=True)
    last_login = db.Column(db.DateTime, default=datetime.utcnow)
    failed = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return '<Device id={}, failed={}>'.format(self.id, self.failed)

class Tournament(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    payin = db.Column(db.Float, nullable=False)
    payout = db.Column(db.Float, nullable=False)

    aborted = db.Column(db.Boolean, nullable=False, default=False, server_default='false')

    winner_id = db.Column(db.Integer(), db.ForeignKey(Player.id), required=False)
    winner = db.relationship(Player, backref='won_tournaments')

    players = db.relationship(
        Player,
        secondary='participant',
        backref='tournaments',
        lazy='dynamic',
        collection_class=list,
    )

    rounds_count = db.Column(db.Integer)

    @hybrid_property
    def participants_cap(self):
        return 2 ** self.rounds_count

    @hybrid_property
    def full(self):
        return self.participants_count > self.participants_cap

    @hybrid_property
    def participants_count(self):
        return Participant.query.filter(Participant.tournament_id == self.id).count()

    open_date = db.Column(db.DateTime, nullable=False)
    start_date = db.Column(db.DateTime, nullable=False)
    finish_date = db.Column(db.DateTime, nullable=False)

    @hybrid_property
    def tournament_length(self) -> timedelta:
        return self.finish_date - self.start_date

    @hybrid_property
    def round_length(self) -> timedelta:
        return self.tournament_length / self.rounds_count

    @property
    def rounds_dates(self):
        for i in range(self.rounds_count):
            round_start = self.start_date + self.round_length * i
            round_end = self.start_date + self.round_length * (i+1)
            yield {
                'start': round_start,
                'end': round_end,
            }

    @property
    def current_round(self):
        return (datetime.utcnow() - self.start_date) // self.round_length

    @hybrid_property
    def available(self):
        return self.open_date < datetime.utcnow() < self.start_date and (
            self.participants_count < self.participants_cap
        )

    @hybrid_property
    def started(self):
        return self.start_date < datetime.utcnow()

    def __init__(self, rounds_count, open_date, start_date, finish_date):
        assert rounds_count >= 0
        assert open_date < start_date < finish_date
        self.rounds_count = rounds_count
        self.start_date, self.finish_date, self.open_date = open_date, start_date, finish_date

    def create_participant(self, player: Player):
        if self.participants_count >= self.participants_cap:
            return None
        participant = Participant(player.id, self.id)
        if not self.participants:
            participant.order = 0
        else:
            participant.order = self.participants[-1].order + 1
        return participant

    def add_player(self, player: Player):
        if self.aborted:
            return False, 'This tournament was aborted', 'aborted'
        if self.open_date > datetime.utcnow():
            return False, 'This tournament is not open yet', 'not_open'
        if self.started:
            return False, 'This tournament has already started', 'started'
        if player.available < self.payin:
            return False, 'You don\'t have enough coins', 'coins'

        participant = self.create_participant(player)
        if participant:
            player.locked += self.payin
            db.session.add(participant)
            db.session.commit()
            return True, 'Success', None
        else:
            return False, 'Tournament is full', 'participants_cap'

    def abort(self):
        self.aborted = True
        for participant in self.participants:
            participant.player.locked -= self.payin
            db.session.delete(participant)
        db.session.commit()

    def set_winner(self, participant: Participant):
        self.winner = participant.player
        for participant in self.participants:
            participant.player.locked -= self.payin
            participant.player.balance -= self.payin
            db.session.add(Transaction(
                player=participant.player,
                type='other',
                sum=self.payin,
                balance=participant.player.balance,
                comment='Tournament buy in'
            ))
        self.winner.balance += self.payout
        db.session.add(Transaction(
            player=self.winner,
            type='win',
            sum=self.payout,
            balance=self.winner.balance,
            comment='Tournament payout'
        ))
        db.session.commit()

    def check_winner(self):
        maybe_winner = None
        for participant in self.participants:
            if not maybe_winner or maybe_winner.round < participant.round:
                maybe_winner = participant
        if maybe_winner and maybe_winner.round > self.rounds_count:
            self.set_winner(maybe_winner)

    @property
    def participants_by_round(self):
        result = [[
            (p1, p2)
            for p1, p2 in zip(self.participants[::2], self.participants[1::2])
        ]]
        for round_index in range(2, self.rounds_count + 1):
            participants = []
            for p1, p2 in result[-1]:
                if p1.round >= round_index > p2.round:
                    participants.append(p1)
                    continue
                if p2.round >= round_index > p1.round:
                    participants.append(p2)
                    continue
                participants.append(None)

            result.append([
                (p1, p2)
                for p1, p2 in zip(participants[::2], participants[1::2])
            ])
        return result

    @property
    def current_opponents_by_id(self) -> dict:
        opponents_by_id = {}
        participants = self.participants_by_round[self.current_round - 1]
        for p1, p2 in participants:
            if p1:
                opponents_by_id[p1.player_id] = p2
            if p2:
                opponents_by_id[p2.player_id] = p1
        return opponents_by_id

    def get_opponent(self, player: Player):
        current_participant = Participant.query.get((player.id, self.id))
        if current_participant.defeated:
            return None, 'You were defeated'
        if current_participant.round < self.current_round:
            if not current_participant.defeated:
                current_participant.defeated = True
                db.session.commit()
            return None, 'You\'re late'
        if current_participant.round > self.current_round:
            return None, 'Next round haven\'t begun yet'

        opponent = self.current_opponents_by_id.get(player.id, None)
        if opponent:
            return opponent.player, None
        current_participant.round += 1
        db.session.commit()
        self.check_state()

    def check_state(self):
        if self.started and not self.full:
            self.abort()
        if not self.aborted and not self.winner:
            self.check_winner()

    def handle_game_result(self, winner: Player, looser: Player):
        winner_participant = Participant.query.get((winner.id, self.id))
        looser_participant = Participant.query.get((looser.id, self.id))
        looser_participant.defeated = True
        winner_participant.round += 1
        db.session.commit()
        self.check_state()


class Participant(db.Model):
    __tablename__ = 'participant'

    def __init__(self, player_id, tournament_id):
        self.player_id, self.tournament_id = player_id, tournament_id

    player_id = db.Column(db.Integer(), db.ForeignKey(Player.id), index=True)
    tournament_id = db.Column(db.Integer(), db.ForeignKey(Tournament.id), index=True)

    tournament = db.relationship(
        Tournament, backref=db.backref(
            'participants', order_by='Participant.order'
        )
    )
    player = db.relationship(Player, backref='participations')

    defeated = db.Column(db.Boolean, default=False, server_default='false', nullable=False)
    round = db.Column(db.Integer, default=1, server_default='1', nullable=False)
    order = db.Column(db.Integer, nullable=True)

    db.PrimaryKeyConstraint(player_id, tournament_id)
    db.UniqueConstraint(tournament_id, order)


class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey('player.id'), index=True)
    creator = db.relationship(Player, foreign_keys='Game.creator_id')
    opponent_id = db.Column(db.Integer, db.ForeignKey('player.id'), index=True)
    opponent = db.relationship(Player, foreign_keys='Game.opponent_id')
    parent_id = db.Column(db.Integer, db.ForeignKey('game.id'),
                          index=True)
    parent = db.relationship('Game', foreign_keys='Game.parent_id',
                             backref='children', remote_side='Game.id')

    gamertag_creator = db.Column(db.String(128))
    gamertag_opponent = db.Column(db.String(128))
    twitch_handle = db.Column(db.String(128))
    twitch_identity_creator = db.Column(db.String(128))
    twitch_identity_opponent = db.Column(db.String(128))

    gametype = db.Column(db.String(64), nullable=False)
    gamemode = db.Column(db.String(64), nullable=False)
    meta = db.Column(db.Text)  # for poller to use

    bet = db.Column(db.Float, nullable=False)
    create_date = db.Column(db.DateTime, default=datetime.utcnow)
    state = db.Column(db.Enum(
        'new', 'cancelled', 'accepted', 'declined', 'finished', 'aborted',
    ), default='new')
    accept_date = db.Column(db.DateTime, nullable=True)

    aborter_id = db.Column(db.Integer, db.ForeignKey('player.id'))
    aborter = db.relationship(Player, foreign_keys='Game.aborter_id')
    report_creator = db.Column(db.Enum('creator', 'opponent', 'draw'),
                               nullable=True)
    report_creator_date = db.Column(db.DateTime, nullable=True)
    report_opponent = db.Column(db.Enum('creator', 'opponent', 'draw'),
                                nullable=True)
    report_opponent_date = db.Column(db.DateTime, nullable=True)
    report_try = db.Column(db.Integer, default=0)

    winner = db.Column(db.Enum('creator', 'opponent', 'draw'), nullable=True)
    details = db.Column(db.Text, nullable=True)
    finish_date = db.Column(db.DateTime, nullable=True)

    tournament_id = db.Column(db.Integer(), db.ForeignKey(Tournament.id), index=True, nullable=True)
    tournament = db.relationship(Tournament, backref='games')

    def _make_identity_getter(kind, prop):
        def _getter(self):
            if not self.gametype:
                return None
            from .polling import Poller

            poller = Poller.findPoller(self.gametype)
            identity = getattr(poller, kind)
            if not identity:
                return None
            return getattr(identity, prop)

        _getter.__name__ = '_'.join((kind, prop))
        return _getter

    for kind in 'identity', 'twitch_identity':
        for prop in 'id', 'name':
            # I know it is not good to modify locals(),
            # but it works here (as we are not in function).
            # At least it works in python 3.4/3.5
            locals()['_'.join((kind, prop))] = property(
                _make_identity_getter(kind, prop)
            )
    del _make_identity_getter

    def _make_identity_splitter(role, prop):
        attr = 'gamertag_' + role
        seq = {'val': 0, 'text': 1}[prop]

        def _getter(self):
            # formatter returns tuple (internal, human_readable)
            return self.identity.formatter(getattr(self, attr))[seq]

        return _getter

    for role in 'creator', 'opponent':
        for prop in 'val', 'text':
            locals()['gamertag_{}_{}'.format(role, prop)] = property(
                _make_identity_splitter(role, prop)
            )
    del _make_identity_splitter

    @property
    def is_root(self):
        """
        Returns true if this game is session starter
        """
        return not bool(self.parent)

    @property
    def root(self):
        """
        Returns root game for this game
        """
        if not self.parent:
            return self
        return self.parent.root

    @property
    def has_message(self):
        from .routes import GameMessageResource

        return bool(GameMessageResource.findfile(self))

    @property
    def is_ingame(self):
        from .polling import Poller

        return (self.gamemode in
                Poller.findPoller(self.gametype).gamemodes_ingame)

    @hybrid_method
    def is_game_player(self, player):
        return (player.id == self.creator_id) | (player.id == self.opponent_id)

    @hybrid_method
    def other(self, player):
        if player.id == self.creator_id:
            return self.opponent
        if player.id == self.opponent_id:
            return self.creator
        return None

    @other.expression
    def other(cls, player):
        return case([
            (player.id == cls.creator_id, cls.opponent),
            (player.id == cls.opponent_id, cls.creator),
        ], else_=None)

    def __repr__(self):
        return '<Game id={} state={}>'.format(self.id, self.state)


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('player.id'), index=True)
    sender = db.relationship(Player, foreign_keys='ChatMessage.sender_id')
    receiver_id = db.Column(db.Integer, db.ForeignKey('player.id'), index=True)
    receiver = db.relationship(Player, foreign_keys='ChatMessage.receiver_id')
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), index=True)
    game = db.relationship(Game)
    text = db.Column(db.Text)
    time = db.Column(db.DateTime, default=datetime.utcnow)
    has_attachment = db.Column(db.Boolean, default=False)
    viewed = db.Column(db.Boolean, default=False)

    @hybrid_method
    def is_for(self, user):
        return (user.id == self.sender_id) | (user.id == self.receiver_id)

    def other(self, user):
        if user == self.sender:
            return self.receiver
        if user == self.receiver:
            return self.sender
        raise ValueError('Message is unrelated to user %d' % user.id)

    @classmethod
    def for_user(cls, user):
        return cls.query.filter(or_(
            cls.sender_id == user.id,
            cls.receiver_id == user.id,
        ))

    @classmethod
    def for_users(cls, a, b):
        return cls.query.filter(
            cls.sender_id.in_([a.id, b.id]),
            cls.receiver_id.in_([a.id, b.id]),
        )

    def __repr__(self):
        return '<ChatMessage id={} text={}>'.format(self.id, self.text)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # game should be the root game of inner challenges hierarchy
    # as it denotes gaming session
    # TODO: maybe make it optional?
    root_id = db.Column(db.Integer, db.ForeignKey('game.id'),
                        index=True, nullable=False)
    root = db.relationship(Game, backref='events', foreign_keys='Event.root_id')

    @db.validates('root')
    def validate_root(self, key, game):
        # ensure it is the root of game session
        return game.root

    time = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    type = db.Column(db.Enum(
        'message',  # one user sent message to another
        'system',  # system notification about game state, bet state unchanged
        'betstate',  # some bet changed its state, or was created
        'abort',  # request to abort one of bets in this session
        'report',  # user reported about game result
    ), nullable=False)

    # for 'message' type
    message_id = db.Column(db.Integer, db.ForeignKey('chat_message.id'))
    message = db.relationship(ChatMessage)
    # for 'system', 'betstate' and 'abort' types
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'))
    game = db.relationship(Game, foreign_keys='Event.game_id')
    # for 'system' and probably 'betstate' types
    text = db.Column(db.Text)
    # for 'betstate' type
    newstate = db.Column(db.String(128))


class Beta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(128))
    name = db.Column(db.String(128))
    gametypes = db.Column(db.Text)
    platforms = db.Column(db.String(128))
    PLATFORMS = [
        'Android',
        'iOS',
        'Windows Mobile',
        'Web',
        'other',
    ]
    console = db.Column(db.String(128))
    create_date = db.Column(db.DateTime, default=datetime.utcnow)
    flags = db.Column(db.Text, default='')  # probably json
    backup = db.Column(db.Text)

    def __repr__(self):
        return '<Beta id={}>'.format(self.id)


class TGT(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    iou = db.Column(db.String(255), index=True)
    tgt = db.Column(db.String(255))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


def fast_count_noexec(query):
    return query.statement.with_only_columns([func.count()]).order_by(None)


def fast_count(query):
    """
    Get count of queried items avoiding using subquery (like query.count() does)
    """
    return query.session.execute(fast_count_noexec(query)).scalar()
