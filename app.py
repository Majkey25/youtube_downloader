from flask import Flask, request, jsonify, render_template
import subprocess
import os
import threading
import re

app = Flask(__name__)
progress = 0
output_file = ""  # Initialize output_file globally

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/download', methods=['POST'])
def download():
    global progress, output_file  # Declare both progress and output_file as global
    progress = 0
    youtube_link = request.form['youtubeLink']

    def run_download():
        global progress, output_file
        title = youtube_link.split('=')[-1]
        output_file = f"{title}.mp3"  # Set output_file inside the thread
        output_path = os.path.expanduser(f'~/Downloads/{output_file}')
        cmd = ['yt-dlp', '-x', '--audio-format', 'mp3', '-o', output_path, youtube_link]

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        while True:
            output = process.stdout.readline()
            if output == '' and process.poll() is not None:
                break
            if output:
                update_progress(output)
        process.wait()

    thread = threading.Thread(target=run_download)
    thread.start()

    return jsonify({'status': 'Downloading...', 'output_file': output_file})  # Now output_file is defined

@app.route('/progress')
def get_progress():
    return jsonify({'progress': progress})

def update_progress(output):
    global progress
    match = re.search(r'(\d+)%', output)
    if match:
        progress = int(match.group(1))

if __name__ == '__main__':
    app.run(debug=True)
