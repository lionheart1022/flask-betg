#!/usr/bin/env python3
from flask import Flask, request, session, render_template
from flask import abort, redirect, url_for

import requests

import config

app = Flask(__name__, static_folder = 'static-m')
# for session...
app.config['SECRET_KEY'] = config.JWT_SECRET
app.config['API'] = 'http://betgame.co.uk/v1'
app.config['API_ROOT'] = 'http://betgame.co.uk/v1'

@app.route('/')
def bets():
    return render_template('newbet.html')

@app.route('/gametype', methods=['GET','POST'])
def gametype():
    if request.method == 'POST':
        session.gametype = request.args.get('gametype')
        return redirect(url_for('bets'))
    games = requests.get(app.config['API_ROOT']+'/gametypes').json().get('gametypes')
    return render_template('gametype.html', games=games)

@app.route('/leaders')
def leaderboard():
    return render_template('leaderboard.html')

@app.route('/challenges')
def challenges():
    return render_template('challenges.html')

@app.route('/profile')
def profile():
    return render_template('profile.html')

if __name__ == '__main__':
    app.run(debug=True)
