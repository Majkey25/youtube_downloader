let isDownloading = false;  
let loadingInterval;  

document.getElementById('downloadForm').addEventListener('submit', async function(event) {
    event.preventDefault();  

    const loadingText = document.getElementById('loadingText');
    const downloadLink = document.getElementById('downloadLink');
    const downloadMp3Anchor = document.getElementById('downloadMp3Anchor'); 
    const downloadMp4Anchor = document.getElementById('downloadMp4Anchor');  
    const errorText = document.getElementById('error');  

    // Reset and show loading text
    loadingText.style.display = 'block'; 
    loadingText.innerText = 'Loading';  
    downloadLink.style.display = 'none';  
    errorText.innerText = '';  

    let dotCount = 0;
    loadingInterval = setInterval(() => {
        dotCount = (dotCount + 1) % 4;  
        loadingText.innerText = 'Loading' + '.'.repeat(dotCount);  
    }, 500);

    const formData = new FormData(this);  

    try {
        const response = await fetch('/download', {
            method: 'POST',
            body: formData
        });

        clearInterval(loadingInterval);  

        if (response.ok) {
            const responseData = await response.json();
            isDownloading = true;  

            loadingText.innerText = 'Download complete!';  
            downloadLink.style.display = 'block';  

            if (responseData.files && downloadMp3Anchor && downloadMp4Anchor) {
                const mp3File = responseData.files.mp3_file;
                const mp4File = responseData.files.mp4_file;  
            
                // Verify if the file is correctly set
                console.log(`MP3 File: ${mp3File}`);
                console.log(`MP4 File: ${mp4File}`);
            
                downloadMp3Anchor.href = `/downloads/${mp3File}`;  
                downloadMp3Anchor.download = mp3File;  
                downloadMp4Anchor.href = `/downloads/${mp4File}`;  
                downloadMp4Anchor.download = mp4File;  
                downloadMp3Anchor.style.display = 'block'; 
                downloadMp4Anchor.style.display = 'block'; 
            } else {
                errorText.innerText = 'Download links are not available.'; 
                loadingText.style.display = 'none'; 
            }
            
        } else {
            const errorData = await response.json();
            errorText.innerText = errorData.error || 'An error occurred during download.';  
            loadingText.style.display = 'none';  
        }
    } catch (error) {
        console.error('Fetch Error:', error);  
        errorText.innerText = 'An error occurred: ' + error.message;  
        loadingText.style.display = 'none';  
    } finally {
        isDownloading = false;  
    }
});

// Cleanup on page unload
window.addEventListener('beforeunload', async (event) => {
    if (isDownloading) {  
        const confirmationMessage = "You have a download in progress. Are you sure you want to leave?";
        event.returnValue = confirmationMessage; 
    }

    // Call the delete endpoint to remove files on server
    await fetch('/delete', {
        method: 'POST',
    });
});
