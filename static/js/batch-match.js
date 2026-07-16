/* ═══════════════════════════════════════════
   PageKeeper — batch match page
   ═══════════════════════════════════════════ */

var selectionState = {
    audiobook: null,
    storyteller: null,
    ebook: null,
    ebookSourceId: '',
    ebookDisplayName: '',
};

function applySelection(card) {
    var group = card.dataset.selectGroup;
    var value = card.dataset.value || '';
    var targetInputId = card.dataset.targetInput;
    var targetInput = document.getElementById(targetInputId);
    if (!targetInput) return;

    document.querySelectorAll('[data-select-group="' + group + '"]').forEach(function (el) {
        el.classList.remove('selected');
    });

    card.classList.add('selected');
    targetInput.value = value;

    if (group === 'audiobook') {
        selectionState.audiobook = value || null;
    } else if (group === 'storyteller') {
        selectionState.storyteller = value || null;
    } else if (group === 'ebook') {
        selectionState.ebook = value || null;
        selectionState.ebookSourceId = card.dataset.sourceId || '';
        document.getElementById('selected_ebook_source_id').value = selectionState.ebookSourceId;
        selectionState.ebookDisplayName = card.dataset.displayName || value;
        var displayInput = document.getElementById(card.dataset.displayInput);
        if (displayInput) {
            displayInput.value = selectionState.ebookDisplayName;
        }
    }

    if (group !== 'ebook' && !selectionState.ebook) {
        var displayNameInput = document.getElementById('selected_ebook_display_name');
        if (displayNameInput) displayNameInput.value = '';
    }

    updateBatchActionState();
}

function updateBatchActionState() {
    var addButton = document.getElementById('addToQueueBtn');
    var statusLabel = document.getElementById('selectionStatus');
    if (!addButton || !statusLabel) return;
    var hasAudiobook = Boolean(selectionState.audiobook);
    var hasEbook = Boolean(selectionState.ebook);
    var hasStoryteller = Boolean(selectionState.storyteller);
    var hasLinkedSource = hasEbook || hasStoryteller;
    var hasAnything = hasAudiobook || hasLinkedSource;

    addButton.disabled = !hasAnything;

    if (hasAudiobook && !hasLinkedSource) {
        addButton.textContent = 'Add Audio Only to Queue';
    } else if (!hasAudiobook && hasEbook) {
        addButton.textContent = 'Add Ebook Only to Queue';
    } else if (!hasAudiobook && hasStoryteller && !hasEbook) {
        addButton.textContent = 'Add Storyteller Only to Queue';
    } else {
        addButton.textContent = 'Add to Queue';
    }

    if (!hasAnything) {
        statusLabel.textContent = 'Select a book to enable queueing.';
        return;
    }

    if (!hasAudiobook && hasStoryteller && !hasEbook) {
        statusLabel.textContent = 'Queue will be created as Storyteller-only.';
        return;
    }

    if (!hasAudiobook) {
        statusLabel.textContent = 'Queue will be created as ebook-only.';
        return;
    }

    if (!hasLinkedSource) {
        statusLabel.textContent = 'Queue will be created as audio-only.';
        return;
    }

    if (hasStoryteller && hasEbook) {
        statusLabel.textContent = 'Queue will include both Storyteller and ebook.';
        return;
    }

    if (hasStoryteller) {
        statusLabel.textContent = 'Queue will use Storyteller as the linked source.';
        return;
    }

    statusLabel.textContent = 'Queue will use the selected ebook source.';
}

(function initBatchMatch() {
    document.querySelectorAll('.batch-select-card').forEach(function (card) {
        card.addEventListener('click', function () { applySelection(card); });
    });

    var preselectedAudiobook = document.querySelector('[data-select-group="audiobook"].selected');
    if (preselectedAudiobook) {
        selectionState.audiobook = preselectedAudiobook.dataset.value || null;
        document.getElementById('selected_audiobook_id').value = selectionState.audiobook || '';
    }

    var preselectedStoryteller = document.querySelector('[data-select-group="storyteller"].selected');
    if (preselectedStoryteller) {
        selectionState.storyteller = preselectedStoryteller.dataset.value || null;
        document.getElementById('selected_storyteller_uuid').value = preselectedStoryteller.dataset.value || '';
    }

    var preselectedEbook = document.querySelector('[data-select-group="ebook"].selected');
    if (preselectedEbook) {
        selectionState.ebook = preselectedEbook.dataset.value || null;
        selectionState.ebookSourceId = preselectedEbook.dataset.sourceId || '';
        selectionState.ebookDisplayName = preselectedEbook.dataset.displayName || selectionState.ebook || '';
        document.getElementById('selected_ebook_filename').value = selectionState.ebook || '';
        document.getElementById('selected_ebook_source_id').value = selectionState.ebookSourceId;
        document.getElementById('selected_ebook_display_name').value = selectionState.ebookDisplayName;
    }

    updateBatchActionState();
})();
