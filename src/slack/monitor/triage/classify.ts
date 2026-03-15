import { completeSimple, type TextContent } from "@mariozechner/pi-ai";
import { getApiKeyForModel, requireApiKey } from "../../../agents/model-auth.js";
import { resolveModelRefFromString, type ModelRef } from "../../../agents/model-selection.js";
import { resolveModel } from "../../../agents/pi-embedded-runner/model.js";
import type { OpenClawConfig } from "../../../config/config.js";
import { logVerbose } from "../../../globals.js";
import type { SlackChannelConfigResolved } from "../channel-config.js";
import type { TriageDecision, TriageResult } from "./types.js";

const SYSTEM_PROMPT = `You are a Slack message classifier for Nate, an engineering lead at DIN whose work focuses specifically on: RPC infrastructure, blockchain provider management, network operations, router health, and the DIN spin-out from Consensys.

Classify the message as exactly one of: IMMEDIATE, DIGEST, DROP

IMMEDIATE - needs Nate's attention now:
- Production incidents, outages, service disruptions affecting DIN infrastructure
- Direct questions or requests addressed to Nate or requiring his timely response
- Security or access issues
- Deployment problems or CI/CD failures on DIN services

DIGEST - directly relevant to Nate's work, include in periodic summary:
- RPC provider issues, configuration changes, or status updates
- DIN router, gateway, or network operations discussions
- DIN spin-out planning, legal, or organizational decisions
- Infrastructure changes that affect DIN services
- PRs or code reviews on repos Nate owns or contributes to

DROP - not relevant to Nate, skip entirely:
- General engineering discussions not related to DIN/RPC/networking
- Frontend, UI, or product discussions Nate is not involved in
- HR, social, or team-building messages
- Greetings, social chat ("good morning", "happy friday")
- Emoji-only or reaction-only messages
- Automated status messages that are not incidents
- Messages with no actionable content for Nate
- PR reviews and code discussions on unrelated repos

When in doubt, DROP. Only DIGEST messages that Nate would actually want to read.

Respond with one word: IMMEDIATE, DIGEST, or DROP`;

const DROP_SUBTYPES = new Set([
  "channel_join",
  "channel_leave",
  "channel_archive",
  "channel_unarchive",
  "channel_topic",
  "channel_purpose",
  "channel_name",
]);

const DEFAULT_TRIAGE_MODEL = "openai/gpt-5-mini";

function isTextContent(block: { type: string }): block is TextContent {
  return block.type === "text";
}

function parseDecision(text: string): TriageDecision {
  const upper = text.trim().toUpperCase();
  if (upper.startsWith("IMMEDIATE")) return "IMMEDIATE";
  if (upper.startsWith("DROP")) return "DROP";
  return "DIGEST";
}

export function triageByRules(params: {
  channelConfig: SlackChannelConfigResolved;
  messageText: string;
  userId?: string;
  botId?: string;
  subtype?: string;
  isDirectMessage: boolean;
  channelId: string;
}): TriageResult | null {
  const { channelConfig, messageText, userId, botId, subtype, isDirectMessage, channelId } = params;

  // Drop bot messages if configured (default: true)
  if (botId && (channelConfig.triageDropBots !== false)) {
    return { decision: "DROP", reason: "bot message" };
  }

  // Drop messages from ignored users
  const dropUsers = channelConfig.triageDropUsers ?? [];
  if (userId && dropUsers.includes(userId)) {
    return { decision: "DROP", reason: `ignored user ${userId}` };
  }

  // Drop system subtypes
  if (subtype && DROP_SUBTYPES.has(subtype)) {
    return { decision: "DROP", reason: `subtype: ${subtype}` };
  }

  // DMs are always immediate
  if (isDirectMessage) {
    return { decision: "IMMEDIATE", reason: "direct message" };
  }

  // Check @mentions
  const mentions = channelConfig.triageImmediateMentions ?? [];
  for (const mentionId of mentions) {
    if (messageText.includes(`<@${mentionId}>`)) {
      return { decision: "IMMEDIATE", reason: `mentioned ${mentionId}` };
    }
  }

  // Check immediate users (map of userId -> channel list, "*" means all channels)
  const immediateUsers = channelConfig.triageImmediateUsers ?? {};
  if (userId && userId in immediateUsers) {
    const channels = immediateUsers[userId];
    if (channels.includes("*") || channels.includes(channelId)) {
      return { decision: "IMMEDIATE", reason: `immediate user ${userId}` };
    }
  }

  // Check keyword matches
  const keywords = channelConfig.triageImmediateKeywords ?? [];
  if (keywords.length > 0) {
    const lowerText = messageText.toLowerCase();
    for (const kw of keywords) {
      if (lowerText.includes(kw.toLowerCase())) {
        return { decision: "IMMEDIATE", reason: `keyword match: ${kw}` };
      }
    }
  }

  // No rule matched, need LLM
  return null;
}

export async function triageWithLlm(params: {
  channelLabel: string;
  senderName: string;
  messageText: string;
  cfg: OpenClawConfig;
  triageModel?: string;
}): Promise<TriageResult> {
  const { channelLabel, senderName, messageText, cfg, triageModel } = params;
  const modelStr = triageModel ?? DEFAULT_TRIAGE_MODEL;

  try {
    // Parse "provider/model" string into a ref
    let ref: ModelRef;
    const slashIdx = modelStr.indexOf("/");
    if (slashIdx > 0) {
      ref = { provider: modelStr.slice(0, slashIdx), model: modelStr.slice(slashIdx + 1) };
    } else {
      const result = resolveModelRefFromString({ raw: modelStr, defaultProvider: "openai" });
      if (!result) {
        logVerbose(`triage: model resolution failed for ${modelStr}, defaulting to DIGEST`);
        return { decision: "DIGEST", reason: "model resolution failed" };
      }
      ref = result.ref;
    }

    const resolved = resolveModel(ref.provider, ref.model, undefined, cfg);
    if (!resolved.model) {
      logVerbose(`triage: model resolution failed for ${modelStr}, defaulting to DIGEST`);
      return { decision: "DIGEST", reason: "model resolution failed" };
    }

    const apiKey = requireApiKey(
      await getApiKeyForModel({ model: resolved.model, cfg }),
      ref.provider,
    );

    const truncatedText = messageText.length > 500 ? messageText.slice(0, 500) + "..." : messageText;
    const userMessage = `[${channelLabel}] ${senderName}: ${truncatedText}`;

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);

    try {
      const res = await completeSimple(
        resolved.model,
        {
          messages: [
            {
              role: "user",
              content: `${SYSTEM_PROMPT}\n\n${userMessage}`,
              timestamp: Date.now(),
            },
          ],
        },
        {
          apiKey,
          maxTokens: 5,
          temperature: 0,
          signal: controller.signal,
        },
      );

      const text = res.content
        .filter(isTextContent)
        .map((b) => b.text.trim())
        .join("")
        .trim();

      const decision = parseDecision(text);
      logVerbose(`triage LLM: "${userMessage}" -> ${decision} (raw: "${text}")`);
      return { decision, reason: `llm: ${text}` };
    } finally {
      clearTimeout(timeout);
    }
  } catch (err) {
    logVerbose(`triage LLM error: ${String(err)}, defaulting to DIGEST`);
    return { decision: "DIGEST", reason: `llm error: ${String(err)}` };
  }
}
