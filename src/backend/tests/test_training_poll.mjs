import {strict as assert} from "node:assert";
import {test} from "node:test";
import {refreshStatus} from "../apps/preferences/static/preferences/model-training.mjs";

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
