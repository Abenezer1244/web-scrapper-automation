"""Credits-limited resubmission math for the skip-trace dispatcher
(src/workers/skip_trace_dispatcher.py). Pure, no DB, no Tracerfy.

Pinned from prod on 2026-09-02: the account had 118 credits, the FIFO head was a
344-row normal batch, and every 5-minute tick failed 402 ("You need 226 more
credits") and returned — 565 rows across 7 jobs sat in 'queued' for 7+ hours,
and even a 200-credit top-up would not have unblocked the 344-row batch.
"""
import pytest

from src.workers.skip_trace_dispatcher import affordable_row_count, classify_submit_failure

_PROD_402 = (
    'Tracerfy returned 402: {"error":"Insufficient credits for normal trace. '
    'You need 226 more credits to complete this request. Your account requires '
    'sufficient credits to upload. Please add credits to your account."}'
)


def test_prod_message_normal_trace():
    # 344 rows × 1 credit, 226 short → 118 affordable
    assert affordable_row_count(_PROD_402, 344, "normal") == 118


def test_advanced_trace_costs_two_credits_per_row():
    # 79 rows × 2 = 158 credits needed, 40 short → 118 available → 59 rows
    msg = "Tracerfy returned 402: You need 40 more credits"
    assert affordable_row_count(msg, 79, "advanced") == 59


def test_advanced_odd_credit_rounds_down():
    # 10 rows × 2 = 20 needed, 15 short → 5 available → 2 rows (not 2.5)
    assert affordable_row_count("need 15 more credits", 10, "advanced") == 2


def test_shortfall_equal_or_above_batch_cost_is_zero():
    assert affordable_row_count("need 344 more credits", 344, "normal") == 0
    assert affordable_row_count("need 500 more credits", 344, "normal") == 0


def test_unparseable_message_does_not_guess():
    assert affordable_row_count("Tracerfy returned 402: Insufficient credits", 344, "normal") == 0
    assert affordable_row_count("", 344, "normal") == 0
    assert affordable_row_count(None, 344, "normal") == 0


def test_never_exceeds_batch_size():
    # A nonsensical "need 0 more" still caps at the batch
    assert affordable_row_count("need 0 more credits", 5, "normal") == 5


@pytest.mark.parametrize("n", [0, -1])
def test_empty_batch_is_zero(n):
    assert affordable_row_count(_PROD_402, n, "normal") == 0


def test_singular_credit_wording():
    assert affordable_row_count("You need 1 more credit to complete this request.", 3, "normal") == 2


# ─── classify_submit_failure: what the claim does with each failure ──────────

@pytest.mark.parametrize("message, kind", [
    (_PROD_402, "out_of_credits"),
    ("Tracerfy rate limit hit (429). Dispatcher should back off and retry next tick.", "rate_limited"),
    ("Connection error submitting batch: HTTPSConnectionPool(...) refused", "connection_error"),
    ("Tracerfy returned 503: upstream unavailable", "provider_unavailable"),
    ("Tracerfy returned 500: internal error", "provider_unavailable"),
    # Ambiguous — Tracerfy may have accepted and charged the batch.
    ("Network error submitting batch: HTTPSConnectionPool(...) Read timed out.", "unknown_outcome"),
    ("Tracerfy returned non-JSON: <html>", "unknown_outcome"),
    ("Tracerfy response missing queue_id: {'ok': true}", "unknown_outcome"),
    # Definite rejections of THIS batch / configuration.
    ("Tracerfy returned 400: bad json_data", "provider_error"),
    ("TRACERFY_API_BASE_URL must use HTTPS", "provider_error"),
    ("Refusing unsafe Tracerfy endpoint: private address", "provider_error"),
])
def test_classify_submit_failure(message, kind):
    assert classify_submit_failure(message) == kind
