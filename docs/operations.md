# Operations

## A provider reports `structure_changed`

`structure_changed` means the provider's page or export format changed. Aggregato does not retry
it: repeated automated requests would only hit the same incompatible format. The standard operator
response is:

1. Check the provider's action-required message and preserve the retained failure payload.
2. Pull the current release: `docker compose -f docker/compose.yml pull`.
3. Restart it: `docker compose -f docker/compose.yml up -d`.
4. Trigger one provider sync from the Providers screen. If it still reports `structure_changed`,
   file an issue with the provider id and the sanitized failure context; do not keep retrying it.

## Retention and storage growth

The defaults retain raw payloads for 90 days, successful sync history for 30 days, and failed or
partial runs for 180 days. Failures are intentionally retained longer because they are the evidence
needed to repair a provider or replay a poison record. The cleanup worker runs at startup and then
daily.

Storage usage in Settings separates database bytes, retained raw payload bytes, and image-cache
bytes. Raw payloads are the offline replay source, so lowering that retention trades away the
ability to rebuild old derived data without contacting a platform. Turning off image caching returns
a local placeholder and cleanup removes cached image files. Export an archive before lowering any
retention setting if the old data matters.
