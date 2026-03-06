/* PageKeeper — Reading Tab */

// Attach cover-fallback logic via event delegation (no inline onerror needed)
document.addEventListener('error', function (e) {
  const img = e.target;
  if (!img || !img.classList.contains('r-cover-img')) return;

  const fallbackId = (img.dataset.fallbackId || '').trim();
  if (fallbackId && !img.dataset.fallbackAttempted) {
    img.dataset.fallbackAttempted = '1';
    img.src = '/covers/' + encodeURIComponent(fallbackId) + '.jpg';
    return;
  }

  img.style.display = 'none';
  var placeholder = img.nextElementSibling;
  if (placeholder) {
    placeholder.classList.remove('hidden');
  }
}, true);

function initReadingPage(currentYear) {
  const grid = document.getElementById('book-grid');
  if (!grid) return;

  const cards = () => Array.from(grid.querySelectorAll('.r-book-card'));
  const searchInput = document.getElementById('reading-search');
  const sortSelect = document.getElementById('reading-sort');
  const tabs = document.querySelectorAll('.r-tab');
  const viewBtns = document.querySelectorAll('.r-view-btn');
  const resultsInfo = document.getElementById('results-info');
  const resultsText = document.getElementById('results-text');
  const emptyTab = document.getElementById('empty-tab');

  let activeFilter = 'all';
  let originalOrder = cards().map(c => c.dataset.title + c.dataset.status);

  // ── Tab switching ──────────────────────────────────────────

  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      tabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      activeFilter = tab.dataset.filter;
      applyFilters();
    });
  });

  // ── Search ─────────────────────────────────────────────────

  if (searchInput) {
    searchInput.addEventListener('input', () => applyFilters());
  }

  // ── Sort ───────────────────────────────────────────────────

  if (sortSelect) {
    sortSelect.addEventListener('change', () => applySort());
  }

  // ── View toggle ────────────────────────────────────────────

  viewBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      viewBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const view = btn.dataset.view;
      grid.classList.toggle('r-list-view', view === 'list');
      try { localStorage.setItem('pk-reading-view', view); } catch (e) {}
    });
  });

  // Restore saved view preference
  try {
    const savedView = localStorage.getItem('pk-reading-view');
    if (savedView === 'list') {
      grid.classList.add('r-list-view');
      viewBtns.forEach(b => {
        b.classList.toggle('active', b.dataset.view === 'list');
      });
    }
  } catch (e) {}

  // ── Core filter/display logic ──────────────────────────────

  function applyFilters() {
    const term = (searchInput ? searchInput.value : '').toLowerCase();
    let visibleCount = 0;
    let totalForTab = 0;

    // Remove existing dividers
    grid.querySelectorAll('.r-grid-divider').forEach(d => d.remove());

    cards().forEach(card => {
      const status = card.dataset.status;
      const title = (card.dataset.title || '').toLowerCase();
      const matchesTab = activeFilter === 'all' || status === activeFilter;
      const matchesSearch = !term || title.includes(term);
      const visible = matchesTab && matchesSearch;
      card.style.display = visible ? '' : 'none';
      if (matchesTab) totalForTab++;
      if (visible) visibleCount++;
    });

    // Show/hide empty state
    if (emptyTab) {
      emptyTab.style.display = visibleCount === 0 ? '' : 'none';
    }
    grid.style.display = visibleCount === 0 ? 'none' : '';

    // Show results info when searching
    if (resultsInfo && resultsText) {
      if (term) {
        resultsInfo.style.display = '';
        resultsText.textContent = `Showing ${visibleCount} of ${totalForTab} books`;
      } else {
        resultsInfo.style.display = 'none';
      }
    }

    // Insert dividers
    const sortValue = sortSelect ? sortSelect.value : 'default';
    if (activeFilter === 'all' && sortValue === 'default') {
      insertStatusDividers();
    } else if (activeFilter === 'finished') {
      insertYearDividers();
    }
  }

  // ── Sort logic ─────────────────────────────────────────────

  function applySort() {
    const sortValue = sortSelect ? sortSelect.value : 'default';
    const allCards = cards();

    if (sortValue === 'default') {
      // Restore original order by re-sorting to match originalOrder
      allCards.sort((a, b) => {
        const keyA = a.dataset.title + a.dataset.status;
        const keyB = b.dataset.title + b.dataset.status;
        return originalOrder.indexOf(keyA) - originalOrder.indexOf(keyB);
      });
    } else {
      allCards.sort((a, b) => {
        switch (sortValue) {
          case 'title-asc':
            return (a.dataset.title || '').localeCompare(b.dataset.title || '');
          case 'title-desc':
            return (b.dataset.title || '').localeCompare(a.dataset.title || '');
          case 'rating':
            return (parseFloat(b.dataset.rating) || 0) - (parseFloat(a.dataset.rating) || 0);
          case 'finished-desc':
            return (b.dataset.finished || '').localeCompare(a.dataset.finished || '');
          case 'started-desc':
            return (b.dataset.started || '').localeCompare(a.dataset.started || '');
          case 'progress-desc':
            return (parseFloat(b.dataset.progress) || 0) - (parseFloat(a.dataset.progress) || 0);
          default:
            return 0;
        }
      });
    }

    // Re-append in sorted order (moves DOM nodes)
    allCards.forEach(card => grid.appendChild(card));

    // Re-apply filters to update visibility and year dividers
    applyFilters();
  }

  // ── Divider helpers ─────────────────────────────────────────

  const statusLabels = {
    reading: 'Currently Reading',
    finished: 'Finished',
    paused: 'Paused',
    dnf: 'Did Not Finish',
    not_started: 'Not Started',
  };

  function makeDivider(text, count) {
    const div = document.createElement('div');
    div.className = 'r-grid-divider';
    const label = document.createElement('span');
    label.textContent = text;
    div.appendChild(label);
    if (count != null) {
      const badge = document.createElement('span');
      badge.className = 'r-divider-count';
      badge.textContent = count;
      div.appendChild(badge);
    }
    return div;
  }

  function insertStatusDividers() {
    const visibleCards = cards().filter(c => c.style.display !== 'none');
    let lastStatus = null;

    visibleCards.forEach(card => {
      const status = card.dataset.status;
      if (status !== lastStatus) {
        lastStatus = status;
        const group = visibleCards.filter(c => c.dataset.status === status);
        grid.insertBefore(
          makeDivider(statusLabels[status] || status, group.length),
          card
        );
      }
    });

    // Also insert year sub-dividers within the finished group
    let lastYear = null;
    visibleCards.filter(c => c.dataset.status === 'finished').forEach(card => {
      const finished = card.dataset.finished;
      if (!finished) return;
      const year = finished.substring(0, 4);
      if (year !== lastYear) {
        lastYear = year;
        const div = makeDivider(year);
        div.classList.add('r-grid-divider-sub');
        grid.insertBefore(div, card);
      }
    });
  }

  function insertYearDividers() {
    const visibleCards = cards().filter(c => c.style.display !== 'none');
    let lastYear = null;

    visibleCards.forEach(card => {
      const finished = card.dataset.finished;
      if (!finished) return;
      const year = finished.substring(0, 4);
      if (year !== lastYear) {
        lastYear = year;
        const count = visibleCards.filter(c =>
          c.style.display !== 'none' && (c.dataset.finished || '').startsWith(year)
        ).length;
        grid.insertBefore(makeDivider(year, count), card);
      }
    });
  }

  // Run on initial load so All tab gets its dividers
  applyFilters();

  // ── Goal modal ─────────────────────────────────────────────

  const goalCard = document.getElementById('goal-card');
  const goalModal = document.getElementById('goal-modal');
  const goalClose = document.getElementById('goal-modal-close');
  const goalCancel = document.getElementById('goal-cancel');
  const goalSave = document.getElementById('goal-save');
  const goalInput = document.getElementById('goal-input');

  function showModal()  { if (goalModal) goalModal.style.display = 'flex'; }
  function hideModal()  { if (goalModal) goalModal.style.display = 'none'; }

  if (goalCard) goalCard.addEventListener('click', showModal);
  if (goalClose) goalClose.addEventListener('click', hideModal);
  if (goalCancel) goalCancel.addEventListener('click', hideModal);
  if (goalModal) goalModal.addEventListener('click', e => { if (e.target === goalModal) hideModal(); });

  if (goalSave) {
    goalSave.addEventListener('click', () => {
      const target = parseInt(goalInput?.value, 10);
      if (!target || target < 1) return;
      goalSave.disabled = true;
      fetch(`/api/reading/goal/${currentYear}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_books: target }),
      })
        .then(r => {
          if (!r.ok) throw new Error('Failed to save goal');
          return r.json();
        })
        .then(data => {
          if (data.success) window.location.reload();
      star.addEventListener('click', () => {
        const value = parseInt(star.dataset.value, 10);
        fetch(`/api/reading/book/${absId}/rating`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ rating: value }),
        })
          .then(r => {
            if (!r.ok) throw new Error('Failed to save rating');
            return r.json();
          })
          .then(data => {
            if (data.success) {
              stars.forEach((s, i) => {
                s.classList.toggle('filled', i + 1 <= data.rating);
                s.classList.remove('half');
              });
              if (label) label.textContent = data.rating + '/5';
            }
          })
          .catch(() => {
            // Optionally show user feedback
          });
      });
        const value = parseInt(star.dataset.value, 10);
        fetch(`/api/reading/book/${absId}/rating`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ rating: value }),
        })
          .then(r => r.json())
          .then(data => {
            if (data.success) {
              stars.forEach((s, i) => {
                s.classList.toggle('filled', i + 1 <= data.rating);
                s.classList.remove('half');
              });
              if (label) label.textContent = data.rating + '/5';
            }
          });
      });
    });
  }

  // ── Date fields ──
  function bindDate(field, inputId) {
    const input = document.getElementById(inputId);
    if (!input) return;
    input.addEventListener('change', () => {
      const payload = {};
      payload[field] = input.value || null;
      fetch(`/api/reading/book/${input.dataset.absId}/dates`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
        .then(r => r.json())
        .then(data => {
          if (!data.success) {
            input.style.outline = '2px solid var(--color-danger, red)';
            setTimeout(() => { input.style.outline = ''; }, 2000);
      fetch(`/api/reading/book/${absId}/journal`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entry }),
      })
        .then(r => {
          if (!r.ok) throw new Error('Failed to save');
          return r.json();
        })
        .then(data => {
          if (!data.success) return;
          textarea.value = '';

          if (timeline) {
            const empty = timeline.querySelector('.r-journal-empty');
            if (empty) empty.remove();
            timeline.prepend(buildJournalNode(data.journal));
          }
        })
        .catch(() => {
          // Show error feedback to user
        });
    const timeline = document.getElementById('journal-timeline');

    form.addEventListener('submit', e => {
      e.preventDefault();
      const entry = textarea.value.trim();
      if (!entry) return;

      fetch(`/api/reading/book/${absId}/journal`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entry }),
      })
        .then(r => r.json())
        .then(data => {
          if (!data.success) return;
          textarea.value = '';

          if (timeline) {
            const empty = timeline.querySelector('.r-journal-empty');
            if (empty) empty.remove();
            timeline.prepend(buildJournalNode(data.journal));
          }
        });
    });

    // Delete (event delegation)
    if (timeline) timeline.addEventListener('click', e => {
      const btn = e.target.closest('.r-tl-delete');
      if (!btn) return;
      function flashError() {
        btn.style.outline = '2px solid var(--color-danger, red)';
        setTimeout(() => { btn.style.outline = ''; }, 2000);
      }
      fetch(`/api/reading/journal/${btn.dataset.journalId}`, { method: 'DELETE' })
        .then(r => {
          if (!r.ok) throw new Error('Delete failed');
          return r.json();
        })
        .then(data => {
          if (!data.success) { flashError(); return; }
          const item = btn.closest('.r-tl-item');
          if (item) {
            item.style.transition = 'opacity 0.3s';
            item.style.opacity = '0';
            setTimeout(() => item.remove(), 300);
          }
        })
        .catch(() => flashError());
    });
  }
}


/** Build a journal timeline node using safe DOM methods. */
function buildJournalNode(j) {
  const item = document.createElement('div');
  item.className = 'r-tl-item';
  item.dataset.journalId = j.id;

  const line = document.createElement('div');
  line.className = 'r-tl-line';
  item.appendChild(line);

  const dot = document.createElement('div');
  dot.className = 'r-tl-dot r-tl-dot-note';
  item.appendChild(dot);

  const body = document.createElement('div');
  body.className = 'r-tl-body';

  const head = document.createElement('div');
  head.className = 'r-tl-head';

  const evtSpan = document.createElement('span');
  evtSpan.className = 'r-tl-event r-tl-event-note';
  evtSpan.textContent = 'Note';
  head.appendChild(evtSpan);

  if (j.created_at) {
    const dateSpan = document.createElement('span');
    dateSpan.className = 'r-tl-date';
    dateSpan.textContent = new Date(j.created_at).toLocaleDateString('en-US', {
      month: 'short', day: 'numeric', year: 'numeric'
    });
    head.appendChild(dateSpan);
  }

  if (j.percentage != null) {
    const pct = document.createElement('span');
    pct.className = 'r-tl-pct';
    pct.textContent = Math.round(j.percentage * 100) + '%';
    head.appendChild(pct);
  }

  body.appendChild(head);

  if (j.entry) {
    const text = document.createElement('p');
    text.className = 'r-tl-text';
    text.textContent = j.entry;
    body.appendChild(text);
  }

  const del = document.createElement('button');
  del.className = 'r-tl-delete';
  del.dataset.journalId = j.id;
  del.title = 'Delete';
  del.textContent = '\u00D7';
  body.appendChild(del);

  item.appendChild(body);
  return item;
}
