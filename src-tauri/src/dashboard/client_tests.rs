use super::*;
use axum::{routing::get, Json, Router};
use serde_json::json;

async fn server(app: Router) -> (String, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let task = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (format!("http://{address}"), task)
}

#[test]
fn rejects_unsafe_service_destinations() {
    for origin in [
        "http://intranet.example", "https://user:password@example.com",
        "https://example.com/path", "https://example.com?token=secret",
        "https://example.com#fragment", "file:///tmp/service",
    ] {
        assert!(DashboardClient::new(origin).is_err(), "{origin}");
    }
    assert!(DashboardClient::new("https://dashboards.example.com").is_ok());
}

#[tokio::test]
async fn employee_credential_is_forwarded_and_machine_identity_is_rejected() {
    let app = Router::new().route("/api/v1/me", get(|headers: HeaderMap| async move {
        assert_eq!(headers.get("authorization").unwrap(), "Bearer employee-token");
        Json(json!({"principal_id":"svc-1", "principal_type":"service", "display_name":"CI"}))
    }));
    let (origin, task) = server(app).await;
    let client = DashboardClient::new(&origin).unwrap();
    assert_eq!(client.verify_human("employee-token").await.unwrap_err().code, "dashboard_human_required");
    task.abort();
}

#[tokio::test]
async fn verified_employee_identity_is_returned() {
    let app = Router::new().route("/api/v1/me", get(|| async {
        Json(json!({"principal_id":"user-1", "principal_type":"human", "display_name":"Employee"}))
    }));
    let (origin, task) = server(app).await;
    let client = DashboardClient::new(&origin).unwrap();
    assert_eq!(client.verify_human("employee-token").await.unwrap().principal_id, "user-1");
    task.abort();
}

#[tokio::test]
async fn redirects_are_not_followed_and_errors_do_not_echo_credentials() {
    let app = Router::new()
        .route("/api/v1/me", get(|| async {
            (StatusCode::TEMPORARY_REDIRECT, [("location", "/should-not-follow")], "employee-secret")
        }))
        .route("/should-not-follow", get(|| async { panic!("redirect followed"); "" }));
    let (origin, task) = server(app).await;
    let client = DashboardClient::new(&origin).unwrap();
    let error = client.verify_human("employee-secret").await.unwrap_err();
    assert!(!error.message.contains("employee-secret"));
    assert_eq!(error.code, "dashboard_service_redirect");
    assert!(client.get_json("employee-secret", "/api/v1/../admin", &[]).await.is_err());
    task.abort();
}

#[test]
fn missing_or_malformed_authorization_is_rejected() {
    let mut headers = HeaderMap::new();
    assert!(extract_token(&headers).is_err());
    headers.insert("authorization", "Bearer abc def".parse().unwrap());
    assert!(extract_token(&headers).is_err());
    headers.insert("authorization", "Bearer abc".parse().unwrap());
    assert_eq!(extract_token(&headers).unwrap(), "abc");
}
