let isDownloading = false;

document.getElementById('downloadForm').addEventListener('submit', async function(event) {
    event.preventDefault();

    const loadingText = document.getElementById('loadingText');
    const downloadLink = document.getElementById('downloadLink');
    const downloadAnchor = document.getElementById('downloadAnchor');

    loadingText.style.display = 'block'; 
    loadingText.innerText = 'Loading'; // Start with initial loading text
    loadingText.style.animation = 'none'; // Reset animation

    const formData = new FormData(this);

    try {
        // Send the request to download the file
        const response = await fetch('/download', {
            method: 'POST',
            body: formData
        });

        if (response.ok) {
            const responseData = await response.json();
            isDownloading = true;

            // Since we're no longer tracking progress, just update the loading text and show download link
            loadingText.innerText = 'Download complete!';
            downloadLink.style.display = 'block';
            downloadAnchor.href = `/downloads/${responseData.output_file}`; // Set the correct file path
            downloadAnchor.download = responseData.output_file; // Suggest a filename for download
        } else {
            const errorData = await response.json();
            document.getElementById('error').innerText = errorData.error || 'An error occurred during download.';
            loadingText.style.display = 'none'; 
        }
    } catch (error) {
        document.getElementById('error').innerText = 'An error occurred: ' + error.message;
        loadingText.style.display = 'none'; 
    }
});
