from flask import Flask, request, jsonify, render_template, send_from_directory
import subprocess
import os
import threading

app = Flask(__name__)
output_file = ""

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/download', methods=['POST'])
def download():
    global output_file
    youtube_link = request.form['youtubeLink']

    def run_download():
        global output_file
        
        # Extract a valid title from the YouTube link
        title = youtube_link.split('=')[-1]  # Adjust this according to your needs
        output_file = f"{title}.mp3"
        output_path = os.path.expanduser(f'~/Downloads/{output_file}')  # Path for the downloaded file

        # Prepare the yt-dlp command
        cmd = ['yt-dlp', '-x', '--audio-format', 'mp3', '-o', output_path, youtube_link]

        # Run the command and capture output
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Wait for the download process to complete and capture output
        stdout, stderr = process.communicate()
        
        # Log the output for debugging
        print("STDOUT:", stdout)
        print("STDERR:", stderr)
        
        if process.returncode != 0:
            print(f"Error downloading file: {stderr}")  # Log error for debugging
            output_file = ""  # Clear output file if there was an error

    # Start the download in a separate thread
    thread = threading.Thread(target=run_download)
    thread.start()

    # Return a URL for the downloaded file (initially empty)
    return jsonify({'status': 'Downloading...', 'output_file': output_file})

@app.route('/downloads/<filename>')
def download_file(filename):
    downloads_path = os.path.expanduser('~/Downloads')  # Ensure this path is correct
    try:
        return send_from_directory(downloads_path, filename)
    except FileNotFoundError:
        return jsonify({'error': 'File not found.'}), 404  # Return a 404 error if the file is not found

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080)
