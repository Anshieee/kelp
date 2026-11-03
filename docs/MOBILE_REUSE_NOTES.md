# OpenMausBot mobile UI adaptation

Reference: `milind-soni/OpenMausBot` at `f52effc1a5871e4e54b3d2c699336a28c6cb1f97`, Apache-2.0.

The Midnight and Meadow palette values, including the phone-specific surface ramps, are adapted from `src/styles.css`. The chat-first composition follows `src/components/ChatView.tsx`, `ChatEmptyState.tsx`, and the responsive sidebar/composer patterns. Kelp's CSS and HTML are modified standalone implementations, not the upstream React runtime. Copyright 2026 Milind Soni and OpenMausBot contributors; license and unmodified upstream notices are retained in `third-party/OpenMausBot/LICENSE` and `third-party/OpenMausBot/NOTICE`.

The adaptation includes the charcoal/blue Midnight theme, white/mint/green Meadow theme, desktop conversation sidebar, phone conversation drawer, compact assistant header, centered empty chat, rounded message composer, task activity bubbles, and approval cards. Kelp retains its own name and original CSS avatar. No upstream mascot, brand marks, enterprise code, backend, or hosted services are included.

The existing Kelp `/api/v1` adapter remains responsible for managed authentication, onboarding, tasks, immutable review candidates, and publication. This UI does not add unsupported free-form follow-up messages, voice, attachments, or computer control. A task is a conversation-like activity view over the existing task API. Theme changes are in-memory and contain no user secrets.

Visual checks use isolated test API responses. They demonstrate rendering and interaction only, not live authentication, real remote execution, or a published PR. Production behavior still needs the existing backend configuration.

Geist Latin variable font is bundled locally from `@fontsource-variable/geist`; its SIL Open Font License is in `third-party/Geist/LICENSE`. No external font request is needed.
