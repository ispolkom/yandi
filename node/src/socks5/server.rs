// src/socks5/server.rs
//! SOCKS5 Server Implementation
//! =============================
//!
//! SOCKS5 proxy server for traffic relay

use std::net::SocketAddr;
use std::sync::Arc;
use std::collections::HashMap;
use tokio::net::{TcpListener, TcpStream};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::{Mutex, RwLock};
use anyhow::{Result, anyhow};

use super::protocol::*;
use super::{Socks5Config, Socks5Error, Socks5ProxyRequest, Socks5ProxyResponse, Socks5TunnelData};
use crate::netlayer::P2PTransport;
use crate::util::HashId;
use crate::protocol::Station;
use tracing::{info, error, debug, warn};

/// SOCKS5 server (обычный TCP mode)
pub struct Socks5Server {
    config: Socks5Config,
}

/// SOCKS5 Proxy Server via P2P (аналог HttpProxyClient)
pub struct Socks5ProxyServer {
    config: Socks5Config,
    transport: Arc<P2PTransport>,
    exit_node_id: Option<HashId>,
    next_request_id: Arc<RwLock<u64>>,
    /// Pending requests: request_id -> oneshot sender
    pending_requests: Arc<Mutex<HashMap<u64, tokio::sync::oneshot::Sender<Socks5ProxyResponse>>>>,
    /// Active tunnels: tunnel_id -> write half
    active_tunnels: Arc<Mutex<HashMap<u64, tokio::net::tcp::OwnedWriteHalf>>>,
    /// Channels for responses and tunnel data
    response_rx: Arc<Mutex<Option<tokio::sync::mpsc::Receiver<(HashId, Socks5ProxyResponse)>>>>,
    tunnel_data_rx: Arc<Mutex<Option<tokio::sync::mpsc::Receiver<(HashId, Socks5TunnelData)>>>>,
    pub station: Arc<Station>,
    /// 🆕 Hardening Step 4: маршрут через onion-circuit. Если задан, трафик идёт
    /// через `transport.send_circuit_data_onion(cid, …)` вместо прямого `station.send_train`.
    pub circuit_route: Arc<Mutex<Option<crate::netlayer::circuit::CircuitId>>>,
}

impl Socks5Server {
    /// Create new SOCKS5 server
    pub fn new(config: Socks5Config) -> Self {
        println!("[socks5] Creating SOCKS5 server on {}", config.listen_addr);
        Self { config }
    }

    /// Start SOCKS5 server
    pub async fn run(&self) -> Result<()> {
        let listener = TcpListener::bind(&self.config.listen_addr).await
            .map_err(|e| anyhow!("Failed to bind SOCKS5 server: {}", e))?;

        println!("[socks5] SOCKS5 server listening on {}", self.config.listen_addr);

        loop {
            match listener.accept().await {
                Ok((stream, addr)) => {
                    println!("[socks5] New connection from {}", addr);
                    let config = self.config.clone();

                    tokio::spawn(async move {
                        if let Err(e) = Self::handle_client(stream, addr, config).await {
                            eprintln!("[socks5] Error handling {}: {:?}", addr, e);
                        }
                    });
                }
                Err(e) => {
                    eprintln!("[socks5] Error accepting connection: {}", e);
                }
            }
        }
    }

    /// Handle single SOCKS5 client connection
    async fn handle_client(mut stream: TcpStream, client_addr: SocketAddr, config: Socks5Config) -> Result<()> {
        // Phase 1: Authentication selection
        let auth_method = Self::do_auth_selection(&mut stream, &config).await?;

        // Phase 2: Handle authentication (if required)
        if auth_method == Socks5AuthMethod::UserPass {
            Self::do_username_password_auth(&mut stream, &config).await?;
        }

        // Phase 3: Connection request
        let request = Self::read_request(&mut stream).await?;

        println!("[socks5] Request from {}: {:?}", client_addr, request.command);

        // Phase 4: Execute command
        match request.command {
            Socks5Command::Connect => {
                Self::handle_connect(&mut stream, &request.address, &config).await?;
            }
            Socks5Command::Bind => {
                Self::handle_bind(&mut stream, &request.address, &config).await?;
            }
            Socks5Command::UdpAssociate => {
                if config.enable_udp {
                    Self::handle_udp_associate(&mut stream, &request.address, &config).await?;
                } else {
                    // UDP associate disabled
                    let response = Socks5Response::error(Socks5Error::CommandNotSupported, None);
                    stream.write_all(&response.to_bytes()).await?;
                    return Err(anyhow!("UDP associate not enabled"));
                }
            }
        }

        Ok(())
    }

    /// Phase 1: Authentication selection
    async fn do_auth_selection(stream: &mut TcpStream, config: &Socks5Config) -> Result<Socks5AuthMethod> {
        let mut buf = [0u8; 256];

        // Read client hello
        let n = stream.read(&mut buf).await?;
        let auth_select = Socks5AuthSelect::from_bytes(&buf[..n])?;

        // Select auth method
        let method = if config.auth_required {
            if auth_select.methods.contains(&Socks5AuthMethod::UserPass) {
                Socks5AuthMethod::UserPass
            } else {
                Socks5AuthMethod::NoAcceptable
            }
        } else {
            if auth_select.methods.contains(&Socks5AuthMethod::NoAuth) {
                Socks5AuthMethod::NoAuth
            } else if auth_select.methods.contains(&Socks5AuthMethod::UserPass) {
                Socks5AuthMethod::UserPass
            } else {
                Socks5AuthMethod::NoAcceptable
            }
        };

        // Send selection response
        let response = Socks5AuthResponse::new(method);
        stream.write_all(&response.to_bytes()).await?;

        if method == Socks5AuthMethod::NoAcceptable {
            return Err(anyhow!("No acceptable auth method"));
        }

        Ok(method)
    }

    /// Username/password authentication (RFC 1929)
    async fn do_username_password_auth(stream: &mut TcpStream, config: &Socks5Config) -> Result<()> {
        let mut buf = [0u8; 512];

        let n = stream.read(&mut buf).await?;
        if n < 2 {
            return Err(anyhow!("Auth packet too short"));
        }

        let ulen = buf[1] as usize;
        if n < 2 + ulen {
            return Err(anyhow!("Username too long"));
        }

        let username = String::from_utf8_lossy(&buf[2..2+ulen]).to_string();

        let plen = buf[2+ulen] as usize;
        if n < 2 + ulen + 1 + plen {
            return Err(anyhow!("Password too long"));
        }

        let password = String::from_utf8_lossy(&buf[2+ulen+1..2+ulen+1+plen]).to_string();

        // Verify credentials
        let success = config.username.as_ref().zip(config.password.as_ref())
            .map(|(expected_user, expected_pass)| {
                username == *expected_user && password == *expected_pass
            })
            .unwrap_or(false);

        // Send auth response
        stream.write_all(&[0x01, if success { 0x00 } else { 0x01 }]).await?;

        if !success {
            return Err(anyhow!("Invalid username or password"));
        }

        println!("[socks5] Authentication successful for user: {}", username);
        Ok(())
    }

    /// Read connection request
    async fn read_request(stream: &mut TcpStream) -> Result<Socks5Request> {
        let mut buf = [0u8; 512];

        let n = stream.read(&mut buf).await?;
        let request = Socks5Request::from_bytes(&buf[..n])?;

        Ok(request)
    }

    /// Handle CONNECT command
    async fn handle_connect(stream: &mut TcpStream, addr: &Socks5Address, _config: &Socks5Config) -> Result<()> {
        // Resolve domain if needed
        let target_addr = if let Some(socket_addr) = addr.to_socket_addr() {
            socket_addr
        } else {
            // Domain name - resolve it
            match addr {
                Socks5Address::Domain(domain, port) => {
                    // Use tokio DNS resolution
                    let addrs = tokio::net::lookup_host(format!("{}:{}", domain, port)).await?;
                    addrs.into_iter().next()
                        .ok_or_else(|| anyhow!("Failed to resolve domain: {}", domain))?
                }
                _ => return Err(anyhow!("Invalid address for connect")),
            }
        };

        // Connect to target
        let target_stream = match tokio::time::timeout(
            std::time::Duration::from_secs(10),
            TcpStream::connect(target_addr)
        ).await {
            Ok(Ok(stream)) => stream,
            Ok(Err(e)) => {
                let response = Socks5Response::error(Socks5Error::HostUnreachable, None);
                stream.write_all(&response.to_bytes()).await?;
                return Err(anyhow!("Failed to connect to target: {}", e));
            }
            Err(_) => {
                let response = Socks5Response::error(Socks5Error::TtlExpired, None);
                stream.write_all(&response.to_bytes()).await?;
                return Err(anyhow!("Connection timeout"));
            }
        };

        // Send success response
        let local_addr = target_stream.local_addr()?;
        let bind_addr = Socks5Address::from_socket_addr(local_addr);
        let response = Socks5Response::success(bind_addr);
        stream.write_all(&response.to_bytes()).await?;

        println!("[socks5] Connected to {}", target_addr);

        // Relay data
        let (mut client_read, mut client_write) = stream.split();
        let (mut target_read, mut target_write) = target_stream.into_split();

        let client_to_target = tokio::io::copy(&mut client_read, &mut target_write);
        let target_to_client = tokio::io::copy(&mut target_read, &mut client_write);

        tokio::select! {
            result = client_to_target => {
                if let Err(e) = result {
                    eprintln!("[socks5] Client->Target error: {}", e);
                }
            }
            result = target_to_client => {
                if let Err(e) = result {
                    eprintln!("[socks5] Target->Client error: {}", e);
                }
            }
        }

        println!("[socks5] Connection closed");
        Ok(())
    }

    /// Handle BIND command (not commonly used)
    async fn handle_bind(stream: &mut TcpStream, _addr: &Socks5Address, _config: &Socks5Config) -> Result<()> {
        // BIND is for reverse connections - rarely used
        let response = Socks5Response::error(Socks5Error::CommandNotSupported, None);
        stream.write_all(&response.to_bytes()).await?;
        Err(anyhow!("BIND command not supported"))
    }

    /// Handle UDP ASSOCIATE command
    async fn handle_udp_associate(stream: &mut TcpStream, _addr: &Socks5Address, _config: &Socks5Config) -> Result<()> {
        // Bind UDP socket
        let udp_socket = tokio::net::UdpSocket::bind("0.0.0.0:0").await
            .map_err(|e| anyhow!("Failed to bind UDP: {}", e))?;

        let udp_addr = udp_socket.local_addr()?;
        let bind_addr = Socks5Address::from_socket_addr(udp_addr);

        // Send success response with UDP relay address
        let response = Socks5Response::success(bind_addr);
        stream.write_all(&response.to_bytes()).await?;

        println!("[socks5] UDP relay listening on {}", udp_addr);

        // Keep TCP connection alive and relay UDP packets
        // (Simplified - full implementation would handle UDP relay)
        tokio::time::sleep(std::time::Duration::from_secs(300)).await;

        Ok(())
    }
}

impl Socks5ProxyServer {
    /// Create new P2P SOCKS5 proxy server
    pub fn new(config: Socks5Config, transport: Arc<P2PTransport>) -> Self {
        println!("[socks5-proxy] Creating P2P SOCKS5 proxy on {}", config.listen_addr);

        let station = Station::with_defaults(
            transport.identity().node_id(),
            transport.clone()
        );

        Self {
            config,
            transport,
            exit_node_id: None,
            next_request_id: Arc::new(RwLock::new(1)),
            pending_requests: Arc::new(Mutex::new(HashMap::new())),
            active_tunnels: Arc::new(Mutex::new(HashMap::new())),
            response_rx: Arc::new(Mutex::new(None)),
            tunnel_data_rx: Arc::new(Mutex::new(None)),
            station: Arc::new(station),
            circuit_route: Arc::new(Mutex::new(None)),
        }
    }

    /// 🆕 Hardening Step 4: переключить SOCKS5-сервер на onion-circuit route.
    pub async fn set_circuit_route(&self, cid: Option<crate::netlayer::circuit::CircuitId>) {
        *self.circuit_route.lock().await = cid;
    }

    /// Set response channel
    pub fn with_response_channel(mut self, rx: tokio::sync::mpsc::Receiver<(HashId, Socks5ProxyResponse)>) -> Self {
        self.response_rx = Arc::new(Mutex::new(Some(rx)));
        self
    }

    /// Set tunnel data channel
    pub fn with_tunnel_data_channel(mut self, rx: tokio::sync::mpsc::Receiver<(HashId, Socks5TunnelData)>) -> Self {
        self.tunnel_data_rx = Arc::new(Mutex::new(Some(rx)));
        self
    }

    /// Set exit node for all traffic
    pub fn with_exit_node(mut self, exit_node_id: HashId) -> Self {
        println!("[socks5-proxy] Using exit node: {}", hex::encode(&exit_node_id.0[..8]));
        self.exit_node_id = Some(exit_node_id);
        self
    }

    /// Start P2P SOCKS5 proxy server
    pub async fn run(&self) -> Result<()> {
        let listener = TcpListener::bind(&self.config.listen_addr).await
            .map_err(|e| anyhow!("Failed to bind SOCKS5 proxy server: {}", e))?;

        info!("🧦 SOCKS5 Proxy listening on {}", self.config.listen_addr);
        info!("📡 Exit node: {:?}", self.exit_node_id.map(|id| hex::encode(&id.0[..8])));

        // Subscribe to responses
        let transport_clone = self.transport.clone();
        let pending_clone = self.pending_requests.clone();
        let response_rx_clone = self.response_rx.clone();
        tokio::spawn(async move {
            Self::handle_responses(transport_clone, pending_clone, response_rx_clone).await;
        });

        // Subscribe to tunnel data
        let active_tunnels_clone = self.active_tunnels.clone();
        let tunnel_rx_clone = self.tunnel_data_rx.clone();
        tokio::spawn(async move {
            Self::handle_tunnel_data(active_tunnels_clone, tunnel_rx_clone).await;
        });

        // Accept incoming connections
        loop {
            match listener.accept().await {
                Ok((stream, addr)) => {
                    debug!("📥 New SOCKS5 connection from {}", addr);

                    let client = self.clone_for_handler();
                    tokio::spawn(async move {
                        if let Err(e) = client.handle_client_p2p(stream, addr).await {
                            error!("❌ Error handling SOCKS5 client {}: {}", addr, e);
                        }
                    });
                }
                Err(e) => {
                    error!("❌ Error accepting SOCKS5 connection: {}", e);
                }
            }
        }
    }

    /// Clone for handler
    fn clone_for_handler(&self) -> Self {
        Self {
            config: self.config.clone(),
            transport: self.transport.clone(),
            exit_node_id: self.exit_node_id,
            next_request_id: self.next_request_id.clone(),
            pending_requests: self.pending_requests.clone(),
            active_tunnels: self.active_tunnels.clone(),
            response_rx: self.response_rx.clone(),
            tunnel_data_rx: self.tunnel_data_rx.clone(),
            station: self.station.clone(),
            circuit_route: self.circuit_route.clone(),
        }
    }

    /// Handle incoming responses from exit node
    async fn handle_responses(
        _transport: Arc<P2PTransport>,
        pending: Arc<Mutex<HashMap<u64, tokio::sync::oneshot::Sender<Socks5ProxyResponse>>>>,
        response_rx: Arc<Mutex<Option<tokio::sync::mpsc::Receiver<(HashId, Socks5ProxyResponse)>>>>,
    ) {
        let mut rx_opt = { response_rx.lock().await.take() };

        if let Some(mut rx) = rx_opt {
            while let Some((_source_node, response)) = rx.recv().await {
                debug!("📨 Received SOCKS5 response for request #{}", response.request_id);

                // Find pending request
                let sender_opt = {
                    let mut pending = pending.lock().await;
                    pending.remove(&response.request_id)
                };

                if let Some(sender) = sender_opt {
                    let _ = sender.send(response);
                } else {
                    warn!("⚠️  No pending request for #{}", response.request_id);
                }
            }
        }
    }

    /// Handle tunnel data from exit node
    async fn handle_tunnel_data(
        active_tunnels: Arc<Mutex<HashMap<u64, tokio::net::tcp::OwnedWriteHalf>>>,
        tunnel_rx: Arc<Mutex<Option<tokio::sync::mpsc::Receiver<(HashId, Socks5TunnelData)>>>>,
    ) {
        let mut rx_opt = { tunnel_rx.lock().await.take() };

        if let Some(mut rx) = rx_opt {
            while let Some((_source_node, tunnel_data)) = rx.recv().await {
                let tunnel_id = tunnel_data.tunnel_id;

                // Check if tunnel should be closed
                if tunnel_data.close {
                    debug!("🔚 Tunnel #{} closed by exit node", tunnel_id);
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
                        write_half.write_all(&tunnel_data.data).await
                            .map_err(|e| (e.to_string()))
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

    /// Handle single P2P SOCKS5 client connection
    async fn handle_client_p2p(&self, mut client_stream: TcpStream, client_addr: SocketAddr) -> Result<()> {
        // Phase 1: Authentication selection
        let auth_method = Socks5Server::do_auth_selection(&mut client_stream, &self.config).await?;

        // Phase 2: Handle authentication (if required)
        if auth_method == Socks5AuthMethod::UserPass {
            Socks5Server::do_username_password_auth(&mut client_stream, &self.config).await?;
        }

        // Phase 3: Connection request
        let request = Socks5Server::read_request(&mut client_stream).await?;

        debug!("📨 SOCKS5 request from {}: {:?}", client_addr, request.command);

        // Phase 4: Execute command via P2P
        match request.command {
            Socks5Command::Connect => {
                // Take ownership for CONNECT
                self.handle_connect_p2p(client_stream, &request.address, client_addr).await?;
            }
            Socks5Command::Bind => {
                let response = Socks5Response::error(Socks5Error::CommandNotSupported, None);
                client_stream.write_all(&response.to_bytes()).await?;
                return Err(anyhow!("BIND command not supported"));
            }
            Socks5Command::UdpAssociate => {
                let response = Socks5Response::error(Socks5Error::CommandNotSupported, None);
                client_stream.write_all(&response.to_bytes()).await?;
                return Err(anyhow!("UDP associate not supported in P2P mode"));
            }
        }

        Ok(())
    }

    /// Handle CONNECT command via P2P (аналог HttpProxyClient::handle_connect_tunnel)
    async fn handle_connect_p2p(&self, mut client_stream: TcpStream, target_addr: &Socks5Address, _client_addr: SocketAddr) -> Result<()> {
        // ⚡ TCP NODELAY - critical for SOCKS5 performance!
        if let Err(e) = client_stream.set_nodelay(true) {
            error!("❌ Failed to set TCP_NODELAY on client stream: {}", e);
        } else {
            debug!("✅ TCP_NODELAY enabled for client");
        }

        // 1. Определяем exit node
        let exit_node = self.exit_node_id.ok_or_else(|| anyhow!("No exit node configured"))?;

        // 2. Генерируем request_id
        let request_id = {
            let mut id = self.next_request_id.write().await;
            let current = *id;
            *id = id.wrapping_add(1);
            current
        };

        debug!("🔌 CONNECT request #{} to {:?}", request_id, target_addr);

        // 3. Парсим target address
        let (target_host, target_port) = match target_addr {
            Socks5Address::Ipv4(ip, port) => (ip.to_string(), *port),
            Socks5Address::Domain(domain, port) => (domain.clone(), *port),
            Socks5Address::Ipv6(ip, port) => (ip.to_string(), *port),
        };

        // 4. Создаём Socks5ProxyRequest
        let proxy_request = Socks5ProxyRequest::new_connect(request_id, target_host.clone(), target_port);

        // 5. Создаём oneshot для ответа
        let (tx, rx) = tokio::sync::oneshot::channel();

        // 6. Сохраняем pending request
        {
            let mut pending = self.pending_requests.lock().await;
            pending.insert(request_id, tx);
        }

        // 7. Сериализуем и отправляем через YTP
        let request_bytes = serde_json::to_vec(&proxy_request)
            .map_err(|e| anyhow!("Failed to serialize SOCKS5 request: {}", e))?;

        debug!("📤 Sending SOCKS5 request via YTP to exit node");

        // 🆕 Hardening Step 4: если задан circuit_route, шлём через onion-circuit.
        let circuit_snapshot = *self.circuit_route.lock().await;
        if let Some(cid) = circuit_snapshot {
            self.transport.send_circuit_data_onion(cid, &request_bytes).await
                .map_err(|e| anyhow!("Failed to send SOCKS5 request via circuit: {}", e))?;
        } else {
            self.station.send_train_batched(exit_node, request_bytes).await
                .map_err(|e| anyhow!("Failed to send SOCKS5 request: {}", e))?;
        }

        debug!("⏳ Waiting for SOCKS5 response...");

        // 8. Ждём ответа от exit node
        let proxy_response = tokio::time::timeout(
            std::time::Duration::from_secs(10),
            rx
        ).await
        .map_err(|_| anyhow!("Timeout waiting for SOCKS5 response"))?
        .map_err(|_| anyhow!("Failed to receive SOCKS5 response"))?;

        // 9. Проверяем статус
        if !proxy_response.is_success() {
            let error = Socks5Error::from_reply_byte(proxy_response.status);
            let response = Socks5Response::error(error, None);
            client_stream.write_all(&response.to_bytes()).await?;
            return Err(anyhow!("Exit node failed to connect: status={}", proxy_response.status));
        }

        debug!("✅ Exit node connected successfully");

        // 10. Отправляем успешный SOCKS5 ответ клиенту
        let bind_addr = Socks5Address::Ipv4(std::net::Ipv4Addr::new(0, 0, 0, 0), 0);
        let response = Socks5Response::success(bind_addr);
        client_stream.write_all(&response.to_bytes()).await?;

        debug!("🚪 Starting tunnel #{}", request_id);

        // 11. Разделяем клиентский stream
        let (mut client_read, mut client_write) = client_stream.into_split();

        // 12. Сохраняем write half в active_tunnels
        {
            let mut tunnels = self.active_tunnels.lock().await;
            tunnels.insert(request_id, client_write);
        }

        // 13. Читаем данные от клиента и отправляем в туннель (в фоновом режиме!)
        let station_clone = self.station.clone();
        let exit_node_clone = exit_node;
        // 🆕 Hardening Step 4: snapshot circuit-route на момент tunnel-open
        // (consistent с HttpProxyClient: смена в процессе сессии не учитывается).
        let circuit_snapshot_tunnel = *self.circuit_route.lock().await;
        let transport_for_circuit = self.transport.clone();

        // Запускаем в фоновом режиме, чтобы handle_tunnel_data мог работать параллельно!
        tokio::spawn(async move {
            const BUFFER_SIZE: usize = 16 * 1024; // ⚡ 32 KB instead of 4 KB
            let mut buf = vec![0u8; BUFFER_SIZE];
            loop {
                match client_read.read(&mut buf).await {
                    Ok(0) => {
                        debug!("🔚 Client closed connection");
                        // Send close message
                        let tunnel_close = Socks5TunnelData::close(request_id);
                        let close_bytes = match serde_json::to_vec(&tunnel_close) {
                            Ok(bytes) => bytes,
                            Err(e) => {
                                error!("❌ Failed to serialize close message: {}", e);
                                break;
                            }
                        };
                        let send_res: std::result::Result<(), anyhow::Error> = if let Some(cid) = circuit_snapshot_tunnel {
                            transport_for_circuit.send_circuit_data_onion(cid, &close_bytes).await
                                .map(|_| ())
                                .map_err(|e| anyhow!("circuit send: {}", e))
                        } else {
                            station_clone.send_train(exit_node_clone, close_bytes).await
                                .map(|_| ())
                                .map_err(|e| anyhow!("send_train: {}", e))
                        };
                        if let Err(e) = send_res {
                            error!("❌ Failed to send close message: {}", e);
                        }
                        break;
                    }
                    Ok(n) => {
                        debug!("📤 Read {} bytes from client, sending to tunnel #{}", n, request_id);

                        // Send tunnel data
                        let tunnel_data = Socks5TunnelData::new(request_id, buf[..n].to_vec());
                        let data_bytes = match serde_json::to_vec(&tunnel_data) {
                            Ok(bytes) => bytes,
                            Err(e) => {
                                error!("❌ Failed to serialize tunnel data: {}", e);
                                break;
                            }
                        };

                        let send_res: std::result::Result<(), anyhow::Error> = if let Some(cid) = circuit_snapshot_tunnel {
                            transport_for_circuit.send_circuit_data_onion(cid, &data_bytes).await
                                .map(|_| ())
                                .map_err(|e| anyhow!("circuit send: {}", e))
                        } else {
                            station_clone.send_train(exit_node_clone, data_bytes).await
                                .map(|_| ())
                                .map_err(|e| anyhow!("send_train: {}", e))
                        };
                        if let Err(e) = send_res {
                            error!("❌ Failed to send tunnel data: {}", e);
                            break;
                        }
                    }
                    Err(e) => {
                        error!("❌ Error reading from client: {}", e);
                        break;
                    }
                }
            }
            debug!("🔚 Client-to-tunnel task finished for tunnel #{}", request_id);
        });

        debug!("🚇 Tunnel #{}: background task started, returning from handle_connect_p2p", request_id);

        Ok(())
    }
}
