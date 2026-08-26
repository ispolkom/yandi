// src/netlayer/ws_transport.rs
//! WebSocket-over-TLS транспорт между Mobile и Anchor.
//!
//! **Зачем:** мобильный оператор режет UDP / странные TCP-порты, но почти никогда не блокирует
//! TLS на 443. Mobile подключается к anchor'у как обычный HTTPS-клиент к WebSocket endpoint'у —
//! из сети неотличимо от любого WebSocket-приложения.
//!
//! **Что внутри:** WS upgrade → binary-frames. Каждый binary-frame несёт один зашифрованный
//! wagon (тот же wire-format, что и в UDP — `[sender_id:32][nonce:12][ciphertext][tag:16]`).
//! Это позволяет анчору обрабатывать WS-peer'ов через ту же диспатч-логику, что и UDP-peer'ов.
//!
//! **Архитектура:**
//! - `WsServer` (на Anchor) — принимает соединения, выдаёт `WsConnection` per-client.
//! - `WsClient` (на Mobile) — установить и держать `WsConnection` к anchor'у.
//! - `WsConnection` — owns пару mpsc-каналов (incoming, outgoing). Wagon приходит/уходит через них.
//! - Bridge в основной транспорт — отдельный модуль (Iter 2.6).

use anyhow::{Context, Result};
use futures_util::{SinkExt, StreamExt};
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;
use tokio_rustls::{TlsAcceptor, TlsConnector};
use tokio_tungstenite::{
    accept_async, client_async,
    tungstenite::{protocol::Message, Result as WsResult},
    WebSocketStream,
};

use crate::netlayer::tls_cert::TlsIdentity;

/// Размер канала per-connection в обе стороны.
const CHANNEL_CAPACITY: usize = 256;

/// Одно WS-соединение, абстрактно (server- или client-сторона).
/// Передаёт raw bytes (encrypted wagons) в обе стороны через mpsc-каналы.
pub struct WsConnection {
    /// Канал для отправки (мы кладём — соединение шлёт по сети).
    pub outgoing: mpsc::Sender<Vec<u8>>,
    /// Канал для приёма (соединение кладёт — мы читаем).
    pub incoming: mpsc::Receiver<Vec<u8>>,
    /// Адрес peer'а (для логов).
    pub peer_addr: String,
    /// Хэндл pump-задачи. Drop = закрытие соединения.
    _pump_handle: tokio::task::JoinHandle<()>,
}

impl WsConnection {
    /// Внутренний конструктор: оборачивает уже готовый WebSocketStream и спавнит pump-задачу.
    fn from_stream<S>(
        ws: WebSocketStream<S>,
        peer_addr: String,
    ) -> Self
    where
        S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
    {
        let (out_tx, mut out_rx) = mpsc::channel::<Vec<u8>>(CHANNEL_CAPACITY);
        let (in_tx, in_rx) = mpsc::channel::<Vec<u8>>(CHANNEL_CAPACITY);

        let peer_addr_for_pump = peer_addr.clone();
        let pump = tokio::spawn(async move {
            let (mut sink, mut stream) = ws.split();

            loop {
                tokio::select! {
                    // App кладёт байты на отправку.
                    msg = out_rx.recv() => {
                        match msg {
                            Some(bytes) => {
                                if let Err(e) = sink.send(Message::Binary(bytes.into())).await {
                                    eprintln!("[ws] send to {} failed: {}", peer_addr_for_pump, e);
                                    break;
                                }
                            }
                            None => break, // отправляющая сторона закрыла канал
                        }
                    }
                    // По сети пришли байты.
                    next = stream.next() => {
                        match next {
                            Some(Ok(Message::Binary(data))) => {
                                if in_tx.send(data.to_vec()).await.is_err() {
                                    break; // принимающая сторона ушла
                                }
                            }
                            Some(Ok(Message::Close(_))) => {
                                break;
                            }
                            Some(Ok(Message::Ping(p))) => {
                                let _ = sink.send(Message::Pong(p)).await;
                            }
                            Some(Ok(_)) => { /* ignore Text/Pong/Frame */ }
                            Some(Err(e)) => {
                                eprintln!("[ws] recv from {} error: {}", peer_addr_for_pump, e);
                                break;
                            }
                            None => break,
                        }
                    }
                }
            }
            let _ = sink.send(Message::Close(None)).await;
        });

        Self {
            outgoing: out_tx,
            incoming: in_rx,
            peer_addr,
            _pump_handle: pump,
        }
    }
}

// ---------- Server ----------

/// WS-сервер: bind на TLS endpoint, принимает входящие соединения, отдаёт каждое
/// в виде `WsConnection` через accept-канал.
pub struct WsServer {
    /// Канал, из которого читать новые принятые соединения.
    pub accept_rx: mpsc::Receiver<WsConnection>,
    /// Локальный адрес, на котором сервер реально забиндился.
    pub local_addr: SocketAddr,
    _accept_handle: tokio::task::JoinHandle<()>,
}

impl WsServer {
    /// Поднять WS-over-TLS сервер на указанном адресе с готовой TLS identity.
    pub async fn bind(bind_addr: SocketAddr, tls: &TlsIdentity) -> Result<Self> {
        let server_cfg = crate::netlayer::tls_cert::build_server_config(tls)?;
        let acceptor = TlsAcceptor::from(server_cfg);

        let listener = TcpListener::bind(bind_addr)
            .await
            .with_context(|| format!("WsServer TcpListener::bind {}", bind_addr))?;
        let local_addr = listener.local_addr()?;

        let (accept_tx, accept_rx) = mpsc::channel::<WsConnection>(32);

        let handle = tokio::spawn(async move {
            loop {
                let (tcp, peer) = match listener.accept().await {
                    Ok(p) => p,
                    Err(e) => {
                        eprintln!("[ws] accept TCP failed: {}", e);
                        continue;
                    }
                };
                let acceptor_clone = acceptor.clone();
                let accept_tx_clone = accept_tx.clone();
                tokio::spawn(async move {
                    let tls_stream = match acceptor_clone.accept(tcp).await {
                        Ok(s) => s,
                        Err(e) => {
                            eprintln!("[ws] TLS handshake failed from {}: {}", peer, e);
                            return;
                        }
                    };
                    let ws_stream: WebSocketStream<_> = match accept_async(tls_stream).await {
                        Ok(ws) => ws,
                        Err(e) => {
                            eprintln!("[ws] WS upgrade failed from {}: {}", peer, e);
                            return;
                        }
                    };
                    let conn = WsConnection::from_stream(ws_stream, peer.to_string());
                    if accept_tx_clone.send(conn).await.is_err() {
                        // Сервер завершён, тихо выходим.
                    }
                });
            }
        });

        Ok(Self {
            accept_rx,
            local_addr,
            _accept_handle: handle,
        })
    }
}

// ---------- Client ----------

/// Подключиться к anchor'у по `wss://host:port/`.
/// `expected_fingerprint_hex` — SHA-256 fingerprint TLS-сертификата anchor'а (pinning).
/// Должен быть получен при pairing'е (Iter 4) и сохранён локально.
pub async fn connect_to_anchor(
    anchor_url: &str,
    expected_fingerprint_hex: &str,
) -> Result<WsConnection> {
    let url = anchor_url
        .parse::<url::Url>()
        .with_context(|| format!("parse {}", anchor_url))?;
    let host = url
        .host_str()
        .ok_or_else(|| anyhow::anyhow!("anchor URL без host"))?
        .to_string();
    let port = url
        .port()
        .ok_or_else(|| anyhow::anyhow!("anchor URL без port"))?;

    let client_cfg =
        crate::netlayer::tls_cert::build_client_config_pinned(expected_fingerprint_hex)?;
    let connector = TlsConnector::from(client_cfg);

    let tcp = TcpStream::connect((host.as_str(), port))
        .await
        .with_context(|| format!("TCP connect {}:{}", host, port))?;
    let server_name = rustls::pki_types::ServerName::try_from(host.clone())
        .with_context(|| format!("ServerName parse {}", host))?;
    let tls_stream = connector
        .connect(server_name, tcp)
        .await
        .context("TLS connect")?;

    let (ws_stream, _resp) = client_async(anchor_url, tls_stream)
        .await
        .context("WS handshake")?;

    let peer_addr = format!("{}:{}", host, port);
    Ok(WsConnection::from_stream(ws_stream, peer_addr))
}

// Reference to suppress unused warning when only one side of the module is used.
#[allow(dead_code)]
fn _arc_marker(_: Arc<()>) {}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    /// Спавним сервер и клиента на localhost, гоняем туда-сюда два бинарных фрейма.
    /// Проверяет TLS-handshake с pinning, WS-upgrade и round-trip данных.
    #[tokio::test]
    async fn ws_server_client_roundtrip() {
        let dir = tempdir().unwrap();
        let id = TlsIdentity::load_or_generate_in(dir.path(), "test01").unwrap();
        let fp = id.fingerprint_hex.clone();

        let bind: SocketAddr = "127.0.0.1:0".parse().unwrap();
        let mut server = WsServer::bind(bind, &id).await.unwrap();
        let port = server.local_addr.port();
        let url = format!("wss://localhost:{}/", port);

        // Принимаем в фоне.
        let server_task = tokio::spawn(async move {
            let mut conn = server.accept_rx.recv().await.expect("server accepts conn");
            // эхо: принимаем один пакет и шлём обратно
            let pkt = conn.incoming.recv().await.expect("server gets data");
            conn.outgoing.send(pkt).await.expect("server echoes");
            // держим живым ещё немного, чтобы клиент успел прочесть
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        });

        // Клиент.
        let mut client = connect_to_anchor(&url, &fp).await.expect("client connects");
        let payload = b"hello-yandi-ws".to_vec();
        client.outgoing.send(payload.clone()).await.unwrap();
        let echoed = tokio::time::timeout(
            std::time::Duration::from_secs(3),
            client.incoming.recv(),
        )
        .await
        .expect("recv timeout")
        .expect("recv None");

        assert_eq!(echoed, payload);
        let _ = server_task.await;
    }

    /// Pin mismatch — клиент не должен подключиться.
    #[tokio::test]
    async fn ws_pin_mismatch_rejects() {
        let dir = tempdir().unwrap();
        let id = TlsIdentity::load_or_generate_in(dir.path(), "test02").unwrap();
        let bind: SocketAddr = "127.0.0.1:0".parse().unwrap();
        let server = WsServer::bind(bind, &id).await.unwrap();
        let port = server.local_addr.port();
        let url = format!("wss://localhost:{}/", port);

        // Подсовываем неправильный fingerprint (32 нуля).
        let bad_fp = "0".repeat(64);
        let res = connect_to_anchor(&url, &bad_fp).await;
        assert!(res.is_err(), "client must reject anchor with mismatched pin");
    }
}
