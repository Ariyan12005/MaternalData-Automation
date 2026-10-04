Title: Integrate Atlas backend with reviewed retakes and held WhatsApp intake

Fresh SQLite startup in external extraction mode previously attempted fixture
registration and failed; invalid extractor settings silently selected fixtures.
Startup now validates configuration and preserves existing data while the
published retake/review workflow shares Atlas storage, authentication and leased
extraction. The redesigned UI retains revision checks and selected-field updates.

Encrypted originals are available through bytes or temporary paths with automatic
cleanup. Same-ref retakes invalidate old extraction revisions. Atlas hydration
preserves transport queue state without replaying outbound triggers. Cloud startup
persists an outbound hold and never sends messages; held or Atlas databases cannot
be reset through the demo API.

Validation: 110 unittest cases, 109 passed and live Atlas skipped; disposable
databases, mocked Mongo and Meta only. Manifest hash verification is recorded
in the integration handoff. No live database, Meta write or deployment used.

Limits: local OCR remains unpublished, transport worker ownership leases and
uncertain-send recovery remain Aymane coordination work, and browser validation
was unavailable. See docs/backend-integration.md for API handoff and Atlas limits.
