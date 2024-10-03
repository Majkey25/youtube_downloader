from flask import Flask, request, jsonify, render_template, send_from_directory
import subprocess
import os
import threading
import re

app = Flask(__name__)
output_file = ""  # Sem uložíme název souboru

# Nastavíme cestu pro ukládání stažených souborů na serveru
DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)  # Vytvoří složku, pokud neexistuje

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

        # Získání platného názvu souboru z odkazu YouTube
        video_id = None
        
        # Ověření formátu odkazu YouTube
        if 'v=' in youtube_link:
            video_id = youtube_link.split('v=')[1].split('&')[0]
        elif 'youtu.be/' in youtube_link:
            video_id = youtube_link.split('youtu.be/')[1]
        else:
            output_file = ""
            print("Invalid YouTube link.")
            return

        # Čistíme název souboru a omezujeme zakázané znaky
        title = re.sub(r'[<>:"/\\|?*]', '', video_id)
        output_file = f"{title}.mp3"
        output_path = os.path.join(DOWNLOAD_FOLDER, output_file)  # Uložíme soubor na server

        # Příprava příkazu yt-dlp pro stažení a konverzi do MP3
        cmd = ['yt-dlp', '-x', '--audio-format', 'mp3', '-o', output_path, youtube_link]

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate()

        # Debugging log
        print("STDOUT:", stdout)
        print("STDERR:", stderr)

        # Zkontrolujme, zda došlo k chybě
        if process.returncode != 0:
            print(f"Error downloading file: {stderr}")
            output_file = ""  # Vymazat jméno souboru při chybě

    # Spustit stahování v samostatném vlákně
    download_thread = threading.Thread(target=run_download)
    download_thread.start()
    download_thread.join()  # Počkat, než vlákno dokončí stahování

    # Zkontrolujeme, zda byl soubor úspěšně stažen
    if output_file and os.path.exists(os.path.join(DOWNLOAD_FOLDER, output_file)):
        return jsonify({'status': 'Download complete', 'output_file': output_file})
    else:
        return jsonify({'error': 'Failed to download file.'}), 500

@app.route('/downloads/<filename>')
def download_file(filename):
    return send_from_directory(DOWNLOAD_FOLDER, filename, as_attachment=True)  # Vrátíme soubor jako přílohu ke stažení

@app.route('/delete', methods=['POST'])
def delete_file():
    global output_file
    if output_file:
        file_path = os.path.join(DOWNLOAD_FOLDER, output_file)
        if os.path.exists(file_path):
            os.remove(file_path)
            output_file = ""  # Vymazání názvu souboru
            return jsonify({'status': 'File deleted successfully'})
    return jsonify({'error': 'File not found'}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
