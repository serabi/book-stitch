/* PageKeeper — Dashboard JS
   Extracted from index.html inline scripts. */

function initDashboard() {
    const processingGrid = document.getElementById('processing-grid');
    const currentlyReadingGrid = document.getElementById('currently-reading-grid');
    const finishedGrid = document.getElementById('finished-grid');
    const pausedGrid = document.getElementById('paused-grid');
    const dnfGrid = document.getElementById('dnf-grid');
    const allBooksGrid = document.getElementById('all-books-grid');
    const sortSelect = document.getElementById('sort-select');
    const filterSelect = document.getElementById('filter-select');

    function migrateLocalStorage(newKey, legacyKey) {
        let val = localStorage.getItem(newKey);
        if (val !== null) return val;
        val = localStorage.getItem(legacyKey);
        if (val !== null) localStorage.setItem(newKey, val);
        return val;
    }
    const directionBtn = document.getElementById('sort-direction');
    const dashboardSearch = document.getElementById('dashboard-search');

    function filterBooks() {
        const filterValue = filterSelect.value;
        const searchString = dashboardSearch ? dashboardSearch.value.toLowerCase() : '';
        const cards = document.querySelectorAll('.book-card');

        cards.forEach(card => {
            const syncMode = card.dataset.syncMode || 'audiobook';

            const titleEl = card.querySelector('.book-title');
            const authorEl = card.querySelector('.book-author');
            const title = titleEl ? titleEl.textContent.toLowerCase() : '';
            const author = authorEl ? authorEl.textContent.toLowerCase() : '';

            let isVisible = true;
            const status = card.dataset.status || '';
            const progress = parseFloat(card.dataset.progress) || 0;
            if (filterValue === 'currently_reading') {
                if (!(progress > 0 && progress < 100 && status !== 'completed' && status !== 'paused' && status !== 'dnf' && status !== 'not_started')) {
                    isVisible = false;
                }
            } else if (filterValue === 'finished') {
                if (status !== 'completed' && progress < 100) {
                    isVisible = false;
                }
            } else if (filterValue === 'paused' && status !== 'paused') {
                isVisible = false;
            } else if (filterValue === 'dnf' && status !== 'dnf') {
                isVisible = false;
            } else if (filterValue === 'audiobook' && syncMode !== 'audiobook') {
                isVisible = false;
            } else if (filterValue === 'ebook_only' && syncMode !== 'ebook_only') {
                isVisible = false;
            }

            if (isVisible && searchString) {
                if (!title.includes(searchString) && !author.includes(searchString)) {
                    isVisible = false;
                }
            }

            if (isVisible) {
                card.classList.remove('hidden');
                card.style.display = '';
            } else {
                card.classList.add('hidden');
                card.style.display = 'none';
            }
        });

        const statusGridMap = {
            currently_reading: 'currently-reading-grid',
            finished: 'finished-grid',
            paused: 'paused-grid',
            dnf: 'dnf-grid',
        };
        const targetGridId = statusGridMap[filterValue];

        document.querySelectorAll('.book-section').forEach(section => {
            const grid = section.querySelector('.book-grid');
            if (!grid) return;

            if (targetGridId) {
                section.style.display = grid.id === targetGridId ? '' : 'none';
            } else {
                const hasVisible = grid.querySelector('.book-card:not(.hidden)') !== null;
                section.style.display = hasVisible ? '' : 'none';
            }
        });

        localStorage.setItem('pagekeeper_filter', filterValue);
    }

    let sortState = {
        title: 'asc',
        progress: 'desc',
        status: 'asc',
        last_sync: 'desc'
    };

    let lastSort = null;

    const savedSort = migrateLocalStorage('pagekeeper_sort', 'book_sync_sort') || 'title';
    const savedSortState = migrateLocalStorage('pagekeeper_sort_state', 'book_sync_sort_state');

    if (savedSort && sortSelect) {
        sortSelect.value = savedSort;
    }

    if (savedSortState) {
        try {
            sortState = { ...sortState, ...JSON.parse(savedSortState) };
        } catch (e) {
            console.error("Error parsing saved sort state", e);
        }
    }

    function parseLastSync(syncText) {
        if (!syncText || syncText === 'Never' || syncText === 'Error') return -1;
        const match = syncText.match(/(\d+)([smhd])/);
        if (!match) return -1;
        const value = parseInt(match[1]);
        const unit = match[2];
        if (unit === 's') return value;
        if (unit === 'm') return value * 60;
        if (unit === 'h') return value * 3600;
        if (unit === 'd') return value * 86400;
        return -1;
    }

    function sortCards(grid, sortBy, direction) {
        if (!grid) return;
        const cards = Array.from(grid.querySelectorAll('.book-card'));

        const sortedCards = cards.sort((a, b) => {
            let comparison = 0;
            const valA = a.dataset[sortBy === 'last_sync' ? 'lastSync' : sortBy] || '';
            const valB = b.dataset[sortBy === 'last_sync' ? 'lastSync' : sortBy] || '';

            if (sortBy === 'title') {
                comparison = valA.localeCompare(valB);
            } else if (sortBy === 'progress') {
                comparison = (parseFloat(valA) || 0) - (parseFloat(valB) || 0);
            } else if (sortBy === 'status') {
                comparison = valA.localeCompare(valB);
            } else if (sortBy === 'last_sync') {
                comparison = parseLastSync(valA) - parseLastSync(valB);
            }

            return direction === 'asc' ? comparison : -comparison;
        });

        sortedCards.forEach(card => grid.appendChild(card));
    }

    function applySorting(sortBy) {
        let direction;
        if (lastSort === sortBy) {
            const currentDirection = sortState[sortBy];
            direction = currentDirection === 'asc' ? 'desc' : 'asc';
            sortState[sortBy] = direction;
        } else {
            direction = sortState[sortBy];
        }

        lastSort = sortBy;
        updateSortIndicator(direction);

        sortCards(processingGrid, sortBy, direction);
        sortCards(currentlyReadingGrid, sortBy, direction);
        sortCards(finishedGrid, sortBy, direction);
        sortCards(pausedGrid, sortBy, direction);
        sortCards(dnfGrid, sortBy, direction);
        sortCards(allBooksGrid, sortBy, direction);

        localStorage.setItem('pagekeeper_sort', sortBy);
        localStorage.setItem('pagekeeper_sort_state', JSON.stringify(sortState));
    }

    function updateSortIndicator(direction) {
        if (directionBtn) {
            directionBtn.textContent = direction === 'asc' ? '\u2191' : '\u2193';
        }
    }

    if (sortSelect) {
        sortSelect.addEventListener('change', (e) => {
            applySorting(e.target.value);
        });
    }

    if (directionBtn) {
        directionBtn.addEventListener('click', () => {
            applySorting(sortSelect.value);
        });
    }

    if (currentlyReadingGrid || finishedGrid || pausedGrid || dnfGrid || allBooksGrid) {
        applySorting(savedSort);
    }

    if (filterSelect) {
        filterSelect.addEventListener('change', filterBooks);
        const savedFilter = migrateLocalStorage('pagekeeper_filter', 'book_sync_filter') || 'all';
        filterSelect.value = savedFilter;
        filterBooks();
    }

    if (dashboardSearch) {
        dashboardSearch.addEventListener('input', filterBooks);
    }

    let refreshPaused = false;

    function refreshDashboard() {
        if (refreshPaused) {
            setTimeout(refreshDashboard, 30000);
            return;
        }

        fetch('/api/status')
            .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(data => {
                if (!data || !data.mappings) return;
                data.mappings.forEach(book => {
                    const card = document.querySelector(`.book-card[data-abs-id="${CSS.escape(book.abs_id)}"]`);
                    if (!card) return;

                    const progressPercent = card.querySelector('.progress-percent');
                    if (progressPercent) {
                        const statusLabel = progressPercent.querySelector('.progress-status-label');
                        progressPercent.textContent = `${book.unified_progress.toFixed(0)}%`;
                        if (statusLabel) {
                            progressPercent.appendChild(document.createTextNode(' '));
                            progressPercent.appendChild(statusLabel);
                        }
                    }

                    card.dataset.progress = book.unified_progress;

                    const absItem = card.querySelector('.service-item[title="Open in Audiobookshelf"] .service-value');
                    if (absItem && book.states && book.states.abs) {
                        const ts = book.states.abs.timestamp || 0;
                        const h = Math.floor(ts / 3600);
                        const m = Math.floor((ts % 3600) / 60);
                        const s = Math.floor(ts % 60);
                        absItem.textContent = `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
                    }

                    const kosyncItem = card.querySelector('.service-item[title^="Update KoReader Hash"] .service-value');
                    if (kosyncItem && book.states?.kosync?.percentage != null) {
                        kosyncItem.textContent = `${book.states.kosync.percentage.toFixed(1)}%`;
                    }

                    const stItem = card.querySelector('.service-item[title="Storyteller"] .service-value');
                    if (stItem && book.states?.storyteller?.percentage != null) {
                        stItem.textContent = `${book.states.storyteller.percentage.toFixed(1)}%`;
                    }

                    const lastSync = card.querySelector('.last-sync');
                    if (lastSync && book.last_sync) {
                        lastSync.textContent = `Synced ${book.last_sync}`;
                    }
                });
            })
            .catch(err => { console.error('Failed to refresh dashboard:', err); })
            .finally(() => setTimeout(refreshDashboard, 30000));
    }

    document.addEventListener('click', e => {
        if (e.target.closest('[data-modal], .modal-trigger, button[onclick*="modal"]')) {
            refreshPaused = true;
        }
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') refreshPaused = false;
    });
    document.body.addEventListener('click', e => {
        if (e.target.classList.contains('modal-overlay')) refreshPaused = false;
    });

    setTimeout(refreshDashboard, 30000);

    const _RELOAD_KEY = 'pk_emptyReloadAttempts';
    function _getReloadAttempts() {
        return parseInt(sessionStorage.getItem(_RELOAD_KEY) || '0', 10);
    }
    function _setReloadAttempts(n) {
        sessionStorage.setItem(_RELOAD_KEY, String(n));
    }
    function pollProcessingStatus() {
        const processingSection = document.getElementById('processing-section');
        if (!processingSection) return;

        fetch('/api/processing-status')
            .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(data => {
                if (typeof data !== 'object' || data === null) return;
                const ids = Object.keys(data);
                if (ids.length === 0) {
                    const hasProcessingCards = processingSection.querySelector('.book-card') !== null;
                    if (!hasProcessingCards) {
                        _setReloadAttempts(0);
                        setTimeout(pollProcessingStatus, 5000);
                        return;
                    }

                    const attempts = _getReloadAttempts();
                    if (attempts < 3) {
                        _setReloadAttempts(attempts + 1);
                        setTimeout(() => location.reload(), 2000);
                    } else {
                        setTimeout(pollProcessingStatus, 5000);
                    }
                    return;
                }
                _setReloadAttempts(0);

                let anyStillProcessing = false;
                let shouldReload = false;
                for (const absId of ids) {
                    const info = data[absId];
                    const card = processingSection.querySelector(`.book-card[data-abs-id="${CSS.escape(absId)}"]`);
                    if (!card) continue;

                    const cardStatus = card.dataset.status;
                    if (info.status === 'active' || cardStatus !== info.status) {
                        shouldReload = true;
                        break;
                    }

                    anyStillProcessing = true;

                    if (info.status === 'processing') {
                        let pctVal = Number(info.job_progress);
                        if (!isFinite(pctVal)) pctVal = 0;
                        pctVal = Math.max(0, Math.min(100, pctVal));
                        const fill = card.querySelector('.progress-bar-fill');
                        const pct = card.querySelector('.progress-percent');
                        if (fill) fill.style.width = `${pctVal}%`;
                        if (pct) pct.textContent = `Transcribing... ${Math.round(pctVal)}%`;
                    }
                }

                if (!shouldReload) {
                    const processingCards = processingSection.querySelectorAll('.book-card');
                    for (const card of processingCards) {
                        if (!data[card.dataset.absId]) {
                            shouldReload = true;
                            break;
                        }
                    }
                }

                if (shouldReload) {
                    location.reload();
                    return;
                }

                if (anyStillProcessing) {
                    setTimeout(pollProcessingStatus, 5000);
                }
            })
            .catch(() => setTimeout(pollProcessingStatus, 5000));
    }

    if (document.getElementById('processing-section')) {
        setTimeout(pollProcessingStatus, 5000);
    }
}

function updateKoSyncHash(event) {
    event.stopPropagation();
    const item = event.currentTarget;
    const absId = item.dataset.absId;
    const title = item.dataset.title;
    const currentHash = item.dataset.hash;

    const msg = `Enter new KoSync MD5 Hash for '${title}'\n\nCurrent: ${currentHash}\n\n(Leave empty to automatically recalculate from the ebook file)`;
    const input = prompt(msg);

    if (input !== null) {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = `/update-hash/${encodeURIComponent(absId)}`;
        const inputField = document.createElement('input');
        inputField.type = 'hidden';
        inputField.name = 'new_hash';
        inputField.value = input.trim();

        form.appendChild(inputField);
        document.body.appendChild(form);
        form.submit();
    }
}

function syncNow(absId, btn) {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "...";

    fetch('/api/sync-now/' + encodeURIComponent(absId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    }).then(response => response.json())
        .then(data => {
            if (data.success) {
                if (data.reload) {
                    window.location.reload();
                    return;
                }
                btn.textContent = "OK";
                setTimeout(() => {
                    btn.textContent = originalText;
                    btn.disabled = false;
                    closeAllMenus();
                }, 2000);
            } else {
                btn.textContent = "Err";
                setTimeout(() => {
                    btn.textContent = originalText;
                    btn.disabled = false;
                    closeAllMenus();
                }, 2000);
            }
        }).catch(error => {
            console.error('Error:', error);
            btn.textContent = "Err";
            setTimeout(() => {
                btn.textContent = originalText;
                btn.disabled = false;
                closeAllMenus();
            }, 2000);
        });
}
function pauseBook(absId, btn) {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "...";

    fetch('/api/pause/' + encodeURIComponent(absId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    }).then(response => response.json())
        .then(data => {
            if (data.success) {
                btn.textContent = "OK";
                setTimeout(() => window.location.reload(), 1000);
            } else {
                btn.textContent = "Err";
                setTimeout(() => {
                    btn.textContent = originalText;
                    btn.disabled = false;
                }, 2000);
            }
        }).catch(error => {
            console.error('Error:', error);
            btn.textContent = "Err";
            setTimeout(() => {
                btn.textContent = originalText;
                btn.disabled = false;
            }, 2000);
        });
}

function resumeBook(absId, btn) {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "...";

    fetch('/api/resume/' + encodeURIComponent(absId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    }).then(response => response.json())
        .then(data => {
            if (data.success) {
                btn.textContent = "OK";
                setTimeout(() => window.location.reload(), 1000);
            } else {
                btn.textContent = "Err";
                setTimeout(() => {
                    btn.textContent = originalText;
                    btn.disabled = false;
                }, 2000);
            }
        }).catch(error => {
            console.error('Error:', error);
            btn.textContent = "Err";
            setTimeout(() => {
                btn.textContent = originalText;
                btn.disabled = false;
            }, 2000);
        });
}

function dnfBook(absId, title) {
    closeAllMenus();
    if (!confirm('Mark "' + title + '" as Did Not Finish? This book will be excluded from syncing.')) {
        return;
    }

    fetch('/api/dnf/' + encodeURIComponent(absId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    }).then(response => response.json())
        .then(data => {
            if (data.success) {
                window.location.reload();
            } else {
                alert('Error marking book as DNF: ' + (data.error || 'Unknown error'));
            }
        }).catch(error => {
            console.error('Error:', error);
            alert('Connection error while marking book as DNF');
        });
}

function retryTranscription(absId, btn) {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "...";

    fetch('/api/retry-transcription/' + encodeURIComponent(absId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    }).then(response => response.json())
        .then(data => {
            if (data.success) {
                btn.textContent = "OK";
                setTimeout(() => window.location.reload(), 1000);
            } else {
                btn.textContent = "Err";
                setTimeout(() => {
                    btn.textContent = originalText;
                    btn.disabled = false;
                }, 2000);
            }
        }).catch(error => {
            console.error('Error:', error);
            btn.textContent = "Err";
            setTimeout(() => {
                btn.textContent = originalText;
                btn.disabled = false;
            }, 2000);
        });
}

function markComplete(absId, title) {
    closeAllMenus();
    if (!confirm('Are you sure you want to mark "' + title + '" as complete? This will set progress to 100% on all synced platforms.')) {
        return;
    }
    window._mcAbsId = absId;
    const modal = document.getElementById('delete-mapping-modal');
    if (modal) modal.style.display = 'flex';
}

function closeDeleteMappingModal() {
    const modal = document.getElementById('delete-mapping-modal');
    if (modal) modal.style.display = 'none';
    window._mcAbsId = null;
}
function _dmExecuteFetch(absId, shouldDelete) {
    closeDeleteMappingModal();
    fetch('/api/mark-complete/' + encodeURIComponent(absId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ delete: shouldDelete })
    }).then(response => response.json())
        .then(data => {
            if (data.success) {
                if (shouldDelete) {
                    const cardElement = document.querySelector('.book-card[data-abs-id="' + CSS.escape(absId) + '"]');
                    if (cardElement) {
                        cardElement.remove();
                    } else {
                        window.location.reload();
                    }
                } else {
                    window.location.reload();
                }
            } else {
                alert('Error marking book as complete: ' + (data.error || 'Unknown error'));
            }
        }).catch(error => {
            console.error('Error:', error);
            alert('Connection error while marking book as complete');
        });
}

function _dmYesDelete() { _dmExecuteFetch(window._mcAbsId, true); }
function _dmNoKeepMapping() { _dmExecuteFetch(window._mcAbsId, false); }

let _panelSourceMenu = null;

function openActionPanel(trigger) {
    const card = trigger.closest('.book-card');
    const dropdown = card.querySelector('.card-menu-dropdown');
    const panelBody = document.getElementById('action-panel-body');
    const panel = document.getElementById('action-panel');

    _panelSourceMenu = dropdown;
    while (dropdown.firstChild) {
        panelBody.appendChild(dropdown.firstChild);
    }

    panel.style.display = '';

    const sheet = panel.querySelector('.action-panel-sheet');
    if (window.innerWidth > 600) {
        const rect = trigger.getBoundingClientRect();
        let top = rect.bottom + 4;
        let left = rect.right - 280;

        const sheetHeight = Math.min(sheet.scrollHeight, window.innerHeight * 0.8);
        if (top + sheetHeight > window.innerHeight - 8) {
            top = rect.top - sheetHeight - 4;
        }
        if (left < 8) left = 8;

        sheet.style.position = 'fixed';
        sheet.style.top = top + 'px';
        sheet.style.left = left + 'px';
        sheet.style.right = 'auto';
        sheet.style.bottom = 'auto';
    } else {
        sheet.style.cssText = '';
    }

    panel.offsetHeight;
    panel.classList.add('open');
    trigger.setAttribute('aria-expanded', 'true');
}

function closeActionPanel() {
    const panel = document.getElementById('action-panel');
    const panelBody = document.getElementById('action-panel-body');
    const sheet = panel.querySelector('.action-panel-sheet');

    panel.classList.remove('open');
    sheet.style.cssText = '';

    // Reset submenu state before returning nodes
    var mainView = panelBody.querySelector('.card-menu-main');
    var subView = panelBody.querySelector('.card-menu-status-sub');
    if (mainView) mainView.style.display = '';
    if (subView) subView.style.display = 'none';

    if (_panelSourceMenu) {
        while (panelBody.firstChild) {
            _panelSourceMenu.appendChild(panelBody.firstChild);
        }
        const trigger = _panelSourceMenu.closest('.card-menu')?.querySelector('.card-menu-trigger');
        if (trigger) trigger.setAttribute('aria-expanded', 'false');
        _panelSourceMenu = null;
    }

    panel.style.display = 'none';
}

function closeAllMenus() {
    closeActionPanel();
}

// Focus trapping for modals
document.addEventListener('keydown', function(e) {
    const isTabPressed = (e.key === 'Tab' || e.keyCode === 9);
    if (!isTabPressed) return;

    // Find the currently open modal
    const openModal = document.querySelector('.hc-modal[style*="display: flex"], .confirm-modal[style*="display: flex"], #st-modal:not(.hidden)');
    if (!openModal) return;

    const focusableEls = openModal.querySelectorAll('a[href]:not([disabled]), button:not([disabled]), textarea:not([disabled]), input[type="text"]:not([disabled]), input[type="radio"]:not([disabled]), input[type="checkbox"]:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])');
    if (focusableEls.length === 0) return;
    
    const firstFocusableEl = focusableEls[0];
    const lastFocusableEl = focusableEls[focusableEls.length - 1];

    if (e.shiftKey) { /* shift + tab */
        if (document.activeElement === firstFocusableEl || !openModal.contains(document.activeElement)) {
            lastFocusableEl.focus();
            e.preventDefault();
        }
    } else { /* tab */
        if (document.activeElement === lastFocusableEl || !openModal.contains(document.activeElement)) {
            firstFocusableEl.focus();
            e.preventDefault();
        }
    }
});

function showConfirmModal(title, message, formAction, accentType) {
    closeAllMenus();
    const modal = document.getElementById('confirm-modal');
    if (!modal) return;
    const iconEl = document.getElementById('confirm-modal-icon');
    const titleEl = document.getElementById('confirm-modal-title');
    const msgEl = document.getElementById('confirm-modal-message');
    const formEl = document.getElementById('confirm-modal-form');
    const submitBtn = document.getElementById('confirm-modal-submit');

    titleEl.textContent = title;
    msgEl.textContent = message;
    formEl.action = formAction;

    submitBtn.className = 'btn';
    iconEl.className = 'confirm-modal-icon';

    if (accentType === 'danger') {
        submitBtn.classList.add('btn-danger');
        iconEl.textContent = '\u26A0';
        iconEl.classList.add('confirm-icon-danger');
    } else {
        submitBtn.classList.add('btn-warning');
        iconEl.textContent = '\u26A0';
        iconEl.classList.add('confirm-icon-warning');
    }

    submitBtn.textContent = title;
    modal.style.display = 'flex';
}

function closeConfirmModal() {
    const modal = document.getElementById('confirm-modal');
    if (modal) modal.style.display = 'none';
}
document.addEventListener('click', function(e) {
    const trigger = e.target.closest('.card-menu-trigger');
    if (trigger) {
        e.stopPropagation();
        closeActionPanel();
        openActionPanel(trigger);
        return;
    }
    // Submenu: open status submenu
    var subTrigger = e.target.closest('.card-menu-submenu-trigger');
    if (subTrigger) {
        e.stopPropagation();
        var panelBody = document.getElementById('action-panel-body');
        var mainView = panelBody.querySelector('.card-menu-main');
        var subView = panelBody.querySelector('.card-menu-status-sub');
        if (mainView) mainView.style.display = 'none';
        if (subView) subView.style.display = '';
        return;
    }
    // Submenu: back to main
    var backBtn = e.target.closest('.card-menu-back');
    if (backBtn) {
        e.stopPropagation();
        var panelBody = document.getElementById('action-panel-body');
        var mainView = panelBody.querySelector('.card-menu-main');
        var subView = panelBody.querySelector('.card-menu-status-sub');
        if (mainView) mainView.style.display = '';
        if (subView) subView.style.display = 'none';
        return;
    }
    if (e.target.closest('.action-panel-overlay')) {
        closeActionPanel();
    }
});

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        closeActionPanel();
        closeConfirmModal();
    }
});
