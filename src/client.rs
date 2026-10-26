use crate::protocol::{CLOSE, Envelope, OPEN, SYNC, read_frame, write_frame};
use anyhow::{Context, Result, bail, ensure};
use automerge::{
    Automerge, ObjType, ROOT, ReadDoc, Value,
    sync::{Message, State, SyncDoc},
    transaction::Transactable,
};
use futures_util::{SinkExt, StreamExt, stream::SplitSink};
use std::{sync::Arc, time::Duration};
use tokio::{
    sync::mpsc,
    task::JoinHandle,
    time::{Instant, timeout},
};
use tokio_tungstenite::{
    MaybeTlsStream, WebSocketStream,
    tungstenite::{Message as WsMessage, protocol::WebSocketConfig},
};
use uuid::Uuid;
use wtransport::{ClientConfig, Connection, Endpoint, endpoint::endpoint_side, tls::Sha256Digest};

#[derive(Clone, Debug)]
pub struct Options {
    /// SHA-256 of the DER certificate; None uses the platform root store.
    pub certificate_hash: Option<[u8; 32]>,
    pub timeout: Duration,
    pub max_frame: usize,
    pub inbound_queue: usize,
}
impl Default for Options {
    fn default() -> Self {
        Self {
            certificate_hash: None,
            timeout: Duration::from_secs(30),
            max_frame: 16 * 1024 * 1024,
            inbound_queue: 8,
        }
    }
}
/// One authenticated WebTransport session; each open document owns its own stream.
pub struct Client {
    endpoint: Arc<Endpoint<endpoint_side::Client>>,
    connection: Connection,
    options: Options,
}
impl Client {
    /// `url` is the complete `https://host:4433/sync?token=…` URL.
    pub async fn connect(url: &str, options: Options) -> Result<Self> {
        ensure!(
            (18..=64 * 1024 * 1024).contains(&options.max_frame),
            "invalid frame limit"
        );
        ensure!(
            options.inbound_queue > 0 && options.inbound_queue <= 1024,
            "invalid queue capacity"
        );
        ensure!(!options.timeout.is_zero(), "timeout must be positive");
        rustls::crypto::ring::default_provider()
            .install_default()
            .ok();
        let builder = ClientConfig::builder().with_bind_default();
        let builder = match options.certificate_hash {
            Some(hash) => builder.with_server_certificate_hashes([Sha256Digest::new(hash)]),
            None => builder.with_native_certs(),
        };
        let endpoint = Arc::new(Endpoint::client(builder.build())?);
        let connection = timeout(options.timeout, endpoint.connect(url))
            .await
            .context("WebTransport connect timed out")??;
        Ok(Self {
            endpoint,
            connection,
            options,
        })
    }
    /// Load a snapshot (with a fresh actor) or the common schema, then start sync.
    pub async fn open(&self, id: Uuid, snapshot: Option<&[u8]>) -> Result<Document> {
        let doc = Automerge::load(snapshot.unwrap_or(include_bytes!("../schema.automerge")))?;
        let (mut send, mut recv) = timeout(self.options.timeout, async {
            Ok::<_, anyhow::Error>(self.connection.open_bi().await?.await?)
        })
        .await
        .context("opening document stream timed out")??;
        timeout(
            self.options.timeout,
            write_frame(
                &mut send,
                &Envelope {
                    kind: OPEN,
                    doc: id,
                    payload: vec![],
                },
                self.options.max_frame,
            ),
        )
        .await??;
        let (tx, incoming) = mpsc::channel(self.options.inbound_queue);
        let max = self.options.max_frame;
        let reader = tokio::spawn(async move {
            loop {
                let result = read_frame(&mut recv, max).await.and_then(|e| {
                    ensure!(
                        e.kind == SYNC && e.doc == id,
                        "unexpected document or message type"
                    );
                    Ok(e.payload)
                });
                let failed = result.is_err();
                // Never drop a sync message and continue with advanced sync state.
                if tx.try_send(result).is_err() {
                    recv.stop(wtransport::VarInt::from_u32(1));
                    break;
                }
                if failed {
                    break;
                }
            }
        });
        Ok(Document {
            id,
            doc,
            state: State::new(),
            send: WireSend::WebTransport(send),
            incoming,
            reader,
            options: self.options.clone(),
            failed: false,
            _endpoint: Some(self.endpoint.clone()),
            _connection: Some(self.connection.clone()),
        })
    }
    pub fn close(&self) {
        self.connection
            .close(wtransport::VarInt::from_u32(0), b"client closed")
    }
}

type WsSocket = WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>;
enum WireSend {
    WebTransport(wtransport::SendStream),
    WebSocket(SplitSink<WsSocket, WsMessage>),
}
impl WireSend {
    async fn write(&mut self, envelope: &Envelope, max: usize) -> Result<()> {
        match self {
            Self::WebTransport(send) => write_frame(send, envelope, max).await,
            Self::WebSocket(send) => {
                let bytes = envelope.encode();
                ensure!(bytes.len() <= max, "outbound frame exceeds limit");
                send.send(WsMessage::Binary(bytes.into())).await?;
                Ok(())
            }
        }
    }
    fn reset(&mut self, code: u32) {
        if let Self::WebTransport(send) = self {
            let _ = send.reset(wtransport::VarInt::from_u32(code));
        }
    }
}

/// Pinned WSS benchmark connection. One document per connection in this native
/// adapter; the server and browser adapter also support multiplexing documents.
pub struct WebSocketClient {
    socket: WsSocket,
    options: Options,
}
impl WebSocketClient {
    pub async fn connect(url: &str, options: Options) -> Result<Self> {
        ensure!(
            url.starts_with("wss://"),
            "WebSocket benchmark requires TLS"
        );
        ensure!(
            (18..=64 * 1024 * 1024).contains(&options.max_frame),
            "invalid frame limit"
        );
        ensure!(
            (1..=1024).contains(&options.inbound_queue),
            "invalid queue capacity"
        );
        ensure!(!options.timeout.is_zero(), "timeout must be positive");
        let hash = options
            .certificate_hash
            .context("WebSocket benchmark requires a certificate pin")?;
        rustls::crypto::ring::default_provider()
            .install_default()
            .ok();
        // Same verifier as WT: pin, P256, current validity, and <=14 day lifetime.
        let verifier =
            wtransport::tls::client::ServerHashVerification::new([Sha256Digest::new(hash)]);
        let tls = rustls::ClientConfig::builder_with_protocol_versions(&[&rustls::version::TLS13])
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(verifier))
            .with_no_client_auth();
        let config = WebSocketConfig::default()
            .write_buffer_size(0)
            .max_message_size(Some(options.max_frame))
            .max_frame_size(Some(options.max_frame));
        // disable_nagle=true sets TCP_NODELAY. Compression is not supported or negotiated.
        let (socket, _) = timeout(
            options.timeout,
            tokio_tungstenite::connect_async_tls_with_config(
                url,
                Some(config),
                true,
                Some(tokio_tungstenite::Connector::Rustls(Arc::new(tls))),
            ),
        )
        .await
        .context("WebSocket connect timed out")??;
        Ok(Self { socket, options })
    }
    pub async fn open(self, id: Uuid, snapshot: Option<&[u8]>) -> Result<Document> {
        let doc = Automerge::load(snapshot.unwrap_or(include_bytes!("../schema.automerge")))?;
        let (sink, mut source) = self.socket.split();
        let mut send = WireSend::WebSocket(sink);
        timeout(
            self.options.timeout,
            send.write(
                &Envelope {
                    kind: OPEN,
                    doc: id,
                    payload: vec![],
                },
                self.options.max_frame,
            ),
        )
        .await??;
        let (tx, incoming) = mpsc::channel(self.options.inbound_queue);
        let max = self.options.max_frame;
        let reader = tokio::spawn(async move {
            loop {
                let result = match source.next().await {
                    Some(Ok(WsMessage::Binary(bytes))) => {
                        Envelope::decode(&bytes, max).and_then(|e| {
                            ensure!(
                                e.kind == SYNC && e.doc == id,
                                "unexpected document or message type"
                            );
                            Ok(e.payload)
                        })
                    }
                    Some(Ok(WsMessage::Pong(_))) => continue,
                    _ => Err(anyhow::anyhow!(
                        "WebSocket reader closed or unexpected message"
                    )),
                };
                let failed = result.is_err();
                if tx.try_send(result).is_err() || failed {
                    break;
                }
            }
        });
        Ok(Document {
            id,
            doc,
            state: State::new(),
            send,
            incoming,
            reader,
            options: self.options,
            failed: false,
            _endpoint: None,
            _connection: None,
        })
    }
}

/// Drive `sync` after local edits and `receive` to follow remote changes. Documents
/// can be driven concurrently with `tokio::join!`; there is no shared sync state.
pub struct Document {
    pub id: Uuid,
    doc: Automerge,
    state: State,
    send: WireSend,
    incoming: mpsc::Receiver<Result<Vec<u8>>>,
    reader: JoinHandle<()>,
    options: Options,
    failed: bool,
    // Streams remain usable if the Client handle is dropped.
    _endpoint: Option<Arc<Endpoint<endpoint_side::Client>>>,
    _connection: Option<Connection>,
}
impl Document {
    pub fn document(&self) -> &Automerge {
        &self.doc
    }
    pub fn document_mut(&mut self) -> &mut Automerge {
        &mut self.doc
    }
    pub fn snapshot(&self) -> Vec<u8> {
        self.doc.save()
    }
    pub fn text(&self) -> Result<String> {
        let obj = self.text_object()?;
        Ok(self.doc.text(obj)?)
    }
    fn text_object(&self) -> Result<automerge::ObjId> {
        let Some((Value::Object(ObjType::Text), obj)) = self.doc.get(ROOT, "text")? else {
            bail!("document has no text object")
        };
        Ok(obj)
    }
    pub fn set_text(&mut self, text: &str) -> Result<()> {
        let obj = self.text_object()?;
        let mut tx = self.doc.transaction();
        tx.update_text(&obj, text)?;
        tx.commit();
        Ok(())
    }
    pub fn append(&mut self, text: &str) -> Result<()> {
        let obj = self.text_object()?;
        let mut tx = self.doc.transaction();
        let index = tx.length(&obj);
        tx.splice_text(&obj, index, 0, text)?;
        tx.commit();
        Ok(())
    }
    pub fn acknowledged(&self) -> bool {
        !self.failed && !self.incoming.is_closed() && self.doc.has_our_changes(&self.state)
    }
    async fn send_pending(&mut self) -> Result<()> {
        ensure!(
            !self.failed,
            "document stream failed; reconnect with a fresh sync state"
        );
        ensure!(
            !self.incoming.is_closed(),
            "document reader closed; reconnect"
        );
        if let Some(message) = self.doc.generate_sync_message(&mut self.state) {
            let result = timeout(
                self.options.timeout,
                self.send.write(
                    &Envelope {
                        kind: SYNC,
                        doc: self.id,
                        payload: message.encode(),
                    },
                    self.options.max_frame,
                ),
            )
            .await;
            match result {
                Ok(Ok(())) => {}
                other => {
                    self.failed = true;
                    self.reader.abort();
                    self.send.reset(1);
                    other.context("sync write timed out")??;
                }
            }
        }
        Ok(())
    }
    fn apply(&mut self, bytes: Vec<u8>) -> Result<()> {
        let result = Message::decode(&bytes)
            .map_err(anyhow::Error::from)
            .and_then(|message| {
                self.doc
                    .receive_sync_message(&mut self.state, message)
                    .map_err(Into::into)
            });
        if result.is_err() {
            self.failed = true;
            self.reader.abort();
            self.send.reset(1);
        }
        result
    }
    /// Wait for an incoming sync message. None is an idle timeout, and is safe to retry.
    pub async fn receive(&mut self, wait: Duration) -> Result<Option<()>> {
        self.send_pending().await?;
        match timeout(wait, self.incoming.recv()).await {
            Err(_) => Ok(None),
            Ok(Some(Ok(bytes))) => {
                if let Err(e) = self.apply(bytes) {
                    self.failed = true;
                    return Err(e);
                }
                self.send_pending().await?;
                Ok(Some(()))
            }
            Ok(Some(Err(e))) => {
                self.failed = true;
                Err(e)
            }
            Ok(None) => {
                self.failed = true;
                bail!("document reader closed or inbound queue exceeded")
            }
        }
    }
    /// Synchronize until both peers advertise the same heads, within one deadline.
    /// This acknowledges known local changes; it is not a global barrier against
    /// edits made by another peer after the last received message.
    pub async fn sync(&mut self) -> Result<()> {
        match timeout(self.options.timeout, self.sync_inner()).await {
            Ok(result) => result,
            Err(_) => {
                self.failed = true;
                self.reader.abort();
                self.send.reset(1);
                bail!("document synchronization timed out; reconnect with a fresh sync state")
            }
        }
    }
    async fn sync_inner(&mut self) -> Result<()> {
        let deadline = Instant::now() + self.options.timeout;
        loop {
            while let Ok(result) = self.incoming.try_recv() {
                self.apply(result?)?;
            }
            self.send_pending().await?;
            let heads = self.doc.get_heads();
            if self.doc.has_our_changes(&self.state)
                && self.state.their_heads.as_ref() == Some(&heads)
            {
                return Ok(());
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() || self.receive(remaining).await?.is_none() {
                bail!("document synchronization timed out")
            }
        }
    }
    pub async fn close(mut self) -> Result<()> {
        let result = timeout(
            self.options.timeout,
            self.send.write(
                &Envelope {
                    kind: CLOSE,
                    doc: self.id,
                    payload: vec![],
                },
                self.options.max_frame,
            ),
        )
        .await;
        self.reader.abort();
        result.context("closing document timed out")??;
        Ok(())
    }
}
impl Drop for Document {
    fn drop(&mut self) {
        self.reader.abort();
        self.send.reset(0);
    }
}
