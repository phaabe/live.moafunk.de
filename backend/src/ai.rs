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
Think music journalist, not motivational poster.


If additional context notes are provided, use any relevant music-related or 
artist-related information from them to enrich the bio. Ignore anything 
that is purely logistical or unrelated to the artist's music/identity.";

const INSTAGRAM_POST_PROMPT: &str =
    "You write short, catchy Instagram intro paragraphs for artists \
featured on the radio show UNHEARD by Moafunk Radio.\n\n\
You will be given the artist's name and the show title.\n\n\
Write 1 sentences that introduce this artist as a guest on the show. \
Be creative and vary your style — sometimes enthusiastic, sometimes cool and understated, \
sometimes building anticipation. Think music blog, not corporate announcement.\n\n\
Examples of good intros (for reference only — do NOT copy these):\n\
- 'From our last show UNHEARD #6 we proudly present Estella Boersma!'\n\
- 'UNHEARD #3 brought the heat — and DJ Nova was right in the middle of it.'\n\
- 'One of the highlights of UNHEARD #8? Easy. Kora.'\n\
- 'We had the pleasure of featuring Zara on UNHEARD #5. What a ride.'\n\
- 'UNHEARD #2 just dropped, and with it comes a fresh set from Milo.'\n\n\
STRICT rules:\n\
- Include the show title EXACTLY as provided. Do NOT paraphrase it.\n\
- Do NOT describe the artist's music, sound, genre, or style. Zero adjectives about the artist.\n\
- Do NOT mention track names.\n\
- Do NOT use hashtags or emojis.\n\
- Do NOT use words like 'talented', 'amazing', 'incredible', or any compliments.\n\
- Keep it punchy and natural. Vary sentence structure across calls.\n\
- Output ONLY the intro paragraph, nothing else.";

/// Shared helper: call OpenAI chat completions and return the trimmed response text.
async fn call_openai(
    api_key: &str,
    system_prompt: &str,
    user_content: &str,
    temperature: f32,
    max_tokens: u32,
) -> Result<String> {
    let client = reqwest::Client::new();

    let request = ChatRequest {
        model: "gpt-4o-mini".to_string(),
        messages: vec![
            ChatMessage {
                role: "system".to_string(),
                content: system_prompt.to_string(),
            },
            ChatMessage {
                role: "user".to_string(),
                content: user_content.to_string(),
            },
        ],
        temperature,
        max_tokens,
    };

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

    chat_response
        .choices
        .first()
        .map(|c| c.message.content.trim().to_string())
        .ok_or_else(|| AppError::Internal("OpenAI returned no choices".to_string()))
}

/// Generate an Instagram-ready artist bio from their music description
/// using OpenAI GPT-4o-mini.
pub async fn generate_artist_bio(
    config: &Config,
    artist_name: &str,
    pronouns: &str,
    music_description: &str,
    mentions: Option<&str>,
) -> Result<String> {
    let api_key = config
        .openai_api_key
        .as_ref()
        .ok_or_else(|| AppError::Internal("OPENAI_API_KEY is not configured".to_string()))?;

    let mut user_content = format!(
        "Artist name: {}\nPronouns: {}\n\nHow they describe their music:\n{}",
        artist_name, pronouns, music_description
    );

    if let Some(m) = mentions {
        if !m.trim().is_empty() {
            user_content.push_str(&format!("\n\nAdditional context notes:\n{}", m));
        }
    }

    tracing::info!("Calling OpenAI API for bio generation");
    let bio = call_openai(api_key, SYSTEM_PROMPT, &user_content, 0.7, 200).await?;
    tracing::info!("Bio generated successfully ({} chars)", bio.len());
    Ok(bio)
}

/// Generate a show-context Instagram caption paragraph for an artist.
///
/// Generate a short show-intro sentence for an artist's Instagram post.
///
/// This produces just the intro line (e.g. "From our last show UNHEARD #6
/// we proudly present Estella Boersma!"). The artist bio, track listing,
/// and social links are assembled by the handler.
pub async fn generate_artist_instagram_caption(
    config: &Config,
    artist_name: &str,
    pronouns: &str,
    _music_description: &str,
    show_title: &str,
    _track1_name: &str,
    _track2_name: &str,
) -> Result<String> {
    let api_key = config
        .openai_api_key
        .as_ref()
        .ok_or_else(|| AppError::Internal("OPENAI_API_KEY is not configured".to_string()))?;

    let user_content = format!(
        "Artist name: {}\nPronouns: {}\nShow title: {}",
        artist_name, pronouns, show_title
    );

    tracing::info!(
        "Calling OpenAI API for Instagram intro (artist: {}, show: {})",
        artist_name,
        show_title
    );
    let caption = call_openai(api_key, INSTAGRAM_POST_PROMPT, &user_content, 0.9, 150).await?;
    tracing::info!(
        "Instagram intro generated successfully ({} chars)",
        caption.len()
    );
    Ok(caption)
}
