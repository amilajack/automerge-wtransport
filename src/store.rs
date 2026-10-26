use crate::protocol::{Envelope, SYNC};
use anyhow::{Result, bail};
use automerge::{
    Automerge,
    sync::{Message, State, SyncDoc},
};
use std::{
    collections::HashMap,
    fs::{File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};
use tokio::sync::{Mutex, OwnedSemaphorePermit, Semaphore, mpsc};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;
#[derive(Default)]
pub struct Metrics {
    pub inbound: AtomicU64,
    pub outbound: AtomicU64,
    pub overload: AtomicU64,
    pub persisted: AtomicU64,
    pub peers: AtomicU64,
    pub active: AtomicU64,
    pub outbound_high: AtomicU64,
    pub queue_high: AtomicU64,
}
pub struct Packet {
    pub data: Vec<u8>,
    pub _permit: OwnedSemaphorePermit,
}
pub struct Peer {
    pub out: mpsc::Sender<Packet>,
    pub budget: Arc<Semaphore>,
    pub cancel: CancellationToken,
    pub state: State,
}
pub enum Cmd {
    Join(Uuid, Peer),
    Sync(Uuid, Vec<u8>),
    Leave(Uuid),
}
pub type Worker = mpsc::Sender<Cmd>;
pub struct Store {
    workers: Mutex<HashMap<Uuid, Worker>>,
    pub dir: PathBuf,
    pub capacity: usize,
    pub max: usize,
    pub metrics: Arc<Metrics>,
}
impl Store {
    pub fn new(dir: PathBuf, capacity: usize, max: usize) -> Self {
        Self {
            workers: Mutex::new(HashMap::new()),
            dir,
            capacity,
            max,
            metrics: Arc::new(Metrics::default()),
        }
    }
    pub async fn worker(self: &Arc<Self>, id: Uuid) -> Result<Worker> {
        let mut map = self.workers.lock().await;
        if let Some(w) = map.get(&id)
            && !w.is_closed()
        {
            return Ok(w.clone());
        }
        if map.len() >= 1024 && !map.contains_key(&id) {
            bail!("document limit")
        }
        let path = self.dir.join(format!("{id}.automerge"));
        let doc = match std::fs::read(&path) {
            Ok(b) => Automerge::load(&b)?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                Automerge::load(include_bytes!("../schema.automerge"))?
            }
            Err(e) => return Err(e.into()),
        };
        let (tx, rx) = mpsc::channel(self.capacity);
        map.insert(id, tx.clone());
        let s = self.clone();
        tokio::spawn(async move {
            if let Err(e) = s.run(id, path, doc, rx).await {
                eprintln!("document {id} failed: {e:#}")
            }
        });
        Ok(tx)
    }
    pub fn enqueue(&self, w: &Worker, cmd: Cmd, cancel: &CancellationToken) -> Result<()> {
        self.metrics.queue_high.fetch_max(
            (w.max_capacity() - w.capacity() + 1) as u64,
            Ordering::Relaxed,
        );
        match w.try_send(cmd) {
            Ok(()) => Ok(()),
            Err(mpsc::error::TrySendError::Full(_)) => {
                self.metrics.overload.fetch_add(1, Ordering::Relaxed);
                cancel.cancel();
                bail!("document inbound queue full")
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                cancel.cancel();
                bail!("document worker failed or closed")
            }
        }
    }
    async fn run(
        &self,
        id: Uuid,
        path: PathBuf,
        mut doc: Automerge,
        mut rx: mpsc::Receiver<Cmd>,
    ) -> Result<()> {
        let mut peers: HashMap<Uuid, Peer> = HashMap::new();
        let result: Result<()> = async {
            while let Some(cmd) = rx.recv().await {
                peers.retain(|_, p| {
                    if p.cancel.is_cancelled() {
                        self.metrics.active.fetch_sub(1, Ordering::Relaxed);
                        false
                    } else {
                        true
                    }
                });
                match cmd {
                    Cmd::Join(pid, p) => {
                        if p.cancel.is_cancelled() || p.out.is_closed() {
                            continue;
                        }
                        if peers.len() >= 256 {
                            p.cancel.cancel();
                            continue;
                        }
                        if peers.insert(pid, p).is_none() {
                            self.metrics.active.fetch_add(1, Ordering::Relaxed);
                        }
                        self.metrics.peers.fetch_add(1, Ordering::Relaxed);
                    }
                    Cmd::Leave(pid) => {
                        if peers.remove(&pid).is_some() {
                            self.metrics.active.fetch_sub(1, Ordering::Relaxed);
                        }
                    }
                    Cmd::Sync(pid, bytes) => {
                        let Some(p) = peers.get_mut(&pid) else {
                            continue;
                        };
                        let msg = match Message::decode(&bytes) {
                            Ok(m) => m,
                            Err(_) => {
                                p.cancel.cancel();
                                continue;
                            }
                        };
                        let before = doc.get_heads();
                        if doc.receive_sync_message(&mut p.state, msg).is_err() {
                            p.cancel.cancel();
                            continue;
                        }
                        if doc.get_heads() != before {
                            let bytes = doc.save();
                            let target = path.clone();
                            tokio::task::spawn_blocking(move || atomic_save(&target, &bytes))
                                .await??;
                            self.metrics.persisted.fetch_add(1, Ordering::Relaxed);
                        }
                    }
                }
                // A different sync state is advanced for EACH subscriber. Never broadcast a sync payload.
                for p in peers.values_mut() {
                    if p.cancel.is_cancelled() || p.out.is_closed() {
                        p.cancel.cancel();
                        continue;
                    }
                    if let Some(msg) = doc.generate_sync_message(&mut p.state) {
                        let data = Envelope {
                            kind: SYNC,
                            doc: id,
                            payload: msg.encode(),
                        }
                        .encode();
                        let permit = if data.len() <= self.max {
                            p.budget
                                .clone()
                                .try_acquire_many_owned(data.len() as u32)
                                .ok()
                        } else {
                            None
                        };
                        if let Some(permit) = permit {
                            self.metrics.outbound_high.fetch_max(
                                (p.out.max_capacity() - p.out.capacity() + 1) as u64,
                                Ordering::Relaxed,
                            );
                            let n = data.len();
                            match p.out.try_send(Packet {
                                data,
                                _permit: permit,
                            }) {
                                Ok(()) => {
                                    self.metrics.outbound.fetch_add(n as u64, Ordering::Relaxed);
                                    continue;
                                }
                                Err(mpsc::error::TrySendError::Closed(_)) => {
                                    p.cancel.cancel();
                                    continue;
                                }
                                Err(mpsc::error::TrySendError::Full(_)) => {}
                            }
                        }
                        self.metrics.overload.fetch_add(1, Ordering::Relaxed);
                        p.cancel.cancel();
                    }
                }
            }
            Ok(())
        }
        .await;
        self.metrics
            .active
            .fetch_sub(peers.len() as u64, Ordering::Relaxed);
        for p in peers.values() {
            p.cancel.cancel()
        }
        result
    }
}
pub fn atomic_save(path: &Path, bytes: &[u8]) -> Result<()> {
    let tmp = path.with_extension("tmp");
    let mut f = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(&tmp)?;
    f.write_all(bytes)?;
    f.sync_all()?;
    std::fs::rename(&tmp, path)?;
    File::open(path.parent().unwrap())?.sync_all()?;
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn replacement() {
        let dir = std::env::temp_dir().join(Uuid::new_v4().to_string());
        std::fs::create_dir(&dir).unwrap();
        let p = dir.join("d");
        atomic_save(&p, b"first").unwrap();
        atomic_save(&p, b"second").unwrap();
        assert_eq!(std::fs::read(&p).unwrap(), b"second");
        std::fs::remove_dir_all(dir).unwrap();
    }
}

#[cfg(test)]
mod async_tests {
    use super::*;
    #[tokio::test]
    async fn bounded_queue_cancels_instead_of_dropping() {
        let s = Store::new(PathBuf::new(), 1, 1024);
        let (tx, _rx) = mpsc::channel(1);
        let c = CancellationToken::new();
        s.enqueue(&tx, Cmd::Leave(Uuid::new_v4()), &c).unwrap();
        assert!(s.enqueue(&tx, Cmd::Leave(Uuid::new_v4()), &c).is_err());
        assert!(c.is_cancelled());
        assert_eq!(s.metrics.overload.load(Ordering::Relaxed), 1)
    }
    #[tokio::test]
    async fn failed_snapshot_cancels_all_subscribers() {
        use automerge::{
            ROOT,
            transaction::{CommitOptions, Transactable},
        };
        let mut source = Automerge::new();
        let mut tx = source.transaction();
        tx.put(ROOT, "text", "durable").unwrap();
        tx.commit_with(CommitOptions::default());
        let mut source_state = State::new();
        let mut target = Automerge::new();
        let mut target_state = State::new();
        // Establish enough of the handshake for the source to transmit its changes.
        let first = source.generate_sync_message(&mut source_state).unwrap();
        target
            .receive_sync_message(&mut target_state, first)
            .unwrap();
        let response = target.generate_sync_message(&mut target_state).unwrap();
        source
            .receive_sync_message(&mut source_state, response)
            .unwrap();
        let changes = source
            .generate_sync_message(&mut source_state)
            .unwrap()
            .encode();
        let s = Store::new(PathBuf::new(), 8, 1024 * 1024);
        let (tx, rx) = mpsc::channel(8);
        let (out, _out_rx) = mpsc::channel(8);
        let cancel = CancellationToken::new();
        let pid = Uuid::new_v4();
        tx.send(Cmd::Join(
            pid,
            Peer {
                out,
                budget: Arc::new(Semaphore::new(1024 * 1024)),
                cancel: cancel.clone(),
                state: target_state,
            },
        ))
        .await
        .unwrap();
        tx.send(Cmd::Sync(pid, changes)).await.unwrap();
        drop(tx);
        let path = std::env::temp_dir()
            .join(Uuid::new_v4().to_string())
            .join("missing-parent.automerge");
        assert!(s.run(Uuid::new_v4(), path, target, rx).await.is_err());
        assert!(cancel.is_cancelled());
    }
}

#[cfg(test)]
mod stalled_tests {
    use super::*;
    #[tokio::test]
    async fn stalled_multiplexed_reader_is_disconnected() {
        let s = Arc::new(Store::new(PathBuf::new(), 8, 1024 * 1024));
        let (tx, rx) = mpsc::channel(8);
        let (out, _unread) = mpsc::channel(1);
        let cancel = CancellationToken::new();
        let budget = Arc::new(Semaphore::new(1024 * 1024));
        let peer = |out| Peer {
            out,
            budget: budget.clone(),
            cancel: cancel.clone(),
            state: State::new(),
        };
        tx.send(Cmd::Join(Uuid::new_v4(), peer(out.clone())))
            .await
            .unwrap();
        let runner = s.clone();
        let task = tokio::spawn(async move {
            runner
                .run(Uuid::new_v4(), PathBuf::new(), Automerge::new(), rx)
                .await
        });
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            while s.metrics.outbound.load(Ordering::Relaxed) == 0 {
                tokio::task::yield_now().await
            }
        })
        .await
        .unwrap();
        let (tx2, rx2) = mpsc::channel(8);
        tx2.send(Cmd::Join(Uuid::new_v4(), peer(out)))
            .await
            .unwrap();
        drop(tx2);
        s.run(Uuid::new_v4(), PathBuf::new(), Automerge::new(), rx2)
            .await
            .unwrap();
        assert_eq!(s.metrics.overload.load(Ordering::Relaxed), 1);
        assert!(cancel.is_cancelled());
        drop(tx);
        task.await.unwrap().unwrap();
    }
}

#[cfg(test)]
mod cancelled_join_tests {
    use super::*;
    #[tokio::test]
    async fn cancelled_open_never_subscribes_or_counts_as_overload() {
        let s = Store::new(PathBuf::new(), 8, 1024);
        let (tx, rx) = mpsc::channel(8);
        let (out, mut unread) = mpsc::channel(1);
        let cancel = CancellationToken::new();
        cancel.cancel();
        tx.send(Cmd::Join(
            Uuid::new_v4(),
            Peer {
                out,
                budget: Arc::new(Semaphore::new(1024)),
                cancel,
                state: State::new(),
            },
        ))
        .await
        .unwrap();
        drop(tx);
        s.run(Uuid::new_v4(), PathBuf::new(), Automerge::new(), rx)
            .await
            .unwrap();
        assert!(unread.try_recv().is_err());
        assert_eq!(s.metrics.overload.load(Ordering::Relaxed), 0);
        assert_eq!(s.metrics.peers.load(Ordering::Relaxed), 0);
        assert_eq!(s.metrics.active.load(Ordering::Relaxed), 0);
    }
}
