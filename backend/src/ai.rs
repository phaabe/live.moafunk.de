use crate::{AppError, Config, Result};
use serde::{Deserialize, Serialize};

/// OpenAI Chat Completions request/response types
#[derive(Debug, Serialize)]
struct ChatRequest {
    model: String,
    messages: Vec<ChatMessage>,
    temperature: f32,
    max_tokens: u32,
}

#[derive(Debug, Serialize, Deserialize)]
struct ChatMessage {
    role: String,
    content: String,
}

#[derive(Debug, Deserialize)]
struct ChatResponse {
    choices: Vec<ChatChoice>,
}

#[derive(Debug, Deserialize)]
struct ChatChoice {
    message: ChatMessage,
}

const OPENAI_CHAT_URL: &str = "https://api.openai.com/v1/chat/completions";

const SYSTEM_PROMPT: &str = "You are writing short, engaging Instagram captions for music artists \
featured on a radio show called UNHEARD by Moafunk Radio. \
Based on the artist's description of their music, write a 2-3 sentence \
bio that is casual, music-focused, and captures the artist's vibe. \
Do NOT use hashtags. Do NOT use emojis. \
Keep it concise and authentic.\n\n\
IMPORTANT rules:\n\
- Start the FIRST sentence with the artist's actual name (provided below).\n\
- Use the correct pronouns (provided below) throughout. \
If multiple sets of pronouns are given (e.g. 'he/him x she/her' or 'he, she') \
or the name suggests a band/duo/collective, treat them as a group and use 'they/their/them'.\n\
- Write in third person. Never use 'you' or 'I'.\n\
- End with a short, catchy closing line that hooks the reader - \
something witty or intriguing, but NOT cheesy or cliche. \
Think music journalist, not motivational poster.";

/// Generate an Instagram-ready artist bio from their music description
/// using OpenAI GPT-4o-mini.
pub async fn generate_artist_bio(
    config: &Config,
    artist_name: &str,
    pronouns: &str,
    music_description: &str,
) -> Result<String> {
    let api_key = config
        .openai_api_key
        .as_ref()
        .ok_or_else(|| AppError::Internal("OPENAI_API_KEY is not configured".to_string()))?;

    let client = reqwest::Client::new();

    let user_content = format!(
        "Artist name: {}\nPronouns: {}\n\nHow they describe their music:\n{}",
        artist_name, pronouns, music_description
    );

    let request = ChatRequest {
        model: "gpt-4o-mini".to_string(),
        messages: vec![
            ChatMessage {
                role: "system".to_string(),
                content: SYSTEM_PROMPT.to_string(),
            },
            ChatMessage {
                role: "user".to_string(),
                content: user_content,
            },
        ],
        temperature: 0.7,
        max_tokens: 200,
    };

    tracing::info!("Calling OpenAI API for bio generation");

    let response = client
        .post(OPENAI_CHAT_URL)
        .header("Authorization", format!("Bearer {}", api_key))
        .json(&request)
        .send()
        .await
        .map_err(|e| AppError::Internal(format!("OpenAI API request failed: {}", e)))?;

    if !response.status().is_success() {
        let status = response.status();
        let body = response
            .text()
            .await
            .unwrap_or_else(|_| "unknown error".to_string());
        tracing::error!("OpenAI API error: {} - {}", status, body);
        return Err(AppError::Internal(format!(
            "OpenAI API returned error {}: {}",
            status, body
        )));
    }

    let chat_response: ChatResponse = response
        .json()
        .await
        .map_err(|e| AppError::Internal(format!("Failed to parse OpenAI response: {}", e)))?;

    let bio = chat_response
        .choices
        .first()
        .map(|c| c.message.content.trim().to_string())
        .ok_or_else(|| AppError::Internal("OpenAI returned no choices".to_string()))?;

    tracing::info!("Bio generated successfully ({} chars)", bio.len());
    Ok(bio)
}
