/* ═══════════════════════════════════════════
   PageKeeper — match page
   ═══════════════════════════════════════════
   Dependencies:
     - static/js/utils.js          (escapeHtml, debounce, toggleHiddenSection)
     - static/js/confirm-modal.js  (PKModal)
     - templates/partials/confirm_modal.html

   Expects a global PK_PAGE_DATA object with:
     isAttachEbook        (boolean)
     isAttachAudiobook    (boolean)
     storytellerForceMode (boolean)
     absConfigured        (boolean)
     hasEbookSources      (boolean)
   ═══════════════════════════════════════════ */

(function () {
    'use strict';

    var isAttachEbook = PK_PAGE_DATA.isAttachEbook;
    var isAttachAudiobook = PK_PAGE_DATA.isAttachAudiobook;
    var isAttachFlow = isAttachEbook || isAttachAudiobook;
    var hasStorytellerSection = !!document.getElementById('storytellerSection');
    var storytellerForceMode = PK_PAGE_DATA.storytellerForceMode;
    var isPairingReview = PK_PAGE_DATA.isPairingReview;
    var pairingReady = PK_PAGE_DATA.pairingReady;
    var hasExistingEntryCollision = PK_PAGE_DATA.hasExistingEntryCollision;

    var absConfigured = PK_PAGE_DATA.absConfigured;
    var hasEbookSources = PK_PAGE_DATA.hasEbookSources;
    var currentMode = absConfigured ? 'match' : (hasEbookSources ? 'ebook' : 'match');
    var currentPhase = (currentMode === 'match') ? 'select-audio' : 'done';

    /* ── Remove from library ── */

    function showRemoveModal(absId, title) {
        var nextUrl = window.location.pathname + window.location.search;
        PKModal.confirmForm({
            title: 'Remove from Library',
            message: 'Remove "' + title + '" and all its cached files from your library?',
            formAction: '/delete/' + encodeURIComponent(String(absId)) + '?next=' + encodeURIComponent(nextUrl),
            confirmLabel: 'Remove',
            confirmClass: 'btn btn-danger'
        });
    }

    /* ── Mode / phase helpers ── */

    function setMode(mode) {
        currentMode = mode;
        currentPhase = (mode === 'match') ? 'select-audio' : 'done';

        clearSelections('audiobook_id');
        clearSelections('ebook_filename');
        clearSelections('storyteller_uuid');

        // Restore storyteller default (skip selected) and clear stale title
        var skipRadio = document.querySelector('.st-option.ghost-card input[type="radio"]');
        if (skipRadio) {
            skipRadio.checked = true;
            skipRadio.closest('.st-option').classList.add('selected');
        }
        var stTitleInput = document.getElementById('input_storyteller_title');
        if (stTitleInput) stTitleInput.value = '';

        // Adapt Storyteller hint and transcription visibility per mode
        var stHint = document.querySelector('.match-storyteller-toggle-hint');
        if (stHint) {
            stHint.textContent = (mode === 'ebook')
                ? 'Link to an existing Storyteller book'
                : 'Link to Storyteller for synced audio + text playback';
        }
        var stHelperText = document.getElementById('storytellerHelperText');
        if (stHelperText) {
            stHelperText.textContent = (mode === 'ebook')
                ? 'If this book already exists in Storyteller, select it to sync your reading position.'
                : 'If this book already exists in Storyteller, select it to sync your reading position and use its word-level timings for more precise audio-to-text alignment.';
        }
        var stSubmitOption = document.querySelector('.storyteller-submit-option');
        var stSubmitDetail = document.querySelector('.storyteller-submit-detail');
        var stDivider = document.querySelector('.storyteller-divider');
        if (stSubmitOption) stSubmitOption.style.display = (mode === 'ebook') ? 'none' : '';
        if (stSubmitDetail) stSubmitDetail.style.display = (mode === 'ebook') ? 'none' : '';
        if (stDivider) stDivider.style.display = (mode === 'ebook') ? 'none' : '';

        var stSubmitCheckbox = document.querySelector('input[type="checkbox"][name="storyteller_submit"]');
        var stSubmitHidden = document.querySelector('input[type="hidden"][name="storyteller_submit"]');
        if (stSubmitCheckbox && !storytellerForceMode) {
            stSubmitCheckbox.disabled = (mode === 'ebook');
            if (mode === 'ebook') stSubmitCheckbox.checked = false;
        }
        if (stSubmitHidden) stSubmitHidden.disabled = (mode === 'ebook');

        updateLayout();
        updateFooter();
    }

    function setPhase(phase) {
        currentPhase = phase;
        updateLayout();
        updateFooter();
    }

    function clearSelections(groupName) {
        var cls = '';
        if (groupName === 'audiobook_id') cls = '.ab-option';
        if (groupName === 'storyteller_uuid') cls = '.st-option';
        if (groupName === 'ebook_filename') cls = '.eb-option';
        if (cls) {
            document.querySelectorAll(cls).forEach(function (el) {
                el.classList.remove('selected');
                var r = el.querySelector('input[type="radio"]');
                if (r) r.checked = false;
            });
        }
        if (groupName === 'ebook_filename') {
            var sourceInput = document.getElementById('input_ebook_source_id');
            if (sourceInput) sourceInput.value = '';
        }
    }

    function updateLayout() {
        var audioSection = document.getElementById('audiobookSection');
        var ebookSection = document.getElementById('ebookSection');
        var stSection = document.getElementById('storytellerSection');
        var chip = document.getElementById('selectedChip');

        if (isPairingReview) {
            if (audioSection) audioSection.style.display = '';
            if (ebookSection) ebookSection.style.display = '';
            if (stSection) stSection.style.display = '';
            if (chip) chip.style.display = 'none';
            return;
        }

        if (isAttachFlow) {
            // Attach flows: show only the relevant section, no chip
            if (audioSection) audioSection.style.display = isAttachAudiobook ? '' : 'none';
            if (ebookSection) ebookSection.style.display = isAttachEbook ? '' : 'none';
            if (stSection) stSection.style.display = 'none';
            if (chip) chip.style.display = 'none';
            return;
        }

        if (currentMode === 'match') {
            if (currentPhase === 'select-audio') {
                if (audioSection) audioSection.style.display = '';
                if (ebookSection) ebookSection.style.display = 'none';
                if (stSection) stSection.style.display = 'none';
                if (chip) chip.style.display = 'none';
            } else {
                // select-ebook phase
                if (audioSection) audioSection.style.display = 'none';
                if (ebookSection) ebookSection.style.display = '';
                if (stSection) stSection.style.display = '';
                if (chip) chip.style.display = '';
                updateChip();
            }
        } else if (currentMode === 'audio') {
            if (audioSection) audioSection.style.display = '';
            if (ebookSection) ebookSection.style.display = 'none';
            if (stSection) stSection.style.display = 'none';
            if (chip) chip.style.display = 'none';
        } else if (currentMode === 'ebook') {
            if (audioSection) audioSection.style.display = 'none';
            if (ebookSection) ebookSection.style.display = '';
            if (stSection) stSection.style.display = '';
            if (chip) chip.style.display = 'none';
        }
    }

    function updateChip() {
        var selected = document.querySelector('.ab-option.selected');
        if (!selected) return;
        var title = selected.dataset.title || '';
        var cover = selected.dataset.cover || '';
        var chipCover = document.getElementById('chipCover');
        var chipTitle = document.getElementById('chipTitle');
        if (chipTitle) chipTitle.textContent = title;
        if (chipCover) {
            if (cover) {
                chipCover.src = cover;
                chipCover.style.display = '';
            } else {
                chipCover.style.display = 'none';
            }
        }
    }

    function updateFooter() {
        var btn = document.getElementById('actionBtn');
        var status = document.getElementById('actionStatus');
        if (!btn || !status) return;

        var ab = document.querySelector('input[name="audiobook_id"]:checked');
        var eb = document.querySelector('input[name="ebook_filename"]:checked');
        var st = document.querySelector('input[name="storyteller_uuid"]:checked');
        var hasAudio = !!ab;
        var ebVal = eb ? eb.value : '';
        var stVal = st ? st.value : '';
        var hasText = ebVal !== '' || stVal !== '';

        if (isPairingReview) {
            btn.type = 'submit';
            btn.disabled = !pairingReady;
            btn.textContent = hasExistingEntryCollision
                ? 'Combine existing entries and link'
                : 'Link formats';
            status.textContent = pairingReady ? 'Exact editions verified.' : 'Choose another companion to continue.';
            status.dataset.state = pairingReady ? 'ready' : 'warning';
            return;
        }

        // Summary chips
        var chips = document.getElementById('summaryChips');
        if (chips) {
            var showChips = !isAttachFlow && currentMode === 'match' && currentPhase === 'select-ebook';
            chips.style.display = showChips ? '' : 'none';
            if (showChips) {
                var truncate = function (s, n) { return s && s.length > n ? s.substring(0, n) + '\u2026' : (s || ''); };
                var chipAudio = document.getElementById('summaryAudio');
                var chipEbook = document.getElementById('summaryEbook');
                var chipSt = document.getElementById('summaryStoryteller');

                var abLabel = ab ? ab.closest('.ab-option') : null;
                var abTitle = abLabel ? (abLabel.dataset.title || '') : '';
                chipAudio.textContent = abTitle ? '\uD83C\uDFA7 ' + truncate(abTitle, 28) : '\uD83C\uDFA7 \u2014';
                chipAudio.dataset.empty = abTitle ? 'false' : 'true';

                var ebLabel = eb ? eb.closest('.eb-option') : null;
                var ebTitleEl = ebLabel ? ebLabel.querySelector('.resource-title') : null;
                var ebTitle = ebTitleEl ? ebTitleEl.textContent.trim() : '';
                chipEbook.textContent = ebVal ? '\uD83D\uDCD6 ' + truncate(ebTitle, 28) : '\uD83D\uDCD6 \u2014';
                chipEbook.dataset.empty = ebVal ? 'false' : 'true';

                if (hasStorytellerSection) {
                    var stLabel = st ? st.closest('.st-option') : null;
                    var stTitleEl = (stVal && stLabel) ? stLabel.querySelector('.resource-title') : null;
                    var stTitle = stTitleEl ? stTitleEl.textContent.trim() : '';
                    chipSt.textContent = stVal ? '\uD83D\uDCDA ' + truncate(stTitle, 28) : '\uD83D\uDCDA \u2014';
                    chipSt.dataset.empty = stVal ? 'false' : 'true';
                    chipSt.style.display = '';
                } else {
                    chipSt.style.display = 'none';
                }
            }
        }

        if (isAttachEbook) {
            var ready = ebVal !== '';
            btn.type = 'submit';
            btn.disabled = !ready;
            btn.textContent = 'Attach Ebook';
            status.textContent = ready ? 'eBook selected. Ready to attach.' : 'Select an ebook to attach.';
            status.dataset.state = ready ? 'ready' : 'warning';
            return;
        }

        if (isAttachAudiobook) {
            btn.type = 'submit';
            btn.disabled = !hasAudio;
            btn.textContent = 'Attach Audiobook';
            status.textContent = hasAudio ? 'Audiobook selected. Ready to attach.' : 'Select an audiobook to link.';
            status.dataset.state = hasAudio ? 'ready' : 'warning';
            return;
        }

        // Normal modes
        if (currentMode === 'match') {
            if (currentPhase === 'select-audio') {
                btn.type = 'button';
                btn.disabled = !hasAudio;
                btn.textContent = 'Continue';
                status.textContent = hasAudio ? 'Audiobook selected. Continue to ebooks.' : 'Select an audiobook to continue.';
                status.dataset.state = 'warning';
            } else {
                // select-ebook phase
                var ready = hasText;
                btn.type = 'submit';
                btn.disabled = !ready;
                btn.textContent = hasExistingEntryCollision
                    ? 'Combine existing entries and pair'
                    : 'Create Mapping';
                document.getElementById('input_action').value = '';
                status.textContent = ready
                    ? 'Ready to create mapping.'
                    : 'Select an ebook to complete the mapping.';
                status.dataset.state = ready ? 'ready' : 'warning';
            }
        } else if (currentMode === 'audio') {
            btn.type = 'submit';
            btn.disabled = !hasAudio;
            btn.textContent = 'Add Audio Only';
            document.getElementById('input_action').value = 'audio_only';
            status.textContent = hasAudio ? 'Ready to add audiobook.' : 'Select an audiobook.';
            status.dataset.state = hasAudio ? 'ready' : 'warning';
        } else if (currentMode === 'ebook') {
            btn.type = 'submit';
            btn.disabled = !hasText;
            btn.textContent = 'Add eBook';
            document.getElementById('input_action').value = 'ebook_only';
            status.textContent = hasText ? 'Ready to add ebook.' : 'Select an ebook.';
            status.dataset.state = hasText ? 'ready' : 'warning';
        }
    }

    function selectItem(element, groupName) {
        var wrapperClass = '';
        if (groupName === 'audiobook_id') wrapperClass = '.ab-option';
        if (groupName === 'storyteller_uuid') wrapperClass = '.st-option';
        if (groupName === 'ebook_filename') wrapperClass = '.eb-option';

        if (wrapperClass) {
            document.querySelectorAll(wrapperClass).forEach(function (el) {
                el.classList.remove('selected');
            });
        }

        element.classList.add('selected');
        var radio = element.querySelector('input[type="radio"]');
        if (radio) radio.checked = true;

        if (groupName === 'storyteller_uuid') {
            var titleEl = element.querySelector('.resource-title');
            var stInput = document.getElementById('input_storyteller_title');
            if (stInput) {
                stInput.value = (radio && radio.value) ? (titleEl ? titleEl.textContent.trim() : '') : '';
            }
        }
        if (groupName === 'ebook_filename') {
            var sourceInput = document.getElementById('input_ebook_source_id');
            if (sourceInput) sourceInput.value = element.dataset.grimmoryId || '';
            var searchEbook = document.getElementById('search_ebook_filename');
            var searchEbookSource = document.getElementById('search_ebook_source_id');
            if (searchEbook) searchEbook.value = radio ? radio.value : '';
            if (searchEbookSource) searchEbookSource.value = element.dataset.grimmoryId || '';
        }
        if (groupName === 'audiobook_id') {
            var searchAudiobook = document.getElementById('search_audiobook_id');
            if (searchAudiobook) searchAudiobook.value = radio ? radio.value : '';
        }

        updateFooter();
    }

    /* ── DOMContentLoaded ── */

    document.addEventListener('DOMContentLoaded', function () {
        var storytellerSection = document.getElementById('storytellerSection');
        var ebookSection = document.getElementById('ebookSection');
        if (storytellerSection && ebookSection) {
            ebookSection.insertAdjacentElement('afterend', storytellerSection);
        }

        document.querySelectorAll('.ab-option input[type="radio"], .eb-option input[type="radio"], .st-option input[type="radio"]').forEach(function (radio) {
            radio.addEventListener('change', function () {
                if (radio.checked) selectItem(radio.closest('label'), radio.name);
            });
        });

        // ── Attach flow: simple init ──
        if (isAttachFlow) {
            updateLayout();
            var initialEb = document.querySelector('input[name="ebook_filename"]:checked');
            if (initialEb) {
                var label = initialEb.closest('.eb-option');
                if (label) {
                    label.classList.add('selected');
                    var sourceInput = document.getElementById('input_ebook_source_id');
                    if (sourceInput) sourceInput.value = label.dataset.grimmoryId || '';
                }
            }
            var initialAb = document.querySelector('input[name="audiobook_id"]:checked');
            if (initialAb) {
                var label = initialAb.closest('.ab-option');
                if (label) label.classList.add('selected');
            }
            updateFooter();
            return;
        }

        // ── Mode selector ──
        var modeBar = document.getElementById('match-mode-bar');
        if (modeBar) {
            var modeBtns = modeBar.querySelectorAll('.r-match-mode-btn');
            modeBtns.forEach(function (btn) {
                btn.addEventListener('click', function () {
                    modeBtns.forEach(function (b) { b.classList.remove('active'); });
                    btn.classList.add('active');
                    setMode(btn.dataset.mode);
                });
            });
        }

        // ── Chip "Change" button ──
        var chipChange = document.getElementById('chipChange');
        if (chipChange) {
            chipChange.addEventListener('click', function () {
                clearSelections('ebook_filename');
                clearSelections('storyteller_uuid');
                // Restore storyteller skip and clear stale title
                var skipRadio = document.querySelector('.st-option.ghost-card input[type="radio"]');
                if (skipRadio) {
                    skipRadio.checked = true;
                    skipRadio.closest('.st-option').classList.add('selected');
                }
                var stTitleInput = document.getElementById('input_storyteller_title');
                if (stTitleInput) stTitleInput.value = '';
                setPhase('select-audio');
            });
        }

        // ── Handle preselected audiobook or single match ──
        var preselectedAb = document.querySelector('.ab-option.selected');
        if (preselectedAb) {
            if (currentMode === 'match' && !isPairingReview) {
                setPhase('select-ebook');
            }
        }
        var preselectedEb = document.querySelector('.eb-option.selected');
        if (preselectedEb) {
            var sourceInput = document.getElementById('input_ebook_source_id');
            if (sourceInput) sourceInput.value = preselectedEb.dataset.grimmoryId || '';
        }

        updateLayout();
        updateFooter();

        var actionBtn = document.getElementById('actionBtn');
        if (actionBtn) {
            actionBtn.addEventListener('click', function (event) {
                if (!isPairingReview && currentMode === 'match' && currentPhase === 'select-audio') {
                    event.preventDefault();
                    setPhase('select-ebook');
                    var firstEbook = document.querySelector('#ebookSection input[type="radio"]');
                    if (firstEbook) firstEbook.focus();
                }
            });
        }

        var mappingForm = document.getElementById('mappingForm');
        if (mappingForm) {
            mappingForm.addEventListener('submit', function () {
                if (actionBtn) {
                    actionBtn.disabled = true;
                    actionBtn.setAttribute('aria-disabled', 'true');
                    actionBtn.textContent = isPairingReview ? 'Linking…' : 'Saving…';
                }
                var actionStatus = document.getElementById('actionStatus');
                if (actionStatus) {
                    actionStatus.textContent = isPairingReview ? 'Linking selected formats…' : 'Saving selection…';
                    actionStatus.dataset.state = 'ready';
                }
            });
        }
    });

    /* ── Expose remove action used by the library badge ── */
    window.showRemoveModal = showRemoveModal;

})();
