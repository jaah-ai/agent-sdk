// Reference agent: echo the prompt back, then settle. Run it with `node agent.ts` under any ACP client.

import { ExtensionError, run, type Turn } from "@jaah/agent-sdk";

run({
  info: { name: "jaah-echo", version: "2.0.0" },
  async prompt(turn: Turn) {
    await turn.say(`You said: ${turn.text}`);
    const summary = `Echoed ${turn.text.length} characters.`;
    // A task in a repository settles `no_pr` (an echo writes no code); one in a folder slot may only settle
    // `outcome`, so a refused `no_pr` falls back to it. A refusal leaves the session unsettled.
    try {
      await turn.complete(summary, "no_pr", {
        no_pr_reason: "no_code_needed",
        no_pr_evidence: `echoed the prompt back (${turn.text.length} characters); writes no code`,
      });
    } catch (err) {
      if (!(err instanceof ExtensionError)) throw err;
      await turn.complete(summary, "outcome");
    }
  },
});
