# Development tools

Project-level development assistant configuration is owned by `main-system`.
Keep editor and agent integrations in the matching directory here; do not add
dot-directories for these tools at the project root.

- `continue/`: Continue agents and rules.
- `devin/`: local Devin permissions (`*.local.json` remains untracked).
- `qodo/`: Qodo agents and workflows.

The former `ollama/` Modelfile directory is retired: Codex B154 (executed
2026-10-02, rev 235) retired Ollama — local inference is served by the
governed native engine only.
