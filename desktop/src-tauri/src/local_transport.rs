//! Loopback-only HTTP transport for the KELP local sidecar.
//!
//! This module is the one place the desktop process talks HTTP to the local
//! Python child. It is deliberately smaller than the hosted [`crate::bridge`]:
//! one dedicated [`reqwest::Client`], one literal loopback origin, and one
//! fixed request profile. The caller never supplies a URL or a header.
//!
//! The committed Python contract (`chat/local_http.py`, `chat/api.py`) accepts
//! only:
//!
//! * exactly one canonical `Host` (reqwest writes `127.0.0.1:<port>`),
//! * exactly one `Origin: http://127.0.0.1:<port>`,
//! * exactly one `Authorization: Bearer <owner>`,
//! * an `X-CSRF-Token` equal to the launch CSRF token on mutations,
//! * no cookie.
//!
//! The transport therefore always sends `Origin` and the bearer, adds the CSRF
//! header only on unsafe methods, disables the cookie jar, disables redirects,
//! ignores proxy environment variables, and refuses anything outside the exact
//! thread list/create/detail, send/stop/retry, and stream profile. The health
//! probe is internal to [`LocalTransport::verify_guard`] and is never part of
//! the public request surface.
//!
//! Secrets live in sensitive [`HeaderValue`]s. Neither the transport nor its
//! credentials implement `Debug`/`Serialize`, and errors carry fixed text: no
//! token, header value, or URL is ever formatted into a message.
//!
//! The module is compiled only for Linux tests at this stage; no desktop
//! entrypoint is bound to it.

use std::fmt;
use std::num::NonZeroU16;
use std::time::Duration;

use reqwest::header::{HeaderValue, AUTHORIZATION, ORIGIN};
use reqwest::{Client, Method, RequestBuilder, Url};
use serde_json::Value;

/// Literal IPv4 loopback. Only the validated port varies.
const LOOPBACK_HOST: &str = "127.0.0.1";
/// A loopback connect should either succeed immediately or fail fast.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
/// Overall deadline for one non-streaming REST request.
const REST_TIMEOUT: Duration = Duration::from_secs(10);
/// Bound for the status envelope read by the guard tripwire.
const GUARD_BODY_LIMIT: usize = 4_096;
/// Bound for the owner and CSRF header values.
const CREDENTIAL_LIMIT: usize = 1_024;
/// The Local contract's mutation header. Never sent on GET.
const CSRF_HEADER: &str = "x-csrf-token";
/// The Local contract's one id segment limit.
const MAX_ID_LEN: usize = 128;

/// A fixed local-transport refusal.
///
/// Display and Debug text contain no credential, header value, path, or URL.
/// The variants are returned before any socket I/O except for
/// [`Self::GuardProbeFailed`], which is the one fixed result of a mismatched or
/// broken guard probe.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum LocalTransportError {
    InvalidOwnerCredential,
    InvalidCsrfCredential,
    ClientBuild,
    UnsupportedRequest,
    GuardProbeFailed,
}

impl fmt::Display for LocalTransportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidOwnerCredential => "local owner credential is not usable",
            Self::InvalidCsrfCredential => "local csrf credential is not usable",
            Self::ClientBuild => "local transport client could not be built",
            Self::UnsupportedRequest => "local transport request is not permitted",
            Self::GuardProbeFailed => "local transport guard verification failed",
        })
    }
}

impl std::error::Error for LocalTransportError {}

/// One loopback HTTP client bound to one sidecar launch.
///
/// Not `Debug`/`Serialize`: the owner and CSRF header values are sensitive and
/// must not be printed through a container. Access is limited to the methods
/// below.
pub(crate) struct LocalTransport {
    client: Client,
    port: NonZeroU16,
    origin: HeaderValue,
    owner: HeaderValue,
    csrf: HeaderValue,
}

impl LocalTransport {
    /// Build a transport for a validated ready-frame port.
    ///
    /// `owner` and `csrf` come from `Supervisor::credentials()`. Empty values,
    /// non-visible-ASCII values, or values too large to be a header are refused
    /// before the client is created. The port is a [`NonZeroU16`], so the
    /// origin is always a real `http://127.0.0.1:<port>`.
    pub(crate) fn new(
        port: NonZeroU16,
        owner: &str,
        csrf: &str,
    ) -> Result<Self, LocalTransportError> {
        credential(owner).ok_or(LocalTransportError::InvalidOwnerCredential)?;
        let csrf = credential(csrf).ok_or(LocalTransportError::InvalidCsrfCredential)?;
        let owner = sensitive_header(&format!("Bearer {owner}"))
            .ok_or(LocalTransportError::InvalidOwnerCredential)?;
        let origin = HeaderValue::from_str(&origin_for(port))
            .map_err(|_| LocalTransportError::ClientBuild)?;
        let client = Client::builder()
            // The desktop environment may carry proxy settings. A loopback
            // sidecar must never be reached through one.
            .no_proxy()
            // No ambient credential: the Local guard reads the bearer only.
            .cookie_store(false)
            // A redirect is a server behaviour change, not a transport detail.
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(CONNECT_TIMEOUT)
            .build()
            .map_err(|_| LocalTransportError::ClientBuild)?;
        Ok(Self {
            client,
            port,
            origin,
            owner,
            csrf,
        })
    }

    /// Build one allowlisted REST request.
    ///
    /// `path` is matched literally against the Local profile: thread list with
    /// at most one `limit=<digits>` query, thread create, thread detail, send,
    /// stop, and retry. There is no prefix check and no caller header. `body`
    /// is JSON only where the endpoint takes one.
    pub(crate) fn api_request(
        &self,
        method: Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<RequestBuilder, LocalTransportError> {
        let route = RestRoute::parse(&method, path)?;
        if route.is_read() && body.is_some() {
            return Err(LocalTransportError::UnsupportedRequest);
        }
        if route.requires_body() && body.is_none() {
            return Err(LocalTransportError::UnsupportedRequest);
        }
        if route.forbids_body() && body.is_some() {
            return Err(LocalTransportError::UnsupportedRequest);
        }

        let url = self.url(path)?;
        let mut request = self
            .client
            .request(method, url)
            .timeout(REST_TIMEOUT)
            .header(ORIGIN, self.origin.clone())
            .header(AUTHORIZATION, self.owner.clone());
        if route.is_mutation() {
            request = request.header(CSRF_HEADER, self.csrf.clone());
        }
        if let Some(payload) = body {
            request = request.json(&payload);
        }
        Ok(request)
    }

    /// Build the SSE request for one trusted thread id and cursor.
    ///
    /// The path is assembled from an already validated id and a `u64`, so no
    /// caller text reaches the query. The request carries `Origin` and the
    /// bearer like every other Local request, but no CSRF header because the
    /// method is GET. No overall timeout is installed: the stream is expected
    /// to stay open.
    pub(crate) fn stream_request(
        &self,
        thread_id: &str,
        after: u64,
    ) -> Result<RequestBuilder, LocalTransportError> {
        if !valid_id(thread_id) {
            return Err(LocalTransportError::UnsupportedRequest);
        }
        let path = format!("/api/threads/{thread_id}/stream?after={after}");
        let url = self.url(&path)?;
        Ok(self
            .client
            .get(url)
            .header(ORIGIN, self.origin.clone())
            .header(AUTHORIZATION, self.owner.clone()))
    }

    /// Provisionally verify that this port is the committed guarded Local
    /// server, using only bounded read-only requests.
    ///
    /// The sequence is fixed:
    ///
    /// 1. `GET /api/healthz` with the bearer and **no** `Origin` must answer
    ///    403 `invalid_origin`.
    /// 2. `GET /api/healthz` with the exact `Origin` and **no** bearer must
    ///    answer 401 `unauthorized`.
    /// 3. `GET /api/health` with both must answer 200.
    /// 4. `GET /api/auth/devices` with both must answer 404 `not_found`, which
    ///    proves the profile gate runs before hosted routing.
    ///
    /// Any transport error, status, missing/malformed envelope, or oversize
    /// body collapses to [`LocalTransportError::GuardProbeFailed`]. `/api/readyz`
    /// is never probed: older hosted servers could dial a provider there.
    pub(crate) async fn verify_guard(&self) -> Result<(), LocalTransportError> {
        let (status, code) = self.probe("/api/healthz", false, true).await?;
        if status != 403 || code.as_deref() != Some("invalid_origin") {
            return Err(LocalTransportError::GuardProbeFailed);
        }

        let (status, code) = self.probe("/api/healthz", true, false).await?;
        if status != 401 || code.as_deref() != Some("unauthorized") {
            return Err(LocalTransportError::GuardProbeFailed);
        }

        let (status, _) = self.probe("/api/health", true, true).await?;
        if status != 200 {
            return Err(LocalTransportError::GuardProbeFailed);
        }

        let (status, code) = self.probe("/api/auth/devices", true, true).await?;
        if status != 404 || code.as_deref() != Some("not_found") {
            return Err(LocalTransportError::GuardProbeFailed);
        }
        Ok(())
    }

    async fn probe(
        &self,
        path: &str,
        with_origin: bool,
        with_bearer: bool,
    ) -> Result<(u16, Option<String>), LocalTransportError> {
        let url = self
            .url(path)
            .map_err(|_| LocalTransportError::GuardProbeFailed)?;
        let mut request = self.client.get(url).timeout(REST_TIMEOUT);
        if with_origin {
            request = request.header(ORIGIN, self.origin.clone());
        }
        if with_bearer {
            request = request.header(AUTHORIZATION, self.owner.clone());
        }
        let response = request
            .send()
            .await
            .map_err(|_| LocalTransportError::GuardProbeFailed)?;
        let status = response.status().as_u16();
        let body = read_bounded(response).await?;
        let code = if status == 200 {
            None
        } else {
            error_code(&body)
        };
        Ok((status, code))
    }

    fn url(&self, path: &str) -> Result<Url, LocalTransportError> {
        Url::parse(&format!("{}{path}", origin_for(self.port)))
            .map_err(|_| LocalTransportError::UnsupportedRequest)
    }
}

/// The Local profile's REST routes. Ordering here mirrors the committed
/// `_local_profile_target` in `chat/api.py`.
#[derive(Clone, Copy, PartialEq, Eq)]
enum RestRoute {
    ListThreads,
    CreateThread,
    ThreadDetail,
    SendTurn,
    StopTurn,
    RetryTurn,
}

impl RestRoute {
    fn parse(method: &Method, target: &str) -> Result<Self, LocalTransportError> {
        // Origin-form only: one leading slash, no scheme/host, no fragment.
        if target.is_empty()
            || !target.starts_with('/')
            || target.starts_with("//")
            || target.contains('#')
        {
            return Err(LocalTransportError::UnsupportedRequest);
        }
        let (path, query) = match target.split_once('?') {
            Some((path, query)) => (path, Some(query)),
            None => (target, None),
        };
        // A second `?` is not a query value the profile accepts.
        if matches!(query, Some(query) if query.contains('?')) {
            return Err(LocalTransportError::UnsupportedRequest);
        }
        let mut pieces = path.split('/');
        if pieces.next() != Some("") {
            return Err(LocalTransportError::UnsupportedRequest);
        }
        let segments: Vec<&str> = pieces.collect();
        if segments.is_empty() || segments.iter().any(|segment| segment.is_empty()) {
            return Err(LocalTransportError::UnsupportedRequest);
        }

        if *method == Method::GET {
            match segments.as_slice() {
                ["api", "threads"] => {
                    digits_query(query, "limit")?;
                    Ok(Self::ListThreads)
                }
                ["api", "threads", id] if valid_id(id) => {
                    no_query(query)?;
                    Ok(Self::ThreadDetail)
                }
                _ => Err(LocalTransportError::UnsupportedRequest),
            }
        } else if *method == Method::POST {
            match segments.as_slice() {
                ["api", "threads"] => {
                    no_query(query)?;
                    Ok(Self::CreateThread)
                }
                ["api", "threads", id, "turns"] if valid_id(id) => {
                    no_query(query)?;
                    Ok(Self::SendTurn)
                }
                ["api", "turns", id, "stop"] if valid_id(id) => {
                    no_query(query)?;
                    Ok(Self::StopTurn)
                }
                ["api", "turns", id, "retry"] if valid_id(id) => {
                    no_query(query)?;
                    Ok(Self::RetryTurn)
                }
                _ => Err(LocalTransportError::UnsupportedRequest),
            }
        } else {
            Err(LocalTransportError::UnsupportedRequest)
        }
    }

    fn is_read(self) -> bool {
        matches!(self, Self::ListThreads | Self::ThreadDetail)
    }

    fn is_mutation(self) -> bool {
        !self.is_read()
    }

    fn requires_body(self) -> bool {
        matches!(self, Self::SendTurn)
    }

    fn forbids_body(self) -> bool {
        matches!(self, Self::StopTurn | Self::RetryTurn)
    }
}

fn origin_for(port: NonZeroU16) -> String {
    format!("http://{LOOPBACK_HOST}:{port}")
}

/// Accept only a non-empty, bounded, visible-ASCII credential. CR/LF, spaces,
/// tabs, and non-ASCII all fail closed.
fn credential(value: &str) -> Option<HeaderValue> {
    if value.is_empty() || value.len() > CREDENTIAL_LIMIT {
        return None;
    }
    if !value.bytes().all(|byte| (0x21..=0x7e).contains(&byte)) {
        return None;
    }
    sensitive_header(value)
}

fn sensitive_header(value: &str) -> Option<HeaderValue> {
    let mut header = HeaderValue::from_str(value).ok()?;
    header.set_sensitive(true);
    Some(header)
}

fn valid_id(id: &str) -> bool {
    (1..=MAX_ID_LEN).contains(&id.len())
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
}

fn no_query(query: Option<&str>) -> Result<(), LocalTransportError> {
    match query {
        None => Ok(()),
        Some(_) => Err(LocalTransportError::UnsupportedRequest),
    }
}

/// Allow exactly one `name=<digits>` pair. The value is left as written; the
/// server owns its numeric range.
fn digits_query(query: Option<&str>, name: &str) -> Result<(), LocalTransportError> {
    let Some(query) = query else {
        return Ok(());
    };
    let (key, value) = query
        .split_once('=')
        .ok_or(LocalTransportError::UnsupportedRequest)?;
    if key != name || value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(LocalTransportError::UnsupportedRequest);
    }
    Ok(())
}

/// Read at most [`GUARD_BODY_LIMIT`] bytes from a probe response.
async fn read_bounded(mut response: reqwest::Response) -> Result<Vec<u8>, LocalTransportError> {
    let mut body = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| LocalTransportError::GuardProbeFailed)?
    {
        if body.len().saturating_add(chunk.len()) > GUARD_BODY_LIMIT {
            return Err(LocalTransportError::GuardProbeFailed);
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

fn error_code(body: &[u8]) -> Option<String> {
    let value: Value = serde_json::from_slice(body).ok()?;
    value.get("error")?.get("code")?.as_str().map(str::to_owned)
}

#[cfg(test)]
mod tests;
