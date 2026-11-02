export type Role = "user" | "assistant";

export interface ChatMessage {
  id: string;
  role: Role;
  text: string;
}

export interface Conversation {
  id: string;
  title: string;
  /** ISO date, used only to group the history list. */
  updatedAt: string;
  messages: ChatMessage[];
}

export interface ConversationGroup {
  label: string;
  conversations: Conversation[];
}
