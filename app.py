import os
import subprocess
import tempfile
import json
import re
from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Cookies file path — persists within a Railway deployment session
COOKIES_PATH = '/tmp/yt_cookies.txt'


# ── helpers ──────────────────────────────────────────────────────────────────

def cookies_args():
    """Return ['--cookies', path] if a cookies file has been uploaded, else []."""
    if os.path.exists(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0:
        return ['--cookies', COOKIES_PATH]
    return []


def run_ytdlp(args):
    """Run a yt-dlp command and return (stdout, stderr, returncode)."""
    result = subprocess.run(
        ['yt-dlp'] + args,
        capture_output=True,
        text=True,
        timeout=300
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def sanitize_filename(name):
    """Strip characters that break Content-Disposition headers."""
    return re.sub(r'[^\w\s\-.]', '', name).strip() or 'video'


# ── routes ───────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def health():
    has_cookies = os.path.exists(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0
    return jsonify({
        'status': 'ok',
        'service': 'RIPP yt-dlp backend',
        'cookies': 'loaded' if has_cookies else 'missing'
    })


@app.route('/api/cookies', methods=['POST'])
def upload_cookies():
    """Accept a cookies.txt upload and save it for yt-dlp to use."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file in request'}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty filename'}), 400

    content = f.read().decode('utf-8', errors='ignore')

    # Basic sanity check — valid Netscape cookie files contain this
    if 'HTTP Cookie File' not in content and '# Netscape' not in content and '.youtube.com' not in content:
        return jsonify({'error': 'Does not look like a valid Netscape cookies.txt — make sure you exported from YouTube'}), 400

    with open(COOKIES_PATH, 'w') as out:
        out.write(content)

    line_count = sum(1 for line in content.splitlines() if line.strip() and not line.startswith('#'))
    return jsonify({'status': 'ok', 'cookies_loaded': line_count})


@app.route('/api/cookies', methods=['DELETE'])
def delete_cookies():
    """Remove the saved cookies file."""
    try:
        if os.path.exists(COOKIES_PATH):
            os.remove(COOKIES_PATH)
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cookies/status', methods=['GET'])
def cookies_status():
    has = os.path.exists(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0
    return jsonify({'loaded': has})


@app.route('/api/info', methods=['POST'])
def info():
    """Return video metadata (title, thumbnail, duration, uploader)."""
    data = request.get_json(silent=True) or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    stdout, stderr, code = run_ytdlp(
        cookies_args() + [
            '--dump-json',
            '--no-playlist',
            '--no-warnings',
            url
        ]
    )

    if code != 0:
        return jsonify({'error': stderr or 'yt-dlp failed to fetch info'}), 500

    try:
        meta = json.loads(stdout)
    except json.JSONDecodeError:
        return jsonify({'error': 'Could not parse video info'}), 500

    return jsonify({
        'title':     meta.get('title', 'Unknown'),
        'thumbnail': meta.get('thumbnail', ''),
        'duration':  meta.get('duration_string', ''),
        'uploader':  meta.get('uploader', ''),
        'id':        meta.get('id', ''),
    })


@app.route('/api/download', methods=['POST'])
def download():
    """Download a video with yt-dlp, merge with ffmpeg, stream back as MP4."""
    data = request.get_json(silent=True) or {}
    url     = data.get('url', '').strip()
    quality = data.get('quality', '1080')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    allowed = {'720', '1080', '1440', '2160'}
    if quality not in allowed:
        quality = '1080'

    fmt = (
        f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]'
        f'/bestvideo[height<={quality}]+bestaudio'
        f'/best[height<={quality}]/best'
    )

    tmpdir = tempfile.mkdtemp()
    output_template = os.path.join(tmpdir, '%(title).80s.%(ext)s')

    stdout, stderr, code = run_ytdlp(
        cookies_args() + [
            '-f', fmt,
            '--merge-output-format', 'mp4',
            '--no-playlist',
            '--no-warnings',
            '-o', output_template,
            url
        ]
    )

    if code != 0:
        return jsonify({'error': stderr or 'Download failed'}), 500

    files = [f for f in os.listdir(tmpdir) if f.endswith('.mp4')]
    if not files:
        return jsonify({'error': 'Output file not found after download'}), 500

    filepath = os.path.join(tmpdir, files[0])
    download_name = sanitize_filename(files[0])

    @after_this_request
    def cleanup(response):
        try:
            os.remove(filepath)
            os.rmdir(tmpdir)
        except Exception:
            pass
        return response

    return send_file(
        filepath,
        mimetype='video/mp4',
        as_attachment=True,
        download_name=download_name
    )


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
