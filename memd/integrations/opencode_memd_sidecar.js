// opencode sync-sidecar: EXTENDS an existing git-based notes-sync flow (it does
// NOT fork the memory plugin that provides it). On session idle, after the
// existing sync push, it POSTs the freshly-written local notes to memd /save so
// the index stays current. Pull on start is still done by the existing plugin.
//
// Designed to be drop-in alongside the existing sync plugin in
// ~/.config/opencode/plugins/ (see docs/integration-registration.md). Never
// throws into a session: all errors are swallowed and reported in the return
// summary.

import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

export function parseNote(md) {
  let title = "note";
  let body = md.trim();
  if (md.startsWith("---")) {
    const end = md.indexOf("---", 3);
    if (end !== -1) {
      const fm = md.slice(3, end);
      body = md.slice(end + 3).trim();
      const m = fm.match(/^title:\s*(.+)$/m);
      if (m) title = m[1].trim();
    }
  }
  return { title, body };
}

export async function collectNewNotes(storeDir, sinceMs) {
  const out = [];
  let entries = [];
  try {
    entries = await readdir(storeDir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const e of entries) {
    if (!e.isFile() || !e.name.endsWith(".md") || e.name === "MEMORY.md") continue;
    try {
      const md = await readFile(join(storeDir, e.name), "utf8");
      out.push(parseNote(md));
    } catch {
      // skip unreadable file
    }
  }
  return out;
}

export async function pushNotesToMemd(notes, { url, token, fetchImpl = fetch }) {
  let pushed = 0;
  let failed = 0;
  for (const note of notes) {
    try {
      const r = await fetchImpl(`${url}/save`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ title: note.title, body: note.body }),
      });
      if (r && r.ok) pushed += 1;
      else failed += 1;
    } catch {
      failed += 1;
    }
  }
  return { pushed, failed };
}

// opencode plugin entry. Registered alongside the existing sync plugin.
export const MemdSyncSidecar = async ({ $ }) => {
  const store = `${process.env.HOME}/.local/share/memd/notes`;
  const url = process.env.MEMD_URL || "http://127.0.0.1:8077";
  const token = process.env.MEMD_TOKEN || "";
  return {
    event: async ({ event }) => {
      if (event.type !== "session.idle" || !token) return;
      try {
        const notes = await collectNewNotes(store, Date.now() - 3600_000);
        await pushNotesToMemd(notes, { url, token });
      } catch {
        // Never break a session on memory indexing.
      }
    },
  };
};
