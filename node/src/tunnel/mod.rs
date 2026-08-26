// src/tunnel/mod.rs
//! UDP Tunnel Module
//! ==================
//!
//! Simple UDP tunnel through P2P network
//! Local UDP socket → P2P Stream → Exit Node → Target

pub mod udp_tunnel;

pub use udp_tunnel::{UdpTunnel, UdpTunnelConfig};
