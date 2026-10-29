# Contributing

Issues and pull requests are welcome on GitHub. Each release is one commit on `main`, so a PR is not merged here: a
Jaah maintainer reviews it, re-applies it crediting you, and closes it with a link to the release that carries it.

1. **Fork** `jaah-ai/agent-sdk` and branch from `main`.
2. **Commits** follow [Conventional Commits](https://www.conventionalcommits.org/): `fix(python): …`,
   `feat(typescript): …`. Small and atomic; rebase on `main` instead of merging it.
3. **Sign off every commit** (`git commit -s`): the `Signed-off-by: Your Name <you@example.com>` line certifies the
   [Developer Certificate of Origin 1.1](https://developercertificate.org/) — that you wrote the change or have the
   right to submit it under this repo's licence, [Apache-2.0](LICENSE). A PR with an unsigned commit is not reviewed.
4. **Green before review:** `./check.sh` passes (the [README](README.md) says what it runs).
