// src/apps/resource.rs
// Optimized and refactored from NET project

use std::collections::HashMap;
use serde::{Deserialize, Serialize};

use crate::util::HashId;

/// Resource type in P2P network
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ResourceKind {
    User,
    Site,
    Service,
    Custom,
}

/// Gateway metadata for storage in ResourceEntry.metadata
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GatewayMetadata {
    #[serde(rename = "type")]
    pub resource_type: String,

    pub mode: GatewayMode,
    pub bandwidth_mbps: u64,
    pub max_clients: u32,
    pub max_tunnels_per_client: u32,
    pub country: Option<String>,
    pub region: Option<String>,
    pub allow_services: Vec<String>,
    pub deny_domains: Vec<String>,
    pub latency_ms: Option<u32>,
    pub uptime_percent: Option<f32>,
    pub rating: Option<f32>,
    pub node_version: String,
    pub load_factor: f32,
    pub privacy_mode: PrivacyMode,
    pub last_updated: u64,
    pub ttl: u64,
}

/// Gateway operation mode
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub enum GatewayMode {
    #[serde(rename = "public")]
    Public,
    #[serde(rename = "friends")]
    Friends,
    #[serde(rename = "private")]
    Private,
    #[serde(rename = "paid")]
    Paid,
}

/// Gateway privacy level
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum PrivacyMode {
    #[serde(rename = "public")]
    Public,
    #[serde(rename = "limited")]
    Limited,
    #[serde(rename = "stealth")]
    Stealth,
}

/// Resource entry in registry
#[derive(Debug, Clone)]
pub struct ResourceEntry {
    pub id: HashId,
    pub owner: HashId,
    pub kind: ResourceKind,
    pub alias: Option<String>,
    pub metadata: Option<String>,
}

impl ResourceEntry {
    pub fn new_gateway(
        id: HashId,
        owner: HashId,
        alias: Option<String>,
        metadata: GatewayMetadata,
    ) -> Self {
        let metadata_json = metadata.to_json()
            .unwrap_or_else(|_| "{\"error\":\"invalid_metadata\"}".to_string());

        Self {
            id,
            owner,
            kind: ResourceKind::Service,
            alias,
            metadata: Some(metadata_json),
        }
    }
}

impl GatewayMetadata {
    pub fn new_mvp(mode: GatewayMode, bandwidth_mbps: u64, max_clients: u32) -> Self {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        Self {
            resource_type: "gateway".to_string(),
            mode,
            bandwidth_mbps,
            max_clients,
            max_tunnels_per_client: 2,
            country: None,
            region: None,
            allow_services: vec!["any".to_string()],
            deny_domains: vec![],
            latency_ms: None,
            uptime_percent: None,
            rating: None,
            node_version: "1.0.0".to_string(),
            load_factor: 0.0,
            privacy_mode: PrivacyMode::Public,
            last_updated: now,
            ttl: 3600,
        }
    }

    pub fn public_gateway(bandwidth_mbps: u64, max_clients: u32) -> Self {
        Self::new_mvp(GatewayMode::Public, bandwidth_mbps, max_clients)
    }

    pub fn private_gateway(bandwidth_mbps: u64, max_clients: u32) -> Self {
        Self::new_mvp(GatewayMode::Private, bandwidth_mbps, max_clients)
    }

    pub fn friends_gateway(bandwidth_mbps: u64, max_clients: u32) -> Self {
        Self::new_mvp(GatewayMode::Friends, bandwidth_mbps, max_clients)
    }

    pub fn update_metrics(&mut self, latency_ms: u32, load_factor: f32) {
        self.latency_ms = Some(latency_ms);
        self.load_factor = load_factor;
        self.last_updated = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
    }

    pub fn to_json(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(self)
    }

    pub fn from_json(json: &str) -> Result<Self, serde_json::Error> {
        serde_json::from_str(json)
    }

    pub fn is_gateway_resource(metadata: &str) -> bool {
        if let Ok(parsed) = Self::from_json(metadata) {
            parsed.resource_type == "gateway"
        } else {
            false
        }
    }

    pub fn get_score(&self) -> f32 {
        let mut score = 50.0;

        score += (self.bandwidth_mbps as f32 / 100.0).min(20.0);
        score -= self.load_factor * 30.0;

        if let Some(latency) = self.latency_ms {
            if latency < 50 {
                score += 15.0;
            } else if latency < 100 {
                score += 10.0;
            } else if latency < 200 {
                score += 5.0;
            }
        }

        if let Some(uptime) = self.uptime_percent {
            if uptime > 99.0 {
                score += 20.0;
            } else if uptime > 95.0 {
                score += 15.0;
            } else if uptime > 90.0 {
                score += 10.0;
            }
        }

        if let Some(rating) = self.rating {
            score += rating * 10.0;
        }

        match self.mode {
            GatewayMode::Public => score += 5.0,
            GatewayMode::Friends => score += 0.0,
            GatewayMode::Private => score -= 10.0,
            GatewayMode::Paid => score -= 5.0,
        }

        score.max(0.0).min(100.0)
    }
}

impl ResourceEntry {
    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::new();

        out.extend_from_slice(self.id.as_ref());
        out.extend_from_slice(self.owner.as_ref());

        let kind_byte = match self.kind {
            ResourceKind::User => 1,
            ResourceKind::Site => 2,
            ResourceKind::Service => 3,
            ResourceKind::Custom => 4,
        };
        out.push(kind_byte);

        match &self.alias {
            Some(a) => {
                out.push(1);
                let bytes = a.as_bytes();
                let len = bytes.len() as u32;
                out.extend_from_slice(&len.to_be_bytes());
                out.extend_from_slice(bytes);
            }
            None => out.push(0),
        }

        match &self.metadata {
            Some(m) => {
                out.push(1);
                let bytes = m.as_bytes();
                let len = bytes.len() as u32;
                out.extend_from_slice(&len.to_be_bytes());
                out.extend_from_slice(bytes);
            }
            None => out.push(0),
        }

        out
    }

    pub fn decode(buf: &[u8]) -> Option<Self> {
        let mut pos = 0;

        if buf.len() < 65 {
            return None;
        }

        let mut id_bytes = [0u8; 32];
        id_bytes.copy_from_slice(&buf[pos..pos + 32]);
        pos += 32;

        let mut owner_bytes = [0u8; 32];
        owner_bytes.copy_from_slice(&buf[pos..pos + 32]);
        pos += 32;

        let kind = match buf.get(pos)? {
            1 => ResourceKind::User,
            2 => ResourceKind::Site,
            3 => ResourceKind::Service,
            4 => ResourceKind::Custom,
            _ => return None,
        };
        pos += 1;

        let alias = match buf.get(pos)? {
            1 => {
                pos += 1;
                if buf.len() < pos + 4 {
                    return None;
                }
                let len = u32::from_be_bytes(buf[pos..pos + 4].try_into().ok()?) as usize;
                pos += 4;
                if buf.len() < pos + len {
                    return None;
                }
                let s = String::from_utf8(buf[pos..pos + len].to_vec()).ok()?;
                pos += len;
                Some(s)
            }
            0 => {
                pos += 1;
                None
            }
            _ => return None,
        };

        let metadata = match buf.get(pos) {
            Some(1) => {
                pos += 1;
                if buf.len() < pos + 4 {
                    return None;
                }
                let len = u32::from_be_bytes(buf[pos..pos + 4].try_into().ok()?) as usize;
                pos += 4;
                if buf.len() < pos + len {
                    return None;
                }
                let s = String::from_utf8(buf[pos..pos + len].to_vec()).ok()?;
                Some(s)
            }
            Some(0) => {
                None
            }
            _ => None,
        };

        Some(Self {
            id: HashId(id_bytes),
            owner: HashId(owner_bytes),
            kind,
            alias,
            metadata,
        })
    }

    pub fn is_gateway(&self) -> bool {
        if self.kind != ResourceKind::Service {
            return false;
        }

        match &self.metadata {
            Some(metadata) => GatewayMetadata::is_gateway_resource(metadata),
            None => false,
        }
    }

    pub fn as_gateway(&self) -> Option<GatewayMetadata> {
        if !self.is_gateway() {
            return None;
        }

        self.metadata.as_ref()
            .and_then(|m| GatewayMetadata::from_json(m).ok())
    }
}

#[derive(Debug, Default, Clone)]
pub struct ResourceRegistry {
    by_id: HashMap<HashId, ResourceEntry>,
    by_alias: HashMap<String, HashId>,
}

impl ResourceRegistry {
    pub fn new() -> Self {
        Self {
            by_id: HashMap::new(),
            by_alias: HashMap::new(),
        }
    }

    pub fn register(&mut self, entry: ResourceEntry) {
        if let Some(alias) = &entry.alias {
            self.by_alias.insert(alias.to_lowercase(), entry.id);
        }
        self.by_id.insert(entry.id, entry);
    }

    pub fn register_site(
        &mut self,
        id: HashId,
        owner: HashId,
        alias: &str,
        metadata: Option<String>,
    ) {
        let entry = ResourceEntry {
            id,
            owner,
            kind: ResourceKind::Site,
            alias: Some(alias.to_string()),
            metadata,
        };
        self.register(entry);
    }

    pub fn register_user(
        &mut self,
        id: HashId,
        alias: &str,
        metadata: Option<String>,
    ) {
        let entry = ResourceEntry {
            id,
            owner: id, // User is owner of themselves
            kind: ResourceKind::User,
            alias: Some(alias.to_string()),
            metadata,
        };
        self.register(entry);
    }

    pub fn get_by_id_bytes(&self, id: &[u8; 32]) -> Option<&ResourceEntry> {
        self.by_id.get(&HashId(*id))
    }

    pub fn get_by_id(&self, id: &HashId) -> Option<&ResourceEntry> {
        self.by_id.get(id)
    }

    pub fn get_by_alias(&self, alias: &str) -> Option<&ResourceEntry> {
        let key = alias.to_lowercase();
        let id = self.by_alias.get(&key)?;
        self.by_id.get(id)
    }

    pub fn list_by_kind(&self, kind: ResourceKind) -> Vec<&ResourceEntry> {
        self.by_id
            .values()
            .filter(|e| e.kind == kind)
            .collect()
    }

    pub fn all_entries(&self) -> Vec<&ResourceEntry> {
        self.by_id.values().collect()
    }

    pub fn register_gateway(
        &mut self,
        id: HashId,
        owner: HashId,
        alias: Option<String>,
        metadata: GatewayMetadata,
    ) {
        let entry = ResourceEntry::new_gateway(id, owner, alias, metadata);
        self.register(entry);
    }

    pub fn list_gateways(&self) -> Vec<&ResourceEntry> {
        self.by_id
            .values()
            .filter(|e| e.is_gateway())
            .collect()
    }

    pub fn list_gateways_with_scores(&self) -> Vec<(HashId, f32, GatewayMetadata)> {
        self.list_gateways()
            .into_iter()
            .filter_map(|entry| {
                entry.as_gateway().map(|metadata| {
                    let score = metadata.get_score();
                    (entry.id, score, metadata)
                })
            })
            .collect()
    }

    pub fn find_best_gateways(
        &self,
        mode_filter: Option<GatewayMode>,
        min_bandwidth: Option<u64>,
        max_load_factor: Option<f32>,
        limit: usize,
    ) -> Vec<(HashId, f32, GatewayMetadata)> {
        let mut gateways = self.list_gateways_with_scores();

        if let Some(mode) = mode_filter {
            gateways.retain(|(_, _, metadata)| {
                matches!(
                    (&mode, &metadata.mode),
                    (GatewayMode::Public, GatewayMode::Public)
                        | (GatewayMode::Friends, GatewayMode::Friends)
                        | (GatewayMode::Private, GatewayMode::Private)
                        | (GatewayMode::Paid, GatewayMode::Paid)
                )
            });
        }

        if let Some(min_bw) = min_bandwidth {
            gateways.retain(|(_, _, metadata)| metadata.bandwidth_mbps >= min_bw);
        }

        if let Some(max_load) = max_load_factor {
            gateways.retain(|(_, _, metadata)| metadata.load_factor <= max_load);
        }

        gateways.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        gateways.truncate(limit);
        gateways
    }

    pub fn get_gateway(&self, id: &HashId) -> Option<(f32, GatewayMetadata)> {
        self.get_by_id(id)
            .filter(|entry| entry.is_gateway())
            .and_then(|entry| {
                entry.as_gateway().map(|metadata| {
                    let score = metadata.get_score();
                    (score, metadata)
                })
            })
    }

    pub fn get_gateway_by_alias(&self, alias: &str) -> Option<(HashId, f32, GatewayMetadata)> {
        self.get_by_alias(alias)
            .filter(|entry| entry.is_gateway())
            .and_then(|entry| {
                entry.as_gateway().map(|metadata| {
                    let score = metadata.get_score();
                    (entry.id, score, metadata)
                })
            })
    }

    pub fn update_gateway_metrics(
        &mut self,
        id: &HashId,
        latency_ms: u32,
        load_factor: f32,
    ) -> bool {
        if let Some(entry) = self.by_id.get_mut(id) {
            if entry.is_gateway() {
                if let Some(metadata_str) = &mut entry.metadata {
                    if let Ok(mut metadata) = GatewayMetadata::from_json(metadata_str) {
                        metadata.update_metrics(latency_ms, load_factor);
                        *metadata_str = metadata.to_json().unwrap_or_default();
                        return true;
                    }
                }
            }
        }
        false
    }

    pub fn gateway_stats(&self) -> GatewayStats {
        let gateways = self.list_gateways_with_scores();
        let total = gateways.len();

        let public_count = gateways.iter().filter(|(_, _, m)| matches!(m.mode, GatewayMode::Public)).count();
        let friends_count = gateways.iter().filter(|(_, _, m)| matches!(m.mode, GatewayMode::Friends)).count();
        let private_count = gateways.iter().filter(|(_, _, m)| matches!(m.mode, GatewayMode::Private)).count();
        let paid_count = gateways.iter().filter(|(_, _, m)| matches!(m.mode, GatewayMode::Paid)).count();

        let total_bandwidth: u64 = gateways.iter().map(|(_, _, m)| m.bandwidth_mbps).sum();
        let avg_load: f32 = if total > 0 {
            gateways.iter().map(|(_, _, m)| m.load_factor).sum::<f32>() / total as f32
        } else {
            0.0
        };

        GatewayStats {
            total_gateways: total,
            public_gateways: public_count,
            friends_gateways: friends_count,
            private_gateways: private_count,
            paid_gateways: paid_count,
            total_bandwidth_mbps: total_bandwidth,
            average_load_factor: avg_load,
        }
    }
}

#[derive(Debug, Clone)]
pub struct GatewayStats {
    pub total_gateways: usize,
    pub public_gateways: usize,
    pub friends_gateways: usize,
    pub private_gateways: usize,
    pub paid_gateways: usize,
    pub total_bandwidth_mbps: u64,
    pub average_load_factor: f32,
}
