use crate::{auth, AppState, Result};
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
    if auth::verify_password(&form.password, &state.config.admin_password_hash) {
        let token = auth::create_session(&state).await?;

        let cookie = format!(
            "session={}; HttpOnly; Secure; SameSite=Strict; Max-Age={}; Path=/",
            token,
            60 * 60 * 24 * 7 // 7 days
        );

        Ok((
            StatusCode::SEE_OTHER,
            [
                (header::SET_COOKIE, cookie),
                (header::LOCATION, "/artists".to_string()),
            ],
        )
            .into_response())
    } else {
        let mut context = tera::Context::new();
        context.insert("error", &Some("Invalid password".to_string()));

        let html = state.templates.render("login.html", &context)?;
        Ok((StatusCode::UNAUTHORIZED, Html(html)).into_response())
    }
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
