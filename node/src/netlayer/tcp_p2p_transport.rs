//! TCP P2P Transport - исправленная версия
//!
//! Аналог UDP транспорта поверх TCP

use crate::util::HashId;
use crate::core::NodeIdentity;
use crate::netlayer::{
    port_manager::PortManager,
    adaptive::AdaptiveController,
    peer::PeerInfo,
    packet::{HelloPacket, HelloType},
    encryption::EncryptionManager,
    tunnel::TunnelManager,
    interface_detector::NetworkTopology,
    relay::RelayManager,
};
use crate::dht::Kademlia;
use crate::dataplane::{SharedStreamRegistry, StreamRegistry};
use crate::protocol::Station;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::net::{TcpListener, TcpStream};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::{Mutex, mpsc, RwLock};
use tracing::{info, error, warn, debug};

pub const DEFAULT_TCP_DISCOVERY_PORT: u16 = 9001;
pub const DEFAULT_TCP_DATA_PORT: u16 = 10001;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransportState {
    Active,
    Rotating,
    Degraded,
}

impl Default for TransportState {
    fn default() -> Self {
        TransportState::Active
    }
}

#[derive(Debug, Clone)]
pub enum HelloEvent {
    Request { from: SocketAddr, packet: HelloPacket },
    Ack { from: SocketAddr, packet: HelloPacket },
}

pub struct TcpP2PTransport {
    pub identity: NodeIdentity,
    pub capabilities: u16,
    pub discovery_listener: Arc<Mutex<Option<TcpListener>>>,
    pub data_listener: Arc<Mutex<Option<TcpListener>>>,
    pub discovery_port: u16,
    pub data_port: u16,
    pub encryption: Arc<Mutex<EncryptionManager>>,
    pub peers: Arc<RwLock<HashMap<HashId, PeerInfo>>>,
    pub hello_tx: mpsc::Sender<HelloEvent>,
    pub station: Arc<Mutex<Option<Arc<Station>>>>,
    pub dht: Arc<Mutex<Kademlia>>,
    pub tunnel_manager: Arc<Mutex<TunnelManager>>,
    pub port_manager: Arc<Mutex<PortManager>>,
    pub adaptive_controller: Arc<Mutex<AdaptiveController>>,
    pub state: Arc<RwLock<TransportState>>,
    pub next_connection_id: Arc<AtomicU64>,
    pub external_ip: Arc<Mutex<Option<String>>>,
    pub topology: Arc<Mutex<Option<NetworkTopology>>>,
    pub relay_manager: Arc<Mutex<RelayManager>>,
    pub port_rotation_tx: mpsc::Sender<(u16, u16)>,
    pub stream_registry: SharedStreamRegistry,
}

impl TcpP2PTransport {
    pub async fn new(identity: NodeIdentity, capabilities: u16) -> Result<Arc<Self>, String> {
        Self::with_handlers(identity, capabilities).await
    }

    pub async fn with_handlers(
        identity: NodeIdentity,
        capabilities: u16,
    ) -> Result<Arc<Self>, String> {
        info!("🚀 Initializing TCP P2P Transport...");

        let (hello_tx, _) = mpsc::channel(100);
        let (port_rotation_tx, _) = mpsc::channel(10);

        let encryption = Arc::new(Mutex::new(EncryptionManager::new(identity.node_id())));
        let port_manager = Arc::new(Mutex::new(PortManager::new(
            DEFAULT_TCP_DISCOVERY_PORT, 
            DEFAULT_TCP_DATA_PORT
        )));
        let adaptive_controller = Arc::new(Mutex::new(AdaptiveController::new()));
        let tunnel_manager = Arc::new(Mutex::new(TunnelManager::new()));
        let relay_manager = Arc::new(Mutex::new(RelayManager::new()));
        let stream_registry = Arc::new(Mutex::new(StreamRegistry::new()));

        // Привязываем TCP listeners
        let discovery_listener = TcpListener::bind(format!("0.0.0.0:{}", DEFAULT_TCP_DISCOVERY_PORT))
            .await
            .map_err(|e| format!("Failed to bind TCP discovery: {}", e))?;

        let data_listener = TcpListener::bind(format!("0.0.0.0:{}", DEFAULT_TCP_DATA_PORT))
            .await
            .map_err(|e| format!("Failed to bind TCP data: {}", e))?;

        let discovery_port = discovery_listener.local_addr()
            .map_err(|e| format!("Failed to get discovery addr: {}", e))?
            .port();

        let data_port = data_listener.local_addr()
            .map_err(|e| format!("Failed to get data addr: {}", e))?
            .port();

        info!("📡 TCP Transport bound: discovery={}, data={}", discovery_port, data_port);

        let node_id = identity.node_id();
        let dht = Arc::new(Mutex::new(Kademlia::new(node_id)));

        let transport = Arc::new(Self {
            identity,
            capabilities,
            discovery_listener: Arc::new(Mutex::new(Some(discovery_listener))),
            data_listener: Arc::new(Mutex::new(Some(data_listener))),
            discovery_port,
            data_port,
            encryption,
            peers: Arc::new(RwLock::new(HashMap::new())),
            hello_tx,
            station: Arc::new(Mutex::new(None)),
            dht,
            tunnel_manager,
            port_manager,
            adaptive_controller,
            state: Arc::new(RwLock::new(TransportState::Active)),
            next_connection_id: Arc::new(AtomicU64::new(1)),
            external_ip: Arc::new(Mutex::new(None)),
            topology: Arc::new(Mutex::new(None)),
            relay_manager,
            port_rotation_tx,
            stream_registry,
        });

        info!("✅ TCP P2P Transport initialized");
        Ok(transport)
    }

    pub fn node_id(&self) -> HashId {
        self.identity.node_id()
    }

    pub fn discovery_addr(&self) -> String {
        format!("0.0.0.0:{}", self.discovery_port)
    }

    pub fn data_addr(&self) -> String {
        format!("0.0.0.0:{}", self.data_port)
    }

    /// Получить локальный публичный ключ X25519
    pub fn get_local_public_key(&self) -> [u8; 32] {
        self.encryption.try_lock()
            .map(|e| e.local_keys.public)
            .unwrap_or([0u8; 32])
    }

    /// Отправить Hello Request через TCP
    pub async fn send_hello_request(&self, addr: &str) -> Result<(), String> {
        info!("📤 Sending Hello Request to {}", addr);

        let hello = HelloPacket::new_request(
            self.node_id(),
            self.identity.public_key(),
            self.get_local_public_key(),
            self.identity.cid(),
            self.capabilities,
        );

        let bytes = hello.to_bytes();

        let mut stream = TcpStream::connect(addr)
            .await
            .map_err(|e| format!("Failed to connect: {}", e))?;

        // TCP framing: [length:4][packet]
        stream.write_all(&(bytes.len() as u32).to_be_bytes())
            .await
            .map_err(|e| format!("Failed to write length: {}", e))?;

        stream.write_all(&bytes)
            .await
            .map_err(|e| format!("Failed to write packet: {}", e))?;

        info!("✅ Hello Request sent to {}", addr);
        Ok(())
    }

    /// Отправить Hello Ack через TCP
    pub async fn send_hello_ack(&self, addr: SocketAddr, request_nonce: u64) -> Result<(), String> {
        info!("📤 Sending Hello Ack to {}", addr);

        let hello = HelloPacket::new_ack(
            self.node_id(),
            self.identity.public_key(),
            self.get_local_public_key(),
            self.identity.cid(),
            self.capabilities,
            request_nonce,
        );

        let bytes = hello.to_bytes();

        let mut stream = TcpStream::connect(addr)
            .await
            .map_err(|e| format!("Failed to connect: {}", e))?;

        stream.write_all(&(bytes.len() as u32).to_be_bytes())
            .await
            .map_err(|e| format!("Failed to write length: {}", e))?;

        stream.write_all(&bytes)
            .await
            .map_err(|e| format!("Failed to write packet: {}", e))?;

        info!("✅ Hello Ack sent to {}", addr);
        Ok(())
    }

    /// Установить сессию с peer (ECDH)
    pub async fn establish_session(&self, peer_id: HashId, remote_public_key: [u8; 32], addr: String) -> Result<u64, String> {
        info!("🔐 Establishing session with peer: {}", hex::encode(&peer_id.0[..8]));

        // Создаём PeerInfo
        let peer = PeerInfo::new(peer_id, addr);

        // Выполняем ECDH и создаём сессию
        let version = {
            let mut encryption = self.encryption.lock().await;
            encryption.handle_key_exchange(&peer, &remote_public_key)
                .map_err(|e| format!("Key exchange failed: {}", e))?
        };

        // Добавляем peer в список
        {
            let mut peers = self.peers.write().await;
            peers.insert(peer_id, peer);
        }

        info!("✅ Session v{} established with peer: {}", version, hex::encode(&peer_id.0[..8]));
        Ok(version)
    }

    /// Отправить зашифрованные данные
    pub async fn send_encrypted(&self, peer_id: HashId, data: &[u8]) -> Result<(), String> {
        debug!("📤 Sending {} bytes to peer: {}", data.len(), hex::encode(&peer_id.0[..8]));

        // Получаем PeerInfo
        let peer = {
            let peers = self.peers.read().await;
            peers.get(&peer_id)
                .cloned()
                .ok_or_else(|| format!("Peer not found: {}", hex::encode(&peer_id.0[..8])))?
        };

        // Шифруем данные
        let encrypted = {
            let encryption = self.encryption.lock().await;
            encryption.encrypt(&peer, data)
                .map_err(|e| format!("Encryption failed: {}", e))?
        };

        // Подключаемся и отправляем
        let addr = peer.addr.clone();
        let mut stream = TcpStream::connect(&addr)
            .await
            .map_err(|e| format!("Failed to connect to {}: {}", addr, e))?;

        // TCP framing: [length:4][encrypted_data]
        stream.write_all(&(encrypted.len() as u32).to_be_bytes())
            .await
            .map_err(|e| format!("Failed to write length: {}", e))?;

        stream.write_all(&encrypted)
            .await
            .map_err(|e| format!("Failed to write data: {}", e))?;

        debug!("✅ Sent {} encrypted bytes to {}", encrypted.len(), addr);
        Ok(())
    }

    /// Запустить транспорт
    pub async fn start(transport: Arc<Self>) -> Result<(), String> {
        info!("🚀 Starting TCP P2P Transport...");

        // Запускаем discovery listener
        let t1 = transport.clone();
        tokio::spawn(async move {
            Self::run_discovery_listener(t1).await;
        });

        // Запускаем data listener
        let t2 = transport.clone();
        tokio::spawn(async move {
            Self::run_data_listener(t2).await;
        });

        info!("✅ TCP P2P Transport started");
        Ok(())
    }

    /// Discovery listener - принимает Hello пакеты
    async fn run_discovery_listener(transport: Arc<Self>) {
        info!("🔍 TCP Discovery listening on port {}", transport.discovery_port);

        loop {
            let listener_opt = transport.discovery_listener.lock().await.take();
            let mut listener = match listener_opt {
                Some(l) => l,
                None => {
                    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
                    continue;
                }
            };
            // Возвращаем listener обратно
            *transport.discovery_listener.lock().await = Some(listener.try_clone().ok());

            match listener.accept().await {
                Ok((mut stream, addr)) => {
                    info!("📥 Discovery connection from {}", addr);

                    let transport_clone = transport.clone();
                    tokio::spawn(async move {
                        // Читаем длину
                        let mut len_buf = [0u8; 4];
                        if let Err(e) = stream.read_exact(&mut len_buf).await {
                            warn!("Failed to read length from {}: {}", addr, e);
                            return;
                        }
                        let len = u32::from_be_bytes(len_buf) as usize;

                        if len > 65536 {
                            warn!("Packet too large from {}: {}", addr, len);
                            return;
                        }

                        // Читаем Hello пакет
                        let mut packet_buf = vec![0u8; len];
                        if let Err(e) = stream.read_exact(&mut packet_buf).await {
                            warn!("Failed to read packet from {}: {}", addr, e);
                            return;
                        }

                        // Парсим Hello
                        match HelloPacket::from_bytes(&packet_buf) {
                            Ok(hello) => {
                                info!("✅ Received Hello {} from {}", 
                                    if hello.is_request() { "Request" } else { "Ack" }, 
                                    addr);

                                match hello.hello_type {
                                    HelloType::Request => {
                                        // Отправляем Ack
                                        if let Err(e) = transport_clone.send_hello_ack(addr, hello.nonce).await {
                                            warn!("Failed to send Hello Ack: {}", e);
                                        }
                                    }
                                    HelloType::Ack => {
                                        // Устанавливаем сессию
                                        if let Err(e) = transport_clone.establish_session(
                                            hello.node_id, 
                                            hello.x25519_public,
                                            addr.to_string()
                                        ).await {
                                            warn!("Failed to establish session: {}", e);
                                        }
                                    }
                                }
                            }
                            Err(e) => {
                                warn!("Failed to parse Hello packet from {}: {}", addr, e);
                            }
                        }
                    });
                }
                Err(e) => {
                    error!("Accept error: {}", e);
                    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
                }
            }
        }
    }

    /// Data listener - принимает зашифрованные данные
    async fn run_data_listener(transport: Arc<Self>) {
        info!("📊 TCP Data listening on port {}", transport.data_port);

        loop {
            let listener_opt = transport.data_listener.lock().await.take();
            let mut listener = match listener_opt {
                Some(l) => l,
                None => {
                    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
                    continue;
                }
            };
            *transport.data_listener.lock().await = Some(listener.try_clone().ok());

            match listener.accept().await {
                Ok((mut stream, addr)) => {
                    debug!("📥 Data connection from {}", addr);

                    let transport_clone = transport.clone();
                    tokio::spawn(async move {
                        loop {
                            // Читаем длину
                            let mut len_buf = [0u8; 4];
                            if let Err(e) = stream.read_exact(&mut len_buf).await {
                                debug!("Connection closed from {}: {}", addr, e);
                                break;
                            }
                            let len = u32::from_be_bytes(len_buf) as usize;

                            if len > 65536 || len == 0 {
                                warn!("Invalid packet size from {}: {}", addr, len);
                                break;
                            }

                            // Читаем зашифрованные данные
                            let mut encrypted = vec![0u8; len];
                            if let Err(e) = stream.read_exact(&mut encrypted).await {
                                warn!("Failed to read data from {}: {}", addr, e);
                                break;
                            }

                            // Расшифровываем
                            match transport_clone.encryption.lock().await.decrypt_by_peer_id(&encrypted) {
                                Ok((sender_id, decrypted)) => {
                                    debug!("✅ Decrypted {} bytes from {}", decrypted.len(), 
                                        hex::encode(&sender_id.0[..8]));
                                    
                                    // TODO: Обработать расшифрованные данные
                                    // Здесь будет вызов station.handle_wagon() и т.д.
                                }
                                Err(e) => {
                                    warn!("Decryption failed from {}: {}", addr, e);
                                }
                            }
                        }
                    });
                }
                Err(e) => {
                    error!("Accept error: {}", e);
                    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
                }
            }
        }
    }
}
