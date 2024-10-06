let isDownloading = false;
let loadingInterval;

document.getElementById('downloadForm').addEventListener('submit', async function(event) {
    event.preventDefault();

    const loadingText = document.getElementById('loadingText');
    const downloadLink = document.getElementById('downloadLink');
    const downloadAnchor = document.getElementById('downloadAnchor');
    const errorText = document.getElementById('error');  // Pro chybové zprávy

    // Reset a zobrazit text "Loading"
    loadingText.style.display = 'block'; 
    loadingText.innerText = 'Loading';  // Nastavíme počáteční text
    downloadLink.style.display = 'none';  // Skrytí tlačítka download, než je soubor připraven
    errorText.innerText = '';  // Vyčistit předchozí chyby

    // Spustíme animaci teček
    let dotCount = 0;
    loadingInterval = setInterval(() => {
        dotCount = (dotCount + 1) % 4;  // Udržujeme se mezi 0 a 3
        loadingText.innerText = 'Loading' + '.'.repeat(dotCount);  // Přidáváme tečky
    }, 500);

    const formData = new FormData(this);

    try {
        // Pošleme požadavek na stahování souboru
        const response = await fetch('/download', {
            method: 'POST',
            body: formData
        });

        clearInterval(loadingInterval);  // Zastavíme animaci

        if (response.ok) {
            const responseData = await response.json();
            isDownloading = true;

            // Jakmile je stahování dokončeno, zobrazí se tlačítko pro stažení souboru
            loadingText.innerText = 'Download complete!';  // Změníme text
            downloadLink.style.display = 'block';  // Zobrazíme tlačítko
            downloadAnchor.href = `/downloads/${responseData.output_file}`;  // Nastavíme správnou cestu k souboru
            downloadAnchor.download = responseData.output_file;  // Nastavíme název souboru ke stažení
        } else {
            const errorData = await response.json();
            errorText.innerText = errorData.error || 'An error occurred during download.';  // Zobrazit chybu
            loadingText.style.display = 'none';  // Skryjeme text "Loading"
        }
    } catch (error) {
        errorText.innerText = 'An error occurred: ' + error.message;  // Zobrazit chybu
        loadingText.style.display = 'none';  // Skryjeme text "Loading"
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