let isDownloading = false;

document.getElementById('downloadForm').addEventListener('submit', async function(event) {
    event.preventDefault();

    const loadingContainer = document.getElementById('loadingContainer');
    const loadingText = document.getElementById('loadingText');
    const progressBar = document.getElementById('loadingBar');
    const downloadLink = document.getElementById('downloadLink');
    const downloadAnchor = document.getElementById('downloadAnchor');

    loadingContainer.style.display = 'block'; 
    loadingText.style.display = 'block'; 
    progressBar.style.width = '0'; 

    const formData = new FormData(this);

    try {
        const response = await fetch('/download', {
            method: 'POST',
            body: formData
        });

        if (response.ok) {
            const responseData = await response.json();
            isDownloading = true;

            const interval = setInterval(async () => {
                const progressResponse = await fetch('/progress');
                const progressData = await progressResponse.json();
                progressBar.style.width = progressData.progress + '%';

                if (progressData.progress >= 100) {
                    clearInterval(interval);
                    loadingText.innerText = 'Download complete!';
                    downloadLink.style.display = 'block';
                    downloadAnchor.href = `/downloads/${responseData.output_file}`; 
                }
            }, 1000);
        } else {
            const errorData = await response.json();
            document.getElementById('error').innerText = errorData.error;
            loadingContainer.style.display = 'none'; 
            loadingText.style.display = 'none'; 
        }
    } catch (error) {
        document.getElementById('error').innerText = 'An error occurred: ' + error.message;
        loadingContainer.style.display = 'none'; 
        loadingText.style.display = 'none'; 
    }
});
