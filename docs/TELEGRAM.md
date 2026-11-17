# Telegram setup

The project owns its Bot API transport and webhook. Configure
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` (comma-separated numeric IDs),
and `TELEGRAM_WEBHOOK_SECRET` (at least 32 random URL-safe characters).

1. Apply migrations and start the API.
2. As an admin, `POST /api/v1/telegram/link` with `user_id` and
   `telegram_user_id`. Linking preserves the existing platform role. Pass null
   to unlink. `/start` cannot register or elevate an account.
3. As an admin, `POST /api/v1/telegram/setup` with
   `{"url":"https://your-domain/api/v1/telegram/webhook"}`. The API registers
   Telegram's secret header and restricts updates to messages and callbacks.
4. Open a private chat with the bot and use `/start`.

Commands: `/start`, `/help`, `/status`, `/agents`, `/tasks`, `/bounties`, `/scan`,
`/report <finding UUID>`, `/cost`, `/approve [approval UUID]`,
`/reject <approval UUID>`, `/stop <task UUID>`.
`/tasks create <agent-slug> <objective>` creates a pending task with a $1 budget;
start execution through the task API. `/scan` reports actual scanner availability;
run scans through the admin research API. `/approve` without an ID displays a
pending request with its payload and inline approve/reject buttons.

Commands require allowlisting and an active linked user. Mutations require
operator or higher. Assigned approvals cannot be decided by another user, and
operators cannot cancel another user's task. Groups are rejected. Replies use
plain text, credential redaction and bounded chunks.

Update IDs persist in `telegram_updates`. Command mutations and their response
commit together. Retrying a processed update resends the stored reply without
replaying the mutation. A network failure after Telegram accepted a reply can
still cause a duplicate reply; outbound delivery is at least once.

Task status/cost/error and pending-approval notifications use
`POST /api/v1/telegram/notifications/deliver` (admin). Set
`TELEGRAM_NOTIFICATIONS_ENABLED=false` to disable. The optional
`scripts/telegram-notifications.run.ts` calls this endpoint every 30 seconds;
configure `AGENTOS_API_URL` and `AGENTOS_API_KEY` in the job environment and
register it with `run scripts/telegram-notifications.run.ts`. It calls only this
project's API. No job is registered automatically. Notifications track task
status changes, not streamed model tokens.

Live verification needs a bot token, public HTTPS webhook and a linked,
allowlisted account. Tests mock the Bot API and do not verify live delivery.
