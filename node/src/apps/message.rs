// src/apps/message.rs
// Message types for P2P communication
// Optimized from NET project

use crate::util::HashId;

/// Message content type
#[derive(Debug, Clone)]
pub enum MessageBody {
    Text(String),
    Binary(Vec<u8>),
}

/// Message encryption state
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MessageSecurity {
    Plain,
    Encrypted,
}

/// Basic message structure between nodes
#[derive(Debug, Clone)]
pub struct NetMessage {
    pub id: HashId,
    pub from: HashId,
    pub to: HashId,
    pub security: MessageSecurity,
    pub body: MessageBody,
}

impl NetMessage {
    pub fn plain_text(id: HashId, from: HashId, to: HashId, text: impl Into<String>) -> Self {
        Self {
            id,
            from,
            to,
            security: MessageSecurity::Plain,
            body: MessageBody::Text(text.into()),
        }
    }

    pub fn encrypted(id: HashId, from: HashId, to: HashId, ciphertext: Vec<u8>) -> Self {
        Self {
            id,
            from,
            to,
            security: MessageSecurity::Encrypted,
            body: MessageBody::Binary(ciphertext),
        }
    }

    pub fn is_plain_text(&self) -> bool {
        matches!(self.security, MessageSecurity::Plain)
            && matches!(self.body, MessageBody::Text(_))
    }

    pub fn as_text(&self) -> Option<&str> {
        if self.is_plain_text() {
            if let MessageBody::Text(ref s) = self.body {
                return Some(s.as_str());
            }
        }
        None
    }
}
