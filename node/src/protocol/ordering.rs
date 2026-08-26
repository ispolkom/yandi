// src/protocol/ordering.rs
//!
//! # Train Ordering Queue
//!
//! Очередь с упорядочиванием поездов по sequence_id.
//! Гарантирует, что поезда доставляются клиенту в правильном порядке.

use std::collections::BTreeMap;
use std::time::{Duration, Instant};
use tokio::sync::{mpsc, Mutex};
use tracing::{info, debug, warn};

/// Упорядоченная очередь поездов
///
/// **Назначение:**
/// - Принимать поезда в произвольном порядке (out-of-order arrival)
/// - Доставлять поезда в строгом порядке по sequence_id
/// - Обрабатывать gaps (потерянные поезда) с timeout
pub struct TrainOrderingQueue {
    /// ID линии (для логирования)
    line_id: u8,

    /// Очередь: sequence_id → (train data, timestamp)
    queue: BTreeMap<u64, (Vec<u8>, Instant)>,

    /// Следующий ожидаемый sequence_id
    next_sequence: u64,

    /// Channel для отправки упорядоченных данных
    tx: mpsc::Sender<Vec<u8>>,

    /// Timeout для gap recovery (сколько ждать потерянный поезд)
    timeout: Duration,
}

impl TrainOrderingQueue {
    /// Создать новую очередь упорядочивания
    pub fn new(
        line_id: u8,
        tx: mpsc::Sender<Vec<u8>>,
        timeout: Duration,
    ) -> Self {
        Self {
            line_id,
            queue: BTreeMap::new(),
            next_sequence: 0,
            tx,
            timeout,
        }
    }

    /// Создать очередь с стандартным timeout 5 секунд
    pub fn with_defaults(line_id: u8, tx: mpsc::Sender<Vec<u8>>) -> Self {
        Self::new(line_id, tx, Duration::from_secs(5))
    }

    /// Добавить поезд в очередь
    pub async fn add_train(&mut self, sequence_id: u64, data: Vec<u8>) {
        let timestamp = Instant::now();

        info!(
            "📦 [Line #{}] Train seq={} added to ordering queue ({} MB)",
            self.line_id,
            sequence_id,
            data.len() / 1_000_000
        );

        // Проверяем на duplicate
        if self.queue.contains_key(&sequence_id) {
            warn!(
                "⚠️  [Line #{}] Duplicate train seq={}, ignoring",
                self.line_id, sequence_id
            );
            return;
        }

        // Сохраняем в очередь (BTreeMap автоматически сортирует по ключу)
        self.queue.insert(sequence_id, (data, timestamp));

        // Пытаемся доставить все готовые поезда
        self.drain_ready().await;
    }

    /// Доставить все поезда в правильном порядке
    async fn drain_ready(&mut self) {
        let now = Instant::now();

        while let Some((&seq_id, (data, timestamp))) = self.queue.first_key_value() {
            if seq_id == self.next_sequence {
                // ✅ Это поезд который мы ждали!
                let data = data.clone();

                info!(
                    "✅ [Line #{}] Delivering train seq={} to client ({} MB)",
                    self.line_id,
                    seq_id,
                    data.len() / 1_000_000
                );

                // Удаляем из очереди
                self.queue.remove(&seq_id);

                // Отправляем клиенту
                if let Err(e) = self.tx.send(data).await {
                    warn!(
                        "❌ [Line #{}] Failed to send train seq={} to client: {}",
                        self.line_id, seq_id, e
                    );
                    // Если channel закрыт - выходим
                    break;
                }

                // Увеличиваем счётчик
                self.next_sequence += 1;

            } else if now.duration_since(*timestamp) > self.timeout {
                // ⏰ Timeout! Пропускаем потерянный поезд
                warn!(
                    "⏰ [Line #{}] Timeout for train seq={}, skipping to seq={} (gap recovery)",
                    self.line_id, self.next_sequence, seq_id
                );

                // Удаляем timeout поезда
                self.queue.remove(&seq_id);

                // Продвигаем next_sequence к seq_id + 1
                self.next_sequence = seq_id + 1;

            } else if seq_id > self.next_sequence {
                // ⏳ Ещё не timeout, ждём
                debug!(
                    "⏳ [Line #{}] Waiting for train seq={} (have seq={}, gap={})",
                    self.line_id,
                    self.next_sequence,
                    seq_id,
                    seq_id - self.next_sequence
                );
                break;

            } else {
                // seq_id < self.next_sequence (устаревший поезд, пропускаем)
                warn!(
                    "⚠️  [Line #{}] Stale train seq={} (expected seq={}), removing",
                    self.line_id, seq_id, self.next_sequence
                );
                self.queue.remove(&seq_id);
            }
        }
    }

    /// Получить текущий размер очереди
    pub fn queue_size(&self) -> usize {
        self.queue.len()
    }

    /// Получить следующий ожидаемый sequence_id
    pub fn next_sequence(&self) -> u64 {
        self.next_sequence
    }

    /// Очистить очередь (например, при переподключении)
    pub fn clear(&mut self) {
        let size = self.queue.len();
        self.queue.clear();
        self.next_sequence = 0;

        if size > 0 {
            info!(
                "🧹 [Line #{}] Cleared ordering queue (removed {} trains)",
                self.line_id, size
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::time::{sleep, Duration};

    #[tokio::test]
    async fn test_ordering_in_order() {
        let (tx, mut rx) = mpsc::channel(100);
        let mut queue = TrainOrderingQueue::with_defaults(0, tx);

        // Отправляем поезда по порядку
        queue.add_train(0, vec![1, 2, 3]).await;
        queue.add_train(1, vec![4, 5, 6]).await;
        queue.add_train(2, vec![7, 8, 9]).await;

        // Проверяем что получаем в правильном порядке
        assert_eq!(rx.recv().await.unwrap(), vec![1, 2, 3]);
        assert_eq!(rx.recv().await.unwrap(), vec![4, 5, 6]);
        assert_eq!(rx.recv().await.unwrap(), vec![7, 8, 9]);
    }

    #[tokio::test]
    async fn test_ordering_out_of_order() {
        let (tx, mut rx) = mpsc::channel(100);
        let mut queue = TrainOrderingQueue::with_defaults(0, tx);

        // Отправляем в разброс
        queue.add_train(2, vec![7, 8, 9]).await; // Прибывает первым
        queue.add_train(0, vec![1, 2, 3]).await; // Прибывает вторым
        queue.add_train(1, vec![4, 5, 6]).await; // Прибывает третьим

        // Но получаем в правильном порядке!
        assert_eq!(rx.recv().await.unwrap(), vec![1, 2, 3]);
        assert_eq!(rx.recv().await.unwrap(), vec![4, 5, 6]);
        assert_eq!(rx.recv().await.unwrap(), vec![7, 8, 9]);
    }

    #[tokio::test]
    async fn test_ordering_with_gap() {
        let (tx, mut rx) = mpsc::channel(100);
        let mut queue = TrainOrderingQueue::new(0, tx, Duration::from_millis(100));

        // Отправляем seq=0 и seq=2 (пропуская seq=1)
        queue.add_train(0, vec![1, 2, 3]).await;
        queue.add_train(2, vec![7, 8, 9]).await;

        // Получаем seq=0 сразу
        assert_eq!(rx.recv().await.unwrap(), vec![1, 2, 3]);

        // Ждём timeout для seq=1
        sleep(Duration::from_millis(150)).await;

        // После timeout получаем seq=2
        assert_eq!(rx.recv().await.unwrap(), vec![7, 8, 9]);
    }

    #[tokio::test]
    async fn test_queue_size() {
        let (tx, _) = mpsc::channel(100);
        let mut queue = TrainOrderingQueue::with_defaults(0, tx);

        assert_eq!(queue.queue_size(), 0);

        // Добавляем поезд с gap
        queue.add_train(5, vec![1, 2, 3]).await;

        // Очередь должна содержать 1 поезд
        assert_eq!(queue.queue_size(), 1);
        assert_eq!(queue.next_sequence(), 0);
    }
}
