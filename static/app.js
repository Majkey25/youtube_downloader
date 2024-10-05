let isDownloading = false;
let loadingInterval;

// Event listener for the download form submission
document.getElementById('downloadForm').addEventListener('submit', async function(event) {
    event.preventDefault();

    const loadingText = document.getElementById('loadingText');
    const downloadLink = document.getElementById('downloadLink');
    const downloadAnchor = document.getElementById('downloadAnchor');
    const errorText = document.getElementById('error');  // For error messages

    // Reset and show "Loading" text
    loadingText.style.display = 'block'; 
    loadingText.innerText = 'Loading';  // Set initial text
    downloadLink.style.display = 'none';  // Hide download button until the file is ready
    errorText.innerText = '';  // Clear previous errors

    // Start loading animation
    let dotCount = 0;
    loadingInterval = setInterval(() => {
        dotCount = (dotCount + 1) % 4;  // Keep between 0 and 3
        loadingText.innerText = 'Loading' + '.'.repeat(dotCount);  // Add dots
    }, 500);

    const formData = new FormData(this);

    try {
        // Send request to download file
        const response = await fetch('/download', {
            method: 'POST',
            body: formData
        });

        clearInterval(loadingInterval);  // Stop animation

        if (response.ok) {
            const responseData = await response.json();
            isDownloading = true;

            // Once the download is complete, show the download button
            loadingText.innerText = 'Download complete!';  // Change text
            downloadLink.style.display = 'block';  // Show button
            downloadAnchor.href = `/downloads/${responseData.output_file}`;  // Set correct file path
            downloadAnchor.download = responseData.output_file;  // Set download file name
        } else {
            const errorData = await response.json();
            errorText.innerText = errorData.error || 'An error occurred during download.';  // Show error
            loadingText.style.display = 'none';  // Hide "Loading" text
        }
    } catch (error) {
        clearInterval(loadingInterval);  // Ensure the loading animation is stopped in case of an error
        errorText.innerText = 'An error occurred: ' + error.message;  // Show error
        loadingText.style.display = 'none';  // Hide "Loading" text
    }
});

// Clean up on page unload
window.addEventListener('beforeunload', async () => {
    if (isDownloading) {
        const response = await fetch('/delete', {
            method: 'POST',
        });
        if (!response.ok) {
            console.error('Failed to delete the file');
        }
    }
});
