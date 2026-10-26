use automerge_wtransport::client::{Client, Options};
use std::{
    fs,
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::Duration,
};
use tokio::time::{Instant, sleep};
use uuid::Uuid;
struct Server {
    child: Child,
    dir: PathBuf,
    port: u16,
    hash: [u8; 32],
}
impl Server {
    async fn start() -> Self {
        let dir = std::env::temp_dir().join(format!("amwt-rust-test-{}", Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let identity = wtransport::Identity::self_signed(["localhost", "127.0.0.1"]).unwrap();
        let hash = *identity.certificate_chain().as_slice()[0].hash().as_ref();
        identity
            .certificate_chain()
            .store_pemfile(dir.join("cert.pem"))
            .await
            .unwrap();
        identity
            .private_key()
            .store_secret_pemfile(dir.join("key.pem"))
            .await
            .unwrap();
        fs::write(dir.join("hash.hex"), hex::encode(hash)).unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let child = Command::new(env!("CARGO_BIN_EXE_automerge-wtransport"))
            .current_dir(env!("CARGO_MANIFEST_DIR"))
            .env("PORT", port.to_string())
            .env("DATA_DIR", dir.join("data"))
            .env("CERT", dir.join("cert.pem"))
            .env("KEY", dir.join("key.pem"))
            .env("CERT_HASH", dir.join("hash.hex"))
            .env("EXPERIMENT_TOKEN", "rust-integration-test-token")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let mut server = Self {
            child,
            dir,
            port,
            hash,
        };
        for _ in 0..100 {
            assert!(server.child.try_wait().unwrap().is_none(), "server exited");
            if tokio::net::TcpStream::connect(("127.0.0.1", port))
                .await
                .is_ok()
            {
                return server;
            }
            sleep(Duration::from_millis(30)).await;
        }
        panic!("server startup timed out")
    }
    fn url(&self) -> String {
        format!(
            "https://127.0.0.1:{}/sync?token=rust-integration-test-token",
            self.port
        )
    }
    fn options(&self) -> Options {
        Options {
            certificate_hash: Some(self.hash),
            timeout: Duration::from_secs(5),
            ..Options::default()
        }
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_dir_all(&self.dir);
    }
}
#[tokio::test]
async fn concurrent_clients_snapshots_and_document_isolation() {
    let server = Server::start().await;
    let a = Client::connect(&server.url(), server.options())
        .await
        .unwrap();
    let b = Client::connect(&server.url(), server.options())
        .await
        .unwrap();
    let id = Uuid::new_v4();
    let mut x = a.open(id, None).await.unwrap();
    let mut y = b.open(id, None).await.unwrap();
    assert_ne!(x.document().get_actor(), y.document().get_actor());
    x.append("left 🌍").unwrap();
    y.append("right 你好").unwrap();
    let (rx, ry) = tokio::join!(x.sync(), y.sync());
    rx.unwrap();
    ry.unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while x.text().unwrap() != y.text().unwrap() || !x.acknowledged() || !y.acknowledged() {
        assert!(Instant::now() < deadline);
        let (rx, ry) = tokio::join!(
            x.receive(Duration::from_millis(20)),
            y.receive(Duration::from_millis(20))
        );
        rx.unwrap();
        ry.unwrap();
    }
    assert!(x.text().unwrap().contains("left 🌍"));
    assert!(x.text().unwrap().contains("right 你好"));
    assert_eq!(x.document().get_heads(), y.document().get_heads());
    let snapshot = x.snapshot();
    let mut isolated = a.open(Uuid::new_v4(), None).await.unwrap();
    isolated.set_text("separate").unwrap();
    isolated.sync().await.unwrap();
    assert!(!x.text().unwrap().contains("separate"));
    isolated.close().await.unwrap();
    x.close().await.unwrap();
    y.close().await.unwrap();
    let mut resumed = b.open(id, Some(&snapshot)).await.unwrap();
    drop(b);
    resumed.append(" offline 🦀").unwrap();
    resumed.sync().await.unwrap();
    let fresh = Client::connect(&server.url(), server.options())
        .await
        .unwrap();
    let mut z = fresh.open(id, None).await.unwrap();
    z.sync().await.unwrap();
    assert_eq!(z.text().unwrap(), resumed.text().unwrap());
    assert_eq!(z.document().get_heads(), resumed.document().get_heads());
    z.close().await.unwrap();
    resumed.close().await.unwrap();
}
#[tokio::test]
async fn invalid_pin_and_token_are_rejected() {
    let s = Server::start().await;
    let mut bad = s.options();
    bad.certificate_hash = Some([0; 32]);
    assert!(Client::connect(&s.url(), bad).await.is_err());
    assert!(
        Client::connect(
            &s.url().replace("rust-integration-test-token", "wrong"),
            s.options()
        )
        .await
        .is_err()
    );
}
#[tokio::test]
async fn idle_receive_can_time_out_without_losing_framing() {
    let s = Server::start().await;
    let c = Client::connect(&s.url(), s.options()).await.unwrap();
    let mut d = c.open(Uuid::new_v4(), None).await.unwrap();
    d.sync().await.unwrap();
    while d
        .receive(Duration::from_millis(10))
        .await
        .unwrap()
        .is_some()
    {}
    d.append("after idle").unwrap();
    d.sync().await.unwrap();
    assert!(d.acknowledged());
    d.close().await.unwrap();
}

#[tokio::test]
async fn bounded_reader_wrong_document_and_sync_deadline() {
    use automerge::sync::SyncDoc;
    use automerge_wtransport::protocol::{Envelope, OPEN, SYNC, read_frame, write_frame};
    for mode in ["idle", "wrong-document", "flood"] {
        let identity = wtransport::Identity::self_signed(["localhost", "127.0.0.1"]).unwrap();
        let hash = *identity.certificate_chain().as_slice()[0].hash().as_ref();
        let endpoint = wtransport::Endpoint::server(
            wtransport::ServerConfig::builder()
                .with_bind_default(0)
                .with_identity(identity)
                .build(),
        )
        .unwrap();
        let port = endpoint.local_addr().unwrap().port();
        let server = tokio::spawn(async move {
            let conn = endpoint
                .accept()
                .await
                .await
                .unwrap()
                .accept()
                .await
                .unwrap();
            let (mut send, mut recv) = conn.accept_bi().await.unwrap();
            let open = read_frame(&mut recv, 1024 * 1024).await.unwrap();
            assert_eq!(open.kind, OPEN);
            if mode != "idle" {
                let doc =
                    automerge::Automerge::load(include_bytes!("../schema.automerge")).unwrap();
                let payload = doc
                    .generate_sync_message(&mut automerge::sync::State::new())
                    .unwrap()
                    .encode();
                let id = if mode == "wrong-document" {
                    Uuid::new_v4()
                } else {
                    open.doc
                };
                for _ in 0..if mode == "flood" { 16 } else { 1 } {
                    if write_frame(
                        &mut send,
                        &Envelope {
                            kind: SYNC,
                            doc: id,
                            payload: payload.clone(),
                        },
                        1024 * 1024,
                    )
                    .await
                    .is_err()
                    {
                        break;
                    }
                }
            }
            sleep(Duration::from_secs(2)).await;
        });
        let client = Client::connect(
            &format!("https://127.0.0.1:{port}/sync"),
            Options {
                certificate_hash: Some(hash),
                timeout: Duration::from_millis(500),
                inbound_queue: 1,
                ..Options::default()
            },
        )
        .await
        .unwrap();
        let mut doc = client.open(Uuid::new_v4(), None).await.unwrap();
        if mode == "flood" {
            sleep(Duration::from_millis(100)).await;
        }
        let start = Instant::now();
        assert!(doc.sync().await.is_err(), "mode {mode}");
        assert!(start.elapsed() < Duration::from_secs(1));
        assert!(!doc.acknowledged());
        client.close();
        server.abort();
    }
}

#[tokio::test]
async fn websocket_pin_auth_and_mixed_transport_convergence() {
    use automerge_wtransport::client::WebSocketClient;
    let server = Server::start().await;
    let url = server
        .url()
        .replace("https://", "wss://")
        .replace("/sync?", "/ws?");
    let mut wrong = server.options();
    wrong.certificate_hash = Some([0; 32]);
    assert!(WebSocketClient::connect(&url, wrong).await.is_err());
    assert!(
        WebSocketClient::connect(
            &url.replace("rust-integration-test-token", "wrong"),
            server.options()
        )
        .await
        .is_err()
    );
    let wt = Client::connect(&server.url(), server.options())
        .await
        .unwrap();
    let ws = WebSocketClient::connect(&url, server.options())
        .await
        .unwrap();
    let id = Uuid::new_v4();
    let mut x = wt.open(id, None).await.unwrap();
    let mut y = ws.open(id, None).await.unwrap();
    x.append("WebTransport 🌍").unwrap();
    y.append("WebSocket 你好").unwrap();
    let (a, b) = tokio::join!(x.sync(), y.sync());
    a.unwrap();
    b.unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while x.document().get_heads() != y.document().get_heads() {
        assert!(Instant::now() < deadline);
        let (a, b) = tokio::join!(
            x.receive(Duration::from_millis(20)),
            y.receive(Duration::from_millis(20))
        );
        a.unwrap();
        b.unwrap();
    }
    assert_eq!(x.text().unwrap(), y.text().unwrap());
    assert!(x.text().unwrap().contains("你好"));
    x.close().await.unwrap();
    y.close().await.unwrap();
    wt.close();
}
