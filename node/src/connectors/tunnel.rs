// src/connectors/tunnel.rs
//! Tunnel Connectors
//! =================
//!
//! Tunneling support for traffic encapsulation and bypass

use std::net::SocketAddr;
use std::time::Duration;
use anyhow::{Result, anyhow};
use tokio::net::UdpSocket;

/// Tunnel type
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TunnelType {
    /// IPIP tunnel (IP-in-IP encapsulation)
    Ipip,
    /// GRE tunnel
    Gre,
    /// SOCKS5 proxy tunnel
    Socks5,
    /// HTTP proxy tunnel
    HttpProxy,
    /// Custom obfuscated tunnel
    Obfuscated,
}

/// Tunnel configuration
#[derive(Debug, Clone)]
pub struct TunnelConfig {
    pub tunnel_type: TunnelType,
    pub remote_addr: SocketAddr,
    pub timeout: Duration,
    pub encrypt: bool,
    /// For proxy tunnels
    pub proxy_addr: Option<SocketAddr>,
}

impl Default for TunnelConfig {
    fn default() -> Self {
        Self {
            tunnel_type: TunnelType::Ipip,
            remote_addr: "0.0.0.0:0".parse().unwrap(),
            timeout: Duration::from_secs(10),
            encrypt: true,
            proxy_addr: None,
        }
    }
}

/// Tunnel connection wrapper
pub struct TunnelConnection {
    config: TunnelConfig,
    /// Underlying UDP socket for tunnel traffic
    socket: UdpSocket,
    /// Remote tunnel endpoint
    remote_endpoint: SocketAddr,
}

impl TunnelConnection {
    /// Create new tunnel connection
    pub async fn connect(config: TunnelConfig) -> Result<Self> {
        let socket = UdpSocket::bind("0.0.0.0:0").await
            .map_err(|e| anyhow!("Failed to bind tunnel socket: {}", e))?;

        println!("[tunnel] {} tunnel to {}",
            match config.tunnel_type {
                TunnelType::Ipip => "IPIP",
                TunnelType::Gre => "GRE",
                TunnelType::Socks5 => "SOCKS5",
                TunnelType::HttpProxy => "HTTP Proxy",
                TunnelType::Obfuscated => "Obfuscated",
            },
            config.remote_addr
        );

        let remote_endpoint = config.remote_addr;

        Ok(Self {
            config,
            socket,
            remote_endpoint,
        })
    }

    /// Send encapsulated packet through tunnel
    pub async fn send(&self, data: &[u8]) -> Result<usize> {
        let packet = self.encapsulate(data)?;
        self.socket.send_to(&packet, self.remote_endpoint).await
            .map_err(|e| anyhow!("Tunnel send failed: {}", e))
    }

    /// Receive decapsulated packet from tunnel
    pub async fn recv(&self, buf: &mut [u8]) -> Result<usize> {
        let mut tunnel_buf = vec![0u8; buf.len() + 100]; // Extra space for tunnel header
        let (n, _from) = self.socket.recv_from(&mut tunnel_buf).await
            .map_err(|e| anyhow!("Tunnel recv failed: {}", e))?;

        let decap = self.decapsulate(&tunnel_buf[..n])?;
        let copy_len = std::cmp::min(decap.len(), buf.len());
        buf[..copy_len].copy_from_slice(&decap[..copy_len]);
        Ok(copy_len)
    }

    /// Encapsulate data with tunnel header
    fn encapsulate(&self, data: &[u8]) -> Result<Vec<u8>> {
        let mut packet = Vec::with_capacity(data.len() + 20);

        // Add tunnel header based on type
        match self.config.tunnel_type {
            TunnelType::Ipip => {
                // Simple IPIP-like header (simplified)
                packet.extend_from_slice(&[0x45]);  // Version + IHL
                packet.extend_from_slice(&[0x00]);  // TOS
                packet.extend_from_slice(&(data.len() as u16 + 20).to_be_bytes());  // Total length
                packet.extend_from_slice(&[0x00, 0x00, 0x00, 0x00]);  // ID, flags, fragment
                packet.extend_from_slice(&[64, 0x01]);  // TTL + Protocol (IPv4)
                packet.extend_from_slice(&[0x00; 2]);  // Checksum (skip for simplicity)
            }
            TunnelType::Gre => {
                // GRE header (simplified)
                packet.extend_from_slice(&[0x00, 0x00]);  // Flags
                packet.extend_from_slice(&[0x08, 0x00]);  // Protocol (IPv4)
                packet.extend_from_slice(&[0x00; 4]);  // Checksum + offset
            }
            TunnelType::Obfuscated => {
                // Obfuscation header
                let key: u16 = 0x4F4B;  // Simple obfuscation key
                packet.extend_from_slice(&key.to_be_bytes());
                packet.extend_from_slice(&(data.len() as u16).to_be_bytes());
            }
            _ => {
                // No header for proxy tunnels (handled at application layer)
            }
        }

        packet.extend_from_slice(data);
        Ok(packet)
    }

    /// Decapsulate data from tunnel packet
    fn decapsulate(&self, packet: &[u8]) -> Result<Vec<u8>> {
        match self.config.tunnel_type {
            TunnelType::Ipip => {
                // Skip IPIP header (20 bytes)
                if packet.len() < 20 {
                    return Ok(Vec::new());
                }
                Ok(packet[20..].to_vec())
            }
            TunnelType::Gre => {
                // Skip GRE header (8 bytes)
                if packet.len() < 8 {
                    return Ok(Vec::new());
                }
                Ok(packet[8..].to_vec())
            }
            TunnelType::Obfuscated => {
                // Skip obfuscation header (4 bytes)
                if packet.len() < 4 {
                    return Ok(Vec::new());
                }
                Ok(packet[4..].to_vec())
            }
            _ => {
                // No encapsulation for proxy tunnels
                Ok(packet.to_vec())
            }
        }
    }

    /// Get tunnel configuration
    pub fn config(&self) -> &TunnelConfig {
        &self.config
    }
}

/// SSH tunnel support (forward local port to remote)
pub struct SshTunnel {
    remote_host: String,
    remote_port: u16,
    local_port: u16,
}

impl SshTunnel {
    /// Create new SSH tunnel
    pub fn new(remote_host: String, remote_port: u16, local_port: u16) -> Self {
        Self {
            remote_host,
            remote_port,
            local_port,
        }
    }

    /// Establish SSH tunnel (simplified - would need SSH library)
    pub async fn connect(&self) -> Result<()> {
        println!("[ssh_tunnel] Tunneling localhost:{} -> {}:{}",
            self.local_port, self.remote_host, self.remote_port);
        println!("[ssh_tunnel] Note: Full SSH tunnel implementation requires ssh2 crate");
        Ok(())
    }

    /// Get local port
    pub fn local_port(&self) -> u16 {
        self.local_port
    }
}
