# Code review: job engine (uncommitted snapshot, 2026-09-26)

Reviewer: independent code-reviewer subagent. Evidence came from probes against the repo's fake gateway, mutation testing of key tests, and `inspect` of the modal 1.5.5 SDK. No Modal network calls were made. At review time, `uv run pytest -q` gave 101 passed and 1 deselected, and `ruff check` was clean. Coordinator dispositions are in the last column.

| # | Severity | Finding | Location | Disposition |
|---|---|---|---|---|
| 1 | High | The timeout is checked before the poll, so after an outage longer than 30 minutes, results already finished (and billed) on Modal are thrown away. The duration and cost estimates then also include the downtime. | `atelier/worker.py:136-141` | **Accept.** Poll first, and apply the timeout only while pending. Add a test with a new `Worker` after the timeout. |
| 2 | Medium | Pillow decode and encode, sha256, file writes and `rglob` run on the event loop, measured at about 42 ms for one real PNG on the laptop. | `worker.py:157,161,170` | **Accept.** Use `asyncio.to_thread` for the storage calls. Cleanup targets the job's known paths, with no tree walk. |
| 3 | Medium | `paused` is reset during a transient backoff, so the banner is off for most of each backoff window. | `worker.py:76-81` | **Accept.** Keep the backoff reason until a spawn succeeds. |
| 4 | Medium | The final PNG and thumbnail are orphaned when a step after the save fails, and the disk guard can't see them. | `worker.py:157-162`, `storage.py:87-91,109-115` | **Accept.** Delete the saved files on any error after the save, and unlink the PNG if the thumbnail write fails. |
| 5 | Medium | A fixed seed is not range-checked. A seed of 2^63 or more renders on the GPU, then can't be stored. | `jobs.py:52-56` | **Accept.** The range is 0 ≤ seed and seed + count − 1 ≤ 2^63 − 1. Random mode keeps 1..2^31 − 1. |
| 6 | Medium | The poll error handlers can raise themselves, which aborts the tick and starves the other jobs. | `worker.py:125-132,169-173` | **Accept.** Each handler is guarded. Only errors from the save and complete steps count as store failures. |
| 7 | Medium | 16-bit grayscale thumbnails are clipped to white instead of normalized. | `storage.py:63-66` | **Accept.** Scale `I;16`, `I` and `F` to 8-bit before converting, and test that 32768 maps to about 128. |
| 8 | Medium | At least 10 contract behaviors have no test that can fail (confirmed by mutation). `shutil.disk_usage` is monkeypatched where a setting would do. | `tests/` | **Accept.** Add a test for each listed behavior, and replace the `disk_usage` patches with `dataclasses.replace(settings, …)`. The `modal.Cls.from_name` stub is recorded as the third accepted gateway-test stub. |
| 9 | Low | `cancel_job` decides from a SELECT, not from the UPDATE's rowcount. | `jobs.py:117-128` | **Accept.** Wrap it in `BEGIN IMMEDIATE` and let the rowcount decide. |
| 10 | Low | A failed blob download of a finished result (any result over 2 MiB) is classified as failed. `InternalFailure` and gRPC INTERNAL take the same path. | `modal_gateway.py:74` | **Accept.** Apply the phase's pre-decided response: add `modal.exception.InternalFailure` and `aiohttp.ClientError` to the transient allowlist, tested with real instances. The ambiguous `ExecutionError` stays failed, and the user can retry it. |
| 11 | Low | `poll()` catches only `Exception`, so a deserialized remote `BaseException` such as `SystemExit` escapes. | `modal_gateway.py:118-121` | **Accept.** Re-raise `CancelledError` and `KeyboardInterrupt`, and classify any other `BaseException`. |
| 12 | Low | `_atomic_write` leaves `.tmp` files behind on failure and doesn't fsync before `os.replace`. | `storage.py:56-60` | **Accept.** |
| 13 | Low | The numeric and timezone settings are not range-checked. | `config.py:43-50,75-78` | **Accept.** Require positive values, and validate `ZoneInfo` at load time. |
| 14 | Low | Jobs waiting only for a free slot fail after 30 minutes with "no reason recorded". | `jobs.py:198-211` | **Accept.** Record "all N slots busy" as the waiting reason. |
| 15 | Low (doc) | The phase's live-test cost estimate is out of date since the switch to a fresh-container re-render. | `phase-02-engine.md` step 14 | **Accept.** The figure is about $0.20. |

Also verified correct by the reviewer:
- every "Verified SDK facts" claim and the classification order;
- the real poll-path tests;
- the dispatch lock (removing it makes the test fail);
- the timeout path never calls Modal cancel, and `complete()` is conditional;
- migration atomicity, including a failed migration rolling back cleanly;
- FTS triggers and graph parity;
- paths built only from the job ID and the UTC date;
- the decompression-bomb guard stays on;
- the config guards.

Answers to the reviewer's open questions:
1. Finding 10 is answered by the phase's pre-decided response, as above.
2. Finding 5 uses the database range 0..2^63 − 1.
3. The rewritten live test (A → B, scale to zero, then A on a fresh container) ran with the owner's approval on 2026-09-26 and **passed** in 262 s. The same-seed renders were pixel-identical, and the second took more than 5 s. The first run, on one warm container, had shown ComfyUI's cache returning the second A in 2.3 s.
