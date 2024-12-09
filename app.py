from flask import Flask, request, jsonify, render_template, send_from_directory, make_response
import subprocess
import os
import threading
import re
import signal
import sys
import random
import time

app = Flask(__name__, static_folder='static')
app.secret_key = 'your_secret_key'
output_file = ""
output_mp4_file = ""

# Set the path for saving downloaded files on the server
base_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_FOLDER = os.path.join(base_dir, 'downloads')

# Ensure the directory exists
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

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

    def run_download():
        global output_file, output_mp4_file

        # Get a valid filename from the YouTube link
        video_id = None
        
        if 'v=' in youtube_link:
            video_id = youtube_link.split('v=')[1].split('&')[0]
        elif 'youtu.be/' in youtube_link:
            video_id = youtube_link.split('youtu.be/')[1]
        else:
            output_file = ""
            output_mp4_file = ""
            print("Invalid YouTube link.")
            return

        title = re.sub(r'[<>:"/\\|?*]', '', video_id)
        output_file = f"{title}.mp3"
        output_mp4_file = f"{title}.mp4"  
        output_mp3_path = os.path.join(DOWNLOAD_FOLDER, output_file)
        output_mp4_path = os.path.join(DOWNLOAD_FOLDER, output_mp4_file)

        # Adding random delay to prevent triggering YouTube rate-limits
        time.sleep(random.uniform(5, 15))  # Increased delay

        # Prepare the yt-dlp command to download and convert to MP3
        cmd_mp3 = [
            'yt-dlp', 
            '-x', 
            '--audio-format', 'mp3', 
            '-o', output_mp3_path, 
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36', 
            youtube_link
        ]

        retry_count = 3
        for attempt in range(retry_count):
            try:
                # Run the command for mp3
                process_mp3 = subprocess.Popen(cmd_mp3, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                stdout_mp3, stderr_mp3 = process_mp3.communicate()

                print("STDOUT MP3:", stdout_mp3)
                print("STDERR MP3:", stderr_mp3)

                if process_mp3.returncode == 0 and os.path.exists(output_mp3_path):
                    print(f"Downloaded MP3 file: {output_mp3_path}")

                    # Run command for MP4
                    cmd_mp4 = [
                        'yt-dlp', 
                        '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]', 
                        '-o', output_mp4_path, 
                        '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36', 
                        youtube_link
                    ]
                    process_mp4 = subprocess.Popen(cmd_mp4, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    stdout_mp4, stderr_mp4 = process_mp4.communicate()

                    print("STDOUT MP4:", stdout_mp4)
                    print("STDERR MP4:", stderr_mp4)

                    if process_mp4.returncode == 0 and os.path.exists(output_mp4_path):
                        print(f"Downloaded MP4 file: {output_mp4_path}")
                    else:
                        print(f"Error downloading MP4: {stderr_mp4}")
                        output_mp4_file = ""  # Clear the mp4 filename on error
                else:
                    if "HTTP Error 429" in stderr_mp3 or "HTTP Error 400" in stderr_mp3:
                        print("Hit rate limit or bad request, sleeping for a longer duration...")
                        time.sleep(60)  # Wait longer before retrying
                    print(f"Error downloading MP3: {stderr_mp3}")
                    output_file = ""  # Clear the filename on error
                    output_mp4_file = ""  # Clear the mp4 filename on error

                break  # Exit the retry loop if successful

            except Exception as e:
                print(f"An error occurred: {str(e)}")
                time.sleep(2 ** attempt)  # Exponential backoff on error
        else:
            print("Max retries reached, download failed.")
            output_file = ""
            output_mp4_file = ""

    download_thread = threading.Thread(target=run_download)
    download_thread.start()
    download_thread.join()

    response_data = {}
    if output_file and os.path.exists(os.path.join(DOWNLOAD_FOLDER, output_file)):
        response_data['mp3_file'] = output_file
    if output_mp4_file and os.path.exists(os.path.join(DOWNLOAD_FOLDER, output_mp4_file)):
        response_data['mp4_file'] = output_mp4_file

    if response_data:
        response = make_response(jsonify({'status': 'Download complete', 'files': response_data}))
        response.set_cookie('downloaded_file', output_file)
        return response
    else:
        print(f"Output file: {output_file}, MP4 file: {output_mp4_file}")
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

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    app.run(host="0.0.0.0", port=8080)
