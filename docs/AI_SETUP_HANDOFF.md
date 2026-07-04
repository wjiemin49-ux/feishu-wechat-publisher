# AI Setup Handoff

Use this file when a user gives this repository to an AI agent and wants the agent to set up the whole local workflow.

## Goal

Set up a local-first workflow:

Feishu command
-> candidate image/text generation
-> WeChat Official Account prepare in MuMu
-> Feishu screenshot confirmation
-> explicit `可以发表 <run_id>`
-> two-step WeChat publish confirmation
-> status screenshot and cleanup.

Xiaohongshu is optional and should normally stay in dry-run/probe mode.

## Ask The User For These Once

Ask for all items at the beginning, then proceed without repeatedly interrupting:

- Windows project directory where this repo should live.
- Python executable path, or permission to create `.venv`.
- Feishu app credentials and target chat configuration.
- AI image/text provider config and local API key file paths.
- MuMu install path.
- MuMu instance index for the WeChat account.
- ADB device address, usually `127.0.0.1:7555`.
- Whether WeChat Official Account Assistant is already logged in.
- Whether final publish should always require Feishu confirmation. Recommended: yes.
- Whether scheduled runs should be enabled. Recommended: WeChat only.
- Optional Xiaohongshu probe profile directory and vision-model config.

Never ask the user to paste secrets into chat if a local key file can be used.

## Files To Configure

Copy examples first:

```powershell
Copy-Item config\xhs_workflow.config.example.json workflow\config.json
Copy-Item config\xhs_probe.config.example.json xhs-probe\config.json
```

Then edit local config files only. Do not commit them.

Minimum WeChat/MuMu settings:

- `wechat_mp_publish_enabled`
- `wechat_mp_publisher_dir`
- `wechat_mp_python`
- `wechat_mp_mumu_cli`
- `wechat_mp_mumu_index`
- `wechat_mp_mumu_device`
- `wechat_mp_require_feishu_confirm`

Minimum Feishu settings:

- app id / app secret or local secret file path
- target chat id or event subscription settings
- image upload permission

## Setup Checks

Run these before any real publishing:

```powershell
python -m py_compile workflow\workflow.py workflow\wechat_mp_publish_bridge.py
python -m py_compile wechat-mumu\wechat_mp_sticker_mumu.py
python -m playwright install chromium
```

Check MuMu/ADB:

```powershell
cd wechat-mumu
python wechat_mp_sticker_mumu.py status
```

The status command must not expose cookies, private app storage, tokens, or keys.

## WeChat Validation Path

1. Run a generation command or place a candidate in `workflow/state/latest_publish_candidate.json`.
2. Run WeChat prepare:

```powershell
cd workflow
python workflow.py wechat-mp-prepare
```

3. Confirm the result is `awaiting_confirm`.
4. Confirm the screenshot shows the editor with the publish button visible.
5. Only after explicit user confirmation, run final publish through the workflow/Feishu command path.
6. Verify the final status screenshot shows the title in the published list.

## Feishu Commands

Recommended commands:

- `发布公众号`: prepare only, sends screenshot, waits for confirmation.
- `可以发表 <run_id>`: final WeChat publish, including the second confirmation dialog.
- `发布小红书` / `小红书预检`: XHS dry-run/probe only.
- `确认发布 <run_id>`: XHS publish attempt, only if the user knowingly enables it.

## Scheduling

For unattended daily preparation, schedule WeChat only:

```powershell
.\workflow\install_four_day_publish_cycle_tasks.ps1 -Platforms wechat
```

Do not schedule final publish without a human confirmation gate.

## Safety Rules

- Do not read Chrome main profile cookie databases.
- Do not read WeChat private app data.
- Do not print API keys, Feishu secrets, tokens, cookies, localStorage, session data, or webhooks.
- Do not bypass captchas, login checks, risk prompts, QR confirmation, or platform controls.
- Do not treat prepare/dry-run as published.
- Do not treat one click as WeChat published; WeChat requires the editor publish button and the second confirmation dialog.

## Known Limits

- UI automation can break when app layout changes.
- ADB can temporarily report incorrect bounds such as `[0,0][0,0]`; the WeChat helper contains guarded fallbacks for known publish controls.
- WeChat success is based on Official Account Assistant UI evidence.
- Xiaohongshu is higher risk for unattended automation and is not recommended for frequent automated publishing.
- Users are responsible for complying with each platform's terms and local laws.
