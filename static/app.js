let isDownloading = false;
let loadingInterval;

document.getElementById('downloadForm').addEventListener('submit', async function(event) {
    event.preventDefault();

    const loadingText = document.getElementById('loadingText');
    const downloadLink = document.getElementById('downloadLink');
    const downloadMp3Anchor = document.getElementById('downloadMp3Anchor'); 
    const downloadMp4Anchor = document.getElementById('downloadWebmpAnchor');  // Change to downloadMp4Anchor
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
    
    // Log individual form data entries
    for (let [key, value] of formData.entries()) {
        console.log(key, value);
    }

    try {
        const response = await fetch('/download', {
            method: 'POST',
            body: formData
        });

        console.log('Response:', response);  // Log the entire response object
        console.log('Response Status:', response.status); // Log response status
        clearInterval(loadingInterval);

        if (response.ok) {
            const responseData = await response.json();
            console.log('Response Data:', responseData);  // Log parsed response data
            isDownloading = true;

            loadingText.innerText = 'Download complete!';  
            downloadLink.style.display = 'block';  

            // Check if anchors exist before setting href
            if (responseData.files && downloadMp3Anchor && downloadMp4Anchor) { // Change here
                const mp3File = responseData.files.mp3_file;
                const mp4File = responseData.files.mp4_file;  // Change here

                console.log('MP3 File:', mp3File);  // Log MP3 file name
                console.log('MP4 File:', mp4File);  // Log MP4 file name

                downloadMp3Anchor.href = `/downloads/${mp3File}`;  
                downloadMp3Anchor.download = mp3File;  
                downloadMp4Anchor.href = `/downloads/${mp4File}`;  // Change here
                downloadMp4Anchor.download = mp4File;  // Change here
                downloadMp3Anchor.style.display = 'block'; // Show the MP3 link
                downloadMp4Anchor.style.display = 'block'; // Show the MP4 link
            } else {
                errorText.innerText = 'Download links are not available.'; // Display error if anchors are not found
            }
        } else {
            const errorData = await response.json();
            errorText.innerText = errorData.error || 'An error occurred during download.';  
            loadingText.style.display = 'none';  
        }
    } catch (error) {
        console.error('Fetch Error:', error);  // Log any errors during fetch
        errorText.innerText = 'An error occurred: ' + error.message;  
        loadingText.style.display = 'none';  
    }
});

window.addEventListener('beforeunload', async () => {
    const response = await fetch('/delete', {
        method: 'POST',
    });
    if (!response.ok) {
        console.error('Failed to delete the file');
    }
});
