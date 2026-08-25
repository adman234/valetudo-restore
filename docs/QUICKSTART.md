# Quick start

1. Start the container and open `http://<host>:8095`.
2. **Settings → Upload SSH key** — the key you use for the robot.
   Stored at `/config/valetudo_key`, mode 0600.
3. Set the **robot host**, save, then **Test connection** on the dashboard.
   You are looking for `HEALTHY`.
4. Click **Download it now** on the binary banner so restores work offline.
5. Click **Back up now** and confirm an archive appears.
6. Only then consider enabling **auto-restore** in Settings.

## Verifying it actually works

A "healthy" test proves less than it looks like it does: the probe short-circuits
on a healthy robot, so plumbing problems can hide. To exercise the real path,
stop Valetudo on the robot and run a monitor tick:

```bash
ssh -i <key> root@<robot> "killall valetudo"
curl -X POST http://<host>:8095/api/monitor-tick
```

You should see `CRASHED`. With `confirm_samples` at its default of 2, the first
tick reports `awaiting confirmation` and the second acts — that is deliberate.

Restart it afterwards if auto-restore is off:

```bash
ssh -i <key> root@<robot> "VALETUDO_CONFIG_PATH=/data/valetudo_config.json setsid /data/valetudo >/dev/null 2>&1 &"
```
