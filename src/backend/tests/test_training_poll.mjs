import {strict as assert} from "node:assert";
import {test} from "node:test";
import {bindStopControls, refreshStatus} from "../apps/preferences/static/preferences/model-training.mjs";

test("keeps the latest state during an outage and stops after completion", async () => {
  const callbacks = [];
  const events = [];
  const responses = [
    {ok: true, json: async () => ({label: "실행중", terminal: false})},
    {ok: false, status: 503},
    {ok: true, json: async () => ({label: "실행 완료", terminal: true})},
  ];
  await refreshStatus("/status/", {
    request: async () => responses.shift(),
    update: state => events.push(state.label),
    delayed: () => events.push("조회 지연"),
    unavailable: () => assert.fail("Run became unavailable"),
    schedule: (callback, delay) => {
      assert.equal(delay, 5000);
      callbacks.push(callback);
    },
  });
  await callbacks.shift()();
  await callbacks.shift()();
  assert.deepEqual(events, ["실행중", "조회 지연", "실행 완료"]);
  assert.equal(callbacks.length, 0);
});

test("stops when the session or company can no longer access the run", async () => {
  let unavailable = false;
  await refreshStatus("/status/", {
    request: async () => ({status: 403}),
    update: () => assert.fail("Updated an unauthorized run"),
    delayed: () => assert.fail("Treated revoked access as a temporary outage"),
    unavailable: () => { unavailable = true; },
    schedule: () => assert.fail("Scheduled another status request"),
  });
  assert.equal(unavailable, true);
});

function stopFixture(request) {
  const nodes = new Map();
  function find(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {
      querySelector: find,
      dataset: {stopUrl: "/stop/"},
      elements: {csrfmiddlewaretoken: {value: "test-csrf"}},
      textContent: "",
      addEventListener(event, listener) { this[event] = listener; },
      showModal() { this.open = true; },
      close() { this.open = false; },
      focus() { this.focused = true; },
    });
    return nodes.get(selector);
  }
  const control = bindStopControls({querySelector: find}, () => control.clear(), request);
  return {control, open: find("[data-stop-open]"), confirm: find("[data-stop-confirm]"),
    cancel: find("[data-stop-continue]"), status: find("[data-stop-state]"), dialog: find("#training-stop-dialog")};
}

test("only explicit confirmation submits; a stale poll and duplicate click cannot reopen the button", async () => {
  const calls = [];
  let reply;
  const ui = stopFixture((url, options) => {
    calls.push({url, options});
    return new Promise(resolve => { reply = resolve; });
  });
  const active = {terminal: false, stop: {state: "available"}};
  ui.control.update(active);
  assert.equal(ui.open.hidden, false);
  ui.open.click();
  assert.equal(ui.dialog.open, true);
  assert.equal(ui.cancel.focused, true);
  ui.dialog.close();
  await ui.confirm.click();
  assert.equal(calls.length, 0);

  ui.open.click();
  const pending = ui.confirm.click();
  await ui.confirm.click();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/stop/");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["X-CSRFToken"], "test-csrf");
  assert.equal(ui.open.hidden, true);
  reply({ok: true, status: 202, json: async () => ({stop: {state: "requested"}})});
  await pending;
  ui.control.update(active);
  assert.equal(ui.status.textContent, "중지 요청 중");
  ui.control.update({terminal: true, stop: {state: "stopped"}});
  assert.equal(ui.status.textContent, "중단됨");
  assert.equal(ui.open.hidden, true);
});

test("closing an active dialog when the run finishes prevents submitting a stale confirmation", async () => {
  const ui = stopFixture(() => assert.fail("Stopped a finished run"));
  ui.control.update({terminal: false, stop: {state: "available"}});
  ui.open.click();
  ui.control.update({terminal: true, stop: {state: "completed"}});
  assert.equal(ui.dialog.open, false);
  await ui.confirm.click();
  assert.equal(ui.open.hidden, true);
});

test("a duplicate POST returning a confirmed stop stays terminal across an older active poll", async () => {
  const ui = stopFixture(async () => ({ok: true, status: 200, json: async () => ({stop: {state: "stopped"}})}));
  const active = {terminal: false, stop: {state: "available"}};
  ui.control.update(active);
  ui.open.click();
  await ui.confirm.click();
  ui.control.update(active);
  assert.equal(ui.status.textContent, "중단됨");
  assert.equal(ui.open.hidden, true);
});

test("a failed request can be retried; permission loss clears controls and details", async () => {
  const replies = [{ok: false, status: 503}, {ok: false, status: 403}];
  const ui = stopFixture(async () => replies.shift());
  ui.control.update({terminal: false, stop: {state: "available"}});
  ui.open.click();
  await ui.confirm.click();
  assert.match(ui.status.textContent, /다시 시도/);
  assert.equal(ui.open.hidden, false);
  ui.open.click();
  await ui.confirm.click();
  assert.equal(ui.open.hidden, true);
  assert.equal(ui.status.textContent, "");
});
