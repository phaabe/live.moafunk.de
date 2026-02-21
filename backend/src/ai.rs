use crate::{models, AppError, AppState, Config, Result};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

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
that is purely logistical or unrelated to the artist's music/identity.

IMPORTANT: The final bio text MUST be under 800 characters (including spaces). \
This is a hard limit — if a draft is too long, shorten it. \
Prioritise being concise over being exhaustive.";

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

// ============================================================================
// Service-layer functions (shared by API handlers + Telegram bot)
// ============================================================================

/// Generate an AI bio for an artist and persist it to the database.
///
/// Returns the generated bio text. Reusable from both HTTP handlers and
/// Telegram command handlers.
pub async fn generate_and_store_artist_bio(
    state: &Arc<AppState>,
    artist_id: i64,
) -> Result<String> {
    let artist: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(artist_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Artist {artist_id} not found")))?;

    let music_description = artist.music_description.ok_or_else(|| {
        AppError::Validation("Artist has no music description to generate bio from".to_string())
    })?;

    let bio = generate_artist_bio(
        &state.config,
        &artist.name,
        &artist.pronouns,
        &music_description,
        artist.mentions.as_deref(),
    )
    .await?;

    sqlx::query("UPDATE artists SET ai_bio = ?, updated_at = datetime('now') WHERE id = ?")
        .bind(&bio)
        .bind(artist_id)
        .execute(&state.db)
        .await?;

    Ok(bio)
}

/// Generate (or reuse) an AI bio, generate a show-intro paragraph, assemble
/// the full Instagram caption, and persist it to the database.
///
/// Returns the full caption text. Reusable from both HTTP handlers and
/// Telegram command handlers.
pub async fn generate_and_store_instagram_caption(
    state: &Arc<AppState>,
    artist_id: i64,
) -> Result<String> {
    let artist: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(artist_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Artist {artist_id} not found")))?;

    let music_description = artist.music_description.as_deref().ok_or_else(|| {
        AppError::Validation("Artist has no music description to generate caption from".to_string())
    })?;

    // Fetch the most recent assigned show for context
    let show: models::Show = sqlx::query_as(
        r#"
        SELECT s.* FROM shows s
        INNER JOIN artist_show_assignments asa ON s.id = asa.show_id
        WHERE asa.artist_id = ?
        ORDER BY s.date DESC
        LIMIT 1
        "#,
    )
    .bind(artist_id)
    .fetch_optional(&state.db)
    .await?
    .ok_or_else(|| {
        AppError::Validation(
            "Artist is not assigned to any show. Assign to a show first.".to_string(),
        )
    })?;

    // Generate (or reuse) the AI artist bio
    let ai_bio = if let Some(ref bio) = artist.ai_bio {
        bio.clone()
    } else {
        let bio = generate_artist_bio(
            &state.config,
            &artist.name,
            &artist.pronouns,
            music_description,
            artist.mentions.as_deref(),
        )
        .await?;
        sqlx::query("UPDATE artists SET ai_bio = ? WHERE id = ?")
            .bind(&bio)
            .bind(artist_id)
            .execute(&state.db)
            .await?;
        bio
    };

    // Generate the show-context paragraph
    let show_bio = generate_artist_instagram_caption(
        &state.config,
        &artist.name,
        &artist.pronouns,
        music_description,
        &show.title,
        &artist.track1_name,
        &artist.track2_name,
    )
    .await?;

    // Assemble the full caption
    let mut caption = format!("UNHEARD Guest: {}\n\n{}", artist.name, show_bio);
    caption.push_str(&format!("\n\n{}", ai_bio));
    caption.push_str(&format!(
        "\n\nTrack 1: \"{}\"\nTrack 2: \"{}\"",
        artist.track1_name, artist.track2_name
    ));

    if let Some(ref sc) = artist.soundcloud {
        if !sc.is_empty() {
            caption.push_str("\n\nSoundcloud link in Bio.");
        }
    }

    if let Some(ref ig) = artist.instagram {
        if !ig.is_empty() {
            let handle = ig.trim_end_matches('/').rsplit('/').next().unwrap_or(ig);
            let handle = handle.trim_start_matches('@');
            caption.push_str(&format!("\n\n@{}", handle));
        }
    }

    // Store the generated caption
    sqlx::query(
        "UPDATE artists SET instagram_caption = ?, updated_at = datetime('now') WHERE id = ?",
    )
    .bind(&caption)
    .bind(artist_id)
    .execute(&state.db)
    .await?;

    tracing::info!(
        "Generated Instagram caption for artist {} ({} chars)",
        artist.name,
        caption.len()
    );

    Ok(caption)
}

// ============================================================================
// Show bio generation (combined from assigned artists' bios)
// ============================================================================

const SHOW_BIO_PROMPT: &str = "You write short, engaging show descriptions for a radio show \
called UNHEARD by Moafunk Radio.\n\n\
You will be given the show title, the individual bios of the artists \
performing on this show (up to 4 artists), and optionally a show description \
that provides context about the show concept or theme.\n\n\
Write a show description following this EXACT structure:\n\
1. Opening paragraph: List all artists briefly (e.g., 'Tonight features Artist1, Artist2, Artist3, and Artist4.')\n\
2. Main body: Introduce each artist one by one with a dedicated sentence highlighting their sound/style based on their bio\n\
3. Closing: End with one or two catchy phrases that capture the vibe of the lineup\n\n\
If a show description is provided, integrate its theme/concept naturally into the bio, \
especially in the opening or closing.\n\n\
STRICT rules:\n\
- Do NOT use hashtags or emojis\n\
- Do NOT mention specific dates or times\n\
- Do NOT repeat the show title — it will already be displayed above\n\
- Do NOT use words like 'talented', 'amazing', 'incredible', or generic compliments\n\
- Each artist must get their own sentence with specific details from their bio\n\
- Include a paragraph break (blank line) between the opening and the detailed artist introductions\n\
- Reads like a music blog preview, not a press release\n\
- The TOTAL output MUST be under 900 characters (including spaces). \
This is a hard limit — shorten if needed. Prioritise being concise over exhaustive.\n\
- Output ONLY the show description, nothing else.";

const NON_UNHEARD_BIO_PROMPT: &str = "You write short, engaging show descriptions for Moafunk Radio events.\n\n\
You will be given the show title, show type (e.g., 'brunchtime' or 'external'), \
and a description that provides context about the event.\n\n\
Write a compelling show description that:\n\
1. Captures the vibe and concept of the event based on the description\n\
2. Sounds inviting and makes people want to attend or tune in\n\
3. Integrates the show type naturally (e.g., if brunchtime, reference the relaxed daytime vibe)\n\n\
STRICT rules:\n\
- Do NOT use hashtags or emojis\n\
- Do NOT mention specific dates or times\n\
- Do NOT repeat the show title — it will already be displayed above\n\
- Do NOT use words like 'talented', 'amazing', 'incredible', or generic compliments\n\
- Reads like a music blog preview, not a press release\n\
- The TOTAL output MUST be under 900 characters (including spaces). \
This is a hard limit — shorten if needed. Prioritise being concise over exhaustive.\n\
- Output ONLY the show description, nothing else.";

/// Generate a show bio from description text only (for non-UNHEARD shows).
pub async fn generate_show_bio_from_text(
    config: &Config,
    user_content: &str,
) -> Result<String> {
    let api_key = config
        .openai_api_key
        .as_ref()
        .ok_or_else(|| AppError::Internal("OPENAI_API_KEY is not configured".to_string()))?;

    tracing::info!("Calling OpenAI API for non-UNHEARD show bio generation");
    let bio = call_openai(api_key, NON_UNHEARD_BIO_PROMPT, user_content, 0.7, 300).await?;
    tracing::info!("Non-UNHEARD show bio generated successfully ({} chars)", bio.len());
    Ok(bio)
}

/// Generate a combined show bio from individual artist bios.
pub async fn generate_show_bio(
    config: &Config,
    artists: &[(String, String)],
    show_title: &str,
    show_description: Option<&str>,
) -> Result<String> {
    let api_key = config
        .openai_api_key
        .as_ref()
        .ok_or_else(|| AppError::Internal("OPENAI_API_KEY is not configured".to_string()))?;

    let bios_text = artists
        .iter()
        .map(|(name, bio)| format!("{} bio:\n{}", name, bio))
        .collect::<Vec<_>>()
        .join("\n\n");

    let user_content = if let Some(desc) = show_description {
        format!(
            "Show title: {}\nShow description: {}\n\n{}",
            show_title, desc, bios_text
        )
    } else {
        format!("Show title: {}\n\n{}", show_title, bios_text)
    };

    tracing::info!(
        "Calling OpenAI API for show bio generation (show: {}, {} artist bios)",
        show_title,
        artists.len()
    );
    let bio = call_openai(api_key, SHOW_BIO_PROMPT, &user_content, 0.7, 300).await?;
    tracing::info!("Show bio generated successfully ({} chars)", bio.len());
    Ok(bio)
}

/// Generate an AI show bio from all assigned artists' bios and persist it.
///
/// For UNHEARD shows: uses artist bios as source material. If no artists are assigned,
/// clears the show's `ai_bio` to NULL. Artists missing an `ai_bio` will have one
/// generated first (if they have a music_description).
///
/// For non-UNHEARD shows (brunchtime, external): uses the show description as the
/// sole source material, ignoring artist assignments entirely.
pub async fn generate_and_store_show_bio(
    state: &Arc<AppState>,
    show_id: i64,
) -> Result<Option<String>> {
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Show {show_id} not found")))?;

    let show_type = show.show_type.as_str();

    if show_type != "unheard" {
        // Non-UNHEARD shows: generate bio from description only
        let description = match show.description.as_deref() {
            Some(desc) if !desc.trim().is_empty() => desc,
            _ => {
                tracing::info!(
                    "Cannot generate bio for non-UNHEARD show {} — no description provided",
                    show_id
                );
                return Ok(None);
            }
        };

        let user_content = format!(
            "Show title: {}\nShow type: {}\nShow description: {}",
            show.title, show_type, description
        );

        let bio = generate_show_bio_from_text(
            &state.config,
            &user_content,
        )
        .await?;

        sqlx::query("UPDATE shows SET ai_bio = ?, updated_at = datetime('now') WHERE id = ?")
            .bind(&bio)
            .bind(show_id)
            .execute(&state.db)
            .await?;

        tracing::info!(
            "Generated show bio for non-UNHEARD show {} ({} chars, from description)",
            show.title,
            bio.len()
        );

        return Ok(Some(bio));
    }

    // UNHEARD shows: original artist-based logic
    // Fetch all assigned artists (full rows so we can generate missing bios)
    let artists: Vec<models::Artist> = sqlx::query_as(
        r#"
        SELECT a.* FROM artists a
        INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id
        WHERE asa.show_id = ?
        ORDER BY a.name ASC
        "#,
    )
    .bind(show_id)
    .fetch_all(&state.db)
    .await?;

    if artists.is_empty() {
        // No artists assigned — clear the show bio
        sqlx::query("UPDATE shows SET ai_bio = NULL, updated_at = datetime('now') WHERE id = ?")
            .bind(show_id)
            .execute(&state.db)
            .await?;
        tracing::info!("Cleared ai_bio for show {} (no artists assigned)", show_id);
        return Ok(None);
    }

    // Generate individual artist bios for any artists missing one
    let mut artist_data: Vec<(String, String)> = Vec::new();
    for artist in &artists {
        if let Some(ref bio) = artist.ai_bio {
            artist_data.push((artist.name.clone(), bio.clone()));
        } else if artist.music_description.is_some() {
            // Artist has no ai_bio but has a music_description — generate one now
            tracing::info!(
                "Generating missing ai_bio for artist {} (id={}) before show bio",
                artist.name,
                artist.id
            );
            match generate_and_store_artist_bio(state, artist.id).await {
                Ok(bio) => artist_data.push((artist.name.clone(), bio)),
                Err(e) => {
                    tracing::warn!(
                        "Failed to generate ai_bio for artist {}: {}, skipping",
                        artist.name,
                        e
                    );
                }
            }
        } else {
            tracing::debug!(
                "Skipping artist {} (id={}) — no ai_bio or music_description",
                artist.name,
                artist.id
            );
        }
    }

    if artist_data.is_empty() {
        // No usable bios — clear the show bio
        sqlx::query("UPDATE shows SET ai_bio = NULL, updated_at = datetime('now') WHERE id = ?")
            .bind(show_id)
            .execute(&state.db)
            .await?;
        tracing::info!(
            "Cleared ai_bio for show {} (no artist bios available)",
            show_id
        );
        return Ok(None);
    }

    let bio = generate_show_bio(
        &state.config,
        &artist_data,
        &show.title,
        show.description.as_deref(),
    )
    .await?;

    sqlx::query("UPDATE shows SET ai_bio = ?, updated_at = datetime('now') WHERE id = ?")
        .bind(&bio)
        .bind(show_id)
        .execute(&state.db)
        .await?;

    tracing::info!(
        "Generated and stored show bio for show {} ({} chars, from {} artist bios)",
        show.title,
        bio.len(),
        artist_data.len()
    );

    Ok(Some(bio))
}
