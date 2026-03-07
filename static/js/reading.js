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
  const sectionsRoot = document.getElementById('reading-sections');
  if (!sectionsRoot) return;

  const cards = () => Array.from(sectionsRoot.querySelectorAll('.r-book-card'));
  const searchInput = document.getElementById('reading-search');
  const mobileSearchInput = document.getElementById('reading-search-mobile');
  const sortSelect = document.getElementById('reading-sort');
  const mobileSortSelect = document.getElementById('reading-sort-mobile');
  const filterChips = document.querySelectorAll('.r-filter-chip');
  const viewBtns = document.querySelectorAll('.r-view-btn');
  const resultsInfo = document.getElementById('results-info');
  const resultsText = document.getElementById('results-text');
  const emptyTab = document.getElementById('empty-tab');
  const sections = Array.from(sectionsRoot.querySelectorAll('.r-section'));
  const controlsModal = document.getElementById('reading-controls-modal');
  const controlsOpen = document.getElementById('reading-controls-open');
  const controlsClose = document.getElementById('reading-controls-close');

  let activeFilter = 'all';
  let currentView = 'list';
  const desktopMedia = window.matchMedia('(min-width: 961px)');

  function syncSearchInputs(source) {
    const value = source ? source.value : '';
    if (searchInput && source !== searchInput) searchInput.value = value;
    if (mobileSearchInput && source !== mobileSearchInput) mobileSearchInput.value = value;
  }

  function syncSortInputs(source) {
    const value = source ? source.value : 'activity-desc';
    if (sortSelect && source !== sortSelect) sortSelect.value = value;
    if (mobileSortSelect && source !== mobileSortSelect) mobileSortSelect.value = value;
  }

  function showControlsModal() {
    if (controlsModal) controlsModal.style.display = 'flex';
  }

  function hideControlsModal() {
    if (controlsModal) controlsModal.style.display = 'none';
  }

  filterChips.forEach(chip => {
    chip.addEventListener('click', () => {
      filterChips.forEach(item => {
        item.classList.remove('active');
        item.setAttribute('aria-selected', 'false');
      });
      chip.classList.add('active');
      chip.setAttribute('aria-selected', 'true');
      activeFilter = chip.dataset.filter;
      applyFiltersAndSort();
    });
  });

  [searchInput, mobileSearchInput].forEach(input => {
    if (!input) return;
    input.addEventListener('input', () => {
      syncSearchInputs(input);
      applyFiltersAndSort();
    });
  });

  [sortSelect, mobileSortSelect].forEach(select => {
    if (!select) return;
    select.addEventListener('change', () => {
      syncSortInputs(select);
      applyFiltersAndSort();
    });
  });

  if (controlsOpen) controlsOpen.addEventListener('click', showControlsModal);
  if (controlsClose) controlsClose.addEventListener('click', hideControlsModal);
  if (controlsModal) {
    controlsModal.addEventListener('click', e => {
      if (e.target === controlsModal) hideControlsModal();
    });
  }

  function setView(view, persist) {
    const forcedView = desktopMedia.matches ? view : 'list';
    currentView = forcedView;
    sectionsRoot.classList.toggle('r-grid-view', forcedView === 'grid');
    viewBtns.forEach(btn => {
      btn.classList.toggle('active', btn.dataset.view === forcedView);
      btn.disabled = !desktopMedia.matches;
    });
    if (persist && desktopMedia.matches) {
      try { localStorage.setItem('pk-reading-view', forcedView); } catch (e) {}
    }
  }

  viewBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      setView(btn.dataset.view, true);
    });
  });

  try {
    const savedView = localStorage.getItem('pk-reading-view');
    setView(savedView === 'grid' ? 'grid' : 'list', false);
  } catch (e) {}
  if (!currentView) setView('list', false);

  const onDesktopChange = () => setView(currentView, false);
  if (desktopMedia.addEventListener) {
    desktopMedia.addEventListener('change', onDesktopChange);
  } else if (desktopMedia.addListener) {
    desktopMedia.addListener(onDesktopChange);
  }

  function compareCards(a, b) {
    const sortValue = sortSelect ? sortSelect.value : 'activity-desc';
    switch (sortValue) {
      case 'title-asc':
        return (a.dataset.title || '').localeCompare(b.dataset.title || '');
      case 'progress-desc':
        return (
          (parseFloat(b.dataset.progress) || 0) - (parseFloat(a.dataset.progress) || 0)
          || (a.dataset.title || '').localeCompare(b.dataset.title || '')
        );
      case 'finished-desc':
        return (
          (b.dataset.finished || '').localeCompare(a.dataset.finished || '')
          || (b.dataset.activity || '').localeCompare(a.dataset.activity || '')
          || (a.dataset.title || '').localeCompare(b.dataset.title || '')
        );
      case 'activity-desc':
      default:
        return (
          (b.dataset.activity || '').localeCompare(a.dataset.activity || '')
          || (b.dataset.finished || '').localeCompare(a.dataset.finished || '')
          || (parseFloat(b.dataset.progress) || 0) - (parseFloat(a.dataset.progress) || 0)
          || (a.dataset.title || '').localeCompare(b.dataset.title || '')
        );
    }
  }

  function getMatches(card, term) {
    const haystack = [
      (card.dataset.title || '').toLowerCase(),
      (card.dataset.author || '').toLowerCase(),
    ].join(' ');
    const matchesSearch = !term || haystack.includes(term);
    const matchesFilter = activeFilter === 'all' || card.dataset.status === activeFilter;
    return matchesSearch && matchesFilter;
  }

  function rebuildFinishedSection(section) {
    const allCards = Array.from(section.querySelectorAll('.r-book-card'));
    section.querySelectorAll('.r-year-group').forEach(group => group.remove());
    const existingStack = section.querySelector('.r-book-stack');
    if (existingStack) existingStack.remove();

    if (allCards.length === 0) return;

    const byYear = new Map();
    allCards.forEach(card => {
      const year = card.dataset.year || 'Unknown';
      if (!byYear.has(year)) byYear.set(year, []);
      byYear.get(year).push(card);
    });

    Array.from(byYear.keys()).sort((a, b) => b.localeCompare(a)).forEach(year => {
      const group = document.createElement('div');
      group.className = 'r-year-group';
      group.dataset.yearGroup = year;

      const heading = document.createElement('div');
      heading.className = 'r-year-heading';
      const text = document.createElement('span');
      text.textContent = year;
      heading.appendChild(text);
      group.appendChild(heading);

      const stack = document.createElement('div');
      stack.className = 'r-book-stack';
      byYear.get(year).forEach(card => stack.appendChild(card));
      group.appendChild(stack);
      section.appendChild(group);
    });
  }

  function sortSection(section) {
    const allCards = Array.from(section.querySelectorAll('.r-book-card'));
    allCards.sort(compareCards);

    if (section.dataset.section === 'finished') {
      allCards.forEach(card => section.appendChild(card));
      rebuildFinishedSection(section);
      return;
    }

    let stack = section.querySelector('.r-book-stack');
    if (!stack) {
      stack = document.createElement('div');
      stack.className = 'r-book-stack';
      section.appendChild(stack);
    }
    allCards.forEach(card => stack.appendChild(card));
  }

  function applyFiltersAndSort() {
    const term = ((searchInput && searchInput.value) || '').toLowerCase().trim();
    let visibleCount = 0;
    let filterTotal = 0;

    cards().forEach(card => {
      if (activeFilter === 'all' || card.dataset.status === activeFilter) {
        filterTotal++;
      }
    });

    sections.forEach(section => {
      sortSection(section);
      const sectionCards = Array.from(section.querySelectorAll('.r-book-card'));
      let sectionVisibleCount = 0;

      sectionCards.forEach(card => {
        const visible = getMatches(card, term);
        card.style.display = visible ? '' : 'none';
        if (visible) {
          visibleCount++;
          sectionVisibleCount++;
        }
      });

      if (section.dataset.section === 'finished') {
        section.querySelectorAll('.r-year-group').forEach(group => {
          const hasVisibleCards = Array.from(group.querySelectorAll('.r-book-card')).some(card => card.style.display !== 'none');
          group.style.display = hasVisibleCards ? '' : 'none';
        });
      }

      const emptyState = section.querySelector('.r-section-empty');
      if (emptyState) emptyState.hidden = sectionVisibleCount !== 0;
      section.style.display = sectionVisibleCount === 0 ? 'none' : '';
    });

    if (resultsInfo && resultsText) {
      if (term || activeFilter !== 'all') {
        resultsInfo.hidden = false;
        resultsText.textContent = `Showing ${visibleCount} of ${filterTotal} books`;
      } else {
        resultsInfo.hidden = true;
      }
    }

    if (emptyTab) emptyTab.hidden = visibleCount !== 0;
    sectionsRoot.style.display = visibleCount === 0 ? 'none' : '';
  }

  syncSortInputs(sortSelect || mobileSortSelect);
  syncSearchInputs(searchInput || mobileSearchInput);
  applyFiltersAndSort();

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
          if (data.success) {
            window.location.reload();
            return;
          }
          goalSave.disabled = false;
        })
        .catch(() => { goalSave.disabled = false; });
    });
  }
}


function initReadingDetail() {
  // ── Rating stars ──
  const rc = document.getElementById('rating-stars');
  if (rc) {
    const absId = rc.dataset.absId;
    const stars = rc.querySelectorAll('.r-star-btn');
    const label = document.getElementById('rating-label');

    stars.forEach(star => {
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
          }
        });
    });
  }
  bindDate('started_at', 'started-at');
  bindDate('finished_at', 'finished-at');

  // ── About This Book description expand/collapse ──
  const descWrap = document.getElementById('about-book-desc-wrap');
  const descMoreBtn = document.getElementById('about-book-more');
  if (descWrap && descMoreBtn) {
    descMoreBtn.addEventListener('click', () => {
      const collapsed = descWrap.classList.toggle('is-collapsed');
      descMoreBtn.textContent = collapsed ? 'Read More' : 'Show Less';
      descMoreBtn.setAttribute('aria-expanded', String(!collapsed));
    });
  }

  // ── Journal ──
  const form = document.getElementById('journal-form');
  if (form) {
    const absId = form.dataset.absId;
    const textarea = document.getElementById('journal-entry');
    const timeline = document.getElementById('journal-timeline');
    const submitBtn = document.getElementById('journal-submit');
    const cancelBtn = document.getElementById('journal-cancel');

    function resetJournalForm() {
      form.dataset.editJournalId = '';
      if (submitBtn) submitBtn.textContent = 'Add Note';
      if (cancelBtn) cancelBtn.classList.add('hidden');
      if (textarea) textarea.value = '';
    }

    form.addEventListener('submit', e => {
      e.preventDefault();
      const entry = textarea.value.trim();
      if (!entry) return;
      const editJournalId = form.dataset.editJournalId;
      const method = editJournalId ? 'PATCH' : 'POST';
      const url = editJournalId
        ? `/api/reading/journal/${editJournalId}`
        : `/api/reading/book/${absId}/journal`;

      fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entry }),
      })
        .then(r => {
          if (!r.ok) throw new Error('Failed to save');
          return r.json();
        })
        .then(data => {
          if (!data.success) return;
          if (timeline) {
            if (editJournalId) {
              const item = timeline.querySelector(`.r-tl-item[data-journal-id="${editJournalId}"]`);
              const text = item?.querySelector('.r-tl-text');
              if (text) text.textContent = data.journal.entry;
              if (item) item.dataset.entry = data.journal.entry;
            } else {
              const empty = timeline.querySelector('.r-journal-empty');
              if (empty) empty.remove();
              timeline.prepend(buildJournalNode(data.journal));
            }
          }
          resetJournalForm();
        })
        .catch(() => {
          // Show error feedback to user
        });
    });

    if (cancelBtn) cancelBtn.addEventListener('click', () => {
      resetJournalForm();
    });

    // Delete (event delegation)
    if (timeline) timeline.addEventListener('click', e => {
      const trigger = e.target.closest('.r-tl-menu-trigger');
      if (trigger) {
        const menu = trigger.closest('.r-tl-menu');
        const isOpen = menu.classList.contains('open');
        timeline.querySelectorAll('.r-tl-menu.open').forEach(item => {
          item.classList.remove('open');
          const btn = item.querySelector('.r-tl-menu-trigger');
          if (btn) btn.setAttribute('aria-expanded', 'false');
        });
        if (!isOpen) {
          menu.classList.add('open');
          trigger.setAttribute('aria-expanded', 'true');
        }
        return;
      }

      const actionBtn = e.target.closest('.r-tl-menu-item');
      if (!actionBtn) return;
      const menu = actionBtn.closest('.r-tl-menu');
      if (!menu) return;
      const journalId = menu.dataset.journalId;
      const eventType = menu.dataset.journalEvent;
      const item = menu.closest('.r-tl-item');

      if (actionBtn.dataset.action === 'edit') {
        if (eventType !== 'note') return;
        const text = item?.querySelector('.r-tl-text')?.textContent || '';
        form.dataset.editJournalId = journalId;
        textarea.value = text;
        if (submitBtn) submitBtn.textContent = 'Save Note';
        if (cancelBtn) cancelBtn.classList.remove('hidden');
        textarea.focus();
        menu.classList.remove('open');
        const triggerBtn = menu.querySelector('.r-tl-menu-trigger');
        if (triggerBtn) triggerBtn.setAttribute('aria-expanded', 'false');
        form.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return;
      }

      if (actionBtn.dataset.action !== 'delete') return;
      if (!confirm('Delete this journal entry?')) return;
      function flashError() {
        menu.style.outline = '2px solid var(--color-danger, red)';
        setTimeout(() => { menu.style.outline = ''; }, 2000);
      }
      fetch(`/api/reading/journal/${journalId}`, { method: 'DELETE' })
        .then(r => {
          if (!r.ok) throw new Error('Delete failed');
          return r.json();
        })
        .then(data => {
          if (!data.success) { flashError(); return; }
          if (item) {
            item.style.transition = 'opacity 0.3s';
            item.style.opacity = '0';
            setTimeout(() => item.remove(), 300);
          }
        })
        .catch(() => flashError());
    });

    document.addEventListener('click', e => {
      if (!e.target.closest('.r-tl-menu')) {
        timeline.querySelectorAll('.r-tl-menu.open').forEach(item => {
          item.classList.remove('open');
          const btn = item.querySelector('.r-tl-menu-trigger');
          if (btn) btn.setAttribute('aria-expanded', 'false');
        });
      }
    });
  }
}


/** Build a journal timeline node using safe DOM methods. */
function buildJournalNode(j) {
  const item = document.createElement('div');
  item.className = 'r-tl-item';
  item.dataset.journalId = j.id;
  item.dataset.entry = j.entry || '';

  const line = document.createElement('div');
  line.className = 'r-tl-line';
  item.appendChild(line);

  const dot = document.createElement('div');
  dot.className = `r-tl-dot r-tl-dot-${j.event || 'note'}`;
  item.appendChild(dot);

  const body = document.createElement('div');
  body.className = 'r-tl-body';

  const head = document.createElement('div');
  head.className = 'r-tl-head';

  const evtSpan = document.createElement('span');
  const eventName = j.event || 'note';
  evtSpan.className = `r-tl-event r-tl-event-${eventName}`;
  evtSpan.textContent = eventName.charAt(0).toUpperCase() + eventName.slice(1);
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

  if (eventName === 'note' || eventName === 'highlight') {
    const menu = document.createElement('div');
    menu.className = 'r-tl-menu';
    menu.dataset.journalId = j.id;
    menu.dataset.journalEvent = eventName;

    const trigger = document.createElement('button');
    trigger.className = 'r-tl-menu-trigger';
    trigger.type = 'button';
    trigger.title = 'More actions';
    trigger.setAttribute('aria-label', 'More actions');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.textContent = '\u22ef';
    menu.appendChild(trigger);

    const dropdown = document.createElement('div');
    dropdown.className = 'r-tl-menu-dropdown';

    if (eventName === 'note') {
      const edit = document.createElement('button');
      edit.className = 'r-tl-menu-item';
      edit.type = 'button';
      edit.dataset.action = 'edit';
      edit.textContent = 'Edit';
      dropdown.appendChild(edit);
    }

    const del = document.createElement('button');
    del.className = 'r-tl-menu-item r-tl-menu-item--danger';
    del.type = 'button';
    del.dataset.action = 'delete';
    del.textContent = 'Delete';
    dropdown.appendChild(del);

    menu.appendChild(dropdown);
    body.appendChild(menu);
  }

  item.appendChild(body);
  return item;
}
