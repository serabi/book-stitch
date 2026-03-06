/**
 * Storyteller Legacy Link Fix Modal
 * Handles searching and linking Storyteller books to existing ABS books.
 */

let currentAbsId = null;

function openStorytellerModal(absId, title) {
    if (typeof closeActionPanel === 'function') closeActionPanel();
    currentAbsId = absId;
    document.getElementById('st-modal-title').textContent = `Link Storyteller: ${title}`;
    document.getElementById('st-modal').classList.remove('hidden');
    document.getElementById('st-search-input').value = title; // Pre-fill with title
    document.getElementById('st-search-input').focus();
    document.getElementById('st-results').innerHTML = ''; // Clear results

    // Auto-search if title is present
    if (title) searchStoryteller();
}

function closeStorytellerModal() {
    document.getElementById('st-modal').classList.add('hidden');
    currentAbsId = null;
}

async function searchStoryteller() {
    const query = document.getElementById('st-search-input').value;
    if (!query) return;

    const resultsDiv = document.getElementById('st-results');
    resultsDiv.textContent = '';
    const searchingDiv = document.createElement('div');
    searchingDiv.className = 'st-loading';
    searchingDiv.textContent = 'Searching...';
    resultsDiv.appendChild(searchingDiv);

    try {
        const response = await fetch(`/api/storyteller/search?q=${encodeURIComponent(query)}`);
        const books = await response.json();

        resultsDiv.innerHTML = '';

        // [NEW] Always show "None" option to allow unlinking
        const noneCard = document.createElement('div');
        noneCard.className = 'st-result-card st-none-option';
        noneCard.style.border = '1px dashed #666';
        noneCard.innerHTML = `
            <div class="st-card-info">
                <div class="st-card-title">None - Do not link</div>
                <div class="st-card-author" style="font-style: italic; color: #888;">Unlink current Storyteller book</div>
            </div>
            <button class="action-btn secondary" onclick="linkStoryteller('none')">Unlink</button>
        `;
        resultsDiv.appendChild(noneCard);

        if (books.length === 0) {
            const noRes = document.createElement('div');
            noRes.className = 'st-no-results';
            noRes.textContent = 'No matching books found via search.';
            resultsDiv.appendChild(noRes);
            return;
        }

        books.forEach(book => {
            const card = document.createElement('div');
            card.className = 'st-result-card';

            const info = document.createElement('div');
            info.className = 'st-card-info';
            const titleDiv = document.createElement('div');
            titleDiv.className = 'st-card-title';
            titleDiv.textContent = book.title;
            const authorDiv = document.createElement('div');
            authorDiv.className = 'st-card-author';
            authorDiv.textContent = (book.authors || []).join(', ');
            info.appendChild(titleDiv);
            info.appendChild(authorDiv);

            const btn = document.createElement('button');
            btn.className = 'action-btn success';
            btn.textContent = 'Link';
            btn.addEventListener('click', () => linkStoryteller(book.uuid));

            card.appendChild(info);
            card.appendChild(btn);
            resultsDiv.appendChild(card);
        });

    } catch (e) {
        resultsDiv.textContent = '';
        const errorDiv = document.createElement('div');
        errorDiv.className = 'st-error';
        errorDiv.textContent = `Error: ${e.message}`;
        resultsDiv.appendChild(errorDiv);
    }
}

async function linkStoryteller(uuid) {
    if (!currentAbsId) return;

    const resultsDiv = document.getElementById('st-results');
    resultsDiv.textContent = '';
    const loadingDiv = document.createElement('div');
    loadingDiv.className = 'st-loading';
    loadingDiv.textContent = 'Linking and downloading...';
    resultsDiv.appendChild(loadingDiv);

    try {
        const response = await fetch(`/api/storyteller/link/${currentAbsId}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ uuid: uuid })
        });

        if (response.ok) {
            window.location.reload();
        } else {
            const err = await response.json();
            throw new Error(err.error || 'Failed to link');
        }
    } catch (e) {
        resultsDiv.textContent = '';
        const errDiv = document.createElement('div');
        errDiv.className = 'st-error';
        errDiv.textContent = 'Link Failed: ' + e.message;
        resultsDiv.appendChild(errDiv);
    }
}

// Event Listeners
document.addEventListener('DOMContentLoaded', () => {
    // Close on click outside
    document.getElementById('st-modal').addEventListener('click', (e) => {
        if (e.target === document.getElementById('st-modal')) {
            closeStorytellerModal();
        }
    });

    // Enter key in search
    document.getElementById('st-search-input').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
            searchStoryteller();
        }
    });
});
