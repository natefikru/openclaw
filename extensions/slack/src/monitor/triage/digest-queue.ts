import { mkdirSync, readdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomBytes } from "node:crypto";
import type { DigestEntry } from "./types.js";

const DIGEST_DIR = join(process.env.HOME ?? "/home/natefikru", ".openclaw", "triage-digest");

function ensureDir(): void {
  mkdirSync(DIGEST_DIR, { recursive: true });
}

export function enqueueDigestMessage(entry: DigestEntry): void {
  ensureDir();
  const shortId = randomBytes(4).toString("hex");
  const filename = `${entry.timestamp}-${shortId}.json`;
  const tmpPath = join(tmpdir(), `triage-digest-${shortId}.tmp`);
  const finalPath = join(DIGEST_DIR, filename);
  writeFileSync(tmpPath, JSON.stringify(entry), "utf8");
  renameSync(tmpPath, finalPath);
}

export function loadAndFlushDigest(): DigestEntry[] {
  ensureDir();
  const files = readdirSync(DIGEST_DIR).filter((f) => f.endsWith(".json"));
  if (files.length === 0) return [];

  const entries: DigestEntry[] = [];
  for (const file of files) {
    const filePath = join(DIGEST_DIR, file);
    try {
      const raw = readFileSync(filePath, "utf8");
      entries.push(JSON.parse(raw) as DigestEntry);
      unlinkSync(filePath);
    } catch {
      // Skip corrupt files, try to clean up
      try { unlinkSync(filePath); } catch {}
    }
  }

  return entries.sort((a, b) => a.timestamp - b.timestamp);
}
