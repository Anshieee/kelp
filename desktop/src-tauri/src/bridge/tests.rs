//! Focused unit tests for the Local command mapping. Nothing here builds a
//! Tauri application or opens a socket.

use super::*;

use std::num::NonZeroU16;
use std::sync::atomic::{AtomicUsize, Ordering};

use crate::local_transport::LocalTransport;

fn dummy_transport() -> LocalTransport {
    LocalTransport::new(
        NonZeroU16::new(9).expect("nonzero port"),
        "owner-token-0123456789abcdef",
        "csrf-token-0123456789abcdef",
    )
    .expect("valid transport")
}

#[test]
fn local_redirects_are_refused_without_parsing_the_body() {
    let marker = "http://127.0.0.1:9/leak";
    let body = format!(r#"{{"url":"{marker}","error":{{"code":"not_found"}}}}"#);
    let error = local_error(302, body.as_bytes());
    assert_eq!(error.status, 502);
    assert_eq!(error.code, "unexpected_redirect");
    assert!(!error.retryable);
    assert!(!error.message.contains(marker));
    assert!(!error.message.contains("127.0.0.1"));
}

#[test]
fn local_auth_failure_is_fixed_non_401() {
    let error = local_error(
        401,
        br#"{"ok":false,"error":{"code":"unauthorized","message":"The local owner credential is required."}}"#,
    );
    assert_eq!(error.status, 500);
    assert_ne!(error.status, 401);
    assert_eq!(error.code, "local_auth_failed");
    assert!(!error.retryable);
    assert!(!error.message.contains("127.0.0.1"));
    assert!(!error.message.contains("Bearer"));
    assert!(!error.message.contains("owner-token"));

    let not_found = local_error(
        404,
        br#"{"ok":false,"error":{"code":"not_found","message":"Unknown endpoint."}}"#,
    );
    assert_eq!(not_found.status, 404);
    assert_eq!(not_found.code, "not_found");

    let malformed = local_error(500, b"not json");
    assert_eq!(malformed.status, 500);
    assert_eq!(malformed.code, "request_failed");
    assert!(!malformed.message.contains("127.0.0.1"));
}

#[test]
fn local_approval_decisions_are_detected_but_not_forwarded() {
    assert!(is_local_approval_decision(
        &reqwest::Method::POST,
        "/api/approvals/approval_1/decision"
    ));
    assert!(!is_local_approval_decision(
        &reqwest::Method::GET,
        "/api/approvals/approval_1/decision"
    ));
    assert!(!is_local_approval_decision(
        &reqwest::Method::POST,
        "/api/approvals/approval.1/decision"
    ));
    assert!(!is_local_approval_decision(
        &reqwest::Method::POST,
        "/api/approvals/approval_1"
    ));
    assert_eq!(
        ApiError::local_approvals_unavailable().code,
        "local_approvals_unavailable"
    );
    assert!(!ApiError::local_approvals_unavailable().retryable);
}

#[test]
fn local_metadata_exposes_no_endpoint_or_credential() {
    let bridge = Bridge::local(dummy_transport(), Arc::new(AtomicBool::new(false)));
    assert!(bridge.is_local());
    assert_eq!(bridge.backend_name(), "local");
    assert_eq!(bridge.server_label(), "local");
    assert!(!bridge.server_label().contains("http"));
    assert!(!bridge.server_label().contains(':'));
    assert!(bridge.hosted().is_none());
}

#[tokio::test]
async fn a_closed_local_child_is_refused_before_any_request() {
    let bridge = Bridge::local(dummy_transport(), Arc::new(AtomicBool::new(true)));
    assert!(bridge.local_closed());
    let error = bridge
        .local_request(reqwest::Method::GET, "/api/threads", None)
        .await
        .expect_err("closed child must be refused");
    assert_eq!(error.code, "local_unavailable");
    assert_eq!(error.status, 0);
    assert!(!error.retryable);
    assert!(!error.message.contains("127.0.0.1"));
}

#[test]
fn local_stream_replacement_suppresses_stale_emissions() {
    let streams = LocalStreams::new();
    let closed = AtomicBool::new(false);
    let (first, first_rx) = streams.start("t1", &closed).expect("first registration");
    let (second, _second_rx) = streams.start("t1", &closed).expect("replacement");

    assert_ne!(first, second);
    assert!(*first_rx.borrow(), "replacement must cancel the old stream");
    assert!(!streams.is_current("t1", first));
    assert!(streams.is_current("t1", second));

    let emitted = AtomicUsize::new(0);
    assert!(!streams.emit_if_current("t1", first, || {
        emitted.fetch_add(1, Ordering::SeqCst);
    }));
    assert!(streams.emit_if_current("t1", second, || {
        emitted.fetch_add(1, Ordering::SeqCst);
    }));
    assert_eq!(emitted.load(Ordering::SeqCst), 1);

    let closed_emits = AtomicUsize::new(0);
    assert!(!streams.finish_if_current("t1", first, || {
        closed_emits.fetch_add(1, Ordering::SeqCst);
    }));
    assert_eq!(closed_emits.load(Ordering::SeqCst), 0);
    assert!(
        streams.is_current("t1", second),
        "stale finish removed the current registration"
    );

    assert!(streams.finish_if_current("t1", second, || {
        closed_emits.fetch_add(1, Ordering::SeqCst);
    }));
    assert_eq!(closed_emits.load(Ordering::SeqCst), 1);
    assert!(!streams.is_current("t1", second));
}

#[test]
fn local_explicit_close_signals_without_removing_the_registration() {
    let streams = LocalStreams::new();
    let closed = AtomicBool::new(false);
    let (generation, receiver) = streams.start("t1", &closed).expect("registration");

    streams.cancel_current("t1");
    assert!(*receiver.borrow(), "cancel must be visible to the receiver");
    assert!(
        streams.is_current("t1", generation),
        "explicit close must leave cleanup to the task"
    );

    let mut closed_emits = 0;
    assert!(streams.finish_if_current("t1", generation, || closed_emits += 1));
    assert_eq!(closed_emits, 1);
    assert!(!streams.is_current("t1", generation));
}

#[test]
fn local_closed_flag_blocks_insertion_and_cancel_all_drains() {
    let streams = LocalStreams::new();
    let closed = AtomicBool::new(false);
    let (generation, receiver) = streams.start("t1", &closed).expect("registration");

    closed.store(true, Ordering::SeqCst);
    assert!(streams.start("t2", &closed).is_none());

    streams.cancel_all();
    assert!(*receiver.borrow(), "cancel must be visible to the receiver");
    assert!(!streams.is_current("t1", generation));
}
