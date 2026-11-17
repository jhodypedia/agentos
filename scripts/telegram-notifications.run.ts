// Calls only this project's authenticated backend; Telegram transport is owned
// by AgentOS. Configure AGENTOS_API_URL and AGENTOS_API_KEY for this job.
import { every } from "@boxd/run";

every("30s", async () => {
  const base = process.env.AGENTOS_API_URL;
  const key = process.env.AGENTOS_API_KEY;
  if (!base || !key) throw new Error("Configure AGENTOS_API_URL and AGENTOS_API_KEY");
  const response = await fetch(`${base.replace(/\/$/, "")}/api/v1/telegram/notifications/deliver`, {
    method: "POST",
    headers: { "X-API-Key": key },
    signal: AbortSignal.timeout(25000),
  });
  if (!response.ok) throw new Error(`AgentOS notification delivery failed: ${response.status}`);
  console.log(await response.json());
});
