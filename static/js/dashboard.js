/* PageKeeper — Dashboard JS
   Extracted from index.html inline scripts. */

var refreshPaused = false;

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
        last_sync: 'desc',
        finished_date: 'desc'
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
            const datasetKey = sortBy === 'last_sync' ? 'lastSync' : sortBy === 'finished_date' ? 'finished' : sortBy;
            const valA = a.dataset[datasetKey] || '';
            const valB = b.dataset[datasetKey] || '';

            if (sortBy === 'title') {
                comparison = valA.localeCompare(valB);
            } else if (sortBy === 'progress') {
                comparison = (parseFloat(valA) || 0) - (parseFloat(valB) || 0);
            } else if (sortBy === 'status') {
                comparison = valA.localeCompare(valB);
            } else if (sortBy === 'last_sync') {
                comparison = parseLastSync(valA) - parseLastSync(valB);
            } else if (sortBy === 'finished_date') {
                if (!valA && !valB) comparison = 0;
                else if (!valA) comparison = 1;   // no date → end
                else if (!valB) comparison = -1;
                else comparison = valA.localeCompare(valB);
            }

            return direction === 'asc' ? comparison : -comparison;
        });

        var fragment = document.createDocumentFragment();
        sortedCards.forEach(function(card) { fragment.appendChild(card); });
        grid.appendChild(fragment);
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

    // Default finished grid to finished_date sort (newest first) unless user chose it globally
    if (savedSort !== 'finished_date' && finishedGrid) {
        sortCards(finishedGrid, 'finished_date', 'desc');
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
                    const card = document.querySelector(`.book-card[data-book-id="${CSS.escape(String(book.id))}"]`);
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
    document.addEventListener('click', e => {
        if (e.target.id === 'pk-modal-cancel' || e.target.id === 'pk-modal-confirm') {
            refreshPaused = false;
        }
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
                for (const bookId of ids) {
                    const info = data[bookId];
                    const card = processingSection.querySelector(`.book-card[data-book-id="${CSS.escape(bookId)}"]`);
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
                        if (!data[card.dataset.bookId]) {
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
    const bookId = item.dataset.bookId;
    const title = item.dataset.title;
    const currentHash = item.dataset.hash;

    var backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.style.zIndex = '1100';

    var content = document.createElement('div');
    content.className = 'modal-content';
    content.style.maxWidth = '420px';
    content.style.padding = '24px';

    var heading = document.createElement('h3');
    heading.style.cssText = 'margin: 0 0 8px; font-size: 16px; font-weight: 600;';
    heading.textContent = 'Update KoSync Hash';
    content.appendChild(heading);

    var desc = document.createElement('p');
    desc.style.cssText = 'margin: 0 0 6px; font-size: 13px; color: var(--color-text-muted); line-height: 1.5;';
    desc.textContent = `Enter new KoSync MD5 Hash for "${title}".`;
    content.appendChild(desc);

    var current = document.createElement('p');
    current.style.cssText = 'margin: 0 0 12px; font-size: 12px; color: var(--color-text-muted); font-family: monospace;';
    current.textContent = `Current: ${currentHash}`;
    content.appendChild(current);

    var hashInput = document.createElement('input');
    hashInput.type = 'text';
    hashInput.className = 'form-control';
    hashInput.placeholder = 'Leave empty to auto-recalculate from ebook file';
    hashInput.style.cssText = 'width: 100%; margin-bottom: 16px; box-sizing: border-box;';
    content.appendChild(hashInput);

    var btns = document.createElement('div');
    btns.style.cssText = 'display: flex; gap: 8px; justify-content: flex-end;';

    var cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-secondary';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', function () { backdrop.remove(); });
    btns.appendChild(cancelBtn);

    var submitBtn = document.createElement('button');
    submitBtn.className = 'btn btn-warning';
    submitBtn.textContent = 'Update';
    submitBtn.addEventListener('click', function () {
        var form = document.createElement('form');
        form.method = 'POST';
        form.action = `/update-hash/${encodeURIComponent(bookId)}`;
        var hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.name = 'new_hash';
        hidden.value = hashInput.value.trim();
        form.appendChild(hidden);
        document.body.appendChild(form);
        form.submit();
    });
    btns.appendChild(submitBtn);

    content.appendChild(btns);
    backdrop.appendChild(content);
    backdrop.addEventListener('click', function (e) { if (e.target === backdrop) backdrop.remove(); });
    document.body.appendChild(backdrop);
    hashInput.focus();
}

function syncNow(bookId, btn) {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "...";

    fetch('/api/sync-now/' + encodeURIComponent(bookId), {
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
function pauseBook(bookId, btn) {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "...";

    fetch('/api/pause/' + encodeURIComponent(bookId), {
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

function addToWantToRead(bookId, btn) {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "...";
    closeAllMenus();

    fetch('/api/reading/tbr/from-library', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ abs_id: bookId })
    }).then(response => response.json())
        .then(data => {
            if (data.success) {
                btn.textContent = data.created ? "Added" : "Already added";
                btn.classList.add('success');
                setTimeout(() => {
                    btn.textContent = originalText;
                    btn.disabled = false;
                    btn.classList.remove('success');
                }, 2000);
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

function resumeBook(bookId, btn) {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "...";

    fetch('/api/resume/' + encodeURIComponent(bookId), {
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

function dnfBook(bookId, title) {
    closeAllMenus();
    PKModal.confirm({
        title: 'Did Not Finish',
        message: 'Mark "' + title + '" as Did Not Finish? This book will be excluded from syncing.',
        confirmLabel: 'Mark DNF',
        confirmClass: 'btn btn-warning',
        onConfirm: function () {
            fetch('/api/dnf/' + encodeURIComponent(bookId), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            }).then(function (response) { return response.json(); })
                .then(function (data) {
                    if (data.success) {
                        window.location.reload();
                    } else {
                        PKModal.alert({ title: 'Error', message: data.error || 'Unknown error' });
                    }
                }).catch(function (error) {
                    console.error('Error:', error);
                    PKModal.alert({ title: 'Error', message: 'Connection error while marking book as DNF' });
                });
        }
    });
}

function retryTranscription(bookId, btn) {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "...";

    fetch('/api/retry-transcription/' + encodeURIComponent(bookId), {
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

function markComplete(bookId, title) {
    closeAllMenus();
    PKModal.confirm({
        title: 'Mark Complete',
        message: 'Mark "' + title + '" as complete? This will set progress to 100% on all synced platforms.',
        confirmLabel: 'Mark Complete',
        confirmClass: 'btn btn-warning',
        onConfirm: function () {
            window._mcBookId = bookId;
            var modal = document.getElementById('delete-mapping-modal');
            if (modal) modal.style.display = 'flex';
        }
    });
}

function closeDeleteMappingModal() {
    const modal = document.getElementById('delete-mapping-modal');
    if (modal) modal.style.display = 'none';
    window._mcBookId = null;
    refreshPaused = false;
}
function _dmExecuteFetch(bookId, shouldDelete) {
    closeDeleteMappingModal();
    fetch('/api/mark-complete/' + encodeURIComponent(bookId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ delete: shouldDelete })
    }).then(response => response.json())
        .then(data => {
            if (data.success) {
                if (shouldDelete) {
                    const cardElement = document.querySelector('.book-card[data-book-id="' + CSS.escape(bookId) + '"]');
                    if (cardElement) {
                        cardElement.remove();
                    } else {
                        window.location.reload();
                    }
                } else {
                    window.location.reload();
                }
            } else {
                PKModal.alert({ title: 'Error', message: data.error || 'Unknown error' });
            }
        }).catch(function (error) {
            console.error('Error:', error);
            PKModal.alert({ title: 'Error', message: 'Connection error while marking book as complete' });
        });
}

function _dmYesDelete() { _dmExecuteFetch(window._mcBookId, true); }
function _dmNoKeepMapping() { _dmExecuteFetch(window._mcBookId, false); }

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
    refreshPaused = false;
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

/* Override the legacy bridge from confirm-modal.js to also close card menus */
var _baseShowConfirmModal = showConfirmModal;
showConfirmModal = function(title, message, formAction, accentType) {
    closeAllMenus();
    _baseShowConfirmModal(title, message, formAction, accentType);
};
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
