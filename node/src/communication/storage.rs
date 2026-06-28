// src/communication/storage.rs
//! Local storage for chat history (encrypted JSON Lines format)

use crate::communication::ChatMessage;
use crate::util::HashId;
use std::path::PathBuf;
use anyhow::Result;
use aes_gcm::{
    aead::{Aead, KeyInit},
    Aes256Gcm, Nonce, Key
};
use rand::RngCore;
use hkdf::Hkdf;
use sha2::Sha256;

/// Получить текущий timestamp в ms
fn now_ms() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64
}

/// Хранилище чатов (локальное для каждой ноды) с AES-256-GCM шифрованием
pub struct ChatStorage {
    my_node_id: HashId,
    chats_dir: PathBuf,
    /// Master key for HKDF-derived chat encryption (None = legacy node_id-based key)
    master_key: Option<[u8; 32]>,
}

impl ChatStorage {
    /// Создать новое хранилище (legacy — key derived from node_id)
    pub fn new(my_node_id: HashId) -> Result<Self> {
        let base_dir = dirs::home_dir()
            .expect("No home directory")
            .join(".yandi/chats");

        std::fs::create_dir_all(&base_dir)?;

        Ok(Self {
            my_node_id,
            chats_dir: base_dir,
            master_key: None,
        })
    }

    /// Создать хранилище с мастер-ключом (HKDF-derived encryption key)
    pub fn new_with_key(my_node_id: HashId, master_key: [u8; 32]) -> Result<Self> {
        let base_dir = dirs::home_dir()
            .expect("No home directory")
            .join(".yandi/chats");

        std::fs::create_dir_all(&base_dir)?;

        Ok(Self {
            my_node_id,
            chats_dir: base_dir,
            master_key: Some(master_key),
        })
    }

    /// Get AES-256-GCM encryption key.
    /// With master_key: HKDF-SHA256(master_key, salt=node_id, info="yandi-chat-v2")
    /// Without master_key: raw node_id bytes (legacy, weak — node_id is public)
    fn get_encryption_key(&self) -> [u8; 32] {
        if let Some(mk) = &self.master_key {
            let hk = Hkdf::<Sha256>::new(Some(&self.my_node_id.0), mk);
            let mut key = [0u8; 32];
            hk.expand(b"yandi-chat-v2", &mut key).expect("HKDF expand failed");
            key
        } else {
            let mut key = [0u8; 32];
            key.copy_from_slice(&self.my_node_id.0[..32]);
            key
        }
    }

    /// Получить путь к зашифрованному файлу чата
    fn chat_file_path_enc(&self, peer_id: &HashId) -> PathBuf {
        let short_id = hex::encode(&peer_id.0[..8]);
        self.chats_dir.join(format!("chat_{}.enc", short_id))
    }

    /// Сохранить исходящее сообщение (шифрованное)
    pub fn save_outgoing(&self, to: &HashId, msg: &ChatMessage) -> Result<()> {
        let chat_file = self.chat_file_path_enc(to);
        self.append_encrypted_message(&chat_file, msg)
    }

    /// Сохранить входящее сообщение (шифрованное)
    pub fn save_incoming(&self, from: &HashId, msg: &ChatMessage) -> Result<()> {
        let chat_file = self.chat_file_path_enc(from);
        self.append_encrypted_message(&chat_file, msg)
    }

    /// Добавить зашифрованное сообщение в файл
    fn append_encrypted_message(&self, chat_file: &PathBuf, msg: &ChatMessage) -> Result<()> {
        use std::fs::OpenOptions;
        use std::io::Write;

        let plaintext = serde_json::to_string(msg)?;
        let key_bytes = self.get_encryption_key();
        let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(&key_bytes));

        let mut nonce_bytes = [0u8; 12];
        rand::thread_rng().fill_bytes(&mut nonce_bytes);
        let nonce = Nonce::from_slice(&nonce_bytes);

        let ciphertext = cipher.encrypt(nonce, plaintext.as_bytes())
            .map_err(|e| anyhow::anyhow!("Encryption failed: {}", e))?;

        let mut encrypted_data = nonce_bytes.to_vec();
        encrypted_data.extend_from_slice(&ciphertext);

        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(chat_file)?;

        writeln!(file, "{}", hex::encode(encrypted_data))?;
        Ok(())
    }

    /// Загрузить историю чата (расшифрованную)
    pub fn load_history(&self, peer_id: &HashId, limit: usize) -> Result<Vec<ChatMessage>> {
        let chat_file = self.chat_file_path_enc(peer_id);

        if !chat_file.exists() {
            return Ok(Vec::new());
        }

        let content = std::fs::read_to_string(chat_file)?;
        let key_bytes = self.get_encryption_key();
        let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(&key_bytes));

        let mut messages = Vec::new();
        for line in content.lines().rev().take(limit) {
            let encrypted_hex = line.trim();
            let encrypted_bytes = match hex::decode(encrypted_hex) {
                Ok(b) => b,
                Err(_) => continue,
            };

            if encrypted_bytes.len() < 12 {
                continue;
            }

            let nonce = Nonce::from_slice(&encrypted_bytes[0..12]);
            let ciphertext = &encrypted_bytes[12..];

            let plaintext = match cipher.decrypt(nonce, ciphertext) {
                Ok(p) => p,
                Err(_) => continue,
            };

            if let Ok(msg) = serde_json::from_slice(&plaintext) {
                messages.push(msg);
            }
        }

        Ok(messages)
    }

    /// Обновить статус сообщения (перезаписывает весь файл с шифрованием)
    pub fn update_message_status(
        &self,
        peer_id: &HashId,
        msg_id: &HashId,
        status: crate::communication::MessageStatus,
    ) -> Result<()> {
        let chat_file = self.chat_file_path_enc(peer_id);
        
        if !chat_file.exists() {
            return Ok(());
        }

        let mut messages = self.load_history(peer_id, usize::MAX)?;
        let mut updated = false;
        
        for msg in messages.iter_mut() {
            if msg.msg_id == *msg_id {
                msg.status = status;
                updated = true;
                break;
            }
        }

        if updated {
            self.rewrite_all_messages(peer_id, &messages)?;
        }

        Ok(())
    }

    /// Перезаписать все сообщения (при редактировании/удалении)
    fn rewrite_all_messages(&self, peer_id: &HashId, messages: &[ChatMessage]) -> Result<()> {
        let chat_file = self.chat_file_path_enc(peer_id);
        
        // Удаляем старый файл
        if chat_file.exists() {
            std::fs::remove_file(&chat_file)?;
        }

        // Записываем все сообщения заново
        for msg in messages {
            self.append_encrypted_message(&chat_file, msg)?;
        }

        Ok(())
    }

    /// Очистить историю чата с конкретным peer
    pub fn clear_history(&self, peer_id: &HashId) -> Result<()> {
        let chat_file = self.chat_file_path_enc(peer_id);
        if chat_file.exists() {
            std::fs::remove_file(chat_file)?;
        }
        Ok(())
    }

    /// Очистить всю историю
    pub fn clear_all(&self) -> Result<()> {
        if self.chats_dir.exists() {
            std::fs::remove_dir_all(&self.chats_dir)?;
            std::fs::create_dir_all(&self.chats_dir)?;
        }
        Ok(())
    }

    /// Обновить текст сообщения (редактирование)
    pub fn update_message_text(
        &self,
        peer_id: &HashId,
        msg_id: &HashId,
        new_text: String,
    ) -> Result<()> {
        let mut messages = self.load_history(peer_id, usize::MAX)?;
        let mut updated = false;
        
        for msg in messages.iter_mut() {
            if msg.msg_id == *msg_id {
                msg.text = new_text.clone();
                msg.edited = true;
                msg.edit_timestamp = Some(now_ms());
                updated = true;
                break;
            }
        }

        if updated {
            self.rewrite_all_messages(peer_id, &messages)?;
        }

        Ok(())
    }

    /// Удалить сообщение
    pub fn delete_message(&self, peer_id: &HashId, msg_id: &HashId) -> Result<()> {
        let messages = self.load_history(peer_id, usize::MAX)?;
        let filtered: Vec<ChatMessage> = messages
            .into_iter()
            .filter(|msg| msg.msg_id != *msg_id)
            .collect();

        self.rewrite_all_messages(peer_id, &filtered)
    }

    /// Получить список всех peer, с которыми был чат
    pub fn list_chats(&self) -> Result<Vec<HashId>> {
        let mut peers = Vec::new();
        
        if !self.chats_dir.exists() {
            return Ok(peers);
        }

        for entry in std::fs::read_dir(&self.chats_dir)? {
            let entry = entry?;
            let filename = entry.file_name();
            let filename_str = filename.to_string_lossy();
            
            if filename_str.starts_with("chat_") && filename_str.ends_with(".enc") {
                let short_id = &filename_str[5..filename_str.len()-4];
                let mut bytes = [0u8; 32];
                if let Ok(short_bytes) = hex::decode(short_id) {
                    bytes[..short_bytes.len()].copy_from_slice(&short_bytes);
                    peers.push(HashId(bytes));
                }
            }
        }

        Ok(peers)
    }
}
