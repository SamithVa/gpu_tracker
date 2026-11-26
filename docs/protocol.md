# GPU usage upload protocol

The sender writes a UTF-8 JSON batch to the standard input of the hub's forced SSH command. The hub takes the sender ID from the command configured for the public key. See the [enrollment instructions](../README.md#enroll-a-remote-sender) for the SSH setup.

```json
{
  "version": 1,
  "intervals": [
    {
      "start_utc": 1795536000,
      "end_utc": 1795536030,
      "users": [{"username": "example_user", "gpus": 2, "memory_mib": 40960}]
    }
  ]
}
```

- Use protocol version `1`, with 1 to 100 intervals per batch and a maximum encoded payload of 512 KiB.
- Use Unix seconds for `start_utc` and `end_utc`. Both timestamps must be within the past 31 days or at most 60 seconds ahead of the hub clock. Each interval lasts more than zero and at most 45 seconds.
- Include one row per GPU owner per interval. `gpus` counts distinct occupied GPUs and `memory_mib` is the total allocated VRAM for that owner. Use `users: []` for idle intervals and `?` for an unknown process owner.
- The receiver commits the entire batch before exiting with status `0` and no output. It accepts an identical retry without counting usage twice. A conflicting repeat or overlap for the same sender rejects the batch.
- Keep the pending batch on failure and retry it unchanged. Advance the upload cursor only after a successful exit.
