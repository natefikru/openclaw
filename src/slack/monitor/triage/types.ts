export type TriageDecision = "IMMEDIATE" | "DIGEST" | "DROP";

export type TriageResult = {
  decision: TriageDecision;
  reason: string;
};

export type DigestEntry = {
  id: string;
  timestamp: number;
  channelId: string;
  channelLabel: string;
  senderName: string;
  messageText: string;
  isThreadReply: boolean;
};
