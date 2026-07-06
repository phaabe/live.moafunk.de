//! Live-chat bridge between the Telegram discussion group and the panel (#278).
//!
//! The bot's dispatcher publishes messages from the configured live-chat group
//! into a [`ChatHub`]; every connected panel WebSocket subscribes to the hub's
//! broadcast channel and receives them in real time. Host replies travel the
//! other way: panel → bot API → group, and are echoed into the hub directly
//! (Telegram never delivers a bot its own messages, so there is no loop).

use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use tokio::sync::{broadcast, Mutex};

/// Messages kept for panels that connect mid-show.
const HISTORY_CAP: usize = 100;
/// Broadcast buffer per subscriber; lagging panels skip, they don't block.
const BROADCAST_CAP: usize = 64;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    /// Telegram message id, or 0 for host replies that failed to report one.
    pub id: i64,
    pub author: String,
    pub text: String,
    /// Unix seconds.
    pub ts: i64,
    /// True for replies sent by the panel host via the bot.
    pub host: bool,
}

pub struct ChatHub {
    history: Mutex<VecDeque<ChatMessage>>,
    tx: broadcast::Sender<ChatMessage>,
}

impl Default for ChatHub {
    fn default() -> Self {
        Self::new()
    }
}

impl ChatHub {
    pub fn new() -> Self {
        let (tx, _) = broadcast::channel(BROADCAST_CAP);
        Self {
            history: Mutex::new(VecDeque::with_capacity(HISTORY_CAP)),
            tx,
        }
    }

    /// Append to the ring buffer and fan out to all connected panels.
    pub async fn publish(&self, msg: ChatMessage) {
        {
            let mut history = self.history.lock().await;
            if history.len() == HISTORY_CAP {
                history.pop_front();
            }
            history.push_back(msg.clone());
        }
        // Err just means no panel is connected right now.
        let _ = self.tx.send(msg);
    }

    /// Snapshot of the retained history, oldest first.
    pub async fn recent(&self) -> Vec<ChatMessage> {
        self.history.lock().await.iter().cloned().collect()
    }

    pub fn subscribe(&self) -> broadcast::Receiver<ChatMessage> {
        self.tx.subscribe()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn msg(id: i64) -> ChatMessage {
        ChatMessage {
            id,
            author: format!("user{id}"),
            text: format!("hello {id}"),
            ts: 1_700_000_000 + id,
            host: false,
        }
    }

    #[tokio::test]
    async fn history_keeps_order_and_caps() {
        let hub = ChatHub::new();
        for i in 0..(HISTORY_CAP as i64 + 10) {
            hub.publish(msg(i)).await;
        }
        let recent = hub.recent().await;
        assert_eq!(recent.len(), HISTORY_CAP);
        assert_eq!(recent.first().unwrap().id, 10);
        assert_eq!(recent.last().unwrap().id, HISTORY_CAP as i64 + 9);
    }

    #[tokio::test]
    async fn subscribers_receive_published_messages() {
        let hub = ChatHub::new();
        let mut rx = hub.subscribe();
        hub.publish(msg(1)).await;
        let received = rx.recv().await.unwrap();
        assert_eq!(received.id, 1);
        assert_eq!(received.text, "hello 1");
    }
}
