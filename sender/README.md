# Remote GPU sender

Run this directory on each remote NVIDIA GPU server. It uses only the Python standard library, `nvidia-smi`, OpenSSH, and user systemd. Use Python 3.10+ to match the hub's supported versions. No Feishu credentials or hub Python dependencies are needed here; the hub handles Feishu exports.

| File | Purpose |
| --- | --- |
| `sender.py` | Collect per-user GPU occupancy every 30 seconds into SQLite |
| `uploader.py` | Deliver bounded, durable JSON batches over SSH |
| `install.py` | Generate user systemd units for the current checkout and interpreter |
| `examples/ssh_config` | SSH configuration template with strict host checking |
| `tests/` | Collector, uploader, installer and real receiver compatibility tests |

## Start local collection

Clone the repository onto the sender and run these commands from its root as the collector's ordinary Linux account, without sudo. Keep the checkout at the same absolute path while installed services use it.

```sh
nvidia-smi --query-gpu=uuid,name --format=csv,noheader
python3 sender/sender.py probe
python3 sender/install.py collect
```

Check that the aggregate usernames in the probe match the expected active users. The collector resolves process owners through `/proc`; owners it cannot resolve are grouped under `?`. It credits each user with their own distinct GPU count when a GPU is shared, and sums that user's process memory in MiB. This measures occupancy, including idle processes holding a GPU context, rather than GPU utilization percentage.

The default state directory is `sender/state/`. Both scripts accept `--state-dir /absolute/path`; `GPUVIEW_STATE_DIR` also sets the default. If you choose a custom directory, use it consistently for installation, verification and manual uploads. The installer writes that absolute state path into both services. A state database belongs to one sender/hub pairing; use separate state and keys for separate destinations.

```sh
# Wait at least 65 seconds, then check for advancing complete intervals.
python3 sender/sender.py status
systemctl --user status gpuview-sender.service
```

Successful snapshots are applied until the next successful sample, with a maximum interval of 45 seconds. Idle intervals contain an empty user list. Failed queries, restarts and long gaps never turn into usage. Only aggregate usernames, GPU counts, memory and interval timestamps are stored; process IDs, commands, jobs and environments are not saved.

## Enroll with the hub

Generate a dedicated key on the sender. Keep the private key on this server. The following commands assume the default state path, created by the collector:

```sh
chmod 700 sender/state
# Run once; do not overwrite an existing sender identity.
ssh-keygen -t ed25519 -N '' -f sender/state/id_ed25519 -C gpu-tracker-sender
cat sender/state/id_ed25519.pub
cp sender/examples/ssh_config sender/state/ssh_config
chmod 600 sender/state/ssh_config
```

Give the public key and a stable sender ID to the hub operator. Follow the [hub enrollment instructions](../README.md#enroll-a-remote-sender) to bind the key to that ID with a fixed receiver command and `restrict`. The hub account must be able to write its database; a dedicated unprivileged account provides stronger isolation than a personal account. Successful SSH transport alone does not prove that server-side key restrictions are installed.

Edit `sender/state/ssh_config`: set the hub host, SSH port, hub account, and absolute paths to the private key and dedicated known-hosts file. Keep the alias `gpuview-hub`. Obtain the hub host key and trusted fingerprint from the hub operator, independently verify their match, and put that key in `sender/state/known_hosts` (mode 0600). For a non-default port, its host field is `[hostname]:port`. Do not disable strict host checking or treat an unverified `ssh-keyscan` result as trusted.

The uploader defaults to `STATE_DIR/ssh_config`. For manual runs, `--ssh-config /absolute/path` can select a different config. The installed service uses `STATE_DIR/ssh_config`.

## Verify uploads, then schedule them

After the receiver and restricted key are enrolled, use real collected samples:

```sh
python3 sender/uploader.py verify
python3 sender/install.py uploads
python3 sender/uploader.py status
systemctl --user list-timers gpuview-upload.timer --all --no-pager
```

`verify` sends a small real batch twice with identical bytes. Both calls must return exit 0 with no output before the cursor advances and duplicate verification is marked complete. It needs at least one unsent interval; if the queue is empty, wait for collection. The upload installer performs this check itself if it has not already succeeded, drains the backlog, and only then enables a three-minute timer. Scheduled uploads refuse to run before verification. Verify the sender's coverage and rankings using the hub dashboard's Server filter after enrollment.

Each batch has at most 100 intervals and 512 KiB. The protocol accepts intervals of up to 45 seconds, from the past 31 days and at most 60 seconds ahead. Exact pending JSON is persisted before transmission and retained for retry on failure. Cursor advancement follows whole-batch acknowledgment. Unsent expired data is retained and blocks upload with an error; investigate clock, connectivity or receiver errors rather than editing the cursor blindly. History is retained locally after delivery, so monitor disk usage during long deployments.

## Operate and troubleshoot

```sh
python3 sender/sender.py status
python3 sender/uploader.py status
python3 sender/uploader.py upload  # manual backlog delivery
journalctl --user -u gpuview-sender.service -n 30 --no-pager
tail -n 30 sender/state/upload.log
```

Upload logs rotate locally and report failure status without logging payload contents. State is owner-only (0700 directory, 0600 database/private key/logs). Collection has little overhead: one NVIDIA query every 30 seconds. Failed uploads retain pending data and try again on the next timer run.

To keep user services running after logout, enable lingering if permitted by the server's policy: `loginctl enable-linger "$USER"`. Otherwise ask the administrator. Installation does not change that policy automatically.

Stop uploads with `systemctl --user disable --now gpuview-upload.timer`; stop any in-progress call with `systemctl --user stop gpuview-upload.service`. Stop collection with `systemctl --user disable --now gpuview-sender.service`.

If migrating an existing sender, stop its collector and upload timer before switching implementations, retain its database, keys and upload cursor, and pass the original absolute state directory to the new installer. Place its SSH config at `STATE_DIR/ssh_config`. Do not run two collectors against the same state. Existing symlinked units must be explicitly replaced; the installer refuses to overwrite their targets.

## Tests

From the repository root, without GPU hardware, SSH access or hub dependencies:

```sh
python3 -m unittest discover -s sender/tests -t sender -v
```

Tests include shared-GPU accounting, idle intervals, failure gaps, batch bounds, durable retry and cursor safety, acknowledgment handling, generated service paths, and actual hub receiver acceptance/deduplication with temporary databases.
