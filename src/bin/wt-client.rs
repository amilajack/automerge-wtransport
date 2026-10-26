use anyhow::{Context, Result, bail};
use automerge_wtransport::client::{Client, Options};
use std::{env, fs, path::PathBuf, time::Duration};
use uuid::Uuid;

#[tokio::main]
async fn main() -> Result<()> {
    let mut url = "https://localhost:4433".to_string();
    let mut id = None;
    let mut hash = None;
    let mut snapshot = None;
    let mut set = None;
    let mut append = None;
    let mut watch = false;
    let mut seconds = 30;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--help" | "-h" => {
                println!(
                    "wt-client --doc UUID [--url https://host:4433] [--cert-hash FILE]\n          [--snapshot FILE] [--set TEXT | --append TEXT] [--watch] [--timeout SECONDS]\nEXPERIMENT_TOKEN supplies the token. No --cert-hash uses system CA roots.\nPrints JSON with text, heads and acknowledgement after sync; --watch emits updates."
                );
                return Ok(());
            }
            "--watch" => watch = true,
            "--url" => url = args.next().context("missing URL")?,
            "--doc" => id = Some(Uuid::parse_str(&args.next().context("missing UUID")?)?),
            "--cert-hash" => hash = Some(args.next().context("missing certificate hash file")?),
            "--snapshot" => {
                snapshot = Some(PathBuf::from(args.next().context("missing snapshot path")?))
            }
            "--set" => set = Some(args.next().context("missing text")?),
            "--append" => append = Some(args.next().context("missing text")?),
            "--timeout" => seconds = args.next().context("missing timeout")?.parse()?,
            _ => bail!("unknown option {arg}; use --help"),
        }
    }
    if set.is_some() && append.is_some() {
        bail!("choose --set or --append")
    }
    let id = id.context("--doc UUID is required")?;
    let token = env::var("EXPERIMENT_TOKEN").context("set EXPERIMENT_TOKEN")?;
    if !token
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
        || token.is_empty()
    {
        bail!("token must use URL-safe letters, numbers, - or _")
    }
    if !url.starts_with("https://") || url[8..].contains(['?', '#', '/']) {
        bail!("--url must be an HTTPS origin, e.g. https://localhost:4433")
    }
    let certificate_hash = hash
        .map(|path| -> Result<[u8; 32]> {
            let bytes = hex::decode(fs::read_to_string(path)?.trim())?;
            bytes
                .try_into()
                .map_err(|_| anyhow::anyhow!("certificate hash must be 32 bytes"))
        })
        .transpose()?;
    let client = Client::connect(
        &format!("{url}/sync?token={token}"),
        Options {
            certificate_hash,
            timeout: Duration::from_secs(seconds),
            ..Options::default()
        },
    )
    .await?;
    let bytes = snapshot.as_ref().map(fs::read).transpose().or_else(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            Ok(None)
        } else {
            Err(e)
        }
    })?;
    let mut doc = client.open(id, bytes.as_deref()).await?;
    // Merge the saved snapshot first, then edit the current server text.
    doc.sync().await?;
    if let Some(text) = set {
        doc.set_text(&text)?
    }
    if let Some(text) = append {
        doc.append(&text)?
    }
    doc.sync().await?;
    loop {
        if let Some(path) = &snapshot {
            let tmp = path.with_extension("tmp");
            fs::write(&tmp, doc.snapshot())?;
            fs::rename(tmp, path)?;
        }
        println!(
            "{}",
            serde_json::json!({"document":id.to_string(),"text":doc.text()?,"heads":doc.document().get_heads().iter().map(ToString::to_string).collect::<Vec<_>>(),"acknowledged":doc.acknowledged()})
        );
        if !watch {
            break;
        }
        tokio::select! {_ = tokio::signal::ctrl_c()=>{client.close();return Ok(())},result=doc.receive(Duration::from_secs(seconds))=>{result?;}}
    }
    doc.close().await?;
    client.close();
    Ok(())
}
