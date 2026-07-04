# Open Source Checklist

Before pushing to GitHub:

- [ ] Run a content scan for `token`, `secret`, `cookie`, `webhook`, `api_key`, `authorization`.
- [ ] Run a path scan for local-only paths such as real Desktop key files, browser profiles, MuMu runtime state, and generated outputs.
- [ ] Confirm no real `config.json` files are committed.
- [ ] Confirm no `.secrets/`, `state/`, `logs/`, `screenshots/`, `profiles/`, `outputs/`, or `.venv/` folders are committed.
- [ ] Confirm no browser profile files such as `Cookies`, `Login Data`, `Local Storage`, `Session Storage`, or `IndexedDB` are committed.
- [ ] Confirm Feishu app ids, app secrets, webhook URLs, open ids, chat ids, and tenant tokens are not committed.
- [ ] Confirm API keys are loaded from local files or environment variables, not source code.
- [ ] Run `python -m py_compile` for the included Python entry points.
- [ ] Create a fresh zip from the sanitized package root, not from a live working directory.
- [ ] Replace the `LICENSE` copyright holder if needed.
- [ ] Review platform terms and only use the project within allowed workflows.
