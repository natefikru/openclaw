import type { OpenClawConfig } from "../../../config/config.js";
import { logVerbose } from "../../../globals.js";
import { sendMessage } from "../../../infra/outbound/message.js";
import { loadAndFlushDigest } from "./digest-queue.js";
import type { DigestEntry } from "./types.js";

function formatDigest(entries: DigestEntry[]): string {
  const byChannel = new Map<string, DigestEntry[]>();
  for (const entry of entries) {
    const key = entry.channelLabel;
    const list = byChannel.get(key) ?? [];
    list.push(entry);
    byChannel.set(key, list);
  }

  const channelCount = byChannel.size;
  const lines: string[] = [
    `Slack Digest (${entries.length} message${entries.length === 1 ? "" : "s"}, ${channelCount} channel${channelCount === 1 ? "" : "s"})`,
    "",
  ];

  for (const [channel, msgs] of byChannel) {
    lines.push(`${channel} (${msgs.length})`);
    for (const msg of msgs) {
      const threadTag = msg.isThreadReply ? " (thread)" : "";
      const preview = msg.messageText.length > 120
        ? msg.messageText.slice(0, 120) + "..."
        : msg.messageText;
      lines.push(`- ${msg.senderName}${threadTag}: ${preview}`);
    }
    lines.push("");
  }

  return lines.join("\n").trim();
}

export async function flushDigestToTelegram(cfg: OpenClawConfig, params?: {
  to?: string;
  channel?: string;
}): Promise<void> {
  const entries = loadAndFlushDigest();
  if (entries.length === 0) {
    logVerbose("triage digest: no entries, skipping");
    return;
  }

  const content = formatDigest(entries);
  const to = params?.to ?? "5176316563";
  const channel = params?.channel ?? "telegram";

  try {
    await sendMessage({ to, content, channel, cfg });
    logVerbose(`triage digest: sent ${entries.length} entries to ${channel}:${to}`);
  } catch (err) {
    logVerbose(`triage digest: failed to send: ${String(err)}`);
  }
}
