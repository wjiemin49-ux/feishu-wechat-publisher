# Architecture

```text
Feishu command or scheduled task
-> workflow/workflow.py
-> generate candidate image + caption
-> write local candidate state
-> platform precheck
   -> WeChat: wechat-mumu/wechat_mp_sticker_mumu.py prepare
   -> XHS: xhs-probe/run_xhs_probe.py dry-run
-> send screenshot/status back to Feishu
-> wait for explicit confirmation with run_id
-> final submit attempt
-> send final screenshot/status back to Feishu
```

The workflow is intentionally local-first. Runtime state, screenshots, browser profiles, and logs stay outside Git.

## Confirmation Gates

- WeChat prepare stops before the final publish tap.
- XHS dry-run stops before final publish.
- Final submit commands require the current waiting state and matching `run_id`.

## Runtime Folders

Create these locally and keep them ignored:

- `state/`
- `logs/`
- `screenshots/`
- `profiles/`
- `outputs/`
- `.secrets/`

