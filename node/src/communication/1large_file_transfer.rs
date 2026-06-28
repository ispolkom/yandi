//! Large file transfer (up to 10GB) with binary protocol

use crate::util::HashId;
use crate::p2p::{P2PTransport, P2PPacket, P2PPacketType};
use std::sync::Arc;
use std::collections::HashMap;
use anyhow::{Result, anyhow};
use tokio::sync::Mutex;
use tracing::{info, debug, error};

const CHUNK_SIZE: usize = 40 * 1024; // 40KB

pub struct LargeFileTransferManager {
    my_node_id: HashId,
    transport: Arc<P2PTransport>,
    outgoing: Mutex<HashMap<String, OutgoingTransfer>>,
    incoming: Mutex<HashMap<String, IncomingTransfer>>,
}

struct OutgoingTransfer {
    file_id: String,
    peer_id: HashId,
    file_path: std::path::PathBuf,
    chunk_size: usize,
    total_chunks: u32,
}

#[derive(Clone)]
struct IncomingTransfer {
    file_id: String,
    from_peer: HashId,
    filename: String,
    file_size: u64,
    chunk_size: usize,
    total_chunks: u32,
    temp_path: std::path::PathBuf,
    received: Vec<bool>,
}

impl LargeFileTransferManager {
    pub fn new(my_node_id: HashId, transport: Arc<P2PTransport>) -> Self {
        let _ = std::fs::create_dir_all("/tmp/yandi_large");
        Self {
            my_node_id,
            transport,
            outgoing: Mutex::new(HashMap::new()),
            incoming: Mutex::new(HashMap::new()),
        }
    }

    pub async fn start_transfer(
        &self,
        to: HashId,
        file_path: std::path::PathBuf,
        filename: String,
        file_size: u64,
    ) -> Result<String> {
        let chunk_size = CHUNK_SIZE;
        let total_chunks = ((file_size + chunk_size as u64 - 1) / chunk_size as u64) as u32;
        let file_id = format!("{}_{}", hex::encode(&self.my_node_id.0[..8]), chrono::Utc::now().timestamp_millis());

        info!("📤 Large file: {} ({} bytes, {} chunks)", filename, file_size, total_chunks);

        let meta = serde_json::json!({
            "file_id": file_id,
            "filename": filename,
            "file_size": file_size,
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
        });

        let meta_bytes = serde_json::to_vec(&meta).map_err(|e| anyhow::anyhow!("{}", e))?;
        let packet = P2PPacket::new(P2PPacketType::LargeFileStart, self.my_node_id, false, meta_bytes);
        self.transport.send_packet_dual_path(to, packet).await.map_err(|e| anyhow::anyhow!("{}", e))?;

        let transfer = OutgoingTransfer {
            file_id: file_id.clone(),
            peer_id: to,
            file_path,
            chunk_size,
            total_chunks,
        };

        self.outgoing.lock().await.insert(file_id.clone(), transfer);

        self.send_chunks(&to, &file_id).await?;

        Ok(file_id)
    }

    async fn send_chunks(&self, to: &HashId, file_id: &str) -> Result<()> {
        let (chunk_size, total_chunks, file_path) = {
            let out = self.outgoing.lock().await;
            let t = out.get(file_id).ok_or_else(|| anyhow!("Not found"))?;
            (t.chunk_size, t.total_chunks, t.file_path.clone())
        };

        for idx in 0..total_chunks {
            let offset = (idx as u64) * (chunk_size as u64);
            let mut file = tokio::fs::File::open(&file_path).await?;
            use tokio::io::{AsyncSeekExt, AsyncReadExt};
            file.seek(std::io::SeekFrom::Start(offset)).await?;
            let mut buf = vec![0u8; chunk_size];
            let n = file.read(&mut buf).await?;
            buf.truncate(n);

            let mut bin = Vec::with_capacity(4 + 4 + n);
            bin.extend_from_slice(&idx.to_le_bytes());
            bin.extend_from_slice(&(n as u32).to_le_bytes());
            bin.extend_from_slice(&buf);

            let packet = P2PPacket::new(P2PPacketType::LargeFileChunk, self.my_node_id, false, bin);
            self.transport.send_packet_dual_path(*to, packet).await.map_err(|e| anyhow::anyhow!("{}", e))?;
            debug!("Chunk {}/{}", idx + 1, total_chunks);
        }

        info!("All {} chunks sent", total_chunks);
        Ok(())
    }

    pub async fn handle_start(&self, from: HashId, data: &[u8]) -> Result<()> {
        let v: serde_json::Value = serde_json::from_slice(data).map_err(|e| anyhow::anyhow!("{}", e))?;
        let file_id = v["file_id"].as_str().unwrap().to_string();
        let filename = v["filename"].as_str().unwrap().to_string();
        let file_size = v["file_size"].as_u64().unwrap();
        let chunk_size = v["chunk_size"].as_u64().unwrap() as usize;
        let total_chunks = v["total_chunks"].as_u64().unwrap() as u32;

        let temp_path = std::path::PathBuf::from("/tmp/yandi_large").join(format!("{}.tmp", file_id));
        let file = tokio::fs::File::create(&temp_path).await?;
        file.set_len(file_size).await?;

        let transfer = IncomingTransfer {
            file_id: file_id.clone(),
            from_peer: from,
            filename: filename.clone(),
            file_size,
            chunk_size,
            total_chunks,
            temp_path,
            received: vec![false; total_chunks as usize],
        };

        self.incoming.lock().await.insert(file_id, transfer);
        info!("Receiving {} ({} bytes)", filename, file_size);
        Ok(())
    }

    pub async fn handle_chunk(&self, from: HashId, data: &[u8]) -> Result<()> {
        if data.len() < 8 {
            return Err(anyhow!("Chunk too small"));
        }
        let idx = u32::from_le_bytes(data[0..4].try_into().unwrap());
        let len = u32::from_le_bytes(data[4..8].try_into().unwrap()) as usize;
        if data.len() < 8 + len {
            return Err(anyhow!("Invalid chunk"));
        }
        let chunk = &data[8..8 + len];

        let file_id = {
            let inc = self.incoming.lock().await;
            if let Some((id, _)) = inc.iter().next() {
                id.clone()
            } else {
                return Ok(());
            }
        };

        let transfer = {
            let inc = self.incoming.lock().await;
            inc.get(&file_id).cloned()
        };

        if let Some(t) = transfer {
            let offset = (idx as u64) * (t.chunk_size as u64);
            let mut file = tokio::fs::OpenOptions::new().write(true).open(&t.temp_path).await?;
            use tokio::io::{AsyncSeekExt, AsyncWriteExt};
            file.seek(std::io::SeekFrom::Start(offset)).await?;
            file.write_all(chunk).await?;
            
            // Отмечаем полученный чанк
            if let Some(inc) = self.incoming.lock().await.get_mut(&file_id) {
                inc.received[idx as usize] = true;
            }
            
            // Проверяем, все ли чанки получены
            let all_received = {
                let inc = self.incoming.lock().await;
                if let Some(inc_transfer) = inc.get(&file_id) {
                    inc_transfer.received.iter().all(|&x| x)
                } else {
                    false
                }
            };
            
            if all_received {
                info!("✅ All chunks received for {}, assembling file...", file_id);
                let final_path = std::path::PathBuf::from("/home/iam/yandi/downloads").join(&t.filename);
                std::fs::create_dir_all("/home/iam/yandi/downloads")?;
                tokio::fs::rename(&t.temp_path, &final_path).await?;
                info!("✅ File saved: {:?}", final_path);
                self.incoming.lock().await.remove(&file_id);
            }
            
            debug!("Chunk {}/{}", idx + 1, t.total_chunks);
        }
        Ok(())
    }
}

impl Clone for LargeFileTransferManager {
    fn clone(&self) -> Self {
        Self {
            my_node_id: self.my_node_id,
            transport: self.transport.clone(),
            outgoing: Mutex::new(HashMap::new()),
            incoming: Mutex::new(HashMap::new()),
        }
    }
}
