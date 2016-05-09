#!/usr/bin/env python
# coding: utf-8

import socket

from datetime import datetime, timedelta
from time import sleep

from flask import Flask, jsonify
from flask_migrate import Migrate, MigrateCommand
from flask_script import Manager
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

app.config.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:////tmp/test.db')
app.config.setdefault('FLIGHT_GAP_HOURS', 2)

db = SQLAlchemy(app)
migrate = Migrate(app, db)
manager = Manager(app)
manager.add_command('db', MigrateCommand)


class Aircraft(db.Model):

    id = db.Column(db.Integer, primary_key=True)
    icao = db.Column(db.Integer, unique=True, nullable=False)

    @property
    def icao_str(self):
        return '%06x' % self.icao

    @classmethod
    def create_from_icao(cls, icao):
        return cls(icao=int(icao, 16))

    @classmethod
    def get_by_icao(cls, icao):
        return cls.query.filter_by(icao=int(icao, 16)).first()

    def __str__(self):
        return self.icao_str

    def __repr__(self):
        return '<Aircraft: %s>' % self.icao_str


class Flight(db.Model):

    id = db.Column(db.Integer, primary_key=True)
    aircraft_id = db.Column(db.Integer, db.ForeignKey(Aircraft.id),
                            nullable=False)
    aircraft = db.relationship('Aircraft', backref='flights',
                               foreign_keys=[aircraft_id])
    name = db.Column(db.String(8))
    seen = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @classmethod
    def get_by_icao(cls, icao):
        return cls.query.join(cls.aircraft).filter(
            Aircraft.icao == int(icao, 16)).first()

    def __str__(self):
        return self.name or self.aircraft.icao_str

    def __repr__(self):
        return '<Flight: %s; aircraft=%s>' % (self.name,
                                              self.aircraft.icao_str)


class FlightPosition(db.Model):

    id = db.Column(db.Integer, primary_key=True)
    flight_id = db.Column(db.Integer, db.ForeignKey(Flight.id),
                          nullable=False)
    flight = db.relationship('Flight', backref='positions',
                             foreign_keys=[flight_id])
    altitude = db.Column(db.Integer)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    speed = db.Column(db.Integer)
    track = db.Column(db.Integer)
    vertical_rate = db.Column(db.Integer)
    time = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return '<FlightPosition: alt=%d; lat=%f; lon=%f>' % (self.altitude,
                                                             self.latitude,
                                                             self.longitude)

    @classmethod
    def get_active_positions(cls):
        return cls.query.filter(
            cls.time >= datetime.utcnow() - timedelta(minutes=2)). \
                group_by(cls.flight_id).all()

    def to_json(self):
        return {'icao': self.flight.aircraft.icao_str,
                'flight': self.flight.name,
                'altitude': self.altitude,
                'latitude': self.latitude,
                'longitude': self.longitude,
                'speed': self.speed,
                'track': self.track,
                'vertical_rate': self.vertical_rate}


class SBSConnection(object):

    def __init__(self, host, port=30003, timeout=5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._buffer = ''
        self._connect()

    def _connect(self):
        if self._sock is not None:
            self._sock.close()
        while True:
            try:
                app.logger.info('Connecting to %s:%d', self.host, self.port)
                self._sock = socket.create_connection((self.host, self.port),
                                                      self.timeout)
            except socket.error:
                sleep(1)
            else:
                break

    def readlines(self):
        try:
            buffer = self._sock.recv(1024)
            if not buffer:
                self._sock.close()
            else:
                self._buffer += buffer
        except socket.error:
            self._connect()
            return self.readlines()
        pos = self._buffer.find('\n')
        lines = []
        while pos != -1:
            lines.append(self._buffer[:pos])
            self._buffer = self._buffer[pos + 1:]
            pos = self._buffer.find('\n')
        return lines


class SBSParser(object):

    def __init__(self):
        self._seen = {}

    def _is_ready(self, state):
        # not having the name yet is ok, because the info takes some time
        # to appear for us.
        #
        # icao is always provided.
        for key in ['altitude', 'latitude', 'longitude', 'speed', 'track',
                    'vertical_rate']:
            if key not in state:
                return False
        return True

    def _set_seen_prop(self, icao, prop, value):
        if icao not in self._seen:
            self._seen[icao] = {}
        self._seen[icao][prop] = value

    def _add_to_database(self, icao):
        if icao not in self._seen:
            return
        if not self._is_ready(self._seen[icao]):
            return

        # get aircraft first
        aircraft = Aircraft.get_by_icao(icao)
        if aircraft is None:
            aircraft = Aircraft.create_from_icao(icao)
            db.session.add(aircraft)

        # get flight
        flight = Flight.get_by_icao(icao)
        if flight is not None:

            # if flight is too old, reject it!
            gap = timedelta(hours=app.config['FLIGHT_GAP_HOURS'])
            if flight.seen < datetime.utcnow() - gap:
                flight = None

        # create new flight, if needed
        if flight is None:
            flight = Flight(aircraft=aircraft)
            db.session.add(flight)

        # fix flight name, if needed
        if 'flight' in self._seen[icao]:
            flight.name = self._seen[icao]['flight']
            del self._seen[icao]['flight']
            db.session.add(flight)

        self._seen[icao]['flight'] = flight

        position = FlightPosition(**self._seen[icao])
        db.session.add(position)

        del self._seen[icao]

        db.session.commit()

    def parse(self, line):
        pieces = line.split(',')
        if len(pieces) != 22 or pieces[0] != 'MSG':
            return

        msgtype = int(pieces[1])
        icao = pieces[4]

        if msgtype == 1:
            self._set_seen_prop(icao, 'flight', pieces[10].strip())
        elif msgtype == 3:
            self._set_seen_prop(icao, 'altitude', int(pieces[11]))
            # its useless for us without latitude/longitude
            if pieces[14]:
                self._set_seen_prop(icao, 'latitude', float(pieces[14]))
            if pieces[15]:
                self._set_seen_prop(icao, 'longitude', float(pieces[15]))
        elif msgtype == 4:
            self._set_seen_prop(icao, 'speed', int(pieces[12] or 0))
            self._set_seen_prop(icao, 'track', int(pieces[13] or 0))
            self._set_seen_prop(icao, 'vertical_rate', int(pieces[16] or 0))
        self._add_to_database(icao)


@app.route('/')
def hello():
    return 'Hello world'


@app.route('/data.json')
def data_json():
    return jsonify(aircrafts=[i.to_json() \
                              for i in FlightPosition.get_active_positions()])


@manager.command
def create_db():
    '''Create database.'''
    db.create_all()


@manager.command
def run_worker(host, port=30003):
    '''Run worker.'''
    try:
        conn = SBSConnection(host, port)
        parser = SBSParser()
        while True:
            for i in conn.readlines():
                parser.parse(i)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    manager.run()
