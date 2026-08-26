// src/bootstrap/mod.rs
//! Bootstrap Module - Initial Peer Discovery
//! ==========================================
//!
//! Simplified bootstrap system for initial P2P network entry

use std::time::{Duration, SystemTime, UNIX_EPOCH};
use serde::{Deserialize, Serialize};
use crate::util::HashId;

/// Bootstrap node information
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BootstrapNode {
    /// Node ID
    pub node_id: HashId,
    /// Network address (IP:port)
    pub address: String,
    /// Node type
    pub node_type: NodeType,
    /// Region (optional)
    pub region: Option<String>,
    /// Last seen timestamp
    pub timestamp: u64,
}

impl BootstrapNode {
    /// Create new bootstrap node
    pub fn new(node_id: HashId, address: String, node_type: NodeType) -> Self {
        Self {
            node_id,
            address,
            node_type,
            region: None,
            timestamp: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs(),
        }
    }

    /// Check if node info is expired (> 24 hours)
    pub fn is_expired(&self) -> bool {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();

        now > self.timestamp + 86400 // 24 hours
    }
}

/// Node type
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum NodeType {
    Regular,
    Relay,
    SuperNode,
    Bootstrap,
}

/// Bootstrap source
#[derive(Debug, Clone, PartialEq)]
pub enum BootstrapSource {
    /// Local JSON file
    LocalFile { path: String },
    /// HTTP/HTTPS URL
    HttpUrl { url: String },
    /// Embedded fallback nodes
    Embedded { nodes: Vec<BootstrapNode> },
}

/// Bootstrap configuration
#[derive(Debug, Clone)]
pub struct BootstrapConfig {
    /// Bootstrap sources
    pub sources: Vec<BootstrapSource>,
    /// Download timeout
    pub download_timeout: Duration,
    /// Refresh interval
    pub refresh_interval: Duration,
    /// Minimum nodes required
    pub min_nodes: usize,
}

impl Default for BootstrapConfig {
    fn default() -> Self {
        Self {
            sources: vec![
                // Priority 1: Local supernodes.json
                BootstrapSource::LocalFile {
                    path: "configs/supernodes.json".to_string(),
                },
                // Priority 2: GitHub raw
                BootstrapSource::HttpUrl {
                    url: "https://raw.githubusercontent.com/iam-freenetwork/coal/main/configs/supernodes.json".to_string(),
                },
                // Priority 3: Embedded fallback
                BootstrapSource::Embedded {
                    nodes: BootstrapManager::create_embedded_fallback(),
                },
            ],
            download_timeout: Duration::from_secs(10),
            refresh_interval: Duration::from_secs(300), // 5 minutes
            min_nodes: 1,
        }
    }
}

/// Bootstrap result
#[derive(Debug, Clone)]
pub struct BootstrapResult {
    /// Loaded nodes
    pub nodes: Vec<BootstrapNode>,
    /// Source used
    pub source: BootstrapSource,
    /// Load time
    pub load_time: Duration,
    /// Successful count
    pub successful_count: usize,
}

/// Bootstrap manager
pub struct BootstrapManager {
    config: BootstrapConfig,
    cached_nodes: Vec<BootstrapNode>,
    last_update: Option<std::time::Instant>,
    failed_sources: Vec<BootstrapSource>,
    /// Local node cache with reputation (L0)
    local_cache: Vec<BootstrapNode>,
}

impl BootstrapManager {
    /// Create new bootstrap manager
    pub fn new(config: BootstrapConfig) -> Self {
        Self {
            config,
            cached_nodes: Vec::new(),
            last_update: None,
            local_cache: Vec::new(),
            failed_sources: Vec::new(),
        }
    }

    /// Load bootstrap nodes
    pub async fn load_nodes(&mut self) -> Result<BootstrapResult, String> {
        println!("[bootstrap] Starting network bootstrap...");

        // Check cache
        if self.is_cache_valid() {
            return Ok(BootstrapResult {
                nodes: self.cached_nodes.clone(),
                source: BootstrapSource::Embedded { nodes: vec![] },
                load_time: Duration::from_millis(0),
                successful_count: self.cached_nodes.len(),
            });
        }

        let start_time = std::time::Instant::now();

        // Try all sources
        for source in self.config.sources.clone() {
            if self.failed_sources.contains(&source) {
                continue;
            }

            match self.try_source(&source).await {
                Ok(nodes) => {
                    let load_time = start_time.elapsed();
                    let node_count = nodes.len();

                    self.update_cache(nodes.clone());
                    self.last_update = Some(std::time::Instant::now());

                    println!("[bootstrap] Loaded {} nodes from {:?} in {:?}", node_count, source, load_time);

                    return Ok(BootstrapResult {
                        nodes,
                        source: source.clone(),
                        load_time,
                        successful_count: node_count,
                    });
                }
                Err(e) => {
                    println!("[bootstrap] Source {:?} failed: {}", source, e);
                    self.failed_sources.push(source.clone());
                }
            }
        }

        Err("All bootstrap sources failed".to_string())
    }

    /// Try specific source
    async fn try_source(&mut self, source: &BootstrapSource) -> Result<Vec<BootstrapNode>, String> {
        match source {
            BootstrapSource::LocalFile { path } => {
                self.load_from_local_file(path).await
            }
            BootstrapSource::HttpUrl { url } => {
                self.load_from_http(url).await
            }
            BootstrapSource::Embedded { nodes } => {
                Ok(nodes.clone())
            }
        }
    }

    /// Load from local file
    async fn load_from_local_file(&self, path: &str) -> Result<Vec<BootstrapNode>, String> {
        let content = tokio::fs::read_to_string(path).await
            .map_err(|e| format!("Failed to read file {}: {}", path, e))?;

        self.parse_bootstrap_json(&content)
    }

    /// Load from HTTP URL
    async fn load_from_http(&self, url: &str) -> Result<Vec<BootstrapNode>, String> {
        let client = reqwest::Client::builder()
            .timeout(self.config.download_timeout)
            .build()
            .map_err(|e| format!("Failed to create HTTP client: {}", e))?;

        let response = client.get(url).send().await
            .map_err(|e| format!("HTTP request failed: {}", e))?;

        if !response.status().is_success() {
            return Err(format!("HTTP error: {}", response.status()));
        }

        let text = response.text().await
            .map_err(|e| format!("Failed to read response: {}", e))?;

        self.parse_bootstrap_json(&text)
    }

    /// Parse bootstrap JSON (supports supernodes.json format)
    fn parse_bootstrap_json(&self, json_text: &str) -> Result<Vec<BootstrapNode>, String> {
        let json: serde_json::Value = serde_json::from_str(json_text)
            .map_err(|e| format!("Failed to parse JSON: {}", e))?;

        let nodes_array = if let Some(supernodes) = json.get("supernodes").and_then(|s| s.as_array()) {
            println!("[bootstrap] Parsing supernodes.json format");
            supernodes
        } else if let Some(nodes) = json.get("nodes").and_then(|n| n.as_array()) {
            println!("[bootstrap] Parsing nodes format");
            nodes
        } else if let Some(nodes) = json.as_array() {
            println!("[bootstrap] Parsing array format");
            nodes
        } else {
            return Err("JSON must contain 'supernodes', 'nodes' array or be an array".to_string());
        };

        let mut nodes = Vec::new();

        for node_value in nodes_array {
            if let Ok(supernode) = serde_json::from_value::<SupernodeConfig>(node_value.clone()) {
                let bootstrap_node = self.convert_supernode(&supernode)?;
                if !bootstrap_node.is_expired() {
                    nodes.push(bootstrap_node);
                }
            } else if let Ok(node) = serde_json::from_value::<BootstrapNode>(node_value.clone()) {
                if !node.is_expired() {
                    nodes.push(node);
                }
            }
        }

        println!("[bootstrap] Parsed {} valid nodes", nodes.len());
        Ok(nodes)
    }

    /// Convert SupernodeConfig to BootstrapNode
    fn convert_supernode(&self, supernode: &SupernodeConfig) -> Result<BootstrapNode, String> {
        let node_id = Self::parse_hex_id(&supernode.node_id)?;

        let node_type = match supernode.role.as_str() {
            "supernode" => NodeType::SuperNode,
            "relay" => NodeType::Relay,
            "bootstrap" => NodeType::Bootstrap,
            _ => NodeType::Regular,
        };

        Ok(BootstrapNode::new(
            node_id,
            format!("{}:{}", supernode.address, supernode.hello_port),
            node_type,
        ))
    }

    /// Parse hex string to HashId
    fn parse_hex_id(hex_str: &str) -> Result<HashId, String> {
        let hex_str = hex_str.trim();
        let mut hash = [0u8; 32];

        // Support both short (16 bytes) and full (32 bytes) hex
        let bytes_to_parse = hex_str.len().min(64);

        for i in (0..bytes_to_parse).step_by(2) {
            let byte_str = &hex_str[i..i+2];
            let byte = u8::from_str_radix(byte_str, 16)
                .map_err(|_| format!("Invalid hex: {}", byte_str))?;
            hash[i/2] = byte;
        }

        Ok(HashId(hash))
    }

    /// Check cache validity
    fn is_cache_valid(&self) -> bool {
        if let Some(last_update) = self.last_update {
            return last_update.elapsed() < self.config.refresh_interval;
        }
        false
    }

    /// Update cache
    fn update_cache(&mut self, nodes: Vec<BootstrapNode>) {
        self.cached_nodes = nodes;
    }

    /// Get cached nodes
    pub fn get_cached_nodes(&self) -> Vec<BootstrapNode> {
        self.cached_nodes.clone()
    }

    /// Force refresh
    pub async fn force_refresh(&mut self) -> Result<BootstrapResult, String> {
        self.last_update = None;
        self.cached_nodes.clear();
        self.failed_sources.clear();
        self.load_nodes().await
    }

    /// Get statistics
    pub fn get_stats(&self) -> BootstrapStats {
        BootstrapStats {
            cached_nodes: self.cached_nodes.len(),
            failed_sources: self.failed_sources.len(),
            sources_total: self.config.sources.len(),
        }
    }

    /// Create embedded fallback nodes
    fn create_embedded_fallback() -> Vec<BootstrapNode> {
        vec![
            // Example supernode (replace with real addresses)
            BootstrapNode {
                node_id: HashId([0x12; 32]),
                address: "example.com:9000".to_string(),
                node_type: NodeType::SuperNode,
                region: Some("global".to_string()),
                timestamp: SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_secs(),
            },
        ]
    }

    /// Create hardcoded seed nodes (L1) - IPv4 + IPv6
    fn create_hardcoded_seeds() -> Vec<BootstrapNode> {
        vec![
            // RU region seeds
            BootstrapNode {
                node_id: HashId([0x01; 32]),
                address: "185.77.205.3:9000".to_string(),
                node_type: NodeType::Bootstrap,
                region: Some("RU".to_string()),
                timestamp: SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs(),
            },
            BootstrapNode {
                node_id: HashId([0x02; 32]),
                address: "[2001:db8:1::1]:9000".to_string(),
                node_type: NodeType::Bootstrap,
                region: Some("RU".to_string()),
                timestamp: SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs(),
            },
            // NL region seeds
            BootstrapNode {
                node_id: HashId([0x03; 32]),
                address: "91.201.114.31:9000".to_string(),
                node_type: NodeType::Bootstrap,
                region: Some("NL".to_string()),
                timestamp: SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs(),
            },
            BootstrapNode {
                node_id: HashId([0x04; 32]),
                address: "[2001:db8:2::1]:9000".to_string(),
                node_type: NodeType::Bootstrap,
                region: Some("NL".to_string()),
                timestamp: SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs(),
            },
        ]
    }
}

/// Bootstrap statistics
#[derive(Debug, Clone)]
pub struct BootstrapStats {
    pub cached_nodes: usize,
    pub failed_sources: usize,
    pub sources_total: usize,
}

/// Supernode config (from supernodes.json)
#[derive(Debug, Clone, Deserialize)]
struct SupernodeConfig {
    pub name: String,
    pub address: String,
    pub hello_port: u16,
    pub region: String,
    pub node_id: String,
    pub role: String,
}
