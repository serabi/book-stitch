/**
 * Cover Picker Modal
 * Allows users to search Hardcover for cover images or paste a custom URL.
 */

var coverPickerState = {
    absId: null,
    currentTitle: '',
    currentQuery: '',
    currentRequestId: 0
};

async function readJsonResponse(resp) {
    try {
        return await resp.json();
    } catch (err) {
        return null;
    }
}

function toCssUrl(url) {
    return 'url(' + JSON.stringify(String(url)) + ')';
}

function openCoverPicker(absId, currentTitle) {
    coverPickerState.absId = absId;
    coverPickerState.currentTitle = currentTitle || '';

    var modal = document.getElementById('cover-picker-modal');
    modal.style.display = '';

    // Show search tab by default
    switchCoverTab('search');

    var searchInput = document.getElementById('cp-search-input');
    searchInput.value = currentTitle;

    // Auto-search if we have a title
    if (currentTitle) {
        searchCovers(currentTitle);
    }
}

function closeCoverPicker() {
    document.getElementById('cover-picker-modal').style.display = 'none';
    coverPickerState.absId = null;
    coverPickerState.currentQuery = '';
    coverPickerState.currentRequestId += 1;
    document.getElementById('cp-results').replaceChildren();
    document.getElementById('cp-search-input').value = '';
    document.getElementById('cp-custom-url').value = '';
    document.getElementById('cp-status').textContent = '';
}

function switchCoverTab(tab) {
    var searchTab = document.getElementById('cp-tab-search');
    var customTab = document.getElementById('cp-tab-custom');
    var searchPane = document.getElementById('cp-search-pane');
    var customPane = document.getElementById('cp-custom-pane');

    if (tab === 'search') {
        searchTab.classList.add('active');
        customTab.classList.remove('active');
        searchPane.style.display = 'block';
        customPane.style.display = 'none';
    } else {
        customTab.classList.add('active');
        searchTab.classList.remove('active');
        customPane.style.display = 'block';
        searchPane.style.display = 'none';
    }
}

async function searchCovers(query) {
    if (!query) return;

    var resultsEl = document.getElementById('cp-results');
    var statusEl = document.getElementById('cp-status');

    resultsEl.replaceChildren();
    statusEl.textContent = 'Searching...';

    coverPickerState.currentQuery = query;
    var requestId = ++coverPickerState.currentRequestId;
    var requestAbsId = coverPickerState.absId;

    try {
        var resp = await fetch('/api/hardcover/cover-search?query=' + encodeURIComponent(query));
        var data = await readJsonResponse(resp);

        if (requestId !== coverPickerState.currentRequestId
            || requestAbsId !== coverPickerState.absId
            || query !== coverPickerState.currentQuery) {
            return;
        }

        if (!resp.ok) {
            statusEl.textContent = (data && data.error) || 'Search failed. Please try again.';
            return;
        }

        if (data && data.error) {
            statusEl.textContent = data.error;
            return;
        }

        var results = (data && data.results) || [];
        if (results.length === 0) {
            statusEl.textContent = 'No results found.';
            return;
        }

        statusEl.textContent = '';
        results.forEach(function(book) {
            var card = document.createElement('div');
            card.className = 'cp-result-card';
            card.tabIndex = 0;
            card.setAttribute('role', 'button');
            card.setAttribute('aria-label', 'Select Hardcover cover for ' + (book.title || 'this book'));
            card.onclick = function() { selectHardcoverCover(book); };
            card.addEventListener('keydown', function(event) {
                if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
                    event.preventDefault();
                    selectHardcoverCover(book);
                }
            });

            var imgDiv = document.createElement('div');
            imgDiv.className = 'cp-result-img';
            if (book.cached_image) {
                var img = document.createElement('img');
                img.src = book.cached_image;
                img.alt = book.title;
                img.onerror = function() { this.style.display = 'none'; };
                imgDiv.appendChild(img);
            } else {
                imgDiv.classList.add('cp-no-image');
                imgDiv.textContent = 'No cover';
            }

            var infoDiv = document.createElement('div');
            infoDiv.className = 'cp-result-info';

            var titleEl = document.createElement('div');
            titleEl.className = 'cp-result-title';
            titleEl.textContent = book.title;

            var authorEl = document.createElement('div');
            authorEl.className = 'cp-result-author';
            authorEl.textContent = book.author || '';

            infoDiv.appendChild(titleEl);
            infoDiv.appendChild(authorEl);

            card.appendChild(imgDiv);
            card.appendChild(infoDiv);
            resultsEl.appendChild(card);
        });
    } catch (err) {
        if (requestId !== coverPickerState.currentRequestId || requestAbsId !== coverPickerState.absId) {
            return;
        }
        statusEl.textContent = 'Search failed. Please try again.';
    }
}

function handleCoverSearch(event) {
    if (event) event.preventDefault();
    var query = document.getElementById('cp-search-input').value.trim();
    if (query) searchCovers(query);
}

async function selectHardcoverCover(book) {
    if (!book.cached_image) {
        document.getElementById('cp-status').textContent = 'This book has no cover image.';
        return;
    }

    var requestAbsId = coverPickerState.absId;

    try {
        var resp = await fetch('/api/book/' + encodeURIComponent(coverPickerState.absId) + '/cover', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source: 'hardcover',
                book_id: book.book_id,
                cached_image: book.cached_image,
                slug: book.slug
            })
        });

        var data = await readJsonResponse(resp);
        if (requestAbsId !== coverPickerState.absId) {
            return;
        }
        if (!resp.ok) {
            document.getElementById('cp-status').textContent = (data && data.error) || 'Failed to set cover.';
            return;
        }

        if (data.success) {
            updatePageCover(data.cover_url);
            closeCoverPicker();
        } else {
            document.getElementById('cp-status').textContent = data.error || 'Failed to set cover.';
        }
    } catch (err) {
        document.getElementById('cp-status').textContent = 'Failed to set cover.';
    }
}

async function submitCustomCoverUrl() {
    var url = document.getElementById('cp-custom-url').value.trim();
    if (!url) return;

    var requestAbsId = coverPickerState.absId;

    try {
        var resp = await fetch('/api/book/' + encodeURIComponent(coverPickerState.absId) + '/cover', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source: 'custom', url: url })
        });

        var data = await readJsonResponse(resp);
        if (requestAbsId !== coverPickerState.absId) {
            return;
        }
        if (!resp.ok) {
            document.getElementById('cp-status').textContent = (data && data.error) || 'Failed to set cover.';
            return;
        }

        if (data.success) {
            updatePageCover(data.cover_url);
            closeCoverPicker();
        } else {
            document.getElementById('cp-status').textContent = data.error || 'Failed to set cover.';
        }
    } catch (err) {
        document.getElementById('cp-status').textContent = 'Failed to set cover.';
    }
}

async function removeCover() {
    var requestAbsId = coverPickerState.absId;

    try {
        var resp = await fetch('/api/book/' + encodeURIComponent(coverPickerState.absId) + '/cover', {
            method: 'DELETE'
        });

        var data = await readJsonResponse(resp);
        if (requestAbsId !== coverPickerState.absId) {
            return;
        }
        if (!resp.ok) {
            document.getElementById('cp-status').textContent = (data && data.error) || 'Failed to remove cover.';
            return;
        }

        if (data.success) {
            // Remove cover from page
            var heroImg = document.querySelector('.r-hero-cover img');
            if (heroImg) {
                heroImg.style.display = 'none';
                var placeholder = heroImg.nextElementSibling;
                if (placeholder) placeholder.classList.remove('hidden');
            }
            var heroBg = document.querySelector('.r-hero-bg');
            if (heroBg) heroBg.style.backgroundImage = 'none';
            closeCoverPicker();
        }
    } catch (err) {
        document.getElementById('cp-status').textContent = 'Failed to remove cover.';
    }
}

function updatePageCover(coverUrl) {
    // Update the hero cover image
    var heroImg = document.querySelector('.r-hero-cover img');
    var placeholder = document.querySelector('.r-hero-cover .r-cover-placeholder');

    if (heroImg) {
        heroImg.src = coverUrl;
        heroImg.style.display = '';
        if (placeholder) placeholder.classList.add('hidden');
    } else if (placeholder) {
        var img = document.createElement('img');
        img.src = coverUrl;
        img.alt = '';
        placeholder.parentNode.insertBefore(img, placeholder);
        placeholder.classList.add('hidden');
    }

    // Update background
    var heroBg = document.querySelector('.r-hero-bg');
    if (heroBg) {
        heroBg.style.backgroundImage = toCssUrl(coverUrl);
    } else {
        var hero = document.querySelector('.r-detail-hero');
        if (hero) {
            var bg = document.createElement('div');
            bg.className = 'r-hero-bg';
            bg.style.backgroundImage = toCssUrl(coverUrl);
            hero.insertBefore(bg, hero.firstChild);
        }
    }
}
