import os
import subprocess
import tempfile
import json
import re
from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Allow all origins — lock this down to your frontend URL in production

# ── helpers ──────────────────────────────────────────────────────────────────

def run_ytdlp(args):
    """Run a yt-dlp command and return (stdout, stderr, returncode)."""
    result = subprocess.run(
        ['yt-dlp'] + args,
        capture_output=True,
        text=True,
        timeout=300  # 5 min max
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def sanitize_filename(name):
    """Strip characters that break Content-Disposition headers."""
    return re.sub(r'[^\w\s\-.]', '', name).strip() or 'video'


# ── routes ───────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'RIPP yt-dlp backend'})


@app.route('/api/info', methods=['POST'])
def info():
    """Return video metadata (title, thumbnail, duration, uploader)."""
    data = request.get_json(silent=True) or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    stdout, stderr, code = run_ytdlp([
        '--dump-json',
        '--no-playlist',
        '--no-warnings',
        url
    ])

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
    """
    Download a video with yt-dlp (merging best video+audio via ffmpeg),
    then stream the resulting MP4 back to the client.
    """
    data = request.get_json(silent=True) or {}
    url     = data.get('url', '').strip()
    quality = data.get('quality', '1080')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    # Validate quality value
    allowed = {'720', '1080', '1440', '2160'}
    if quality not in allowed:
        quality = '1080'

    # Format string: best video up to requested height + best audio, merged to mp4
    fmt = f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best'

    tmpdir = tempfile.mkdtemp()
    output_template = os.path.join(tmpdir, '%(title).80s.%(ext)s')

    stdout, stderr, code = run_ytdlp([
        '-f', fmt,
        '--merge-output-format', 'mp4',
        '--no-playlist',
        '--no-warnings',
        '-o', output_template,
        url
    ])

    if code != 0:
        return jsonify({'error': stderr or 'Download failed'}), 500

    # Find the output file
    files = [f for f in os.listdir(tmpdir) if f.endswith('.mp4')]
    if not files:
        return jsonify({'error': 'Output file not found after download'}), 500

    filepath = os.path.join(tmpdir, files[0])
    download_name = sanitize_filename(files[0])

    # Clean up the temp dir after Flask finishes sending the file
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
