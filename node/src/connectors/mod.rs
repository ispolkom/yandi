// src/connectors/mod.rs
//! Connectors Module - Network Transport
//! ======================================
//!
//! Network connectors for P2P communication with advanced transport support

pub mod tunnel;
pub mod quic;
pub mod obfuscate;

use std::time::{Duration, Instant};
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::{UdpSocket, TcpStream};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use anyhow::{Result, anyhow};

pub use tunnel::{TunnelConnection, TunnelConfig, TunnelType, SshTunnel};
pub use quic::{QuicConnection, QuicConfig, QuicState, QuicEndpoint};
pub use obfuscate::{ObfuscatedConnection, ObfuscationConfig, ObfuscationType, TrafficShaper};

/// Transport type
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransportType {
    Udp,
    Tcp,
}

/// Connection statistics
#[derive(Debug, Clone)]
pub struct ConnectionStats {
    pub transport_type: TransportType,
    pub established_at: Instant,
    pub bytes_sent: u64,
    pub bytes_received: u64,
}

impl ConnectionStats {
    pub fn new(transport_type: TransportType) -> Self {
        Self {
            transport_type,
            established_at: Instant::now(),
            bytes_sent: 0,
            bytes_received: 0,
        }
    }
}

/// UDP connection wrapper
pub struct UdpConnection {
    socket: Arc<UdpSocket>,
    remote_addr: SocketAddr,
    stats: ConnectionStats,
}

impl UdpConnection {
    /// Create new UDP connection
    pub async fn connect(remote_addr: SocketAddr) -> Result<Self> {
        // Bind to any available port
        let socket = UdpSocket::bind("0.0.0.0:0").await
            .map_err(|e| anyhow!("Failed to bind UDP socket: {}", e))?;

        // "Connect" to remote address (filters incoming packets)
        socket.connect(remote_addr).await
            .map_err(|e| anyhow!("Failed to connect UDP socket: {}", e))?;

        println!("[connectors] UDP connected to {}", remote_addr);

        Ok(Self {
            socket: Arc::new(socket),
            remote_addr,
            stats: ConnectionStats::new(TransportType::Udp),
        })
    }

    /// Send data
    pub async fn send(&mut self, data: &[u8]) -> Result<usize> {
        let n = self.socket.send(data).await
            .map_err(|e| anyhow!("UDP send failed: {}", e))?;

        self.stats.bytes_sent += n as u64;
        Ok(n)
    }

    /// Receive data
    pub async fn recv(&mut self, buf: &mut [u8]) -> Result<usize> {
        let n = self.socket.recv(buf).await
            .map_err(|e| anyhow!("UDP recv failed: {}", e))?;

        self.stats.bytes_received += n as u64;
        Ok(n)
    }

    /// Get remote address
    pub fn remote_addr(&self) -> SocketAddr {
        self.remote_addr
    }

    /// Get statistics
    pub fn stats(&self) -> &ConnectionStats {
        &self.stats
    }
}

/// TCP connection wrapper
pub struct TcpConnection {
    stream: TcpStream,
    remote_addr: SocketAddr,
    stats: ConnectionStats,
}

impl TcpConnection {
    /// Create new TCP connection
    pub async fn connect(remote_addr: SocketAddr) -> Result<Self> {
        let stream = tokio::time::timeout(
            Duration::from_secs(5),
            TcpStream::connect(remote_addr)
        ).await
        .map_err(|_| anyhow!("TCP connection timeout to {}", remote_addr))?
        .map_err(|e| anyhow!("TCP connect failed: {}", e))?;

        println!("[connectors] TCP connected to {}", remote_addr);

        Ok(Self {
            stream,
            remote_addr,
            stats: ConnectionStats::new(TransportType::Tcp),
        })
    }

    /// Send data
    pub async fn send(&mut self, data: &[u8]) -> Result<usize> {
        let n = self.stream.write(data).await
            .map_err(|e| anyhow!("TCP send failed: {}", e))?;

        self.stats.bytes_sent += n as u64;
        Ok(n)
    }

    /// Receive data
    pub async fn recv(&mut self, buf: &mut [u8]) -> Result<usize> {
        let n = self.stream.read(buf).await
            .map_err(|e| anyhow!("TCP recv failed: {}", e))?;

        self.stats.bytes_received += n as u64;
        Ok(n)
    }

    /// Get remote address
    pub fn remote_addr(&self) -> SocketAddr {
        self.remote_addr
    }

    /// Get statistics
    pub fn stats(&self) -> &ConnectionStats {
        &self.stats
    }

    /// Split into read and write halves
    pub fn split_into_halves(self) -> (tokio::net::tcp::OwnedReadHalf, tokio::net::tcp::OwnedWriteHalf) {
        self.stream.into_split()
    }
}

/// Simple connector for P2P
pub struct P2PConnector {
    /// Connection timeout
    timeout: Duration,
    /// Preferred transport
    preferred_transport: TransportType,
}

impl P2PConnector {
    /// Create new connector
    pub fn new() -> Self {
        Self {
            timeout: Duration::from_secs(5),
            preferred_transport: TransportType::Udp,
        }
    }

    /// Set connection timeout
    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    /// Set preferred transport
    pub fn with_preferred_transport(mut self, transport: TransportType) -> Self {
        self.preferred_transport = transport;
        self
    }

    /// Connect using preferred transport
    pub async fn connect(&self, addr: SocketAddr) -> Result<Connection> {
        println!("[connectors] Connecting to {} via {:?}", addr, self.preferred_transport);

        match self.preferred_transport {
            TransportType::Udp => {
                let conn = UdpConnection::connect(addr).await?;
                Ok(Connection::Udp(conn))
            }
            TransportType::Tcp => {
                let conn = TcpConnection::connect(addr).await?;
                Ok(Connection::Tcp(conn))
            }
        }
    }

    /// Connect with automatic fallback (try UDP first, then TCP)
    pub async fn connect_with_fallback(&self, addr: SocketAddr) -> Result<Connection> {
        println!("[connectors] Connecting to {} with fallback", addr);

        // Try UDP first
        match UdpConnection::connect(addr).await {
            Ok(conn) => {
                println!("[connectors] UDP connection successful");
                return Ok(Connection::Udp(conn));
            }
            Err(e) => {
                println!("[connectors] UDP failed: {}, trying TCP", e);
            }
        }

        // Fallback to TCP
        match TcpConnection::connect(addr).await {
            Ok(conn) => {
                println!("[connectors] TCP connection successful");
                Ok(Connection::Tcp(conn))
            }
            Err(e) => {
                Err(anyhow!("All connection attempts failed: {}", e))
            }
        }
    }
}

impl Default for P2PConnector {
    fn default() -> Self {
        Self::new()
    }
}

/// Connection enum
pub enum Connection {
    Udp(UdpConnection),
    Tcp(TcpConnection),
}

impl Connection {
    /// Get remote address
    pub fn remote_addr(&self) -> SocketAddr {
        match self {
            Connection::Udp(conn) => conn.remote_addr(),
            Connection::Tcp(conn) => conn.remote_addr(),
        }
    }

    /// Get transport type
    pub fn transport_type(&self) -> TransportType {
        match self {
            Connection::Udp(_) => TransportType::Udp,
            Connection::Tcp(_) => TransportType::Tcp,
        }
    }

    /// Get statistics
    pub fn stats(&self) -> &ConnectionStats {
        match self {
            Connection::Udp(conn) => conn.stats(),
            Connection::Tcp(conn) => conn.stats(),
        }
    }

    /// Send data
    pub async fn send(&mut self, data: &[u8]) -> Result<usize> {
        match self {
            Connection::Udp(conn) => conn.send(data).await,
            Connection::Tcp(conn) => conn.send(data).await,
        }
    }

    /// Receive data
    pub async fn recv(&mut self, buf: &mut [u8]) -> Result<usize> {
        match self {
            Connection::Udp(conn) => conn.recv(buf).await,
            Connection::Tcp(conn) => conn.recv(buf).await,
        }
    }
}

/// Create UDP socket bound to specific address
pub async fn bind_udp(addr: SocketAddr) -> Result<UdpSocket> {
    let socket = UdpSocket::bind(addr).await
        .map_err(|e| anyhow!("Failed to bind UDP to {}: {}", addr, e))?;

    println!("[connectors] UDP bound to {}", addr);
    Ok(socket)
}

/// Create TCP listener
pub async fn listen_tcp(addr: SocketAddr) -> Result<tokio::net::TcpListener> {
    let listener = tokio::net::TcpListener::bind(addr).await
        .map_err(|e| anyhow!("Failed to bind TCP to {}: {}", addr, e))?;

    println!("[connectors] TCP listening on {}", addr);
    Ok(listener)
}
