# Feishu WeChat Publisher

Local-first social publishing workflow for AI image posts, Feishu command control, WeChat Official Account automation, and Xiaohongshu visual probing.

This is a sanitized open-source package. It intentionally excludes browser profiles, cookies, logs, screenshots, generated images, API keys, Feishu app credentials, webhooks, and local state.

Current status:

- WeChat Official Account via MuMu + Official Account Assistant is the primary supported publishing path. It can run prepare, human confirmation, final two-step publish, status verification, screenshot capture, and cleanup.
- Xiaohongshu is included as a probe/dry-run path. It is useful for feasibility testing and preview screenshots, but frequent or unattended publishing is not recommended.
- Final publishing is guarded by an explicit confirmation command and a matching `run_id`.

## What Is Included

- `workflow/`: Feishu-triggered orchestration, content generation flow, scheduled precheck tasks, and confirmation gates.
- `wechat-mumu/`: MuMu + WeChat Official Account Assistant UI automation helper.
- `xhs-probe/`: Xiaohongshu browser probe using Playwright and vision-model page inspection.
- `config/`: example configs with placeholders only.
- `docs/`: architecture, safety notes, and release checklist.

## Safety Model

- Precheck/prepare commands stop before final publish.
- Final publish commands require an explicit confirmation command with the matching `run_id`.
- The code does not read browser cookies, localStorage, Chrome main profiles, private app storage, API keys, or webhooks.
- The code does not bypass captchas, QR login, verification prompts, platform risk prompts, or anti-abuse controls.
- `dry-run` and `prepare` are never treated as published.
- `publish-once` / `confirm-publish` mean submission was attempted; final state must be verified by platform UI evidence.

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
Copy-Item config\xhs_workflow.config.example.json workflow\config.json
Copy-Item config\xhs_probe.config.example.json xhs-probe\config.json
```

Edit the copied configs and provide your own local paths, API key files, and Feishu settings.

## Typical Feishu Flow

1. User sends a generation command in Feishu.
2. `workflow/` generates a candidate image post and writes `latest_publish_candidate.json`.
3. User sends `发布公众号`.
4. `workflow/` calls `wechat-mumu/wechat_mp_sticker_mumu.py prepare`.
5. MuMu opens WeChat Official Account Assistant, uploads the image, fills title/body, and stops before publish.
6. Feishu receives the pre-publish screenshot and asks for `可以发表 <run_id>`.
7. User sends `可以发表 <run_id>`.
8. `confirm-publish` clicks the editor publish button, then the second confirmation dialog publish button.
9. The workflow verifies the post in the Official Account Assistant published list and sends the final screenshot/status.

Xiaohongshu flow is similar, but should normally stop at `dry-run` preview:

1. User sends `发布小红书` or `小红书预检`.
2. Playwright opens the publish page with a persistent local profile.
3. A vision model inspects the page and returns screenshot evidence.
4. Human review decides whether to proceed. High-frequency unattended publishing is not recommended.

## Scheduled Runs

The included Windows Scheduled Task scripts can run prechecks on a cadence. Scheduled runs should normally prepare WeChat posts and wait for explicit confirmation.

```powershell
.\workflow\install_four_day_publish_cycle_tasks.ps1 -Platforms wechat
.\workflow\status_four_day_publish_cycle_tasks.ps1
.\workflow\uninstall_four_day_publish_cycle_tasks.ps1
```

## Give This Repo To An AI Agent

If you want another AI agent to set this up for you, point it to:

- `docs/AI_SETUP_HANDOFF.md`
- `docs/ARCHITECTURE.md`
- `docs/SAFETY.md`
- `config/*.example.json`

The agent should ask for all required local paths and credentials once, write only local config files, run dry checks, then perform a WeChat prepare test before enabling schedules.

## Limits

- This project is local UI automation, so app layout changes can break selectors.
- MuMu, ADB, Feishu permissions, and platform login state are required.
- The project does not solve captchas, account risk checks, QR login, or platform restrictions.
- WeChat publishing is verified by Official Account Assistant UI evidence, not by private APIs.
- Xiaohongshu automation is included for probe/dry-run only and should be used conservatively.

## Before Publishing To GitHub

Run the checklist in `docs/OPEN_SOURCE_CHECKLIST.md`. Do not commit `.secrets`, `.venv`, `state`, `logs`, `screenshots`, `profiles`, generated images, or real config files.
