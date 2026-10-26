use automerge_wtransport::protocol;
mod store;
use anyhow::{Context, Result, bail};
use axum::{
    Json, Router,
    extract::{
        Query, State,
        ws::{Message, WebSocket},
    },
    response::IntoResponse,
    routing::get,
};
use fs2::FileExt;
use futures_util::{SinkExt, StreamExt};
use protocol::*;
use std::{
    collections::HashMap,
    env,
    fs::OpenOptions,
    path::PathBuf,
    sync::{Arc, atomic::Ordering},
    time::Duration,
};
use store::*;
use tokio::sync::{Semaphore, mpsc};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;
#[derive(Clone)]
struct App {
    store: Arc<Store>,
    token: Arc<String>,
    hash: String,
    queue: usize,
    max: usize,
    sessions: Arc<Semaphore>,
}
fn setting<T: std::str::FromStr>(k: &str, d: T) -> T {
    env::var(k)
        .ok()
        .map(|x| x.parse().ok().expect("invalid setting"))
        .unwrap_or(d)
}
#[tokio::main]
async fn main() -> Result<()> {
    rustls::crypto::ring::default_provider()
        .install_default()
        .ok();
    let dir = PathBuf::from(env::var("DATA_DIR").unwrap_or("data".into()));
    std::fs::create_dir_all(&dir)?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(dir.join(".lock"))?;
    lock.try_lock_exclusive()
        .context("data directory is already locked")?;
    let cert = env::var("CERT").unwrap_or("certs/cert.pem".into());
    let key = env::var("KEY").unwrap_or("certs/key.pem".into());
    let token = env::var("EXPERIMENT_TOKEN").context("set EXPERIMENT_TOKEN")?;
    if token.len() < 16 {
        bail!("token must have at least 16 characters")
    }
    let max = setting("MAX_FRAME", 16 * 1024 * 1024usize);
    assert!((18..=64 * 1024 * 1024).contains(&max));
    let queue = setting("QUEUE_CAPACITY", 8usize);
    assert!(queue > 0 && queue <= 1024);
    let app = App {
        store: Arc::new(Store::new(dir, queue, max)),
        token: Arc::new(token),
        hash: std::fs::read_to_string(env::var("CERT_HASH").unwrap_or("certs/hash.hex".into()))?
            .trim()
            .into(),
        queue,
        max,
        sessions: Arc::new(Semaphore::new(setting("MAX_SESSIONS", 256))),
    };
    let port = setting("PORT", 4433u16);
    let wtapp = app.clone();
    let endpoint = wtransport::Endpoint::server(
        wtransport::ServerConfig::builder()
            .with_bind_default(port)
            .with_identity(wtransport::Identity::load_pemfiles(&cert, &key).await?)
            .build(),
    )?;
    tokio::spawn(async move {
        loop {
            let incoming = endpoint.accept().await;
            let a = wtapp.clone();
            tokio::spawn(async move {
                let result:Result<()>=async {
  let req=incoming.await?;
  if req.path()!=format!("/sync?token={}",a.token) {req.forbidden().await;return Ok(())}
  let Ok(_permit)=a.sessions.clone().try_acquire_owned() else{req.forbidden().await;return Ok(())};
  let conn=req.accept().await?;let cancel=CancellationToken::new();let mut tasks=tokio::task::JoinSet::new();
  loop {tokio::select!{biased; _=cancel.cancelled()=>break, _=conn.closed()=>break, Some(_)=tasks.join_next(),if !tasks.is_empty()=>{}, stream=conn.accept_bi()=>{let Ok((send,recv))=stream else {break};if tasks.len()>=64{cancel.cancel();break}let a=a.clone();let c=cancel.child_token();tasks.spawn(async move {if let Err(e)=wt_stream(a,send,recv,c.clone()).await {eprintln!("WT stream: {e:#}");c.cancel();}});}}}
  cancel.cancel();conn.close(wtransport::VarInt::from_u32(1),b"session ended");while tasks.join_next().await.is_some(){} Ok(())
 }.await;
                if let Err(e) = result {
                    eprintln!("WT session: {e:#}")
                }
            });
        }
    });
    let routes = Router::new()
        .route("/healthz", get(|| async { "ok" }))
        .route(
            "/config",
            get(|State(a): State<App>| async move {
                Json(serde_json::json!({"certificateHash":a.hash,"serverPid":std::process::id(),"maxFrame":a.max,"protocol":1}))
            }),
        )
        .route("/metrics", get(metrics))
        .route("/ws", get(ws_upgrade))
        .fallback_service(tower_http::services::ServeDir::new(
            env::var("WEB_DIR").unwrap_or("dist".into()),
        ))
        .with_state(app);
    eprintln!("HTTPS/WSS + WebTransport listening on TCP/UDP {port}");
    let tls = axum_server::tls_rustls::RustlsConfig::from_pem_file(cert, key).await?;
    axum_server::bind_rustls(([0, 0, 0, 0], port).into(), tls)
        .map(|acceptor| acceptor.acceptor(axum_server::accept::NoDelayAcceptor::new()))
        .serve(routes.into_make_service())
        .await?;
    drop(lock);
    Ok(())
}
async fn metrics(
    State(a): State<App>,
    Query(q): Query<HashMap<String, String>>,
) -> axum::response::Response {
    if q.get("token") != Some(a.token.as_ref()) {
        return axum::http::StatusCode::UNAUTHORIZED.into_response();
    }
    let m = &a.store.metrics;
    Json(serde_json::json!({"inboundBytes":m.inbound.load(Ordering::Relaxed),"outboundBytes":m.outbound.load(Ordering::Relaxed),"overloads":m.overload.load(Ordering::Relaxed),"persisted":m.persisted.load(Ordering::Relaxed),"subscriptionsTotal":m.peers.load(Ordering::Relaxed),"queueHighWater":m.queue_high.load(Ordering::Relaxed),"outboundQueueHighWater":m.outbound_high.load(Ordering::Relaxed),"activeSubscriptions":m.active.load(Ordering::Relaxed),"sessionSlotsAvailable":a.sessions.available_permits(),"process":process_stats()})).into_response()
}
async fn ws_upgrade(
    State(a): State<App>,
    Query(q): Query<HashMap<String, String>>,
    ws: axum::extract::WebSocketUpgrade,
) -> axum::response::Response {
    if q.get("token") != Some(a.token.as_ref()) {
        return axum::http::StatusCode::UNAUTHORIZED.into_response();
    }
    let Ok(permit) = a.sessions.clone().try_acquire_owned() else {
        return axum::http::StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    // Axum has no permessage-deflate feature enabled; compression is never negotiated.
    ws.max_message_size(a.max)
        .max_frame_size(a.max)
        .on_upgrade(move |socket| async move {
            let _p = permit;
            if let Err(e) = ws_session(a, socket).await {
                eprintln!("WS: {e:#}")
            }
        })
        .into_response()
}
async fn dispatch(
    a: &App,
    e: Envelope,
    subs: &mut HashMap<Uuid, (Uuid, Worker)>,
    out: &mpsc::Sender<Packet>,
    budget: &Arc<Semaphore>,
    cancel: &CancellationToken,
) -> Result<()> {
    a.store
        .metrics
        .inbound
        .fetch_add((HEADER + e.payload.len()) as u64, Ordering::Relaxed);
    match e.kind {
        OPEN => {
            if subs.contains_key(&e.doc) || subs.len() >= 64 {
                bail!("duplicate open or document limit")
            };
            let w = a.store.worker(e.doc).await?;
            let pid = Uuid::new_v4();
            a.store.enqueue(
                &w,
                Cmd::Join(
                    pid,
                    Peer {
                        out: out.clone(),
                        budget: budget.clone(),
                        cancel: cancel.clone(),
                        state: automerge::sync::State::new(),
                    },
                ),
                cancel,
            )?;
            subs.insert(e.doc, (pid, w));
        }
        SYNC => {
            let (pid, w) = subs.get(&e.doc).context("sync before open")?;
            a.store.enqueue(w, Cmd::Sync(*pid, e.payload), cancel)?
        }
        CLOSE => {
            if let Some((pid, w)) = subs.remove(&e.doc) {
                a.store.enqueue(&w, Cmd::Leave(pid), cancel)?
            }
        }
        _ => bail!("bad kind"),
    };
    Ok(())
}
async fn cleanup(subs: HashMap<Uuid, (Uuid, Worker)>, cancel: CancellationToken) {
    cancel.cancel();
    for (_, (pid, w)) in subs {
        let _ = tokio::time::timeout(Duration::from_secs(2), w.send(Cmd::Leave(pid))).await;
    }
}
async fn ws_session(a: App, socket: WebSocket) -> Result<()> {
    let cancel = CancellationToken::new();
    let (mut sink, mut source) = socket.split();
    let (out, mut rx) = mpsc::channel::<Packet>(a.queue);
    let budget = Arc::new(Semaphore::new(a.max * 2));
    let mut subs = HashMap::new();
    let result=async{loop {tokio::select!{biased;_=cancel.cancelled()=>bail!("cancelled or overloaded"),p=rx.recv()=>{let p=p.context("outbound closed")?;tokio::time::timeout(Duration::from_secs(10),sink.send(Message::Binary(p.data.clone().into()))).await??;},m=source.next()=>{match m {Some(Ok(Message::Binary(b)))=>dispatch(&a,Envelope::decode(&b,a.max)?,&mut subs,&out,&budget,&cancel).await?,Some(Ok(Message::Ping(b)))=>{sink.send(Message::Pong(b)).await?},Some(Ok(Message::Pong(_)))=>{},Some(Ok(Message::Close(_)))|None=>break,_=>bail!("invalid WS message")}}}} Ok(())}.await;
    cleanup(subs, cancel).await;
    result
}
async fn wt_stream(
    a: App,
    mut send: wtransport::SendStream,
    mut recv: wtransport::RecvStream,
    cancel: CancellationToken,
) -> Result<()> {
    let (out, mut rx) = mpsc::channel::<Packet>(a.queue);
    let budget = Arc::new(Semaphore::new(a.max * 2));
    let mut subs = HashMap::new();
    let result:Result<()>=async{
 let first=tokio::time::timeout(Duration::from_secs(10),read_frame(&mut recv,a.max)).await??;if first.kind!=OPEN{bail!("stream must open a document")};let id=first.doc;dispatch(&a,first,&mut subs,&out,&budget,&cancel).await?;
 // Separate reader task: a cancelled select branch must never discard a partial frame.
 let(tx,mut incoming)=mpsc::channel(a.queue);let c=cancel.clone();let max=a.max;
 let reader=tokio::spawn(async move {loop {let e=tokio::select!{_=c.cancelled()=>break,e=read_frame(&mut recv,max)=>e};let end=e.is_err();if tx.try_send(e).is_err(){c.cancel();break}
if end{break}}});
 let result=async{loop{tokio::select!{biased;_=cancel.cancelled()=>bail!("cancelled or overloaded"),p=rx.recv()=>{let p=p.context("outbound closed")?;tokio::time::timeout(Duration::from_secs(10),async{send.write_all(&(p.data.len() as u32).to_be_bytes()).await?;send.write_all(&p.data).await}).await??;},e=incoming.recv()=>{let e=e.context("reader closed")??;if e.doc!=id||e.kind==OPEN{bail!("one document per stream")};let closed=e.kind==CLOSE;dispatch(&a,e,&mut subs,&out,&budget,&cancel).await?;if closed{break}}}}Ok(())}.await;reader.abort();result
 }.await;
    cleanup(subs, cancel).await;
    let _ = send.reset(wtransport::VarInt::from_u32(0));
    result
}

fn process_stats() -> serde_json::Value {
    #[cfg(target_os = "linux")]
    {
        let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
        let rss = status
            .lines()
            .find(|s| s.starts_with("VmRSS:"))
            .and_then(|s| s.split_whitespace().nth(1))
            .and_then(|s| s.parse::<u64>().ok())
            .map(|v| v * 1024);
        let stat = std::fs::read_to_string("/proc/self/stat").unwrap_or_default();
        let rest = stat.rsplit_once(") ").map(|(_, s)| s).unwrap_or("");
        let fields: Vec<_> = rest.split_whitespace().collect();
        let ticks: u64 = [11, 12]
            .iter()
            .filter_map(|&i| fields.get(i).and_then(|s| s.parse::<u64>().ok()))
            .sum();
        let mut cgroup = std::path::PathBuf::from("/sys/fs/cgroup");
        let cg = std::fs::read_to_string("/proc/self/cgroup").unwrap_or_default();
        if let Some(line) = cg.lines().find(|s| s.starts_with("0::")) {
            let p = std::path::Path::new("/sys/fs/cgroup")
                .join(line.trim_start_matches("0::").trim_start_matches('/'));
            if p.exists() {
                cgroup = p
            }
        }
        serde_json::json!({"rssBytes":rss,"cpuTicks":ticks,"ticksPerSecond":100,"cgroupMemory":std::fs::read_to_string(cgroup.join("memory.current")).ok().and_then(|s|s.trim().parse::<u64>().ok()),"memoryEvents":std::fs::read_to_string(cgroup.join("memory.events")).ok(),"cpuQuota":std::fs::read_to_string(cgroup.join("cpu.max")).ok()})
    }
    #[cfg(not(target_os = "linux"))]
    {
        serde_json::json!({"pid":std::process::id()})
    }
}
