//! Pairwise Lambda request/reply benchmark through the Automerge relay.
use anyhow::{Context, Result, bail, ensure};
use automerge::{ROOT, ReadDoc, transaction::Transactable};
use automerge_wtransport::client::{Client, Document, Options, WebSocketClient};
use lambda_runtime::{LambdaEvent, service_fn};
use serde_json::{Value, json};
use std::{env, time::Duration};
use tokio::time::{Instant, sleep, timeout};
use uuid::Uuid;
fn value(doc: &Document, key: &str) -> Result<Option<String>> {
    Ok(doc
        .document()
        .get(ROOT, key)?
        .and_then(|(v, _)| v.as_str().map(str::to_owned)))
}
fn put(doc: &mut Document, key: &str, value: &str) -> Result<()> {
    let mut tx = doc.document_mut().transaction();
    tx.put(ROOT, key, value)?;
    tx.commit();
    Ok(())
}
async fn until(doc: &mut Document, key: &str, expected: &str) -> Result<()> {
    while value(doc, key)?.as_deref() != Some(expected) {
        doc.receive(Duration::from_secs(1)).await?;
    }
    Ok(())
}
async fn benchmark(payload: Value, request_id: String) -> Result<Value> {
    let id = Uuid::parse_str(
        payload["document"]
            .as_str()
            .context("missing document UUID")?,
    )?;
    let role = payload["role"].as_str().context("missing role")?;
    let samples = payload["samples"].as_u64().unwrap_or(10);
    let warmup = payload["warmup"].as_u64().unwrap_or(2);
    let bytes = payload["payload_bytes"].as_u64().unwrap_or(64);
    let interval = payload["interval_ms"].as_u64().unwrap_or(100);
    ensure!(
        (1..=100).contains(&samples) && warmup <= 10 && bytes <= 16384 && interval <= 1000,
        "invalid workload limits"
    );
    let hash: [u8; 32] = hex::decode(env::var("CERTIFICATE_HASH")?)?
        .try_into()
        .map_err(|_| anyhow::anyhow!("invalid hash"))?;
    let url = format!(
        "{}/sync?token={}",
        env::var("RELAY_URL")?,
        env::var("EXPERIMENT_TOKEN")?
    );
    let transport = payload["transport"].as_str().unwrap_or("webtransport");
    ensure!(
        ["webtransport", "websocket"].contains(&transport),
        "invalid transport"
    );
    let start = Instant::now();
    let options = Options {
        certificate_hash: Some(hash),
        inbound_queue: 64,
        ..Options::default()
    };
    let (client, mut doc, ready, initial) = if transport == "websocket" {
        let url = url
            .replacen("https://", "wss://", 1)
            .replace("/sync?", "/ws?");
        let client = WebSocketClient::connect(&url, options).await?;
        let ready = start.elapsed().as_secs_f64() * 1000.;
        let initial = Instant::now();
        (None, client.open(id, None).await?, ready, initial)
    } else {
        let client = Client::connect(&url, options).await?;
        let ready = start.elapsed().as_secs_f64() * 1000.;
        let initial = Instant::now();
        let doc = client.open(id, None).await?;
        (Some(client), doc, ready, initial)
    };
    doc.sync().await?;
    let initial_ms = initial.elapsed().as_secs_f64() * 1000.;
    let mut rtts = Vec::new();
    let mut warmup_rtts = Vec::new();
    let mut received = 0;
    match role {
        "source" => {
            until(&mut doc, "ready", "yes").await?;
            for seq in 0..warmup + samples {
                let marker = format!("{seq}:{}", "x".repeat(bytes as usize));
                let sent = Instant::now();
                put(&mut doc, "request", &marker)?;
                doc.sync().await?;
                until(&mut doc, "response", &marker).await?;
                let elapsed = sent.elapsed().as_secs_f64() * 1000.;
                if seq < warmup {
                    warmup_rtts.push(elapsed)
                } else {
                    rtts.push(elapsed)
                }
                sleep(Duration::from_millis(interval)).await;
            }
            put(&mut doc, "done", "yes")?;
            doc.sync().await?;
            until(&mut doc, "finished", "yes").await?;
            doc.sync().await?;
        }
        "responder" => {
            put(&mut doc, "ready", "yes")?;
            doc.sync().await?;
            let mut last = None;
            loop {
                if value(&doc, "done")?.as_deref() == Some("yes") {
                    put(&mut doc, "finished", "yes")?;
                    doc.sync().await?;
                    break;
                }
                if let Some(marker) = value(&doc, "request")?
                    && last.as_ref() != Some(&marker)
                {
                    put(&mut doc, "response", &marker)?;
                    doc.sync().await?;
                    last = Some(marker);
                    received += 1;
                } else {
                    doc.receive(Duration::from_secs(1)).await?;
                }
            }
        }
        _ => bail!("role must be source or responder"),
    }
    let result = json!({"transport":transport,"region":env::var("AWS_REGION")?,"request_id":request_id,"role":role,"document":id.to_string(),"rtt_ms":rtts,"warmup_rtt_ms":warmup_rtts,"messages_received":received,"connection_ready_ms":ready,"initial_sync_ms":initial_ms,"elapsed_ms":start.elapsed().as_secs_f64()*1000.,"heads":doc.document().get_heads().iter().map(ToString::to_string).collect::<Vec<_>>(),"acknowledged":doc.acknowledged(),"payload_bytes":bytes,"samples":samples,"warmup":warmup,"interval_ms":interval,"architecture":std::env::consts::ARCH,"automerge":"0.12.0","wtransport":"0.7.2"});
    doc.close().await?;
    if let Some(client) = client {
        client.close();
    }
    Ok(result)
}
async fn handler(event: LambdaEvent<Value>) -> Result<Value, lambda_runtime::Error> {
    timeout(
        Duration::from_secs(120),
        benchmark(event.payload, event.context.request_id),
    )
    .await
    .map_err(|_| anyhow::anyhow!("benchmark exceeded 120 second deadline"))?
    .map_err(Into::into)
}
#[tokio::main]
async fn main() -> Result<(), lambda_runtime::Error> {
    lambda_runtime::run(service_fn(handler)).await
}
