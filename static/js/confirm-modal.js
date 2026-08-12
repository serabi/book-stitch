/* ═══════════════════════════════════════════
   PageKeeper — unified confirm modal
   ═══════════════════════════════════════════
   Requires the #pk-confirm-modal partial
   (templates/partials/confirm_modal.html).

   Usage:

     // JS callback mode
     PKModal.confirm({
       title: 'Delete Book',
       message: 'Are you sure?',
       confirmLabel: 'Delete',          // optional, default 'Confirm'
       confirmClass: 'btn btn-danger',  // optional, default 'btn btn-warning'
       onConfirm: function() { ... }
     });

     // Form POST mode
     PKModal.confirmForm({
       title: 'Clear Progress',
       message: 'Clear all progress?',
       formAction: '/clear-progress/123',
       hiddenFields: { action: 'clear_queue' },  // optional
       confirmLabel: 'Clear',
       confirmClass: 'btn btn-warning'
     });

     // Alert / info mode (OK button only)
     PKModal.alert({
       title: 'Success',
       message: 'Operation complete.'
     });

     PKModal.close();
   ═══════════════════════════════════════════ */

var PKModal = (function () {
    'use strict';

    /* ── cached DOM refs (resolved lazily) ── */
    var _modal, _dialog, _icon, _title, _message, _cancelBtn, _confirmBtn, _form, _hiddenContainer;
    var _onConfirmCallback = null;
    var _opener = null;

    function _el(id) { return document.getElementById(id); }

    function _resolve() {
        if (_modal) return;
        _modal = _el('pk-confirm-modal');
        if (!_modal) {
            console.error('PKModal: #pk-confirm-modal not found — is confirm_modal.html included?');
            return;
        }
        _dialog         = _el('pk-modal-dialog');
        _icon           = _el('pk-modal-icon');
        _title          = _el('pk-modal-title');
        _message        = _el('pk-modal-message');
        _cancelBtn      = _el('pk-modal-cancel');
        _confirmBtn     = _el('pk-modal-confirm');
        _form           = _el('pk-modal-form');
        _hiddenContainer = _el('pk-modal-hidden-fields');
    }

    /* ── internal helpers ── */

    function _clearChildren(el) {
        while (el.firstChild) el.removeChild(el.firstChild);
    }

    function _setIcon(accentClass) {
        _icon.className = 'confirm-modal-icon';
        _icon.textContent = '\u26A0';
        if (accentClass) _icon.classList.add(accentClass);
    }

    function _accentClassFromBtn(btnClass) {
        if (!btnClass) return 'confirm-icon-warning';
        if (btnClass.indexOf('danger') !== -1) return 'confirm-icon-danger';
        return 'confirm-icon-warning';
    }

    function _open() {
        _opener = document.activeElement;
        _modal.style.display = 'flex';
        setTimeout(function () {
            var focusable = _focusableElements();
            (focusable[0] || _dialog).focus();
        }, 0);
    }

    function _focusableElements() {
        if (!_dialog) return [];
        return Array.prototype.filter.call(
            _dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]'),
            function (element) {
                return element.style.display !== 'none' && (!_form.contains(element) || _form.style.display !== 'none');
            }
        );
    }

    function _handleConfirmClick() {
        if (_onConfirmCallback) {
            var cb = _onConfirmCallback;
            close();
            cb();
        }
    }

    /* ── public API ── */

    /**
     * JS callback mode — shows modal, calls onConfirm when confirmed.
     */
    function confirm(opts) {
        _resolve();
        if (!_modal) return;
        var confirmClass = opts.confirmClass || 'btn btn-warning';

        _setIcon(_accentClassFromBtn(confirmClass));
        _title.textContent   = opts.title || 'Confirm';
        _message.textContent = opts.message || '';

        /* Cancel button */
        _cancelBtn.textContent = 'Cancel';
        _cancelBtn.style.display = '';

        /* Confirm button (plain button, not form submit) */
        _confirmBtn.style.display = '';
        _confirmBtn.className = confirmClass;
        _confirmBtn.textContent = opts.confirmLabel || 'Confirm';
        _confirmBtn.type = 'button';
        _onConfirmCallback = opts.onConfirm || null;

        /* Hide form */
        _form.style.display = 'none';

        _open();
    }

    /**
     * Form POST mode — shows modal, submits form on confirm.
     */
    function confirmForm(opts) {
        _resolve();
        if (!_modal) return;
        var confirmClass = opts.confirmClass || 'btn btn-warning';

        _setIcon(_accentClassFromBtn(confirmClass));
        _title.textContent   = opts.title || 'Confirm';
        _message.textContent = opts.message || '';

        /* Cancel button */
        _cancelBtn.textContent = 'Cancel';
        _cancelBtn.style.display = '';

        /* Hide plain confirm button */
        _confirmBtn.style.display = 'none';
        _onConfirmCallback = null;

        /* Configure form */
        _form.style.display = '';
        _form.action = opts.formAction || '';

        /* Hidden fields — clear old ones safely, add new */
        _clearChildren(_hiddenContainer);
        if (opts.hiddenFields) {
            Object.keys(opts.hiddenFields).forEach(function (name) {
                var input = document.createElement('input');
                input.type = 'hidden';
                input.name = name;
                input.value = opts.hiddenFields[name];
                _hiddenContainer.appendChild(input);
            });
        }

        /* Submit button inside form */
        var submitBtn = _form.querySelector('button[type="submit"]');
        submitBtn.className = confirmClass;
        submitBtn.textContent = opts.confirmLabel || opts.title || 'Confirm';

        _open();
    }

    /**
     * Alert / info mode — OK button only, no confirm action.
     */
    function alert(opts) {
        _resolve();
        if (!_modal) return;

        _icon.className = 'confirm-modal-icon';
        _icon.textContent = '';

        _title.textContent   = opts.title || '';
        _message.textContent = opts.message || '';
        if (opts.preserveWhitespace) {
            _message.style.whiteSpace = 'pre-line';
        } else {
            _message.style.whiteSpace = '';
        }

        /* Only OK button */
        _cancelBtn.textContent = 'OK';
        _cancelBtn.style.display = '';

        _confirmBtn.style.display = 'none';
        _form.style.display = 'none';
        _onConfirmCallback = null;

        _open();
    }

    /**
     * Close the modal.
     */
    function close() {
        _resolve();
        if (!_modal) return;
        _modal.style.display = 'none';
        _onConfirmCallback = null;
        _message.style.whiteSpace = '';
        if (_opener && _opener.isConnected && typeof _opener.focus === 'function') {
            _opener.focus();
        }
        _opener = null;
    }

    /* ── keyboard support ── */
    document.addEventListener('keydown', function (e) {
        if (!_modal || _modal.style.display === 'none') return;
        if (e.key === 'Escape') {
            close();
        } else if (e.key === 'Tab') {
            var focusable = _focusableElements();
            if (!focusable.length) {
                e.preventDefault();
                _dialog.focus();
                return;
            }
            var first = focusable[0];
            var last = focusable[focusable.length - 1];
            if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
            }
        }
    });

    /* ── delegate click on confirm button ── */
    document.addEventListener('click', function (e) {
        if (e.target && e.target.id === 'pk-modal-confirm') {
            _handleConfirmClick();
        }
    });

    return {
        confirm:     confirm,
        confirmForm: confirmForm,
        alert:       alert,
        close:       close
    };
})();

/* ─── Legacy bridge ───
   Maps the old showConfirmModal(title, message, formAction, accentType)
   signature used in book_card.html and batch_match.html to PKModal.
   Remove once all call-sites are migrated.
   ─────────────────────────────────────────── */
function showConfirmModal(title, message, formAction, accentType) {
    PKModal.confirmForm({
        title: title,
        message: message,
        formAction: formAction,
        confirmLabel: title,
        confirmClass: accentType === 'danger' ? 'btn btn-danger' : 'btn btn-warning'
    });
}

function closeConfirmModal() {
    PKModal.close();
}
