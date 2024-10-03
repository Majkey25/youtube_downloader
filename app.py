from flask import Flask, request, jsonify, render_template, send_from_directory, make_response
import subprocess
import os
import threading
import re

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Set your secret key for cookie signing
output_file = ""  # Variable to store the downloaded file name

# Set the path for saving downloaded files on the server
DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)  # Create folder if it doesn't exist

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)

@app.route('/download', methods=['POST'])
def download():
    global output_file
    youtube_link = request.form['youtubeLink']

    def run_download():
        global output_file

        # Get a valid filename from the YouTube link
        video_id = None
        
        # Validate the YouTube link format
        if 'v=' in youtube_link:
            video_id = youtube_link.split('v=')[1].split('&')[0]
        elif 'youtu.be/' in youtube_link:
            video_id = youtube_link.split('youtu.be/')[1]
        else:
            output_file = ""
            print("Invalid YouTube link.")
            return

        # Clean the filename and limit forbidden characters
        title = re.sub(r'[<>:"/\\|?*]', '', video_id)
        output_file = f"{title}.mp3"
        output_path = os.path.join(DOWNLOAD_FOLDER, output_file)  # Save file on server

        # Prepare the yt-dlp command to download and convert to MP3
        cmd = ['yt-dlp', '-x', '--audio-format', 'mp3', '-o', output_path, youtube_link]

        try:
            # Run the command and capture stdout and stderr
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = process.communicate()

            # Debugging log
            print("STDOUT:", stdout)
            print("STDERR:", stderr)

            # Check if there was an error
            if process.returncode != 0:
                print(f"Error downloading file: {stderr}")
                output_file = ""  # Clear the filename on error

        except Exception as e:
            print(f"An error occurred: {str(e)}")
            output_file = ""  # Clear the filename on error

    # Start the download in a separate thread
    download_thread = threading.Thread(target=run_download)
    download_thread.start()
    download_thread.join()  # Wait for the thread to finish

    # Check if the file was downloaded successfully
    if output_file and os.path.exists(os.path.join(DOWNLOAD_FOLDER, output_file)):
        response = make_response(jsonify({'status': 'Download complete', 'output_file': output_file}))
        response.set_cookie('downloaded_file', output_file)  # Set a cookie with the downloaded file name
        return response
    else:
        print(f"Output file: {output_file}")
        return jsonify({'error': 'Failed to download file.'}), 500

@app.route('/downloads/<filename>')
def download_file(filename):
    return send_from_directory(DOWNLOAD_FOLDER, filename, as_attachment=True)  # Return the file as an attachment to download

@app.route('/check_cookie')
def check_cookie():
    downloaded_file = request.cookies.get('downloaded_file')
    if downloaded_file:
        return f'Last downloaded file: {downloaded_file}'
    return 'No file downloaded yet.'

@app.route('/delete', methods=['POST'])
def delete_file():
    global output_file
    if output_file:
        file_path = os.path.join(DOWNLOAD_FOLDER, output_file)
        if os.path.exists(file_path):
            os.remove(file_path)
            output_file = ""  # Clear the filename
            return jsonify({'status': 'File deleted successfully'})
    return jsonify({'error': 'File not found'}), 404

@app.route('/delete_cookie', methods=['POST'])
def delete_cookie():
    response = make_response(jsonify({'status': 'Cookie deleted'}))
    response.set_cookie('downloaded_file', '', expires=0)  # Delete the cookie
    return response

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)