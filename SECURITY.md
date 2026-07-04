# Security Policy

Do not commit secrets or runtime state.

Never publish:

- API keys, app secrets, tokens, webhooks, Feishu credentials, or key files.
- Browser profiles, cookies, localStorage, session storage, IndexedDB, or Chrome user data.
- Platform screenshots or UI dumps that expose account/private content.
- Generated post assets unless you have the rights to publish them.

If a secret is accidentally committed, rotate it immediately before removing it from Git history.

