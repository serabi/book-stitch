/* ═══════════════════════════════════════════
   PageKeeper — suggestions page
   ═══════════════════════════════════════════
   Depends on: utils.js, confirm-modal.js
   Reads:      window.PK_PAGE_DATA.suggestionsData
               window.PK_PAGE_DATA.selectedSourceId
   ═══════════════════════════════════════════ */

(function () {
    'use strict';

    if (window.PK_PAGE_DATA && window.PK_PAGE_DATA.pairingsInbox) {
        initPairingsInbox();
        return;
    }

    var suggestionData = window.PK_PAGE_DATA.suggestionsData;
    var selectedSourceId = window.PK_PAGE_DATA.selectedSourceId;
    var desktopMedia = window.matchMedia('(min-width: 961px)');
    var currentView = 'list';

    function createRescanPoller(handlers) {
        var timer = null;
        var failures = 0;

        function poll() {
            fetch('/api/suggestions/rescan-status')
                .then(function (response) { return response.json(); })
                .then(function (data) {
                    if (!data.success) throw new Error(data.error || 'Status failed');
                    failures = 0;
                    if (data.running) {
                        handlers.onRunning(data);
                        timer = setTimeout(poll, 1500);
                    } else if (data.phase === 'complete' || data.phase === 'partial') {
                        handlers.onComplete(data);
                    } else {
                        handlers.onIdle(data);
                    }
                })
                .catch(function (error) {
                    failures += 1;
                    if (failures <= 3) {
                        handlers.onRetry();
                        timer = setTimeout(poll, 1000 * failures);
                    } else {
                        handlers.onError(error);
                    }
                });
        }

        return {
            start: function () {
                this.cancel();
                failures = 0;
                poll();
            },
            cancel: function () {
                if (timer) {
                    clearTimeout(timer);
                    timer = null;
                }
            }
        };
    }

    function initPairingsInbox() {
        var button = document.getElementById('rescan-btn');
        var status = document.getElementById('rescan-status');
        var scanObserved = false;

        function updatePairingBadges(resolvedCount) {
            document.querySelectorAll('.nav-badge').forEach(function (badge) {
                var count = Math.max(0, Number(badge.textContent) - resolvedCount);
                if (count) badge.textContent = count;
                else badge.remove();
            });
        }

        function showCaughtUpState() {
            var inbox = document.querySelector('.pairings-inbox');
            var empty = document.createElement('section');
            empty.className = 'pairings-empty';
            empty.setAttribute('aria-labelledby', 'empty-heading');
            empty.innerHTML = '<h2 id="empty-heading" tabindex="-1">All caught up</h2>' +
                '<p>Every detected Currently Reading book has been resolved or dismissed.</p>';
            inbox.appendChild(empty);
            var heading = empty.querySelector('h2');
            heading.focus();
        }

        function showIdleStatus(data) {
            button.disabled = false;
            status.textContent = data.message || '';
        }

        var poller = createRescanPoller({
            onRunning: function (data) {
                scanObserved = true;
                button.disabled = true;
                status.textContent = data.message || 'Rescan in progress...';
            },
            onComplete: function (data) {
                if (scanObserved) {
                    scanObserved = false;
                    window.location.reload();
                } else {
                    showIdleStatus(data);
                }
            },
            onIdle: showIdleStatus,
            onRetry: function () {
                button.disabled = true;
                status.textContent = 'Connection interrupted. Retrying rescan status...';
            },
            onError: function (error) {
                button.disabled = false;
                status.textContent = 'Could not confirm rescan status: ' + (error.message || String(error));
            }
        });

        button.addEventListener('click', function () {
            poller.cancel();
            button.disabled = true;
            status.textContent = 'Queued source rescan...';
            fetch('/api/suggestions/rescan', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            })
                .then(function (response) { return response.json(); })
                .then(function (data) {
                    if (!data.success) throw new Error(data.error || 'Rescan failed');
                    status.textContent = data.message || 'Rescan started...';
                    if (data.rate_limited) {
                        button.disabled = false;
                    } else {
                        scanObserved = true;
                        poller.start();
                    }
                })
                .catch(function (error) {
                    button.disabled = false;
                    status.textContent = error.message || String(error);
                });
        });

        function dismissCard(dismissButton, card, title) {
            var identities = Array.prototype.map.call(
                card.querySelectorAll('.pairing-activity'),
                function (activity) { return activity.dataset; }
            );
            var cards = Array.prototype.slice.call(document.querySelectorAll('.pairing-card'));
            var cardIndex = cards.indexOf(card);
            var recoveryCard = cards[cardIndex + 1] || cards[cardIndex - 1];
            dismissButton.disabled = true;
            status.textContent = 'Dismissing ' + title + '...';
            fetch('/api/detected/dismiss-group', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    identities: identities.map(function (identity) {
                        return { source: identity.source, source_id: identity.sourceId };
                    })
                })
            }).then(function (response) {
                return response.json().then(function (data) {
                    if (!response.ok || !data.success) throw new Error(data.error || 'Dismiss failed');
                    return data;
                });
            })
                .then(function () {
                    var list = card.closest('.pairing-list');
                    card.remove();
                    updatePairingBadges(identities.length);
                    status.textContent = 'Dismissed ' + title + '.';
                    if (recoveryCard) {
                        var recoveryAction = recoveryCard.querySelector('a, button');
                        if (recoveryAction) recoveryAction.focus();
                    } else if (!list.querySelector('.pairing-card')) {
                        showCaughtUpState();
                    }
                })
                .catch(function (error) {
                    dismissButton.disabled = false;
                    status.textContent = 'Could not dismiss ' + title + ': ' + (error.message || String(error));
                });
        }

        document.querySelectorAll('.pairing-dismiss').forEach(function (dismissButton) {
            dismissButton.addEventListener('click', function () {
                var card = dismissButton.closest('.pairing-card');
                var title = card.querySelector('h2').textContent;
                PKModal.confirm({
                    title: 'Dismiss Book?',
                    message: '"' + title + '" will be removed from Currently Reading until new reading activity is detected.',
                    confirmLabel: 'Dismiss',
                    confirmClass: 'btn',
                    onConfirm: function () {
                        dismissCard(dismissButton, card, title);
                    }
                });
            });
        });

        poller.start();
    }

    /* ── helpers ── */

    function formatEvidence(evidence) {
        return (evidence || []).map(function (item) {
            return '<span class="chip' + (item.indexOf('bookfusion') === 0 ? ' chip--bookfusion' : '') + '">' + escapeHtml(item.split('_').join(' ')) + '</span>';
        }).join('');
    }

    function confidenceRank(confidence) {
        if (confidence === 'high') return 3;
        if (confidence === 'medium') return 2;
        return 1;
    }

    function filterSuggestions() {
        var query = (document.getElementById('suggestion-search').value || '').toLowerCase().trim();
        var confidenceFilter = document.getElementById('confidence-filter').value;
        var bfFilterEl = document.getElementById('bookfusion-filter');
        var bookfusionFilter = bfFilterEl ? bfFilterEl.value : 'all';

        return suggestionData.filter(function (suggestion) {
            if (selectedSourceId && suggestion.source_id !== selectedSourceId) return false;
            if (bookfusionFilter === 'bookfusion' && !suggestion.has_bookfusion_evidence) return false;

            var topConfidence = suggestion.top_match ? suggestion.top_match.confidence : 'low';
            if (confidenceFilter === 'high' && topConfidence !== 'high') return false;
            if (confidenceFilter === 'medium' && confidenceRank(topConfidence) < 2) return false;

            if (!query) return true;
            var haystack = [
                suggestion.title,
                suggestion.author
            ].concat((suggestion.matches || []).map(function (match) {
                return [match.title, match.author, match.filename, match.source_family, (match.evidence || []).join(' ')].join(' ');
            })).join(' ').toLowerCase();
            return haystack.indexOf(query) !== -1;
        });
    }

    /* ── rendering ──
       Note: all user-facing strings are passed through escapeHtml() (from utils.js)
       before insertion into HTML markup strings. */

    function renderCandidate(match, suggestion, index) {
        var confidenceClass = 'chip--confidence-' + (match.confidence || 'low');
        var actions = [];
        var sgSource = suggestion.source || 'unknown';

        if (!suggestion.hidden) {
            if (match.source_family === 'bookfusion') {
                actions.push('<button type="button" class="btn btn-sm" onclick=\'PK_Suggestions.linkBookFusion(' + JSON.stringify(suggestion.source_id) + ', ' + index + ', ' + JSON.stringify(sgSource) + ')\'>Link BookFusion</button>');
                if (match.bookfusion_ids && match.bookfusion_ids.length) {
                    actions.push('<button type="button" class="btn btn-sm btn-purple" onclick=\'PK_Suggestions.addBookFusionToDashboard(' + JSON.stringify(match.bookfusion_ids) + ')\'>Add BF Book</button>');
                }
            } else if (match.action_kind === 'create_ebook_mapping') {
                var ebookMappingUrl = '/match?search=' + encodeURIComponent(match.title || suggestion.title || '');
                actions.push('<a class="btn btn-sm" href="' + ebookMappingUrl + '">Create Ebook Mapping</a>');
            } else {
                var mappingUrl = '/match?search=' + encodeURIComponent(suggestion.title || '');
                if (sgSource === 'abs') {
                    mappingUrl = '/match?abs_id=' + encodeURIComponent(suggestion.source_id) + '&search=' + encodeURIComponent(suggestion.title || '');
                } else if (match.abs_id) {
                    mappingUrl = '/match?abs_id=' + encodeURIComponent(match.abs_id) + '&search=' + encodeURIComponent(match.title || suggestion.title || '');
                }
                actions.push('<a class="btn btn-sm" href="' + mappingUrl + '">Create Mapping</a>');
            }
        }

        return '' +
            '<div class="candidate">' +
                '<div class="candidate-top">' +
                    '<div>' +
                        '<div class="candidate-title">' + escapeHtml(match.title || match.filename || 'Untitled') + '</div>' +
                        '<div class="candidate-author">' + escapeHtml(match.author || match.source_family || '') + '</div>' +
                    '</div>' +
                    '<div class="candidate-score">' +
                        '<span class="chip ' + confidenceClass + '">' + escapeHtml(match.confidence || 'low') + '</span>' +
                        '<span>' + Math.round((match.score || 0) * 100) + '%</span>' +
                    '</div>' +
                '</div>' +
                '<div class="badge-row">' +
                    '<span class="chip">' + escapeHtml(match.source_family || match.source || 'unknown') + '</span>' +
                    formatEvidence(match.evidence) +
                '</div>' +
                (match.highlight_count ? '<div class="help-note" style="margin-top:8px;">BookFusion highlights: ' + escapeHtml(match.highlight_count) + '</div>' : '') +
                (actions.length ? '<div class="candidate-actions">' + actions.join('') + '</div>' : '') +
            '</div>';
    }

    function renderSuggestionCard(suggestion) {
        var matches = (suggestion.matches || []).map(function (match, index) {
            return renderCandidate(match, suggestion, index);
        }).join('');

        var suggestionSource = suggestion.source || 'unknown';
        var actionButtons = suggestion.hidden
            ? '<button type="button" class="btn btn-sm" onclick=\'PK_Suggestions.unhideSuggestion(' + JSON.stringify(suggestion.source_id) + ', ' + JSON.stringify(suggestionSource) + ', this)\'>Unhide</button>'
            : '<button type="button" class="btn btn-sm" onclick=\'PK_Suggestions.hideSuggestion(' + JSON.stringify(suggestion.source_id) + ', ' + JSON.stringify(suggestionSource) + ', this)\'>Hide</button>';

        return '' +
            '<article class="suggestion-card' + (suggestion.hidden ? ' suggestion-card--hidden' : '') + '" data-has-bookfusion="' + (suggestion.has_bookfusion_evidence ? 'true' : 'false') + '">' +
                '<div class="suggestion-source">' +
                    (suggestion.cover_url
                        ? '<img src="' + escapeHtml(suggestion.cover_url) + '" alt="" class="suggestion-cover" loading="lazy">'
                        : '<div class="suggestion-cover"></div>') +
                    '<div class="suggestion-meta">' +
                        '<h3>' + escapeHtml(suggestion.title) + '</h3>' +
                        '<p>' + escapeHtml(suggestion.author || 'Unknown author') + '</p>' +
                        '<div class="badge-row">' +
                            (suggestion.source && suggestion.source !== 'abs' ? '<span class="chip chip--source-' + escapeHtml(suggestion.source) + '">' + escapeHtml(suggestion.source) + '</span>' : '') +
                            '<span class="chip">' + escapeHtml((suggestion.matches || []).length) + ' candidates</span>' +
                            (suggestion.hidden ? '<span class="chip">Hidden</span>' : '') +
                            (suggestion.has_bookfusion_evidence ? '<span class="chip chip--bookfusion">BookFusion evidence</span>' : '') +
                        '</div>' +
                    '</div>' +
                '</div>' +
                '<div class="candidate-list">' + matches + '</div>' +
                '<div class="suggestion-actions">' +
                    actionButtons +
                    '<button type="button" class="btn btn-sm btn-danger" onclick=\'PK_Suggestions.ignoreSuggestion(' + JSON.stringify(suggestion.source_id) + ', ' + JSON.stringify(suggestionSource) + ', this)\'>Never Ask</button>' +
                '</div>' +
            '</article>';
    }

    /* ── view toggle ── */

    function setView(view, persist) {
        var results = document.getElementById('suggestions-results');
        var hiddenGrid = document.getElementById('hidden-grid');
        var viewButtons = document.querySelectorAll('.sg-view-btn');
        var forcedView = desktopMedia.matches ? view : 'list';

        currentView = forcedView;

        if (results) {
            results.classList.toggle('sg-grid-view', forcedView === 'grid');
            results.classList.toggle('sg-list-view', forcedView !== 'grid');
        }

        if (hiddenGrid) {
            hiddenGrid.classList.toggle('sg-list-grid', forcedView !== 'grid');
        }

        viewButtons.forEach(function (btn) {
            var isActive = btn.dataset.view === forcedView;
            btn.classList.toggle('active', isActive);
            btn.disabled = !desktopMedia.matches;
            btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
        });

        if (persist && desktopMedia.matches) {
            try { localStorage.setItem('pk-suggestions-view', forcedView); } catch (e) {}
        }
    }

    /* ── main render ── */

    function renderSuggestions() {
        var filtered = filterSuggestions();
        var visible = filtered.filter(function (item) { return !item.hidden; });
        var hidden = filtered.filter(function (item) { return item.hidden; });
        var grid = document.getElementById('suggestion-grid');
        var hiddenSection = document.getElementById('hidden-section');
        var hiddenGrid = document.getElementById('hidden-grid');
        var hiddenCount = document.getElementById('hidden-section-count');
        var empty = document.getElementById('empty-state');

        document.getElementById('visible-count').textContent = visible.length;
        document.getElementById('hidden-count').textContent = hidden.length;
        document.getElementById('total-count').textContent = filtered.length;

        /* All values passed to renderSuggestionCard are escapeHtml-sanitized */
        grid.innerHTML = visible.map(renderSuggestionCard).join('');  // eslint-disable-line no-unsanitized/property

        if (hidden.length) {
            hiddenSection.classList.remove('hidden');
            hiddenGrid.innerHTML = hidden.map(renderSuggestionCard).join('');  // eslint-disable-line no-unsanitized/property
            hiddenCount.textContent = '(' + hidden.length + ')';
        } else {
            hiddenSection.classList.add('hidden');
            hiddenGrid.textContent = '';
            hiddenCount.textContent = '(0)';
        }

        if (!visible.length) {
            empty.classList.remove('hidden');
        } else {
            empty.classList.add('hidden');
        }
    }

    /* ── state management ── */

    function updateSuggestionState(sourceId, updater) {
        suggestionData = suggestionData.map(function (item) {
            if (item.source_id !== sourceId) return item;
            return updater(Object.assign({}, item));
        }).filter(Boolean);
        renderSuggestions();
    }

    function showErrorToast(message) {
        PKModal.alert({ title: 'Error', message: message });
    }

    function actOnSuggestion(url, btn, onSuccess) {
        if (btn) btn.disabled = true;
        fetch(url, { method: 'POST' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) throw new Error(data.error || 'Request failed');
                onSuccess();
            })
            .catch(function (err) {
                if (btn) btn.disabled = false;
                showErrorToast(err.message || String(err));
            });
    }

    /* ── actions ── */

    function hideSuggestion(sourceId, source, btn) {
        PKModal.confirm({
            title: 'Hide Suggestion?',
            message: 'This suggestion will move to the Hidden section. You can restore it later.',
            confirmLabel: 'Hide',
            confirmClass: 'btn',
            onConfirm: function () {
                actOnSuggestion('/api/suggestions/' + encodeURIComponent(sourceId) + '/hide?source=' + encodeURIComponent(source || 'abs'), btn, function () {
                    updateSuggestionState(sourceId, function (item) {
                        item.status = 'hidden';
                        item.hidden = true;
                        return item;
                    });
                });
            }
        });
    }

    function ignoreSuggestion(sourceId, source, btn) {
        PKModal.confirm({
            title: 'Never Ask Again?',
            message: 'This suggestion will be permanently ignored and will not return on future rescans.',
            confirmLabel: 'Never Ask',
            confirmClass: 'btn btn-danger',
            onConfirm: function () {
                actOnSuggestion('/api/suggestions/' + encodeURIComponent(sourceId) + '/ignore?source=' + encodeURIComponent(source || 'abs'), btn, function () {
                    updateSuggestionState(sourceId, function () {
                        return null;
                    });
                });
            }
        });
    }

    function unhideSuggestion(sourceId, source, btn) {
        actOnSuggestion('/api/suggestions/' + encodeURIComponent(sourceId) + '/unhide?source=' + encodeURIComponent(source || 'abs'), btn, function () {
            updateSuggestionState(sourceId, function (item) {
                item.status = 'pending';
                item.hidden = false;
                return item;
            });
        });
    }

    function linkBookFusion(sourceId, matchIndex, source) {
        fetch('/api/suggestions/' + encodeURIComponent(sourceId) + '/link-bookfusion', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ match_index: matchIndex, source: source || 'abs' })
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) throw new Error(data.error || 'Link failed');
                refreshSuggestionsData('BookFusion link created.');
            })
            .catch(function (err) {
                showErrorToast(err.message || String(err));
            });
    }

    function addBookFusionToDashboard(bookfusionIds) {
        fetch('/api/bookfusion/add-to-dashboard', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ bookfusion_ids: bookfusionIds })
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) throw new Error(data.error || 'Add failed');
                window.location.href = '/';
            })
            .catch(function (err) {
                showErrorToast(err.message || String(err));
            });
    }

    /* ── rescan ── */

    function rescanSuggestions() {
        var btn = document.getElementById('rescan-btn');
        var status = document.getElementById('rescan-status');
        btn.disabled = true;
        status.textContent = 'Queued library rescan...';
        fetch('/api/suggestions/rescan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ catalog: true })
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) throw new Error(data.error || 'Rescan failed');
                if (data.rate_limited) {
                    status.textContent = data.message || ('Please wait ' + (data.next_allowed_in || 0) + 's before rescanning again.');
                    btn.disabled = false;
                    return;
                }
                status.textContent = data.message || 'Suggestions rescan started...';
                catalogPoller.start();
            })
            .catch(function (err) {
                status.textContent = err.message || String(err);
                btn.disabled = false;
            });
    }

    var catalogPoller = createRescanPoller({
        onRunning: function (data) {
            document.getElementById('rescan-status').textContent = data.message || 'Rescan in progress...';
            document.getElementById('rescan-btn').disabled = true;
        },
        onComplete: function (data) {
            refreshSuggestionsData(data.message || 'Rescan complete.');
        },
        onIdle: function (data) {
            var status = document.getElementById('rescan-status');
            if (data.rate_limited) {
                status.textContent = data.message || ('Please wait ' + (data.next_allowed_in || 0) + 's before rescanning again.');
            } else if (data.message) {
                status.textContent = data.message;
            }
            document.getElementById('rescan-btn').disabled = false;
        },
        onRetry: function () {
            document.getElementById('rescan-btn').disabled = true;
            document.getElementById('rescan-status').textContent = 'Connection interrupted. Retrying rescan status...';
        },
        onError: function (err) {
            document.getElementById('rescan-status').textContent = err.message || String(err);
            document.getElementById('rescan-btn').disabled = false;
        }
    });

    function refreshSuggestionsData(statusMessage) {
        fetch('/api/suggestions')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                suggestionData = data;
                renderSuggestions();
                var status = document.getElementById('rescan-status');
                var btn = document.getElementById('rescan-btn');
                if (statusMessage) status.textContent = statusMessage;
                btn.disabled = false;
            })
            .catch(function (err) {
                var status = document.getElementById('rescan-status');
                var btn = document.getElementById('rescan-btn');
                status.textContent = 'Refresh failed: ' + (err.message || String(err));
                btn.disabled = false;
            });
    }

    /* ── init ── */

    document.querySelectorAll('.sg-view-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            setView(btn.dataset.view, true);
        });
    });

    try {
        var savedView = localStorage.getItem('pk-suggestions-view');
        setView(savedView === 'grid' ? 'grid' : 'list', false);
    } catch (e) {
        setView('list', false);
    }

    function handleViewportChange() {
        var savedView = 'list';
        try { savedView = localStorage.getItem('pk-suggestions-view') || 'list'; } catch (e) {}
        setView(savedView, false);
    }

    if (desktopMedia.addEventListener) {
        desktopMedia.addEventListener('change', handleViewportChange);
    } else if (desktopMedia.addListener) {
        desktopMedia.addListener(handleViewportChange);
    }

    /* Wire up filter inputs */
    document.getElementById('suggestion-search').addEventListener('input', renderSuggestions);
    document.getElementById('confidence-filter').addEventListener('change', renderSuggestions);
    var bfFilter = document.getElementById('bookfusion-filter');
    if (bfFilter) bfFilter.addEventListener('change', renderSuggestions);

    /* Wire up rescan button */
    document.getElementById('rescan-btn').addEventListener('click', rescanSuggestions);

    renderSuggestions();
    catalogPoller.start();

    /* ── expose functions called from inline onclick in rendered HTML ── */
    window.PK_Suggestions = {
        hideSuggestion: hideSuggestion,
        unhideSuggestion: unhideSuggestion,
        ignoreSuggestion: ignoreSuggestion,
        linkBookFusion: linkBookFusion,
        addBookFusionToDashboard: addBookFusionToDashboard
    };
})();
