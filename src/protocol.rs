use anyhow::{Result, bail};
use uuid::Uuid;
pub const HEADER: usize = 18;
pub const OPEN: u8 = 1;
pub const SYNC: u8 = 2;
pub const CLOSE: u8 = 3;
#[derive(Debug, Clone)]
pub struct Envelope {
    pub kind: u8,
    pub doc: Uuid,
    pub payload: Vec<u8>,
}
impl Envelope {
    pub fn decode(b: &[u8], max: usize) -> Result<Self> {
        if b.len() < HEADER || b.len() > max || b[0] != 1 {
            bail!("invalid version or frame size")
        }
        if !(OPEN..=CLOSE).contains(&b[1]) || (b[1] != SYNC && b.len() != HEADER) {
            bail!("invalid message")
        }
        Ok(Self {
            kind: b[1],
            doc: Uuid::from_slice(&b[2..18])?,
            payload: b[18..].to_vec(),
        })
    }
    pub fn encode(&self) -> Vec<u8> {
        let mut b = vec![1, self.kind];
        b.extend(self.doc.as_bytes());
        b.extend(&self.payload);
        b
    }
}
/// Read one length-prefixed envelope. Do not cancel this future and resume on the
/// same stream: cancellation can consume part of a frame. Clients use a reader task.
pub async fn read_frame(recv: &mut wtransport::RecvStream, max: usize) -> Result<Envelope> {
    let mut h = [0; 4];
    recv.read_exact(&mut h).await?;
    let n = u32::from_be_bytes(h) as usize;
    if n < HEADER || n > max {
        bail!("invalid frame length")
    }
    let mut b = vec![0; n];
    recv.read_exact(&mut b).await?;
    Envelope::decode(&b, max)
}

pub async fn write_frame(
    send: &mut wtransport::SendStream,
    envelope: &Envelope,
    max: usize,
) -> Result<()> {
    let b = envelope.encode();
    if b.len() > max {
        bail!("outbound frame exceeds limit")
    }
    send.write_all(&(b.len() as u32).to_be_bytes()).await?;
    send.write_all(&b).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn envelope_validation() {
        let e = Envelope {
            kind: OPEN,
            doc: Uuid::new_v4(),
            payload: vec![],
        };
        let b = e.encode();
        assert_eq!(Envelope::decode(&b, 18).unwrap().doc, e.doc);
        for n in 0..18 {
            assert!(Envelope::decode(&b[..n], 18).is_err())
        }
        assert!(Envelope::decode(&b, 17).is_err());
        let mut x = b;
        x[0] = 2;
        assert!(Envelope::decode(&x, 100).is_err());
    }
}
