use crate::{auth, models, AppState, Result};
use axum::{
    extract::State,
    http::{header, StatusCode},
    response::{Html, IntoResponse, Response},
    Form,
};
use serde::Deserialize;
use std::sync::Arc;

#[derive(Deserialize)]
pub struct LoginForm {
    username: String,
    password: String,
}

pub async fn login_page(State(state): State<Arc<AppState>>) -> Result<Html<String>> {
    let mut context = tera::Context::new();
    context.insert("error", &Option::<String>::None);

    let html = state.templates.render("login.html", &context)?;
    Ok(Html(html))
}

pub async fn login(
    State(state): State<Arc<AppState>>,
    Form(form): Form<LoginForm>,
) -> Result<Response> {
    // Look up user by username
    let user: Option<models::User> = sqlx::query_as("SELECT * FROM users WHERE username = ?")
        .bind(&form.username)
        .fetch_optional(&state.db)
        .await?;
    
    let user = match user {
        Some(u) => u,
        None => {
            let mut context = tera::Context::new();
            context.insert("error", &Some("Invalid username or password".to_string()));
            let html = state.templates.render("login.html", &context)?;
            return Ok((StatusCode::UNAUTHORIZED, Html(html)).into_response());
        }
    };
    
    // Verify password
    if !auth::verify_password(&form.password, &user.password_hash) {
        let mut context = tera::Context::new();
        context.insert("error", &Some("Invalid username or password".to_string()));
        let html = state.templates.render("login.html", &context)?;
        return Ok((StatusCode::UNAUTHORIZED, Html(html)).into_response());
    }
    
    // Check if account is expired
    if user.is_expired() {
        let mut context = tera::Context::new();
        context.insert("error", &Some("Your account has expired. Please contact an administrator.".to_string()));
        let html = state.templates.render("login.html", &context)?;
        return Ok((StatusCode::UNAUTHORIZED, Html(html)).into_response());
    }
    
    // Create session
    let token = auth::create_session(&state, user.id).await?;
    
    // Determine redirect based on role
    let redirect_url = match user.role_enum() {
        models::UserRole::Artist => "/stream",
        models::UserRole::Admin | models::UserRole::Superadmin => "/artists",
    };
    
    let cookie = format!(
        "session={}; HttpOnly; Secure; SameSite=Strict; Max-Age={}; Path=/",
        token,
        60 * 60 * 24 * 7 // 7 days
    );

    Ok((
        StatusCode::SEE_OTHER,
        [
            (header::SET_COOKIE, cookie),
            (header::LOCATION, redirect_url.to_string()),
        ],
    )
        .into_response())
}

pub async fn logout(State(_state): State<Arc<AppState>>) -> Result<Response> {
    let cookie = "session=; HttpOnly; Secure; SameSite=Strict; Max-Age=0; Path=/";

    Ok((
        StatusCode::SEE_OTHER,
        [
            (header::SET_COOKIE, cookie.to_string()),
            (header::LOCATION, "/login".to_string()),
        ],
    )
        .into_response())
}
