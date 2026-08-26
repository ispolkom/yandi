// src/socks5/exit_node.rs
//! SOCKS5 Exit Node Handler
//! =======================
//!
//! Обрабатывает SOCKS5 запросы от клиентов и устанавливает реальные TCP соединения
//! (аналог HttpProxyGateway из proxy/gateway.rs)

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpStream;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::Mutex;
use anyhow::{Result, anyhow};

use crate::netlayer::P2PTransport;
use crate::util::HashId;
use crate::protocol::Station;
use super::{Socks5ProxyRequest, Socks5ProxyResponse, Socks5TunnelData};
use tracing::{info, error, debug, warn};

/// SOCKS5 Exit Node Handler (аналог HttpProxyGateway)
pub struct ExitNodeHandler {
    pub transport: Arc<P2PTransport>,  // ✅ Публичный для NACK handler
    pub station: Arc<Station>,
    /// Active tunnels: tunnel_id -> write half
    active_tunnels: Arc<Mutex<HashMap<u64, tokio::net::tcp::OwnedWriteHalf>>>,
    /// Channels
    request_rx: Arc<Mutex<Option<tokio::sync::mpsc::Receiver<(HashId, Socks5ProxyRequest)>>>>,
    tunnel_data_rx: Arc<Mutex<Option<tokio::sync::mpsc::Receiver<(HashId, Socks5TunnelData)>>>>,
}

impl ExitNodeHandler {
    /// Create new exit node handler
    pub fn new(transport: Arc<P2PTransport>) -> Self {
        info!("🌍 Creating SOCKS5 Exit Node Handler");

        let station = Station::with_defaults(
            transport.identity().node_id(),
            transport.clone()
        );

        Self {
            transport,
            station: Arc::new(station),
            active_tunnels: Arc::new(Mutex::new(HashMap::new())),
            request_rx: Arc::new(Mutex::new(None)),
            tunnel_data_rx: Arc::new(Mutex::new(None)),
        }
    }

    /// Set request channel
    pub fn with_request_channel(mut self, rx: tokio::sync::mpsc::Receiver<(HashId, Socks5ProxyRequest)>) -> Self {
        self.request_rx = Arc::new(Mutex::new(Some(rx)));
        self
    }

    /// Set tunnel data channel
    pub fn with_tunnel_data_channel(mut self, rx: tokio::sync::mpsc::Receiver<(HashId, Socks5TunnelData)>) -> Self {
        self.tunnel_data_rx = Arc::new(Mutex::new(Some(rx)));
        self
    }

    /// Start the handler
    pub async fn run(&self) -> Result<()> {
        info!("🌍 SOCKS5 Exit Node Handler started");

        // Start tunnel data handler
        let active_tunnels_clone = self.active_tunnels.clone();
        let tunnel_rx_clone = self.tunnel_data_rx.clone();
        tokio::spawn(async move {
            Self::handle_tunnel_data(active_tunnels_clone, tunnel_rx_clone).await;
        });

        // Get the receiver
        let mut rx_opt = {
            let mut rx_lock = self.request_rx.lock().await;
            rx_lock.take()
        };

        let mut rx = match rx_opt {
            Some(r) => r,
            None => {
                warn!("⚠️  No request channel configured!");
                return Ok(());
            }
        };

        info!("✅ SOCKS5 Exit Node ready to process requests");

        // Listen for incoming requests
        while let Some((source_node, request)) = rx.recv().await {
            let handler = self.clone_for_handler();

            tokio::spawn(async move {
                if let Err(e) = handler.handle_request(source_node, request).await {
                    error!("❌ Error handling SOCKS5 request: {}", e);
                }
            });
        }

        warn!("⚠️  SOCKS5 Exit Node request channel closed");
        Ok(())
    }

    /// Clone for handler
    pub fn clone_for_handler(&self) -> Self {
        Self {
            transport: self.transport.clone(),
            station: self.station.clone(),
            active_tunnels: self.active_tunnels.clone(),
            request_rx: self.request_rx.clone(),
            tunnel_data_rx: self.tunnel_data_rx.clone(),
        }
    }

    /// Handle SOCKS5 CONNECT request (аналог HttpProxyGateway::handle_connect)
    pub async fn handle_request(&self, source_node: HashId, request: Socks5ProxyRequest) -> Result<()> {
        info!("📨 SOCKS5 CONNECT request #{} from {}", request.request_id, hex::encode(&source_node.0[..8]));
        debug!("🎯 Target: {}:{}", request.target_host, request.target_port);

        // Connect to target
        let target_addr = format!("{}:{}", request.target_host, request.target_port);

        info!("🔌 Connecting to {} ...", target_addr);

        let target_stream = match tokio::time::timeout(
            Duration::from_secs(10),
            TcpStream::connect(&target_addr)
        ).await {
            Ok(Ok(mut stream)) => {
                // ⚡ TCP NODELAY - critical for SOCKS5 performance!
                if let Err(e) = stream.set_nodelay(true) {
                    error!("❌ Failed to set TCP_NODELAY: {}", e);
                } else {
                    debug!("✅ TCP_NODELAY enabled for target");
                }

                info!("✅ Connected to {}", target_addr);
                stream
            }
            Ok(Err(e)) => {
                error!("❌ Failed to connect to {}: {}", target_addr, e);
                // Send error response
                let proxy_response = Socks5ProxyResponse::error(request.request_id, 0x05); // Connection refused
                self.send_response(source_node, proxy_response).await?;
                return Err(anyhow!("Failed to connect: {}", e));
            }
            Err(_) => {
                error!("⏱️ Timeout connecting to {}", target_addr);
                let proxy_response = Socks5ProxyResponse::error(request.request_id, 0x06); // TTL expired
                self.send_response(source_node, proxy_response).await?;
                return Err(anyhow!("Connection timeout"));
            }
        };

        // Send success response
        let proxy_response = Socks5ProxyResponse::success(request.request_id);
        self.send_response(source_node, proxy_response).await?;

        info!("✅ SOCKS5 CONNECT established for tunnel #{}", request.request_id);

        // Split TCP stream
        let (mut target_read, mut target_write) = target_stream.into_split();

        // Store write half in active_tunnels
        {
            let mut tunnels = self.active_tunnels.lock().await;
            tunnels.insert(request.request_id, target_write);
        }

        // Start reading from target and sending to client
        let station_clone = self.station.clone();
        let source_node_clone = source_node;
        let tunnel_id = request.request_id;

        let target_to_client = async move {
            const BUFFER_SIZE: usize = 16 * 1024; // ⚡ 32 KB instead of 4 KB
            let mut buf = vec![0u8; BUFFER_SIZE];
            loop {
                let n = target_read.read(&mut buf).await?;
                if n == 0 {
                    debug!("🔚 Target closed connection");
                    // Send close message
                    let tunnel_close = Socks5TunnelData::close(tunnel_id);
                    let close_bytes = serde_json::to_vec(&tunnel_close)?;
                    station_clone.send_train(source_node_clone, close_bytes).await?;
                    break;
                }

                debug!("📤 Read {} bytes from target, sending to tunnel #{}", n, tunnel_id);

                // Send tunnel data
                let tunnel_data = Socks5TunnelData::new(tunnel_id, buf[..n].to_vec());
                let data_bytes = serde_json::to_vec(&tunnel_data)?;
                station_clone.send_train(source_node_clone, data_bytes).await?;
            }
            Ok::<(), anyhow::Error>(())
        };

        // Run tunnel in background (так же как в HTTP Proxy gateway!)
        tokio::spawn(async move {
            if let Err(e) = target_to_client.await {
                error!("❌ Tunnel #{} error: {}", tunnel_id, e);
            }
            debug!("🔚 Target-to-client task finished for tunnel #{}", tunnel_id);
        });

        debug!("🚇 Tunnel #{} established, background task started", tunnel_id);

        Ok(())
    }

    /// Handle tunnel data from client
    async fn handle_tunnel_data(
        active_tunnels: Arc<Mutex<HashMap<u64, tokio::net::tcp::OwnedWriteHalf>>>,
        tunnel_rx: Arc<Mutex<Option<tokio::sync::mpsc::Receiver<(HashId, Socks5TunnelData)>>>>,
    ) {
        debug!("🚇 [tunnel-data-handler] Starting tunnel data handler task");

        let mut rx_opt = { tunnel_rx.lock().await.take() };

        debug!("🚇 [tunnel-data-handler] Receiver obtained: {}", rx_opt.is_some());

        if let Some(mut rx) = rx_opt {
            debug!("🚇 [tunnel-data-handler] Ready to receive tunnel data");
            while let Some((_source_node, tunnel_data)) = rx.recv().await {
                debug!("🚇 [tunnel-data-handler] Received data for tunnel #{}", tunnel_data.tunnel_id);
                let tunnel_id = tunnel_data.tunnel_id;

                // Check if tunnel should be closed
                if tunnel_data.close {
                    debug!("🔚 Tunnel #{} closed by client", tunnel_id);
                    // Remove from active tunnels
                    let mut tunnels = active_tunnels.lock().await;
                    tunnels.remove(&tunnel_id);
                    continue;
                }

                // Get write half for this tunnel (keep lock during write!)
                let write_result = {
                    let mut tunnels = active_tunnels.lock().await;

                    if let Some(write_half) = tunnels.get_mut(&tunnel_id) {
                        // Write data while holding lock
                        debug!("📥 Writing {} bytes to tunnel #{}", tunnel_data.data.len(), tunnel_id);
                        write_half.write_all(&tunnel_data.data).await
                            .map_err(|e| e.to_string())
                    } else {
                        Err("No active tunnel".to_string())
                    }
                };

                if let Err(e) = write_result {
                    error!("❌ Error writing to tunnel #{}: {}", tunnel_id, e);
                    // Remove broken tunnel
                    let mut tunnels = active_tunnels.lock().await;
                    tunnels.remove(&tunnel_id);
                }
            }
        }
    }

    /// Send response back to client
    async fn send_response(&self, target_node: HashId, response: Socks5ProxyResponse) -> Result<()> {
        let response_bytes = serde_json::to_vec(&response)
            .map_err(|e| anyhow!("Failed to serialize response: {}", e))?;

        let _stream_id = self.station.send_train_batched(target_node, response_bytes).await
            .map_err(|e| anyhow!("Failed to send response: {}", e))?;

        Ok(())
    }
}
