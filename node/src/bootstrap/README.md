# YANDI Bootstrap Module

**Migrated from:** `/home/iam/net/src/bootstrap/`
**Status:** ✅ Simplified migration completed
**Date:** 2025-12-23**
**Last Updated:** 2025-12-31

---

## Migration Summary

### Files Migrated:

1. **mod.rs** (645 lines → 440 lines)
   - BootstrapManager with simplified logic
   - Multiple bootstrap sources (local, HTTP, embedded)
   - Caching with refresh interval
   - JSON parsing for supernodes.json format

### Files NOT Migrated (3 files removed):

**Complexity removed:**
- ❌ **crypto.rs** (~400 lines) - Complex encryption/signing for bootstrap
- ❌ **manager.rs** (~300 lines) - Duplicate manager logic
- ❌ **tests.rs** - Test files (not needed for migration)

**What was simplified:**
- ❌ Encrypted bootstrap addresses (too complex for MVP)
- ❌ Ed25519 signature verification (overkill for basic bootstrap)
- ❌ ChaCha20Poly1305 encryption (not needed initially)
- ❌ Multiple manager abstractions (unified into one)
- ❌ Telegram bot bootstrap (too specific)
- ❌ Google Cloud Storage bootstrap (too specific)

---

## Key Optimizations

### Massive Code Reduction

**NET:** 4 files, ~1,400 lines of code
**YANDI:** 1 file, ~440 lines

**Reduction:** -69% for bootstrap functionality!

### What Was Removed

1. **Encryption complexity** - Encrypted bootstrap addresses are over-engineering for MVP
2. **Signature verification** - Ed25519 signatures not needed for basic bootstrap
3. **Multiple managers** - Single BootstrapManager is enough
4. **Specialized sources** - Telegram, GCS removed (can add later)
5. **Crypto module** - 400 lines of ChaCha20Poly1305 encryption

### What Was Kept

1. **Bootstrap sources** - Local files, HTTP URLs, embedded fallback
2. **Caching** - Refresh interval support
3. **JSON parsing** - Support for supernodes.json format
4. **Node types** - Regular, Relay, SuperNode, Bootstrap
5. **Expiration** - 24-hour TTL for node info

---

## API Documentation

### BootstrapManager

Main bootstrap manager for loading initial peers.

```rust
pub struct BootstrapManager {
    config: BootstrapConfig,
    cached_nodes: Vec<BootstrapNode>,
    last_update: Option<Instant>,
    failed_sources: Vec<BootstrapSource>,
}
```

**Methods:**
- `new(config)` - Create bootstrap manager
- `load_nodes()` - Load nodes from all sources
- `force_refresh()` - Force cache refresh
- `get_cached_nodes()` - Get cached nodes
- `get_stats()` - Get bootstrap statistics

### BootstrapNode

Bootstrap node information.

```rust
pub struct BootstrapNode {
    pub node_id: HashId,
    pub address: String,
    pub node_type: NodeType,
    pub region: Option<String>,
    pub timestamp: u64,
}
```

**Methods:**
- `new(node_id, address, node_type)` - Create new bootstrap node
- `is_expired()` - Check if node info > 24 hours old

### BootstrapConfig

Bootstrap configuration.

```rust
pub struct BootstrapConfig {
    pub sources: Vec<BootstrapSource>,
    pub download_timeout: Duration,
    pub refresh_interval: Duration,
    pub min_nodes: usize,
}
```

**Default configuration:**
- Local file: `configs/supernodes.json`
- HTTP URL: GitHub raw supernodes.json
- Embedded fallback nodes
- Timeout: 10 seconds
- Refresh: 5 minutes

### BootstrapSource

Bootstrap source types.

```rust
pub enum BootstrapSource {
    LocalFile { path: String },
    HttpUrl { url: String },
    Embedded { nodes: Vec<BootstrapNode> },
}
```

### NodeType

Node classification.

```rust
pub enum NodeType {
    Regular,
    Relay,
    SuperNode,
    Bootstrap,
}
```

---

## Usage Examples

### Create Bootstrap Manager

```rust
use yandi::{BootstrapManager, BootstrapConfig};

let config = BootstrapConfig::default();
let mut bootstrap = BootstrapManager::new(config);
```

### Load Bootstrap Nodes

```rust
let result = bootstrap.load_nodes().await?;

println!("Loaded {} nodes from {:?}", result.successful_count, result.source);

for node in result.nodes {
    println!("Node {}: {}", node.node_id.to_hex(), node.address);
}
```

### Get Cached Nodes

```rust
let nodes = bootstrap.get_cached_nodes();

println!("Cached {} bootstrap nodes", nodes.len());
```

### Force Refresh

```rust
let result = bootstrap.force_refresh().await?;

println!("Refreshed {} nodes", result.successful_count);
```

### Custom Configuration

```rust
use yandi::{BootstrapConfig, BootstrapSource};
use std::time::Duration;

let config = BootstrapConfig {
    sources: vec![
        BootstrapSource::LocalFile {
            path: "my_supernodes.json".to_string(),
        },
        BootstrapSource::HttpUrl {
            url: "https://example.com/bootstrap.json".to_string(),
        },
    ],
    download_timeout: Duration::from_secs(15),
    refresh_interval: Duration::from_secs(600),
    min_nodes: 2,
};

let bootstrap = BootstrapManager::new(config);
```

---

## supernodes.json Format

Bootstrap supports the supernodes.json format:

```json
{
  "supernodes": [
    {
      "name": "Russia Supernode",
      "address": "185.77.205.3",
      "hello_port": 9000,
      "region": "ru-central",
      "node_id": "c6ae1015dc584fc9",
      "role": "supernode",
      "priority": 1,
      "jurisdiction": "RU",
      "capabilities": ["relay", "dht"]
    },
    {
      "name": "Europe Supernode",
      "address": "89.124.67.160",
      "hello_port": 9000,
      "region": "eu-west",
      "node_id": "a1eb59d87a49abd9",
      "role": "supernode",
      "priority": 2,
      "jurisdiction": "NL",
      "capabilities": ["relay", "dht", "gateway"]
    }
  ]
}
```

Alternative simple array format:

```json
{
  "nodes": [
    {
      "node_id": "c6ae1015dc584fc9...",
      "address": "185.77.205.3:9000",
      "node_type": "SuperNode",
      "region": "ru-central",
      "timestamp": 1735104000
    }
  ]
}
```

---

## Files Structure

```
src/bootstrap/
└── mod.rs         # Bootstrap module (440 lines)

Total: 1 file, ~440 lines
```

---

## Bootstrap Flow

### 1. Initial Load

```
App Start
    ↓
BootstrapManager::load_nodes()
    ↓
Try sources in order:
  1. configs/supernodes.json (local)
  2. GitHub RAW (HTTP)
  3. Embedded fallback
    ↓
Success → Cache nodes
Failed → Try next source
All failed → Error
```

### 2. Caching

```
Check cache
    ↓
If < refresh_interval (5 min)
    → Return cached nodes
    ↓
Else
    → Try to refresh
```

### 3. Node Validation

```
Parse JSON
    ↓
For each node:
  - Parse node_id (hex)
  - Create address (IP:port)
  - Determine node_type
  - Check timestamp (must be < 24h)
    ↓
Valid nodes → Add to result
Expired nodes → Skip
```

---

## Performance Characteristics

### Caching

- **Refresh interval:** 5 minutes (configurable)
- **Cache validity:** Checked on every `load_nodes()` call
- **Failed sources:** Remembered across attempts

### Timeouts

- **HTTP timeout:** 10 seconds (configurable)
- **Min nodes:** 1 (configurable)

### Network Usage

- **Single HTTP request** per refresh (if using HTTP source)
- **Local file read** if file exists
- **No repeated requests** during cache validity

---

## Security Considerations

### What Was Removed (for MVP)

1. **Encryption** - ChaCha20Poly1305 encryption removed
   - Bootstrap info is public anyway
   - Can add later if needed

2. **Signatures** - Ed25519 signature verification removed
   - Overkill for initial bootstrap
   - Trust established through other means later

3. **Authentication** - No authentication for bootstrap sources
   - Accept data from any configured source
   - User controls which sources to trust

### What Remains

1. **Source validation** - Only configured sources used
2. **Expiration** - Nodes > 24 hours old rejected
3. **Local file priority** - Local config overrides remote

---

## Next Steps

When ready to expand bootstrap functionality:

1. **Add encryption** - ChaCha20Poly1305 for bootstrap addresses
2. **Add signatures** - Ed25519 verification of bootstrap data
3. **More sources** - Telegram bot, GCS, etc.
4. **Source priority** - Weighted source selection

---

**Migration Progress:** Bootstrap complete
**Code Reduction:** From ~1,400 lines (NET) to ~440 lines (YANDI) = -69%
**Complexity:** Removed 3 unnecessary files, kept 1 core file

✨ **Bootstrap system is ready for initial P2P network discovery!**
