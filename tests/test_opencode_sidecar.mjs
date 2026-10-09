import { test } from "node:test";
import assert from "node:assert/strict";
import { parseNote, pushNotesToMemd } from "../memd/integrations/opencode_memd_sidecar.js";

test("parseNote extracts title and body from frontmatter", () => {
  const md = "---\ntitle: GPU Thrash\nslug: gpu-thrash\n---\nmodel eviction fix\n";
  const note = parseNote(md);
  assert.equal(note.title, "GPU Thrash");
  assert.equal(note.body, "model eviction fix");
});

test("pushNotesToMemd posts each note to /save with bearer token", async () => {
  const calls = [];
  const fakeFetch = async (url, opts) => {
    calls.push({ url, opts });
    return { ok: true, status: 200, json: async () => ({ slug: "x", action: "created" }) };
  };
  const notes = [
    { title: "A", body: "body a" },
    { title: "B", body: "body b" },
  ];
  const res = await pushNotesToMemd(notes, {
    url: "http://127.0.0.1:8077",
    token: "tok",
    fetchImpl: fakeFetch,
  });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, "http://127.0.0.1:8077/save");
  assert.equal(calls[0].opts.headers.Authorization, "Bearer tok");
  assert.equal(JSON.parse(calls[0].opts.body).title, "A");
  assert.equal(res.pushed, 2);
});

test("pushNotesToMemd swallows network errors (never breaks a session)", async () => {
  const fakeFetch = async () => {
    throw new Error("connect ECONNREFUSED");
  };
  const res = await pushNotesToMemd([{ title: "A", body: "b" }], {
    url: "http://127.0.0.1:8077",
    token: "tok",
    fetchImpl: fakeFetch,
  });
  assert.equal(res.pushed, 0);
  assert.equal(res.failed, 1);
});
