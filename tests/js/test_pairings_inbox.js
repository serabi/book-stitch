'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const handlers = {};
const dismissHandlers = {};
const button = {
    disabled: false,
    addEventListener: function (event, handler) { handlers[event] = handler; }
};
const status = { textContent: '' };
let cardRemoved = false;
let caughtUpAppended = false;
let caughtUpFocused = false;
let badgeRemoved = false;
const badge = { textContent: '1', remove: function () { badgeRemoved = true; } };
const inbox = { appendChild: function () { caughtUpAppended = true; } };
const section = {
    querySelectorAll: function () { return []; },
    querySelector: function () { return { textContent: '' }; },
    remove: function () {}
};
const card = {
    dataset: { source: 'kosync', sourceId: 'shared:id' },
    closest: function () { return section; },
    querySelector: function () { return { textContent: 'Dismiss Me' }; },
    remove: function () { cardRemoved = true; }
};
const dismissButton = {
    disabled: false,
    addEventListener: function (event, handler) { dismissHandlers[event] = handler; },
    closest: function () { return card; }
};
const responses = [
    { success: true, running: false, phase: 'complete', message: 'Earlier rescan complete.' }
];
let reloads = 0;
const fetchedUrls = [];

function response(data) {
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve(data); } });
}

const context = {
    console: console,
    document: {
        getElementById: function (id) { return id === 'rescan-btn' ? button : status; },
        querySelectorAll: function (selector) {
            if (selector === '.pairing-dismiss') return [dismissButton];
            if (selector === '.pairing-card') return [card];
            if (selector === '.nav-badge') return [badge];
            return [];
        },
        querySelector: function (selector) { return selector === '.pairings-inbox' ? inbox : null; },
        createElement: function () {
            return {
                className: '',
                innerHTML: '',
                setAttribute: function () {},
                querySelector: function () {
                    return { focus: function () { caughtUpFocused = true; } };
                }
            };
        }
    },
    fetch: function (url) {
        fetchedUrls.push(url);
        return response(responses.shift());
    },
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    window: {
        PK_PAGE_DATA: { pairingsInbox: true },
        location: { reload: function () { reloads += 1; } }
    }
};

function flush() {
    return new Promise(function (resolve) { setImmediate(resolve); });
}

(async function () {
    const script = fs.readFileSync('static/js/suggestions.js', 'utf8');
    vm.runInNewContext(script, context);
    await flush();
    await flush();
    assert.equal(reloads, 0, 'a stale complete status must not reload the page');

    responses.push(
        { success: true, running: true, phase: 'queued', message: 'Queued.' },
        { success: true, running: false, phase: 'complete', message: 'Complete.' }
    );
    handlers.click();
    await flush();
    await flush();
    await flush();
    assert.equal(reloads, 1, 'a page-initiated rescan reloads exactly once after completion');

    responses.push(
        { success: true, running: true, phase: 'queued', message: 'Queued.' },
        { success: true, running: false, phase: 'partial', message: 'Partial.' }
    );
    handlers.click();
    await flush();
    await flush();
    await flush();
    assert.equal(reloads, 2, 'a partial rescan reloads successful source results');
    assert.equal((script.match(/data\.phase === 'partial'/g) || []).length, 2,
        'both inbox and catalog polling treat partial as terminal');

    responses.push({ success: true });
    dismissHandlers.click();
    await flush();
    await flush();
    assert.equal(fetchedUrls[fetchedUrls.length - 1], '/api/detected/kosync/shared%3Aid/dismiss');
    assert.equal(cardRemoved, true);
    assert.equal(status.textContent, 'Dismissed Dismiss Me.');
    assert.equal(badgeRemoved, true);
    assert.equal(caughtUpAppended, true);
    assert.equal(caughtUpFocused, true);
    console.log('pairings inbox JS checks passed');
})().catch(function (error) {
    console.error(error);
    process.exitCode = 1;
});
