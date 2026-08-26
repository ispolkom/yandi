// src/tunnel/udp_tunnel.rs
//! UDP Tunnel Implementation
//! =========================
//!
//! Listens on local UDP socket and forwards through P2P stream to exit node

use std::sync::Arc;
use std::net::SocketAddr;
use tokio::net::UdpSocket;
use tokio::sync::Mutex;
use anyhow::{Result, anyhow};
use std::collections::HashMap;

use crate::netlayer::transport::P2PTransport;
use crate::util::HashId;

/// UDP tunnel configuration
#[derive(Debug, Clone)]
pub struct UdpTunnelConfig {
    /// Local UDP address to bind
    pub bind_addr: SocketAddr,

    /// Target host:port to connect to through exit node
    pub target: String,

    /// Exit node HashId
    pub exit_node: HashId,

    /// Maximum packet size
    pub max_packet_size: usize,
}

impl Default for UdpTunnelConfig {
    fn default() -> Self {
        Self {
            bind_addr: "127.0.0.1:1080".parse().unwrap(),
            target: String::new(),
            exit_node: HashId::default(),
            max_packet_size: 65535,
        }
    }
}

/// UDP Tunnel Server
///
/// Listens on local UDP socket and forwards all packets through P2P stream
pub struct UdpTunnel {
    config: UdpTunnelConfig,
    transport: Arc<P2PTransport>,
    socket: Arc<UdpSocket>,
    clients: Arc<Mutex<HashMap<SocketAddr, u64>>>, // peer_addr → stream_id
}

impl UdpTunnel {
    /// Create new UDP tunnel
    pub fn new(config: UdpTunnelConfig, transport: Arc<P2PTransport>) -> Result<Self> {
        let socket = std::net::UdpSocket::bind(config.bind_addr)
            .map_err(|e| anyhow::anyhow!("Failed to bind UDP socket: {}", e))?;

        socket.set_nonblocking(true)
            .map_err(|e| anyhow::anyhow!("Failed to set non-blocking: {}", e))?;

        println!("🚇 UDP Tunnel listening on: {}", config.bind_addr);
        println!("   Target: {} via exit node {}", config.target, hex::encode(&config.exit_node.0[..8]));

        Ok(Self {
            config,
            transport,
            socket: Arc::new(UdpSocket::from_std(socket)?),
            clients: Arc::new(Mutex::new(HashMap::new())),
        })
    }

    /// Get local address
    pub fn local_addr(&self) -> Result<SocketAddr> {
        self.socket.local_addr()
            .map_err(|e| anyhow::anyhow!("Failed to get local addr: {}", e))
    }

    /// Run tunnel server
    pub async fn run(&self) -> Result<()> {
        println!("🚇 UDP Tunnel server started");
        println!("   Listening: {}", self.config.bind_addr);
        println!("   Target: {} via {}", self.config.target, hex::encode(&self.config.exit_node.0[..8]));
        println!();

        let mut buf = vec![0u8; self.config.max_packet_size];

        loop {
            // Receive UDP packet from local application
            match self.socket.recv_from(&mut buf).await {
                Ok((n, peer_addr)) => {
                    eprintln!("[udp-tunnel] 📦 Received {} bytes from {}", n, peer_addr);

                    // Get or create stream for this peer
                    let stream_id = {
                        let mut clients = self.clients.lock().await;
                        if let Some(&sid) = clients.get(&peer_addr) {
                            sid
                        } else {
                            // Create new stream to exit node
                            match self.create_stream().await {
                                Ok(sid) => {
                                    println!("[udp-tunnel] ✅ New stream {} for peer {}", sid, peer_addr);
                                    clients.insert(peer_addr, sid);
                                    sid
                                }
                                Err(e) => {
                                    eprintln!("[udp-tunnel] ❌ Failed to create stream: {}", e);
                                    continue;
                                }
                            }
                        }
                    };

                    // Send through P2P stream
                    match self.transport.stream_write(stream_id as u32, &buf[..n]).await {
                        Ok(_) => {
                            eprintln!("[udp-tunnel] ✅ Sent {} bytes through stream {}", n, stream_id);
                        }
                        Err(e) => {
                            eprintln!("[udp-tunnel] ❌ Failed to send through stream: {}", e);
                            // Remove failed stream
                            let mut clients = self.clients.lock().await;
                            clients.remove(&peer_addr);
                            continue;
                        }
                    }

                    // Read response from stream
                    let mut read_buf = vec![0u8; 65535];
                    match self.transport.stream_read(stream_id as u32, &mut read_buf).await {
                        Ok(read_n) => {
                            if read_n > 0 {
                                eprintln!("[udp-tunnel] 📥 Received {} bytes from stream {}", read_n, stream_id);

                                // Send back to local peer
                                if let Err(e) = self.socket.send_to(&read_buf[..read_n], peer_addr).await {
                                    eprintln!("[udp-tunnel] ❌ Failed to send response: {}", e);
                                } else {
                                    eprintln!("[udp-tunnel] ✅ Sent {} bytes back to {}", read_n, peer_addr);
                                }
                            }
                        }
                        Err(e) => {
                            eprintln!("[udp-tunnel] ⚠️  Stream read error: {}", e);
                        }
                    }
                }
                Err(e) => {
                    eprintln!("[udp-tunnel] ⚠️  UDP recv error: {}", e);
                    tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;
                }
            }
        }
    }

    /// Create new P2P stream to exit node
    async fn create_stream(&self) -> Result<u64> {
        let stream_id = self.transport.stream_open(self.config.exit_node).await
            .map_err(|e| anyhow!("Failed to open stream: {}", e))?;

        // Send ConnectRequest to exit node
        use crate::socks5::proxy_protocol::{ProxyMsgType, ConnectRequest};
        use crate::socks5::protocol::Socks5Address;

        // Parse target string "host:port" to Socks5Address
        let parts: Vec<&str> = self.config.target.split(':').collect();
        if parts.len() != 2 {
            return Err(anyhow!("Invalid target format. Expected 'host:port', got: {}", self.config.target));
        }

        let host = parts[0];
        let port: u16 = parts[1].parse()
            .map_err(|e| anyhow!("Invalid port number: {}", e))?;

        // Try to parse as IP address first
        let address = if let Ok(ip) = host.parse::<std::net::Ipv4Addr>() {
            Socks5Address::Ipv4(ip, port)
        } else {
            // Domain name
            Socks5Address::Domain(host.to_string(), port)
        };

        let req = ConnectRequest::new(address);
        let req_bytes = req.to_bytes();

        self.transport.stream_write(stream_id, &req_bytes).await
            .map_err(|e| anyhow!("Failed to send ConnectRequest: {}", e))?;

        // Wait for ConnectResponse
        let mut resp_buf = [0u8; 1024];
        let timeout = tokio::time::Duration::from_secs(5);
        let start = std::time::Instant::now();

        while start.elapsed() < timeout {
            tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;

            match self.transport.stream_read(stream_id, &mut resp_buf).await {
                Ok(n) => {
                    if n >= 2 && resp_buf[0] == ProxyMsgType::ConnectResponse as u8 {
                        let status = resp_buf[1];
                        if status == 0x00 {
                            println!("[udp-tunnel] ✅ Connected to {}", self.config.target);
                            return Ok(stream_id as u64);
                        } else {
                            return Err(anyhow!("Connect failed with status: {}", status));
                        }
                    }
                }
                Err(_) => continue,
            }
        }

        Err(anyhow!("Timeout waiting for ConnectResponse"))
    }

    /// Spawn tunnel server in background
    pub fn spawn(self) -> tokio::task::JoinHandle<Result<()>> {
        tokio::spawn(async move {
            self.run().await
        })
    }
}
