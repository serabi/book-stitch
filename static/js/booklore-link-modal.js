/**
 * Booklore Link Modal
 * Allows linking a PageKeeper book to a Booklore book by searching and selecting.
 */

var bookloreModalState = { absId: null };

function _blEl(tag, className, text) {
    var e = document.createElement(tag);
    if (className) e.className = className;
    if (text) e.textContent = text;
    return e;
}

function openBookloreModal(absId, title) {
    if (typeof closeActionPanel === 'function') closeActionPanel();
    bookloreModalState.absId = absId;
    document.getElementById('bl-modal-title').textContent = 'Link to Booklore: ' + title;
    document.getElementById('bl-modal').style.display = 'flex';
    document.getElementById('bl-search-input').value = title;
    var resultsDiv = document.getElementById('bl-results');
    while (resultsDiv.firstChild) resultsDiv.removeChild(resultsDiv.firstChild);
    document.getElementById('bl-search-input').focus();
    if (title) searchBooklore();
}

function closeBookloreModal() {
    document.getElementById('bl-modal').style.display = 'none';
    bookloreModalState.absId = null;
}

function searchBooklore() {
    var query = document.getElementById('bl-search-input').value.trim();
    if (!query) return;

    var resultsDiv = document.getElementById('bl-results');
    while (resultsDiv.firstChild) resultsDiv.removeChild(resultsDiv.firstChild);
    resultsDiv.appendChild(_blEl('div', 'st-loading', 'Searching Booklore...'));

    fetch('/api/booklore/search?q=' + encodeURIComponent(query))
        .then(function(r) {
            if (!r.ok) throw new Error('Search failed: ' + r.status);
            return r.json();
        })
        .then(function(books) {
            while (resultsDiv.firstChild) resultsDiv.removeChild(resultsDiv.firstChild);

            // Unlink option
            var noneCard = _blEl('div', 'st-result-card st-none-option');
            noneCard.style.border = '1px dashed #666';
            var noneInfo = _blEl('div', 'st-card-info');
            noneInfo.appendChild(_blEl('div', 'st-card-title', 'None - Do not link'));
            var noneDesc = _blEl('div', 'st-card-author', 'Unlink current Booklore book');
            noneDesc.style.fontStyle = 'italic';
            noneDesc.style.color = '#888';
            noneInfo.appendChild(noneDesc);
            noneCard.appendChild(noneInfo);
            var unlinkBtn = _blEl('button', 'action-btn secondary', 'Unlink');
            unlinkBtn.addEventListener('click', function() { linkBooklore(''); });
            noneCard.appendChild(unlinkBtn);
            resultsDiv.appendChild(noneCard);

            if (!books.length) {
                resultsDiv.appendChild(_blEl('div', 'st-no-results', 'No matching books found.'));
                return;
            }

            books.forEach(function(book) {
                var card = _blEl('div', 'st-result-card');
                var info = _blEl('div', 'st-card-info');
                info.appendChild(_blEl('div', 'st-card-title', book.title || book.fileName));
                info.appendChild(_blEl('div', 'st-card-author',
                    (book.authors || '') + (book.source ? ' \u00B7 ' + book.source : '')));
                if (book.fileName) {
                    var fileEl = _blEl('div', 'st-card-author', book.fileName);
                    fileEl.style.fontSize = '0.75rem';
                    fileEl.style.opacity = '0.6';
                    info.appendChild(fileEl);
                }
                card.appendChild(info);

                var btn = _blEl('button', 'action-btn success', 'Link');
                btn.addEventListener('click', function() {
                    linkBooklore(book.fileName, book.source_tag);
                });
                card.appendChild(btn);
                resultsDiv.appendChild(card);
            });
        })
        .catch(function(e) {
            while (resultsDiv.firstChild) resultsDiv.removeChild(resultsDiv.firstChild);
            resultsDiv.appendChild(_blEl('div', 'st-error', 'Error: ' + e.message));
        });
}

function linkBooklore(filename, sourceTag) {
    if (!bookloreModalState.absId) return;

    var resultsDiv = document.getElementById('bl-results');
    while (resultsDiv.firstChild) resultsDiv.removeChild(resultsDiv.firstChild);
    resultsDiv.appendChild(_blEl('div', 'st-loading', 'Linking...'));

    fetch('/api/booklore/link/' + encodeURIComponent(bookloreModalState.absId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: filename, source_tag: sourceTag || '' })
    })
    .then(function(r) {
        if (r.ok) {
            window.location.reload();
            return;
        }
        var contentType = r.headers.get('content-type') || '';
        if (contentType.indexOf('application/json') !== -1) {
            return r.json().then(function(err) { throw new Error(err.error || 'Failed to link'); });
        }
        throw new Error('Failed to link (HTTP ' + r.status + ')');
    })
    .catch(function(e) {
        while (resultsDiv.firstChild) resultsDiv.removeChild(resultsDiv.firstChild);
        resultsDiv.appendChild(_blEl('div', 'st-error', 'Link Failed: ' + e.message));
    });
}

document.addEventListener('DOMContentLoaded', function() {
    document.getElementById('bl-modal').addEventListener('click', function(e) {
        if (e.target === document.getElementById('bl-modal')) {
            closeBookloreModal();
        }
    });
    document.getElementById('bl-search-input').addEventListener('keypress', function(e) {
        if (e.key === 'Enter') searchBooklore();
    });
});
