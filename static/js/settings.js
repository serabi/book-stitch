/* PageKeeper — Settings Page */
/* global SETTINGS_CONFIG */

/* ─── Tab Switching ─── */
function switchTab(tabId) {
    document.querySelectorAll('.sidebar-tab').forEach(function(t) {
        t.classList.remove('active');
        t.setAttribute('aria-selected', 'false');
    });
    document.querySelectorAll('.tab-panel').forEach(function(p) {
        p.classList.remove('active');
        p.setAttribute('aria-hidden', 'true');
        p.hidden = true;
    });

    var tab = document.querySelector('[data-tab="' + tabId + '"]');
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    var panel = document.getElementById('panel-' + tabId);
    panel.classList.add('active');
    panel.setAttribute('aria-hidden', 'false');
    panel.hidden = false;

    document.getElementById('activeTabInput').value = tabId;

    panel.style.animation = 'none';
    panel.offsetHeight;
    panel.style.animation = '';

    if (window.innerWidth <= 768) {
        panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

function getInputValue(name) {
    var input = document.querySelector('[name="' + name + '"]') || document.getElementById(name);
    return input ? input.value.trim() : '';
}

function getServiceTestPayload(service) {
    if (service === 'abs') {
        return {
            server: getInputValue('ABS_SERVER'),
            token: getInputValue('ABS_KEY')
        };
    }
    if (service === 'storyteller') {
        return {
            api_url: getInputValue('STORYTELLER_API_URL'),
            user: getInputValue('STORYTELLER_USER'),
            password: getInputValue('STORYTELLER_PASSWORD')
        };
    }
    if (service === 'grimmory') {
        return {
            server: getInputValue('GRIMMORY_SERVER'),
            user: getInputValue('GRIMMORY_USER'),
            password: getInputValue('GRIMMORY_PASSWORD')
        };
    }
    if (service === 'grimmory2') {
        return {
            server: getInputValue('GRIMMORY_2_SERVER'),
            user: getInputValue('GRIMMORY_2_USER'),
            password: getInputValue('GRIMMORY_2_PASSWORD')
        };
    }
    if (service === 'cwa') {
        return {
            server: getInputValue('CWA_SERVER'),
            user: getInputValue('CWA_USERNAME'),
            password: getInputValue('CWA_PASSWORD')
        };
    }
    if (service === 'hardcover') {
        return {
            token: getInputValue('HARDCOVER_TOKEN')
        };
    }
    if (service === 'telegram') {
        return {
            bot_token: getInputValue('TELEGRAM_BOT_TOKEN'),
            chat_id: getInputValue('TELEGRAM_CHAT_ID')
        };
    }
    if (service === 'bookfusion') {
        return {
            api_key: getInputValue('BOOKFUSION_API_KEY')
        };
    }
    if (service === 'bookfusion_upload') {
        return {
            api_key: getInputValue('BOOKFUSION_UPLOAD_API_KEY')
        };
    }
    if (service === 'kosync') {
        return {
            server: getInputValue('kosync_external_url'),
            user: getInputValue('KOSYNC_SERVER_USER') || getInputValue('KOSYNC_USER'),
            key: getInputValue('kosync_server_key_input') || getInputValue('KOSYNC_KEY')
        };
    }
    return {};
}

function setTestButtonState(btn, success, detail, originalText) {
    if (success) {
        btn.textContent = '\u2713 ' + (detail || 'Connected');
        btn.classList.add('btn-success');
    } else {
        btn.textContent = '\u2717 ' + (detail || 'Failed');
        btn.classList.add('btn-danger');
    }
    setTimeout(function() {
        btn.textContent = originalText;
        btn.disabled = false;
        btn.classList.remove('btn-success', 'btn-danger');
    }, 3000);
}

/* ─── Test Connection ─── */
function testConnection(service, btn) {
    var originalText = btn.textContent;
    btn.textContent = 'Testing...';
    btn.disabled = true;
    fetch('/api/test-connection/' + service, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(getServiceTestPayload(service))
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            setTestButtonState(btn, data.success, data.detail, originalText);
        })
        .catch(function() {
            setTestButtonState(btn, false, 'Error', originalText);
        });
}

/* ─── Test KOSync External Connection ─── */
function testKosyncConnection(btn) {
    var originalText = btn.textContent;
    btn.textContent = 'Testing...';
    btn.disabled = true;
    fetch('/api/kosync/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(getServiceTestPayload('kosync'))
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var result = document.getElementById('kosync_test_result');
            result.textContent = data.success ? '\u2713 ' + (data.detail || 'Connected') : '\u2717 ' + (data.detail || 'Failed');
            result.className = 'test-result ' + (data.success ? 'success' : 'error');
            btn.disabled = false;
            btn.textContent = originalText;
        })
        .catch(function() {
            var result = document.getElementById('kosync_test_result');
            result.textContent = '\u2717 Error';
            result.className = 'test-result error';
            btn.disabled = false;
            btn.textContent = originalText;
        });
}

/* ─── BookFusion: extract token from obsidian:// URI ─── */
function extractBfToken(input) {
    var val = input.value;
    if (val.indexOf('obsidian://') === 0) {
        try {
            var params = new URLSearchParams(val.split('?')[1] || '');
            var token = params.get('token');
            if (token) input.value = token;
        } catch(e) {}
    }
}

/* ─── Grimmory Library Scanning ─── */
function fetchGrimmoryLibs(url, event) {
    var btn = event.target;
    var originalText = btn.textContent;
    btn.textContent = 'Scanning...';
    btn.disabled = true;

    fetch(url)
        .then(function(response) { return response.json(); })
        .then(function(data) {
            if (data.error) {
                PKModal.alert({ title: 'Error', message: data.error });
                return;
            }
            if (data.length === 0) {
                PKModal.alert({ title: 'No Libraries', message: 'No libraries found. Check connection or try syncing first.' });
                return;
            }

            var lines = data.map(function(lib) {
                return 'ID: ' + lib.id + '  \u2014  ' + lib.name;
            });
            PKModal.alert({ title: 'Found Libraries', message: lines.join('\n'), preserveWhitespace: true });
        })
        .catch(function(err) { PKModal.alert({ title: 'Error', message: 'Failed to fetch libraries: ' + err }); })
        .finally(function() {
            btn.textContent = originalText;
            btn.disabled = false;
        });
}

/* ─── Toggle helpers ─── */
function toggleSection(bodyId, isChecked) {
    var body = document.getElementById(bodyId);
    if (isChecked) {
        body.classList.remove('collapsed');
    } else {
        body.classList.add('collapsed');
    }
}

function togglePollSeconds(rowId, mode) {
    var row = document.getElementById(rowId);
    if (!row) return;
    if (mode === 'custom') {
        row.classList.remove('is-disabled');
    } else {
        row.classList.add('is-disabled');
    }
}

function toggleKosyncSourceMode() {
    var isBuiltin = document.getElementById('kosync_mode_builtin').checked;
    var hiddenInput = document.getElementById('kosync_server_input');
    var builtinSection = document.getElementById('kosync_builtin_section');
    var externalSection = document.getElementById('kosync_external_section');
    var externalUrl = document.getElementById('kosync_external_url');
    if (!hiddenInput || !builtinSection || !externalSection || !externalUrl) return;
    var builtinUrl = 'http://127.0.0.1:' + SETTINGS_CONFIG.kosyncPort;

    if (isBuiltin) {
        hiddenInput.value = builtinUrl;
        builtinSection.classList.remove('hidden');
        externalSection.classList.add('hidden');
    } else {
        builtinSection.classList.add('hidden');
        externalSection.classList.remove('hidden');
        if (externalUrl.value && externalUrl.value !== builtinUrl) {
            hiddenInput.value = externalUrl.value;
        } else {
            externalUrl.value = '';
            hiddenInput.value = '';
        }
    }
}

/* ─── Clipboard ─── */
function copyInputValue(inputId) {
    var copyText = document.getElementById(inputId);
    copyText.select();
    copyText.setSelectionRange(0, 99999);
    navigator.clipboard.writeText(copyText.value);
    copyText.classList.add('input-copied');
    setTimeout(function() { copyText.classList.remove('input-copied'); }, 200);
}



/* ─── Tool Actions ─── */
function syncReadingDates(btn) {
    btn.disabled = true;
    btn.textContent = 'Syncing\u2026';
    fetch('/api/sync-reading-dates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                var parts = [];
                if (data.updated) parts.push(data.updated + ' updated');
                if (data.completed) parts.push(data.completed + ' newly completed');
                if (data.errors) parts.push(data.errors + ' errors');
                PKModal.alert({ title: 'Sync Complete', message: parts.length ? parts.join(', ') + '.' : 'All reading dates are already up to date.' });
            } else {
                PKModal.alert({ title: 'Error', message: 'Failed: ' + (data.error || 'Unknown error') });
            }
        })
        .catch(function(err) {
            console.error('Error syncing reading dates:', err);
            PKModal.alert({ title: 'Error', message: 'An error occurred. Check console for details.' });
        })
        .finally(function() {
            btn.disabled = false;
            btn.textContent = 'Sync Reading Dates';
        });
}

/* ─── ABS Library Picker ─── */
async function fetchAbsLibraries() {
    var listEl = document.getElementById('abs_library_list');
    var hiddenInput = document.getElementById('abs_library_ids_input');
    listEl.textContent = 'Loading...';
    try {
        var r = await fetch('/api/abs/libraries');
        var libs = await r.json();
        if (libs.error) {
            listEl.textContent = libs.error;
            listEl.className = 'status-text status-text-error';
            return;
        }
        var currentIds = (hiddenInput.value || '').split(',').map(function(s) { return s.trim(); }).filter(Boolean);
        listEl.textContent = '';
        listEl.className = '';
        libs.forEach(function(lib) {
            var div = document.createElement('div');
            div.className = 'settings-option-row';
            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.id = 'abs-lib-' + lib.id;
            cb.value = lib.id;
            cb.checked = currentIds.includes(lib.id);
            cb.addEventListener('change', updateAbsLibraryIds);
            var lbl = document.createElement('label');
            lbl.className = 'settings-option-label';
            lbl.htmlFor = 'abs-lib-' + lib.id;
            lbl.textContent = lib.name + ' (' + lib.mediaType + ')';
            div.appendChild(cb);
            div.appendChild(lbl);
            listEl.appendChild(div);
        });
        if (libs.length === 0) {
            listEl.textContent = 'No libraries found';
            listEl.className = 'status-text status-text-muted';
        }
    } catch (e) {
        listEl.textContent = 'Failed to fetch: ' + e.message;
        listEl.className = 'status-text status-text-error';
    }
}

function updateAbsLibraryIds() {
    var checkboxes = document.querySelectorAll('#abs_library_list input[type="checkbox"]:checked');
    var ids = Array.from(checkboxes).map(function(cb) { return cb.value; });
    document.getElementById('abs_library_ids_input').value = ids.join(',');
}

/* ─── Dynamic Provider / Device Forms ─── */
function toggleProviderSettings() {
    var provider = document.querySelector('select[name="TRANSCRIPTION_PROVIDER"]').value;

    var groupLocal = document.getElementById('group_local');
    var groupDeepgram = document.getElementById('group_deepgram');
    var groupWhisperCpp = document.getElementById('group_whispercpp');
    var groupWhisperModel = document.getElementById('group_whisper_model');

    if (groupLocal) groupLocal.classList.add('hidden');
    if (groupDeepgram) groupDeepgram.classList.add('hidden');
    if (groupWhisperCpp) groupWhisperCpp.classList.add('hidden');
    if (groupWhisperModel) groupWhisperModel.classList.add('hidden');

    if (provider === 'local') {
        if (groupLocal) groupLocal.classList.remove('hidden');
        if (groupWhisperModel) groupWhisperModel.classList.remove('hidden');
    } else if (provider === 'deepgram' && groupDeepgram) {
        groupDeepgram.classList.remove('hidden');
    } else if (provider === 'whispercpp') {
        if (groupWhisperCpp) groupWhisperCpp.classList.remove('hidden');
        if (groupWhisperModel) groupWhisperModel.classList.remove('hidden');
    }
}

function initDynamicForms() {
    var providerSelect = document.querySelector('select[name="TRANSCRIPTION_PROVIDER"]');
    if (providerSelect) {
        providerSelect.addEventListener('change', toggleProviderSettings);
        toggleProviderSettings();
    }

    var deviceSelect = document.querySelector('select[name="WHISPER_DEVICE"]');
    if (deviceSelect) {
        deviceSelect.addEventListener('change', function () {
            if (this.value === 'cuda') {
                PKModal.alert({ title: 'NVIDIA GPU', message: 'To use an NVIDIA GPU, you must modify your docker-compose.yml to include the \'deploy\' block with \'capabilities: [gpu]\' and ensure the NVIDIA Container Toolkit is installed on your host.' });
            }
        });
    }
}

/* ─── Password Visibility Toggle ─── */
function togglePasswordVisibility(inputId, btn) {
    var input = document.getElementById(inputId);
    if (input.type === 'password') {
        var secretKey = input.dataset.secretKey;
        if (secretKey && !input.dataset.fetched && !input.value) {
            btn.textContent = '...';
            btn.disabled = true;
            fetch('/api/settings/secret/' + secretKey)
                .then(function(r) {
                    if (!r.ok) throw new Error('Failed to fetch secret');
                    return r.json();
                })
                .then(function(data) {
                    if (data && data.value) {
                        input.value = data.value;
                        input.placeholder = '';
                        input.dataset.fetched = 'true';
                        input.type = 'text';
                        btn.textContent = 'Hide';
                    } else {
                        input.type = 'password';
                        delete input.dataset.fetched;
                        btn.textContent = 'Show';
                    }
                    btn.disabled = false;
                })
                .catch(function() {
                    input.type = 'password';
                    delete input.dataset.fetched;
                    btn.textContent = 'Show';
                    btn.disabled = false;
                });
            return;
        }
        input.type = 'text';
        btn.textContent = 'Hide';
    } else {
        input.type = 'password';
        btn.textContent = 'Show';
    }
}

/* ─── Unsaved Changes Guard ─── */
var isFormDirty = false;

function markDirty() {
    isFormDirty = true;
}

function setupDirtyCheck() {
    var inputs = document.querySelectorAll('input, select, textarea');
    inputs.forEach(function(input) {
        input.addEventListener('change', markDirty);
        input.addEventListener('input', markDirty);
    });

    window.addEventListener('beforeunload', function (e) {
        if (isFormDirty) {
            var msg = 'You have unsaved changes. Are you sure you want to leave?';
            e.returnValue = msg;
            return msg;
        }
    });

    var timezone = document.getElementById('timezone');
    if (timezone) {
        timezone.addEventListener('input', function () { timezone.setCustomValidity(''); });
    }

    var form = document.querySelector('form');
    if (form) {
        form.addEventListener('submit', function (event) {
            if (timezone && !timezone.value.trim()) {
                event.preventDefault();
                switchTab('general');
                timezone.setCustomValidity('Enter an IANA timezone such as America/New_York.');
                timezone.focus();
                timezone.reportValidity();
                return;
            }
            if (timezone) timezone.setCustomValidity('');
            isFormDirty = false;
        });
    }
}

/* ─── Init ─── */
document.addEventListener('DOMContentLoaded', function () {
    var params = new URLSearchParams(window.location.search);
    var savedTab = params.get('tab');
    if (savedTab && document.getElementById('panel-' + savedTab)) {
        switchTab(savedTab);
    }
    var focusTarget = document.getElementById(params.get('focus'));
    if (focusTarget) focusTarget.focus();

    var configuredPort = SETTINGS_CONFIG.kosyncPort;
    var lanAddress = window.location.protocol + '//' + window.location.hostname + ':' + configuredPort;

    var lanInput = document.getElementById('kosync_lan_address');
    if (lanInput) lanInput.value = lanAddress;

    var publicUrlInput = document.getElementById('public_kosync_url');
    if (publicUrlInput && !publicUrlInput.value) {
        publicUrlInput.placeholder = lanAddress;
    }

    initDynamicForms();

    var builtinUrl = 'http://127.0.0.1:' + configuredPort;
    var kosyncServerVal = document.getElementById('kosync_server_input').value;
    var builtinRadio = document.getElementById('kosync_mode_builtin');
    var externalRadio = document.getElementById('kosync_mode_external');
    var externalUrl = document.getElementById('kosync_external_url');
    if (kosyncServerVal === builtinUrl || kosyncServerVal === '') {
        builtinRadio.checked = true;
        externalRadio.checked = false;
    } else {
        builtinRadio.checked = false;
        externalRadio.checked = true;
        if (externalUrl) externalUrl.value = kosyncServerVal;
    }
    toggleKosyncSourceMode();

    if (externalUrl) {
        externalUrl.addEventListener('input', function() {
            document.getElementById('kosync_server_input').value = this.value;
        });
    }

    setupDirtyCheck();

    // BookFusion re-sync all button handler
    var bfResyncBtn = document.getElementById('bf-resync-all-btn');
    if (bfResyncBtn) {
        bfResyncBtn.addEventListener('click', function() {
            if (!confirm('This will re-download all highlights from BookFusion. Continue?')) {
                return;
            }
            bfResyncBtn.classList.remove('error', 'done');
            bfResyncBtn.disabled = true;
            bfResyncBtn.textContent = 'Syncing...';
            fetch('/api/bookfusion/sync-highlights', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ full_resync: true })
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    bfResyncBtn.classList.remove('error');
                    bfResyncBtn.textContent = 'Synced (' + data.new_highlights + ' new)';
                    bfResyncBtn.classList.add('done');
                } else {
                    bfResyncBtn.classList.remove('done');
                    bfResyncBtn.textContent = data.error || 'Failed';
                    bfResyncBtn.classList.add('error');
                }
            })
            .catch(function() {
                bfResyncBtn.classList.remove('done');
                bfResyncBtn.textContent = 'Error';
                bfResyncBtn.classList.add('error');
            })
            .finally(function() {
                bfResyncBtn.disabled = false;
            });
        });
    }
});

/* ─── Libby pairing ─── */
function libbyConnect(btn) {
    var codeInput = document.getElementById('libby_setup_code');
    libbyPair(btn, { code: (codeInput ? codeInput.value : '').trim() }, 'Connecting...');
}

function libbyConnectToken(btn) {
    var tokenInput = document.getElementById('libby_identity_token');
    libbyPair(btn, { token: (tokenInput ? tokenInput.value : '').trim() }, 'Verifying...');
}

function libbyPair(btn, body, busyText) {
    var originalText = btn.textContent;
    var result = document.getElementById('libby_connect_result');
    btn.textContent = busyText;
    btn.disabled = true;
    if (result) {
        result.textContent = '';
        result.classList.remove('field-error');
    }
    fetch('/api/libby/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
        .then(function(r) { return r.json().then(function(data) { return { status: r.status, data: data }; }); })
        .then(function(resp) {
            if (!resp.data.success) {
                btn.textContent = originalText;
                btn.disabled = false;
                if (result) {
                    result.textContent = resp.data.detail || 'Pairing failed.';
                    result.className = 'help-text field-error';
                }
                return;
            }
            var cards = resp.data.cards || [];
            var lines = cards.map(function(c) {
                return '\u2022 ' + (c.name || c.id) + (c.library ? ' \u2014 ' + c.library : '');
            });
            var progressNote = resp.data.can_read_positions
                ? 'Progress sync is active.'
                : 'Loans & TBR tracking active. Add a browser token to enable progress sync.';
            PKModal.alert({
                title: 'Libby Connected',
                message: (cards.length ? 'Linked card(s):\n' + lines.join('\n') : 'Paired successfully.') +
                    '\n\n' + progressNote,
                preserveWhitespace: true
            });
            window.location.reload();
        })
        .catch(function() {
            btn.textContent = originalText;
            btn.disabled = false;
            if (result) {
                result.textContent = 'Error reaching Libby.';
                result.className = 'help-text field-error';
            }
        });
}

function libbyDisconnect(btn) {
    var doDisconnect = function() {
        btn.disabled = true;
        btn.textContent = 'Disconnecting...';
        fetch('/api/libby/disconnect', { method: 'POST', headers: { 'Content-Type': 'application/json' } })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    window.location.reload();
                } else {
                    btn.disabled = false;
                    btn.textContent = 'Disconnect';
                    var result = document.getElementById('libby_connect_result');
                    if (result) {
                        result.textContent = data.detail || 'Disconnect failed.';
                        result.className = 'help-text field-error';
                    }
                }
            })
            .catch(function() {
                btn.disabled = false;
                btn.textContent = 'Disconnect';
            });
    };
    PKModal.confirm({
        title: 'Disconnect Libby',
        message: "This revokes the PageKeeper identity chip on Libby's servers and clears the stored token. Continue?",
        confirmLabel: 'Disconnect',
        confirmClass: 'btn btn-danger',
        onConfirm: doDisconnect
    });
}
