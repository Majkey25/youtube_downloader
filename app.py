from flask import Flask, request, jsonify, render_template, send_from_directory, make_response
import subprocess
import os
import random
import time
import re
import sys
import signal
import logging

app = Flask(__name__, static_folder='static')
app.secret_key = 'your_secret_key'
output_file = ""
output_mp4_file = ""

logging.basicConfig(level=logging.INFO)

# Set the path for saving downloaded files on the server
DOWNLOAD_FOLDER = 'downloads'

# Check if the downloads folder exists, create it if it doesn't
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)
    os.chmod(DOWNLOAD_FOLDER, 0o755)  # Set permissions to 755

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)

@app.route('/download', methods=['POST'])
def download():
    global output_file, output_mp4_file
    youtube_link = request.form['youtubeLink']

    # Validate YouTube link format
    if not (('v=' in youtube_link) or ('youtu.be/' in youtube_link)):
        return jsonify({'error': 'Invalid YouTube link.'}), 400

    # Extract video ID
    video_id = None
    if 'v=' in youtube_link:
        video_id = youtube_link.split('v=')[1].split('&')[0]
    elif 'youtu.be/' in youtube_link:
        video_id = youtube_link.split('youtu.be/')[1]

    title = re.sub(r'[<>:"/\\|?*]', '', video_id)
    output_file = f"{title}.mp3"
    output_mp4_file = f"{title}.mp4"
    output_mp3_path = os.path.join(DOWNLOAD_FOLDER, output_file)
    output_mp4_path = os.path.join(DOWNLOAD_FOLDER, output_mp4_file)

    # Adding random delay to prevent triggering YouTube rate-limits
    time.sleep(random.uniform(1, 3))

    # Prepare the yt-dlp command to download and convert to MP3
    cmd_mp3 = ['yt-dlp', '-x', '--audio-format', 'mp3', '-o', output_mp3_path, youtube_link]

    # Using subprocess to download files
    try:
        # Run the command for mp3
        process_mp3 = subprocess.run(cmd_mp3, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if process_mp3.returncode == 0 and os.path.exists(output_mp3_path):
            # Run command for MP4
            cmd_mp4 = ['yt-dlp', '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]', '-o', output_mp4_path, youtube_link]
            process_mp4 = subprocess.run(cmd_mp4, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            if process_mp4.returncode == 0 and os.path.exists(output_mp4_path):
                response_data = {
                    'status': 'Download complete',
                    'files': {
                        'mp3_file': output_file,
                        'mp4_file': output_mp4_file
                    }
                }
                response = make_response(jsonify(response_data))
                response.set_cookie('downloaded_file', output_file)
                return response
            else:
                output_mp4_file = ""  # Clear the mp4 filename on error
        else:
            output_file = ""  # Clear the filename on error
            output_mp4_file = ""  # Clear the mp4 filename on error

    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'error': 'Failed to download file or download links are not available.'}), 500

@app.route('/downloads/<filename>')
def download_file(filename):
    return send_from_directory(DOWNLOAD_FOLDER, filename, as_attachment=True)

@app.route('/check_cookie')
def check_cookie():
    downloaded_file = request.cookies.get('downloaded_file')
    if downloaded_file:
        return f'Last downloaded file: {downloaded_file}'
    return 'No file downloaded yet.'

@app.route('/delete', methods=['POST'])
def delete_file():
    global output_file, output_mp4_file
    files_deleted = []
    
    if output_file:
        file_path = os.path.join(DOWNLOAD_FOLDER, output_file)
        if os.path.exists(file_path):
            os.remove(file_path)
            files_deleted.append(output_file)
    
    if output_mp4_file:
        mp4_path = os.path.join(DOWNLOAD_FOLDER, output_mp4_file)
        if os.path.exists(mp4_path):
            os.remove(mp4_path)
            files_deleted.append(output_mp4_file)

    # Reset global file variables
    output_file = ""
    output_mp4_file = ""
    
    if files_deleted:
        return jsonify({'status': 'Files deleted successfully', 'files': files_deleted})
    
    return jsonify({'error': 'No files to delete or files not found'}), 404

@app.route('/delete_cookie', methods=['POST'])
def delete_cookie():
    response = make_response(jsonify({'status': 'Cookie deleted'}))
    response.set_cookie('downloaded_file', '', expires=0)
    return response

def cleanup_downloads():
    print("Cleaning up downloads...")
    if os.path.exists(DOWNLOAD_FOLDER):
        for filename in os.listdir(DOWNLOAD_FOLDER):
            file_path = os.path.join(DOWNLOAD_FOLDER, filename)
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    print(f"Deleted file: {file_path}")
                else:
                    print(f"{file_path} is not a file. Skipping.")
            except Exception as e:
                print(f"Error deleting file {file_path}: {e}")
    else:
        print(f"Download folder {DOWNLOAD_FOLDER} does not exist.")

def signal_handler(sig, frame):
    print("Shutting down server and cleaning up downloads...")
    cleanup_downloads()
    sys.exit(0)

@app.before_request
def before_request():
    pass

@app.after_request
def after_request(response):
    return response

# JavaScript code to handle leaving the page and deleting files
@app.route('/leave', methods=['POST'])
def leave():
    return delete_file()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    app.run(host="0.0.0.0", port=8080)
