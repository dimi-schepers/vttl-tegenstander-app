import re
import time
import threading
from flask import Flask, jsonify, send_from_directory
import requests
from bs4 import BeautifulSoup

app = Flask(__name__, static_folder='public', static_url_path='')

cache = {}
cache_lock = threading.Lock()
CACHE_TTL = 300  # 5 minutes

HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; VTTL-Analyzer/1.0)'}


def fetch_player_results(player_id, season=1):
    cache_key = f"{player_id}-{season}"
    with cache_lock:
        if cache_key in cache:
            entry = cache[cache_key]
            if time.time() - entry['ts'] < CACHE_TTL:
                return entry['data']

    url = f"https://competitie.vttl.be/speler/{player_id}/uitslagen/{season}"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, 'html.parser')

    # Extract player name — the site puts first/last name in two divs with large font-size
    name_parts = []
    for div in soup.find_all('div', style=re.compile(r'font-size:\s*\d{3}%')):
        text = div.get_text(strip=True)
        if text:
            name_parts.append(text)
    player_name = ' '.join(name_parts) if name_parts else ''

    results = []

    for row in soup.find_all('tr'):
        cells = row.find_all('td')
        if len(cells) < 6:
            continue

        opponent_id = None
        opponent_name = None
        opponent_ranking = None
        date = None
        player_sets = None
        opponent_sets = None

        date = cells[0].get_text(strip=True)

        # Find opponent link: href contains menu=6 and sel=ID
        for i, cell in enumerate(cells):
            link = cell.find('a', href=re.compile(r'menu=6.*sel=\d+|sel=\d+.*menu=6'))
            if link:
                href = link.get('href', '')
                m = re.search(r'sel=(\d+)', href)
                if m:
                    opponent_id = int(m.group(1))
                    opponent_name = link.get_text(strip=True)
                    # Ranking is typically the next cell
                    if i + 1 < len(cells):
                        opponent_ranking = cells[i + 1].get_text(strip=True)
                break

        if not opponent_id:
            continue

        # Find score cell: "X - Y" pattern
        for cell in cells:
            text = cell.get_text(strip=True)
            m = re.match(r'^(\d+)\s*[-–]\s*(\d+)$', text)
            if m:
                player_sets = int(m.group(1))
                opponent_sets = int(m.group(2))
                break

        if player_sets is None:
            continue

        results.append({
            'date': date,
            'opponentId': opponent_id,
            'opponentName': opponent_name,
            'opponentRanking': opponent_ranking,
            'playerSets': player_sets,
            'opponentSets': opponent_sets,
            'won': player_sets > opponent_sets,
        })

    data = {'playerId': int(player_id), 'playerName': player_name, 'results': results}
    with cache_lock:
        cache[cache_key] = {'ts': time.time(), 'data': data}
    return data


def fetch_clubs():
    with cache_lock:
        if 'clubs' in cache and time.time() - cache['clubs']['ts'] < 3600:
            return cache['clubs']['data']

    resp = requests.get('https://competitie.vttl.be/spelers', headers=HEADERS, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    select = soup.find('select', id='ClubList')
    clubs = []
    for opt in select.find_all('option'):
        val = opt.get('value', '').strip()
        text = opt.get_text(strip=True)
        if val and val != '0':
            clubs.append({'id': val, 'name': text})

    with cache_lock:
        cache['clubs'] = {'ts': time.time(), 'data': clubs}
    return clubs


def parse_players_from_soup(soup, seen):
    players = []
    for row in soup.find_all('tr'):
        cells = row.find_all('td')
        if len(cells) < 4:
            continue
        result_link = row.find('a', href=re.compile(r'menu=6.*sel=\d+.*result=1|result=1.*sel=\d+.*menu=6'))
        if not result_link:
            continue
        m = re.search(r'sel=(\d+)', result_link['href'])
        if not m:
            continue
        player_id = int(m.group(1))
        if player_id in seen:
            continue
        seen.add(player_id)
        row_text = [c.get_text(strip=True) for c in cells]
        full_name = row_text[4] if len(row_text) > 4 else f"{row_text[3]} {row_text[2]}"
        ranking = row_text[5] if len(row_text) > 5 else ''
        players.append({'id': player_id, 'name': full_name, 'ranking': ranking})
    return players


def fetch_club_players(club_id):
    cache_key = f"club-{club_id}"
    with cache_lock:
        if cache_key in cache and time.time() - cache[cache_key]['ts'] < 3600:
            return cache[cache_key]['data']

    players = []
    seen = set()
    page = 1
    while True:
        url = f"https://competitie.vttl.be/?menu=6&club_id={club_id}&cur_page={page}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        page_players = parse_players_from_soup(soup, seen)
        players.extend(page_players)
        # Check if there is a next page link
        has_next = soup.find('a', href=re.compile(rf'cur_page={page + 1}'))
        if not has_next:
            break
        page += 1

    players.sort(key=lambda p: p['name'])
    with cache_lock:
        cache[cache_key] = {'ts': time.time(), 'data': players}
    return players


@app.route('/api/clubs')
def get_clubs():
    try:
        return jsonify(fetch_clubs())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/clubs/<club_id>/players')
def get_club_players(club_id):
    if not re.match(r'^\d+$', club_id):
        return jsonify({'error': 'Invalid club ID'}), 400
    try:
        return jsonify(fetch_club_players(club_id))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/')
def index():
    return send_from_directory('public', 'index.html')


@app.route('/api/results/<int:player_id>')
def get_results(player_id):
    try:
        data = fetch_player_results(player_id)
        return jsonify(data)
    except requests.HTTPError as e:
        return jsonify({'error': f'Speler niet gevonden: {e}'}), 404
    except Exception as e:
        print(f"Error fetching player {player_id}: {e}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 3000))
    print(f"VTTL App running at http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
