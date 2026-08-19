/* PageKeeper — Kobo Device Book Management */
(function () {
    'use strict';

    var data = window.PK_PAGE_DATA;
    var books = data.books || [];

    var READ_STATUS = { 0: 'unread', 1: 'reading', 2: 'finished' };

    // ── Toast ──

    function showToast(message) {
        var existing = document.querySelector('.r-tbr-toast');
        if (existing) existing.remove();
        var toast = document.createElement('div');
        toast.className = 'r-tbr-toast';
        toast.textContent = message;
        document.body.appendChild(toast);
        setTimeout(function () {
            toast.style.transition = 'opacity 0.3s';
            toast.style.opacity = '0';
            setTimeout(function () { toast.remove(); }, 300);
        }, 3000);
    }

    // ── Helpers ──

    function timeAgo(isoStr) {
        if (!isoStr) return 'never';
        var diff = Date.now() - new Date(isoStr).getTime();
        var mins = Math.floor(diff / 60000);
        if (mins < 1) return 'just now';
        if (mins < 60) return mins + 'm ago';
        var hours = Math.floor(mins / 60);
        if (hours < 24) return hours + 'h ago';
        var days = Math.floor(hours / 24);
        if (days < 30) return days + 'd ago';
        var months = Math.floor(days / 30);
        return months + 'mo ago';
    }

    function clearEl(el) {
        while (el.firstChild) el.removeChild(el.firstChild);
    }

    function makeEmpty(msg) {
        var el = document.createElement('div');
        el.className = 'kobo-empty';
        el.textContent = msg;
        return el;
    }

    function makeButton(cls, label, onClick, title) {
        var btn = document.createElement('button');
        btn.className = cls;
        btn.textContent = label;
        btn.type = 'button';
        if (title) btn.title = title;
        btn.addEventListener('click', onClick);
        return btn;
    }

    // ── Categorize ──

    function categorize() {
        var matching = [];
        var matched = [];
        var hidden = [];
        books.forEach(function (b) {
            if (b.hidden) {
                hidden.push(b);
            } else if (!b.matched_book_id) {
                matching.push(b);
            } else {
                matched.push(b);
            }
        });
        // Most recently read first in the action queue
        matching.sort(function (a, b) {
            return (b.date_last_read || '').localeCompare(a.date_last_read || '');
        });
        var byTitle = function (a, b) { return (a.title || '').localeCompare(b.title || ''); };
        matched.sort(byTitle);
        hidden.sort(byTitle);
        return { matching: matching, matched: matched, hidden: hidden };
    }

    // ── Stats ──

    function renderStats(cats) {
        var statsEl = document.getElementById('kobo-stats');
        clearEl(statsEl);

        var items = [
            { label: 'need matching', count: cats.matching.length, cls: cats.matching.length > 0 ? 'kobo-stat--alert' : '' },
            { label: 'matched', count: cats.matched.length, cls: '' },
            { label: 'hidden', count: cats.hidden.length, cls: '' },
            { label: 'total device books', count: books.length, cls: '' }
        ];

        items.forEach(function (item) {
            var pill = document.createElement('div');
            pill.className = 'kobo-stat' + (item.cls ? ' ' + item.cls : '');
            var strong = document.createElement('strong');
            strong.textContent = item.count;
            pill.appendChild(strong);
            pill.appendChild(document.createTextNode(' ' + item.label));
            statsEl.appendChild(pill);
        });
    }

    // ── Cards ──

    function buildMeta(b) {
        var meta = document.createElement('div');
        meta.className = 'kobo-card-meta';

        if (b.author) {
            var author = document.createElement('span');
            author.textContent = b.author;
            meta.appendChild(author);
        }

        var status = document.createElement('span');
        status.textContent = READ_STATUS[b.read_status] || 'unread';
        meta.appendChild(status);

        if (b.read_status > 0) {
            var pct = document.createElement('span');
            pct.textContent = b.read_status === 2 ? '100%' : (b.percent || 0) + '%';
            pct.className = 'kobo-meta--highlight';
            meta.appendChild(pct);
        }

        if (b.date_last_read) {
            var last = document.createElement('span');
            last.textContent = 'last read ' + timeAgo(b.date_last_read);
            meta.appendChild(last);
        }

        if (b.bookmark_count) {
            var bm = document.createElement('span');
            bm.textContent = b.bookmark_count + (b.bookmark_count === 1 ? ' highlight' : ' highlights');
            meta.appendChild(bm);
        }

        return meta;
    }

    function buildMatchingCard(b) {
        var card = document.createElement('div');
        card.className = 'kobo-card kobo-card--attention';

        var info = document.createElement('div');
        info.className = 'kobo-card-info';

        var tag = document.createElement('span');
        tag.className = 'kobo-tag kobo-tag--unmatched';
        tag.textContent = 'Unmatched';
        info.appendChild(tag);

        var title = document.createElement('div');
        title.className = 'kobo-card-title';
        title.textContent = b.title || '(untitled)';
        info.appendChild(title);
        info.appendChild(buildMeta(b));
        card.appendChild(info);

        var actions = document.createElement('div');
        actions.className = 'kobo-card-actions';
        actions.appendChild(makeButton('btn btn-primary', 'Link to Book', function () {
            toggleSearchPanel(card, b);
        }, 'Search your library and link this device book'));
        actions.appendChild(makeButton('btn btn-secondary', 'Hide', function () {
            PKModal.confirm({
                title: 'Hide Device Book',
                message: 'Hide "' + (b.title || 'this book') + '"? It will not appear in matching or suggestions. You can unhide it later.',
                confirmLabel: 'Hide',
                confirmClass: 'btn btn-warning',
                onConfirm: function () { setHidden(b.content_id, true); }
            });
        }));
        card.appendChild(actions);
        return card;
    }

    function buildMatchedCard(b) {
        var card = document.createElement('div');
        card.className = 'kobo-card';

        var info = document.createElement('div');
        info.className = 'kobo-card-info';

        var title = document.createElement('div');
        title.className = 'kobo-card-title';
        title.textContent = b.title || '(untitled)';
        info.appendChild(title);

        var link = document.createElement('div');
        link.className = 'kobo-card-link';
        link.textContent = '→ ' + (b.matched_book_title || 'library book #' + b.matched_book_id);
        info.appendChild(link);

        info.appendChild(buildMeta(b));
        card.appendChild(info);

        var actions = document.createElement('div');
        actions.className = 'kobo-card-actions';
        if (b.bookmark_count > 0) {
            actions.appendChild(makeButton('btn btn-primary', 'Import Highlights', function () {
                importHighlights(b);
            }, 'Import this book\u2019s highlights and notes into its reading journal'));
        }
        actions.appendChild(makeButton('btn btn-secondary', 'Unlink', function () {
            PKModal.confirm({
                title: 'Unlink Device Book',
                message: 'Unlink "' + (b.title || 'this book') + '" from "' + (b.matched_book_title || 'the library book') + '"? Stored highlights stay on the device book.',
                confirmLabel: 'Unlink',
                confirmClass: 'btn btn-warning',
                onConfirm: function () { unlinkBook(b.content_id); }
            });
        }));
        actions.appendChild(makeButton('btn btn-secondary', 'Hide', function () {
            setHidden(b.content_id, true);
        }));
        card.appendChild(actions);
        return card;
    }

    function buildHiddenCard(b) {
        var card = document.createElement('div');
        card.className = 'kobo-card kobo-card--hidden';

        var info = document.createElement('div');
        info.className = 'kobo-card-info';

        var title = document.createElement('div');
        title.className = 'kobo-card-title';
        title.textContent = b.title || '(untitled)';
        info.appendChild(title);
        info.appendChild(buildMeta(b));
        card.appendChild(info);

        var actions = document.createElement('div');
        actions.className = 'kobo-card-actions';
        actions.appendChild(makeButton('btn btn-secondary', 'Unhide', function () {
            setHidden(b.content_id, false);
        }));
        card.appendChild(actions);
        return card;
    }

    // ── Inline library search ──

    function toggleSearchPanel(card, koboBook) {
        var existing = card.querySelector('.kobo-search-panel');
        if (existing) {
            existing.remove();
            return;
        }

        var panel = document.createElement('div');
        panel.className = 'kobo-search-panel';

        var input = document.createElement('input');
        input.type = 'text';
        input.className = 'search-box';
        input.placeholder = 'Search library by title...';
        input.autocomplete = 'off';
        if (koboBook.title) input.value = koboBook.title;
        panel.appendChild(input);

        var results = document.createElement('div');
        panel.appendChild(results);

        card.querySelector('.kobo-card-info').appendChild(panel);
        input.focus();
        input.select();

        var search = function (q) { searchBooks(q, results, koboBook.content_id); };
        if (input.value.trim().length >= 2) search(input.value.trim());

        var timer = null;
        input.addEventListener('input', function () {
            clearTimeout(timer);
            var q = input.value.trim();
            if (q.length < 2) { clearEl(results); return; }
            timer = setTimeout(function () { search(q); }, 350);
        });
    }

    function searchBooks(query, resultsEl, contentId) {
        clearEl(resultsEl);
        resultsEl.appendChild(makeEmpty('Searching...'));

        fetch('/api/reading/library-search?q=' + encodeURIComponent(query))
            .then(function (r) { return r.json(); })
            .then(function (results) {
                clearEl(resultsEl);
                if (!results.length) {
                    resultsEl.appendChild(makeEmpty('No matching books found.'));
                    return;
                }
                results.forEach(function (book) {
                    var row = document.createElement('div');
                    row.className = 'kobo-search-result';

                    var info = document.createElement('div');
                    info.className = 'kobo-search-result-info';
                    var t = document.createElement('div');
                    t.className = 'kobo-search-result-title';
                    t.textContent = book.title || book.abs_id || '(untitled)';
                    info.appendChild(t);
                    var s = document.createElement('div');
                    s.className = 'kobo-search-result-status';
                    s.textContent = (book.status || '').replace(/_/g, ' ');
                    info.appendChild(s);
                    row.appendChild(info);

                    var btn = document.createElement('button');
                    btn.className = 'btn btn-primary';
                    btn.textContent = 'Link';
                    btn.type = 'button';
                    btn.style.cssText = 'flex-shrink: 0; padding: 4px 12px; font-size: 12px;';
                    btn.addEventListener('click', function () { linkBook(contentId, book.id); });
                    row.appendChild(btn);
                    resultsEl.appendChild(row);
                });
            })
            .catch(function () {
                clearEl(resultsEl);
                resultsEl.appendChild(makeEmpty('Search failed.'));
            });
    }

    // ── Toggle sections ──

    function setupToggle(btnId, listId) {
        var btn = document.getElementById(btnId);
        var list = document.getElementById(listId);
        if (btn && list) {
            btn.addEventListener('click', function () {
                var visible = list.style.display !== 'none';
                list.style.display = visible ? 'none' : 'block';
                btn.textContent = visible ? 'show' : 'hide';
            });
        }
    }

    setupToggle('toggle-matched', 'matched-list');
    setupToggle('toggle-hidden', 'hidden-list');

    // ── API actions ──

    function post(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {})
        }).then(function (r) { return r.json(); });
    }

    function linkBook(contentId, bookId) {
        post('/api/kobo/link', { content_id: contentId, book_id: bookId })
            .then(function (d) { showToast(d.success ? 'Linked' : (d.error || 'Failed')); if (d.success) refreshAll(); })
            .catch(function () { showToast('Link failed'); });
    }

    function unlinkBook(contentId) {
        post('/api/kobo/unlink', { content_id: contentId })
            .then(function (d) { showToast(d.success ? 'Unlinked' : (d.error || 'Failed')); if (d.success) refreshAll(); })
            .catch(function () { showToast('Unlink failed'); });
    }

    function setHidden(contentId, hidden) {
        post('/api/kobo/hide', { content_id: contentId, hidden: hidden })
            .then(function (d) { showToast(d.success ? (hidden ? 'Hidden' : 'Unhidden') : (d.error || 'Failed')); if (d.success) refreshAll(); })
            .catch(function () { showToast('Update failed'); });
    }

    function importHighlights(b) {
        post('/api/kobo/save-journal', { book_id: b.matched_book_id })
            .then(function (d) {
                if (d.error) { showToast(d.error); return; }
                showToast('Imported ' + d.saved + (d.skipped ? ' (' + d.skipped + ' already there)' : ''));
            })
            .catch(function () { showToast('Import failed'); });
    }

    function syncNow() {
        var btn = document.getElementById('kobo-sync-btn');
        btn.disabled = true;
        post('/api/kobo/sync')
            .then(function (d) { showToast(d.changed ? 'New data ingested' : 'Already up to date'); refreshAll(); })
            .catch(function () { showToast('Sync failed'); })
            .finally(function () { btn.disabled = false; });
    }

    function uploadDatabase(file) {
        var formData = new FormData();
        formData.append('file', file);
        fetch('/api/kobo/upload', { method: 'POST', body: formData })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d.error) { showToast(d.error); return; }
                showToast('Database ingested — ' + d.device_books + ' device books');
                refreshAll();
            })
            .catch(function () { showToast('Upload failed'); });
    }

    // ── Wire header buttons ──

    var uploadInput = document.getElementById('kobo-upload-input');
    document.getElementById('kobo-upload-btn').addEventListener('click', function () {
        uploadInput.click();
    });
    uploadInput.addEventListener('change', function () {
        if (uploadInput.files && uploadInput.files.length) {
            uploadDatabase(uploadInput.files[0]);
            uploadInput.value = '';
        }
    });
    document.getElementById('kobo-sync-btn').addEventListener('click', syncNow);

    // ── Render ──

    function renderAll() {
        var cats = categorize();
        renderStats(cats);

        var matchingList = document.getElementById('matching-list');
        clearEl(matchingList);
        if (!cats.matching.length) {
            matchingList.appendChild(makeEmpty(books.length ? 'Everything is matched.' : 'No device books yet.'));
        } else {
            cats.matching.forEach(function (b) { matchingList.appendChild(buildMatchingCard(b)); });
        }

        var matchedList = document.getElementById('matched-list');
        clearEl(matchedList);
        if (!cats.matched.length) {
            matchedList.appendChild(makeEmpty('No matched books yet.'));
        } else {
            cats.matched.forEach(function (b) { matchedList.appendChild(buildMatchedCard(b)); });
        }

        var hiddenSection = document.getElementById('hidden-section');
        var hiddenList = document.getElementById('hidden-list');
        clearEl(hiddenList);
        if (!cats.hidden.length) {
            hiddenSection.style.display = 'none';
        } else {
            hiddenSection.style.display = '';
            cats.hidden.forEach(function (b) { hiddenList.appendChild(buildHiddenCard(b)); });
        }
    }

    function refreshAll() {
        fetch('/api/kobo/books')
            .then(function (r) { return r.json(); })
            .then(function (d) {
                books = d.books || [];
                renderAll();
            })
            .catch(function () { showToast('Failed to refresh'); });
    }

    renderAll();
})();
