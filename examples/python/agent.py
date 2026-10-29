"""Reference agent: echo the prompt back, then settle. Run it with `python agent.py` under any ACP client."""

import jaah_agent


class Echo(jaah_agent.Agent):
    name = "jaah-echo"
    version = "2.0.0"

    async def prompt(self, turn: jaah_agent.Turn) -> None:
        await turn.say(f"You said: {turn.text}")
        summary = f"Echoed {len(turn.text)} characters."
        # A task in a repository settles `no_pr` (an echo writes no code); one in a folder slot may only settle
        # `outcome`, so a refused `no_pr` falls back to it. A refusal leaves the session unsettled.
        try:
            await turn.complete(summary, terminal_type="no_pr", no_pr_reason="no_code_needed",
                                no_pr_evidence=f"echoed the prompt back ({len(turn.text)} characters); writes no code")
        except jaah_agent.ExtensionError:
            await turn.complete(summary, terminal_type="outcome")


if __name__ == "__main__":
    jaah_agent.run(Echo())
