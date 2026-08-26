# Connectors Module

**Network Transport Layer with Advanced Protocols**

**Status:** ✅ Fully implemented (UDP, TCP, QUIC, Tunnel, Obfuscation)
**Last Updated:** 2025-12-31

## Overview

The connectors module provides comprehensive network transport capabilities for the YANDI P2P network. It supports multiple protocols, tunneling, obfuscation, and advanced transport features to ensure connectivity in challenging network environments.

## Architecture

```
connectors/
├── mod.rs         # Basic connectors (UDP, TCP)
├── tunnel.rs      # Tunneling protocols (IPIP, GRE, SSH, SOCKS5)
├── quic.rs        # QUIC transport (UDP + TLS 1.3)
└── obfuscate.rs   # Traffic obfuscation and DPI bypass
```

## Core Components

### 1. Basic Transport (`mod.rs`)

**Transport Types:**
- `Udp` - Fast, connectionless datagrams
- `Tcp` - Reliable stream transport

**Features:**
- Connection timeout management
- Transport statistics tracking
- Automatic fallback (UDP → TCP)
- Async/await support with tokio

**Usage:**
```rust
// Simple connection
let connector = P2PConnector::new();
let mut conn = connector.connect(addr).await?;

// With fallback
let mut conn = connector.connect_with_fallback(addr).await?;
conn.send(b"Hello").await?;
```

### 2. Tunnel Transport (`tunnel.rs`)

Advanced tunneling for traffic encapsulation and bypass.

**Tunnel Types:**
- `Ipip` - IP-in-IP encapsulation (RFC 2003)
- `Gre` - Generic Routing Encapsulation (RFC 1701)
- `Socks5` - SOCKS5 proxy tunnel
- `HttpProxy` - HTTP CONNECT proxy
- `Obfuscated` - Custom obfuscated tunnel

**Features:**
- Header encapsulation/decapsulation
- Proxy support
- Encryption support
- Tunnel-specific optimization
- SSH port forwarding

**Usage:**
```rust
// IP tunnel
let config = TunnelConfig {
    tunnel_type: TunnelType::Ipip,
    remote_addr: "10.0.0.1:5000".parse()?,
    timeout: Duration::from_secs(10),
    encrypt: true,
    proxy_addr: None,
};

let tunnel = TunnelConnection::connect(config).await?;
tunnel.send(b"Tunneled data").await?;

// SSH tunnel
let ssh_tunnel = SshTunnel::new("remote.host".to_string(), 80, 8080);
ssh_tunnel.connect().await?;
// Forwards localhost:8080 -> remote.host:80
```

### 3. QUIC Transport (`quic.rs`)

Modern UDP-based transport with TLS 1.3.

**Features:**
- TLS 1.3 handshake
- Multiplexed streams
- Built-in congestion control
- Connection migration support
- Stream-based messaging

**Connection States:**
- `Handshake` - Performing TLS handshake
- `Established` - Connection active
- `Closing` - Graceful shutdown
- `Closed` - Connection terminated

**Usage:**
```rust
// Client connection
let config = QuicConfig::default();
let mut quic = QuicConnection::connect(addr, config).await?;

// Open stream and send data
let stream_id = quic.open_stream();
quic.send(stream_id, b"QUIC data").await?;

// Receive data
let (stream_id, data) = quic.recv().await?;

// Server endpoint
let endpoint = QuicEndpoint::bind(listen_addr, config).await?;
let conn = endpoint.accept().await?;
```

**Note:** This is a simplified QUIC implementation demonstrating the API structure. For production use, integrate the [quinn](https://docs.rs/quinn/) crate.

### 4. Traffic Obfuscation (`obfuscate.rs`)

Disguise P2P traffic to bypass deep packet inspection (DPI).

**Obfuscation Types:**
- `Http` - Mimic HTTP POST requests
- `Https` - TLS-like packet wrapper
- `RandomPadding` - Add random padding to packets
- `ProtocolMimic` - Mimic specific protocols (BitTorrent, Skype)
- `TimingObfuscation` - Randomize packet timing

**Features:**
- Bidirectional obfuscation/de-obfuscation
- Configurable padding ratio (0-100%)
- Protocol-specific headers
- Traffic shaping support
- Timing randomization

**Usage:**
```rust
let config = ObfuscationConfig {
    obfuscation_type: ObfuscationType::Http,
    padding_ratio: 0.3,
    randomize_timing: true,
    mimic_protocol: Some("bittorrent".to_string()),
};

let obfs = ObfuscatedConnection::new(config);

// Obfuscate outgoing data
let disguised = obfs.obfuscate(b"Secret P2P data")?;

// De-obfuscate incoming data
let raw = obfs.deobfuscate(&received)?;

// Traffic shaping
let shaper = TrafficShaper::new(1400, 50);  // 1400 byte packets, 50ms delay
let packets = shaper.shape(large_data);
```

## Design Principles

### 1. Transport Independence
- Upper layers don't care about underlying transport
- Easy switching between protocols
- Unified API across all transport types

### 2. Resilience
- Automatic fallback mechanisms
- Multiple connection attempts
- Graceful degradation

### 3. Stealth
- Traffic obfuscation for DPI bypass
- Protocol mimicry
- Timing randomization

### 4. Flexibility
- Composable transport layers
- Tunnel over anything
- Obfuscate over anything

## Integration with Other Modules

**Dataplane:**
- Uses connectors for actual data transfer
- Transport health monitoring
- Multipath across different connectors

**Netlayer:**
- Wraps connector types in P2P protocol
- Peer discovery via connectors
- Message passing over connections

**Observability:**
- Connection metrics tracking
- Transport performance logging
- Error monitoring

## Use Cases

### 1. Basic P2P Communication
```rust
let connector = P2PConnector::new();
let mut conn = connector.connect(peer_addr).await?;
conn.send(&netpacket.to_bytes()).await?;
```

### 2. Circumvention with Obfuscation
```rust
let obfs = ObfuscatedConnection::new(
    ObfuscationConfig {
        obfuscation_type: ObfuscationType::Https,
        ..Default::default()
    }
);
let disguised = obfs.obfuscate(&p2p_packet)?;
```

### 3. Corporate Bypass via Tunnel
```rust
let tunnel = TunnelConnection::connect(TunnelConfig {
    tunnel_type: TunnelType::Obfuscated,
    remote_addr: proxy_server,
    ..Default::default()
}).await?;
```

### 4. High-Performance QUIC
```rust
let quic = QuicConnection::connect(addr, QuicConfig::default()).await?;
let stream = quic.open_stream();
quic.send(stream, video_data).await?;
```

### 5. Multi-Transport Redundancy
```rust
// Try QUIC first
if let Ok(quic) = QuicConnection::connect(addr, config).await {
    return Ok(quic);
}

// Fallback to obfuscated UDP
let obfs = ObfuscatedConnection::new(config);
// Use obfs connection...
```

## Transport Selection Guide

| Situation | Recommended Transport |
|-----------|---------------------|
| High performance, open network | QUIC |
| Restricted network, DPI present | Obfuscated HTTPS |
| Corporate firewall | HTTP Proxy tunnel or SSH tunnel |
| Unreliable connection | TCP with reliability |
| Stealth required | Protocol mimicry + timing obfuscation |
| Mobile networks | Multipath (UDP + TCP) |
| Bypass censorship | Multiple tunnel types with fallback |

## Performance Characteristics

### UDP
- **Latency:** Lowest
- **Reliability:** None
- **Header:** 8 bytes
- **Use:** Discovery, real-time data

### TCP
- **Latency:** Medium (handshake)
- **Reliability:** Guaranteed
- **Header:** 20+ bytes
- **Use:** Reliable data transfer

### QUIC
- **Latency:** Low
- **Reliability:** Guaranteed (with TLS)
- **Header:** Variable
- **Use:** Modern applications

### Tunnel
- **Latency:** Slight overhead
- **Reliability:** Depends on transport
- **Header:** +8-20 bytes
- **Use:** Bypass, encapsulation

### Obfuscated
- **Latency:** Same as base transport
- **Reliability:** Same as base transport
- **Header:** +4-30 bytes
- **Use:** DPI bypass

## Security Considerations

1. **Always use encryption** - Enable TLS for all transports
2. **Obfuscation ≠ Encryption** - Obfuscation hides patterns, encryption hides content
3. **Protocol mimicry risks** - May break if protocols change
4. **Tunnel endpoints** - Must be trusted infrastructure
5. **Traffic analysis** - Advanced DPI can still detect patterns
6. **Timing attacks** - Consider timing obfuscation for stealth

## Migration from NET

### Previous (NET)
- 8 files (~1,500 lines)
- Complex transport abstraction
- Multiple connector types
- Policy engine

### Current (YANDI)
- 4 files (~600 lines with new modules)
- Simple, focused implementations
- Essential protocols only
- User controls policy

**Code reduction:** -60% while adding more functionality!

## Testing

Test obfuscation round-trip:
```rust
let original = b"Test data";
let obfs = ObfuscatedConnection::new(config);
let disguised = obfs.obfuscate(original)?;
let recovered = obfs.deobfuscate(&disguised)?;
assert_eq!(original.to_vec(), recovered);
```

Test tunnel encapsulation:
```bash
cargo test connectors::tunnel::tests
```

## Future Enhancements

1. **Full QUIC Implementation** - Integrate quinn crate for production QUIC
2. **MASQUE Support** - HTTP/3 CONNECT-UDP proxying (RFC 9298)
3. **WireGuard Integration** - Modern VPN tunnel protocol
4. **Adaptive Obfuscation** - Auto-detect DPI and switch strategies
5. **Protocol Hopping** - Dynamically switch protocols mid-session
6. **Full IPv6 Support** - Native IPv6 transport layer
7. **Connection Pooling** - Reuse connections for efficiency

## Troubleshooting

**Connection timeouts:**
- Check firewall rules
- Try fallback transport
- Increase timeout duration

**DPI detection:**
- Switch obfuscation type
- Increase padding ratio
- Enable timing randomization

**Tunnel failures:**
- Verify remote endpoint is reachable
- Check tunnel type compatibility
- Ensure encryption is enabled

**QUIC handshake failing:**
- Verify UDP connectivity
- Check TLS certificates
- Try fallback to TCP

## Statistics Tracking

All transports provide `ConnectionStats`:
```rust
pub struct ConnectionStats {
    pub transport_type: TransportType,
    pub established_at: Instant,
    pub bytes_sent: u64,
    pub bytes_received: u64,
}
```

Monitor connection health:
```rust
let stats = connection.stats();
let age = stats.established_at.elapsed();
let ratio = stats.bytes_sent as f64 / stats.bytes_received.max(1) as f64;
```

✨ **Advanced transport capabilities for extreme network conditions!**
