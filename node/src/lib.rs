// src/lib.rs
//! YANDI - P2P Network Refactored from NET
//! ==========================================
//!
//! Clean architecture P2P network with production-grade cryptography.
//!
//! ## Architecture
//!
//! ```
//! ┌─────────────────────────────────────────────────────────────┐
//! │                    Application Layer                         │
//! │              (apps - resources, messages)                   │
//! ├─────────────────────────────────────────────────────────────┤
//! │                    Network Layer                             │
//! │        (netlayer - peers, packets, discovery)              │
//! ├─────────────────────────────────────────────────────────────┤
//! │                    Core Layer                               │
//! │  (core - identity, cryptography)                            │
//! ├─────────────────────────────────────────────────────────────┤
//! │                    Utilities                                │
//! │  (util - types, helpers)                                   │
//! └─────────────────────────────────────────────────────────────┘
//! ```
//!
//! ## Modules
//!
//! - **apps** - Application layer (resources, messages)
//! - **netlayer** - Network transport layer
//! - **dht** - Kademlia distributed hash table
//! - **bootstrap** - Initial peer discovery
//! - **connectors** - Network transport (UDP/TCP)
//! - **core** - Cryptographic identity and configuration
//! - **util** - Common types and utilities

pub mod apps;
pub mod ai_rpc;
pub mod netlayer;
pub mod dht;
// pub mod bootstrap;  // TODO: конфликтует с netlayer::bootstrap
pub mod connectors;
pub mod dataplane;
pub mod socks5;
pub mod tunnel;
pub mod observability;
pub mod core;
pub mod util;
pub mod proxy;
pub mod protocol;
pub mod mdns;
pub mod web;
pub mod communication;
pub mod p2p_tunnel;
pub mod p2p;

// Re-exports for convenience
pub use core::{NodeIdentity, NetConfig, YandiConfig, PortsConfig, ClientConfig, WsConfig, init_config, get_config, update_config, set_ws_bind_override, effective_ws_bind};
pub use util::{HashId, OSDetector, SystemInfo, NodePower, OperatingSystem, mask_hash_id, mask_ipv6, mask_ipv4, mask_public_key, format_bytes};
pub use apps::resource::{ResourceRegistry, ResourceEntry, GatewayMetadata};
pub use apps::message::NetMessage;
pub use netlayer::{PeerInfo, NetPacket, PacketType, HelloPacket, HelloType, ExternalIpService, NetworkTopology, NodeIntrospection, NodeCapabilities, NodeRole, NodeProfile, LocalX25519, SessionKey, EncryptionManager, P2PTransport, HelloEvent, P2PCli, BootstrapManager, BootstrapConfig, ExitHandlerRequest, YandiTunManager, IPv6PacketInfo};
pub use netlayer::adaptive::{AdaptiveController, TransportMode, AdaptiveMetrics};
pub use netlayer::transport::{TransportState, StreamStats};

pub use netlayer::port_manager::{PortManager, PortState};
pub use dht::{Kademlia, KTable, KBucket, DhtStorage, DhtQuery, DhtResponse, DhtQueryType};
// pub use bootstrap::{BootstrapManager, BootstrapNode, BootstrapConfig, BootstrapSource, NodeType};  // TODO: конфликтует с netlayer::bootstrap
pub use connectors::{P2PConnector, Connection, UdpConnection, TcpConnection, TransportType, bind_udp, listen_tcp};
pub use dataplane::{DataTransport, TransportConfig, TransportType as DataTransportType, QoSManager, PacketPriority, DataplaneMetrics, TransportStats, MultipathManager, PathSelector};
pub use socks5::{Socks5Server, Socks5ProxyServer, Socks5Client, Socks5Config, Socks5Command, Socks5AuthMethod, Socks5Address, ExitNodeHandler};
pub use tunnel::{UdpTunnel, UdpTunnelConfig};
pub use observability::{NetworkMetrics, init_logging, LogLevel};
pub use proxy::{HttpProxyClient, HttpProxyGateway, ProxyConfig};
pub use mdns::{MdnsService, MdnsAnnouncer, MdnsBrowser, DiscoveredNode, YANDI_SERVICE_TYPE, YANDI_ADMIN_TYPE};
pub use web::{WebServer, NodeInfo};
pub use web::auth::{AuthState, load_auth_state};
// P2P Transport for communications (port 9999) - без алиаса, используем полный путь

/// YANDI version
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// YANDI name
pub const NAME: &str = "YANDI";

pub mod media;

// Initialize media system
pub fn init_media() -> Result<(), String> {
    media::init()
}

// State Manager - adaptive transport control plane
pub mod state_manager;
