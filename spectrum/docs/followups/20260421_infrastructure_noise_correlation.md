# Followup — correlate suspect compression events with leap-box scheduled jobs

**Opened**: 2026-04-21

## Context
Nine of the eleven `medium+` compression events detected in today's archaeology share a −174 kHz spur offset in the FM band and carry depression values of 1–2 dB (barely above the 5 dB threshold for `sig_baseline`). They cluster at specific timestamps across days. Before blaming a tuner artifact, we should rule out **infrastructure noise from the leap box itself** — scheduled cron jobs, systemd timers, backup processes — that might be radiating broadband RF when they run.

Representative cluster of suspect events (UTC):
- 2026-04-16 12:26, 15:35
- 2026-04-17 05:20, 12:05, 20:43, 21:21
- 2026-04-18 09:16
- 2026-04-21 02:15, 09:29

## Question
Do any of these timestamps line up with scheduled leap-box activity (cron.daily, backup jobs, systemd timers, disk scrubs, fwupd checks)?

## Why it matters
- If "yes, Apr 21 02:15 correlates with daily backup", then the detector is catching RFI from the adjacent USB cable / drive / NIC, NOT external emitters. That changes the narrative from "mystery signal" to "known local noise source" and suggests hardware-level mitigation (shielding, different USB port, dedicated power).
- If "no, no correlation", we rule out infrastructure and have more confidence the −174 kHz family is either a tuner artifact or genuine external activity.

## Approach
Five minutes of grepping on leap:

```bash
ssh dio_nysis@192.168.2.10

# cron.* schedules
ls -la /etc/cron.daily /etc/cron.hourly /etc/cron.weekly
grep -rnH . /etc/cron.d/ 2>/dev/null

# systemd timers
systemctl list-timers --all

# journald for specific timestamps from the suspect cluster
journalctl --since '2026-04-17 05:15' --until '2026-04-17 05:25'
journalctl --since '2026-04-21 02:10' --until '2026-04-21 02:20'

# any daily scrub / fstrim / snapper / btrfs maintenance
systemctl status snapper-timeline snapper-cleanup btrfs-scrub* fstrim.timer 2>/dev/null
```

For each suspect timestamp, look for anything that started within ±3 minutes.

## Expected outcome
A short table:
| Suspect time (UTC) | Concurrent leap activity | Plausible RFI source? |
|---|---|---|
| 2026-04-17 05:20 | e.g. "fstrim.timer fired 05:19" | yes, SSD firmware activity |
| 2026-04-21 02:15 | e.g. "no scheduled jobs" | no, ruled out |

If >3 events correlate with infrastructure activity, re-scope: "we have a local RFI problem" → add an RFI mitigation followup + note in CLAUDE.md.

If <=3 events correlate, move on: the −174 kHz family is likely a tuner artifact (see `20260421_fs_quarter_spur_hypothesis.md`).
