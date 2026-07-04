# Safety Notes

This project automates normal local UI workflows. It is not designed for evasion.

Do not use it to:

- bypass captchas, QR login, identity verification, or platform risk prompts;
- disguise browser fingerprints;
- scrape or publish at high frequency;
- hide automation from platform controls;
- upload content you do not have rights to publish.

The safe production pattern is:

1. Generate or select candidate content.
2. Run platform precheck/prepare.
3. Review screenshot evidence.
4. Confirm manually with the exact `run_id`.
5. Treat final submission as "attempted/submitted", not guaranteed approval.

## Platform Boundaries

WeChat Official Account publishing is the primary supported path in this
open-source package. The MuMu-based flow prepares the post, captures evidence,
waits for explicit confirmation, then performs the two-step publish confirmation
that the official app shows in the UI.

Xiaohongshu support is intentionally conservative. The browser probe is useful
for local login-state validation, screenshot inspection, and dry-run upload
checks. It is not recommended for frequent unattended publishing, and should stop
when login, verification, risk prompts, or unexpected dialogs appear.

Automation must not imply platform approval. A successful click sequence means
only that submission was attempted or that the UI reported a submitted/reviewing
state. Final visibility and moderation status belong to the platform.
