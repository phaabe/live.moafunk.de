use crate::models;

fn pdf_escape_text(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '(' => out.push_str("\\("),
            ')' => out.push_str("\\)"),
            // Keep it simple: replace control chars with spaces.
            c if c.is_control() => out.push(' '),
            c => out.push(c),
        }
    }
    out
}

fn wrap_text_lines(text: &str, max_chars: usize) -> Vec<String> {
    let text = text.trim();
    if text.is_empty() {
        return vec![String::new()];
    }

    let mut out: Vec<String> = Vec::new();
    for raw_line in text.lines() {
        let mut line = raw_line.trim();
        if line.is_empty() {
            out.push(String::new());
            continue;
        }

        while line.len() > max_chars {
            let mut split_at = None;
            for (idx, ch) in line.char_indices().take(max_chars + 1) {
                if ch.is_whitespace() {
                    split_at = Some(idx);
                }
            }

            let cut = split_at.unwrap_or_else(|| {
                line.char_indices()
                    .nth(max_chars)
                    .map(|(i, _)| i)
                    .unwrap_or(line.len())
            });

            let (left, right) = line.split_at(cut);
            out.push(left.trim_end().to_string());
            line = right.trim_start();
            if line.is_empty() {
                break;
            }
        }

        if !line.is_empty() {
            out.push(line.to_string());
        }
    }

    out
}

/// Minimal, dependency-free PDF with a single page using built-in Helvetica.
///
/// This is intentionally simple: the show has max 4 artists, so a single A4 page
/// is enough for the required fields.
pub fn build_recording_infos_pdf(show: &models::Show, artists: &[models::Artist]) -> Vec<u8> {
    let mut lines: Vec<(u32, String)> = Vec::new();

    lines.push((16, format!("UNHEARD – Recording Infos")));
    lines.push((12, format!("Show: {}", show.title.trim())));
    lines.push((12, format!("Date: {}", show.date.trim())));
    lines.push((12, String::new()));

    for (idx, artist) in artists.iter().enumerate() {
        if idx > 0 {
            lines.push((12, String::new()));
        }

        lines.push((14, format!("Artist: {}", artist.name.trim())));
        lines.push((12, format!("Track 1: {}", artist.track1_name.trim())));
        lines.push((12, format!("Track 2: {}", artist.track2_name.trim())));

        lines.push((12, "Things to mention:".to_string()));
        for l in wrap_text_lines(artist.mentions.as_deref().unwrap_or(""), 92) {
            let v = if l.trim().is_empty() {
                "-".to_string()
            } else {
                format!("- {}", l)
            };
            lines.push((11, v));
        }

        lines.push((12, "Upcoming shows:".to_string()));
        for l in wrap_text_lines(artist.upcoming_events.as_deref().unwrap_or(""), 92) {
            let v = if l.trim().is_empty() {
                "-".to_string()
            } else {
                format!("- {}", l)
            };
            lines.push((11, v));
        }
    }

    // Build PDF content stream.
    // A4: 595 x 842 points.
    let mut content = String::new();
    content.push_str("BT\n");

    // Start near top-left margin.
    let x = 50i32;
    let mut y = 800i32;

    // Initial position.
    content.push_str(&format!("{} {} Td\n", x, y));

    for (font_size, text) in lines {
        // Simple page overflow protection: if we run out of space, stop.
        if y < 60 {
            break;
        }

        let safe = pdf_escape_text(&text);
        content.push_str(&format!("/F1 {} Tf\n", font_size));
        content.push_str(&format!("({}) Tj\n", safe));

        // Advance to next line (move down).
        let step = match font_size {
            16 => 22,
            14 => 20,
            12 => 16,
            _ => 14,
        };
        content.push_str(&format!("0 -{} Td\n", step));
        y -= step;
    }

    content.push_str("ET\n");

    let content_bytes = content.into_bytes();

    // Assemble PDF objects.
    let mut objects: Vec<Vec<u8>> = Vec::new();

    objects.push(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj".to_vec());
    objects.push(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj".to_vec());
    objects.push(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj"
            .to_vec(),
    );
    objects
        .push(b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj".to_vec());

    let stream_obj = {
        let mut s = Vec::new();
        s.extend_from_slice(
            format!("5 0 obj\n<< /Length {} >>\nstream\n", content_bytes.len()).as_bytes(),
        );
        s.extend_from_slice(&content_bytes);
        if !content_bytes.ends_with(b"\n") {
            s.extend_from_slice(b"\n");
        }
        s.extend_from_slice(b"endstream\nendobj");
        s
    };
    objects.push(stream_obj);

    // Write file with xref.
    let mut out: Vec<u8> = Vec::new();
    out.extend_from_slice(b"%PDF-1.4\n%\xFF\xFF\xFF\xFF\n");

    let mut offsets: Vec<usize> = vec![0];
    for obj in &objects {
        offsets.push(out.len());
        out.extend_from_slice(obj);
        out.extend_from_slice(b"\n");
    }

    let xref_start = out.len();
    out.extend_from_slice(format!("xref\n0 {}\n", offsets.len()).as_bytes());
    out.extend_from_slice(b"0000000000 65535 f \n");
    for off in offsets.iter().skip(1) {
        out.extend_from_slice(format!("{:010} 00000 n \n", off).as_bytes());
    }

    out.extend_from_slice(
        format!(
            "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF\n",
            offsets.len(),
            xref_start
        )
        .as_bytes(),
    );

    out
}

/// Generate a PDF with artist information for individual download
pub fn generate_artist_pdf(
    artist: &models::Artist,
    assigned_show: &str,
    voice_status: &str,
) -> Result<Vec<u8>, crate::AppError> {
    let mut lines: Vec<(u32, String)> = Vec::new();

    lines.push((16, format!("UNHEARD – Artist Info")));
    lines.push((12, String::new()));

    lines.push((14, format!("Artist: {}", artist.name.trim())));
    lines.push((12, format!("Pronouns: {}", artist.pronouns.trim())));
    lines.push((12, format!("Status: {}", artist.status.trim())));
    lines.push((12, format!("Assigned Show: {}", assigned_show)));
    lines.push((12, String::new()));

    lines.push((14, "Tracks".to_string()));
    lines.push((12, format!("Track 1: {}", artist.track1_name.trim())));
    lines.push((12, format!("Track 2: {}", artist.track2_name.trim())));
    lines.push((12, format!("Voice Message: {}", voice_status)));
    lines.push((12, String::new()));

    lines.push((14, "Social Media".to_string()));
    lines.push((12, format!("Instagram: {}", artist.instagram.as_deref().unwrap_or("N/A"))));
    lines.push((12, format!("SoundCloud: {}", artist.soundcloud.as_deref().unwrap_or("N/A"))));
    lines.push((12, format!("Bandcamp: {}", artist.bandcamp.as_deref().unwrap_or("N/A"))));
    lines.push((12, format!("Spotify: {}", artist.spotify.as_deref().unwrap_or("N/A"))));
    lines.push((12, format!("Other: {}", artist.other_social.as_deref().unwrap_or("N/A"))));
    lines.push((12, String::new()));

    lines.push((14, "Things to Mention".to_string()));
    for l in wrap_text_lines(artist.mentions.as_deref().unwrap_or("N/A"), 92) {
        lines.push((11, l));
    }
    lines.push((12, String::new()));

    lines.push((14, "Upcoming Events".to_string()));
    for l in wrap_text_lines(artist.upcoming_events.as_deref().unwrap_or("N/A"), 92) {
        lines.push((11, l));
    }

    // Build PDF content stream (A4: 595 x 842 points)
    let mut content = String::new();
    content.push_str("BT\n");

    let x = 50i32;
    let mut y = 800i32;
    content.push_str(&format!("{} {} Td\n", x, y));

    for (font_size, text) in lines {
        if y < 60 {
            break;
        }

        let safe = pdf_escape_text(&text);
        content.push_str(&format!("/F1 {} Tf\n", font_size));
        content.push_str(&format!("({}) Tj\n", safe));

        let step = match font_size {
            16 => 22,
            14 => 20,
            12 => 16,
            _ => 14,
        };
        content.push_str(&format!("0 -{} Td\n", step));
        y -= step;
    }

    content.push_str("ET\n");

    let content_bytes = content.into_bytes();

    // Assemble PDF objects
    let mut objects: Vec<Vec<u8>> = Vec::new();
    objects.push(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj".to_vec());
    objects.push(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj".to_vec());
    objects.push(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj"
            .to_vec(),
    );
    objects.push(b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj".to_vec());

    let stream_obj = {
        let mut s = Vec::new();
        s.extend_from_slice(
            format!("5 0 obj\n<< /Length {} >>\nstream\n", content_bytes.len()).as_bytes(),
        );
        s.extend_from_slice(&content_bytes);
        if !content_bytes.ends_with(b"\n") {
            s.extend_from_slice(b"\n");
        }
        s.extend_from_slice(b"endstream\nendobj");
        s
    };
    objects.push(stream_obj);

    // Write file with xref
    let mut out: Vec<u8> = Vec::new();
    out.extend_from_slice(b"%PDF-1.4\n%\xFF\xFF\xFF\xFF\n");

    let mut offsets: Vec<usize> = vec![0];
    for obj in &objects {
        offsets.push(out.len());
        out.extend_from_slice(obj);
        out.extend_from_slice(b"\n");
    }

    let xref_start = out.len();
    out.extend_from_slice(format!("xref\n0 {}\n", offsets.len()).as_bytes());
    out.extend_from_slice(b"0000000000 65535 f \n");
    for off in offsets.iter().skip(1) {
        out.extend_from_slice(format!("{:010} 00000 n \n", off).as_bytes());
    }

    out.extend_from_slice(
        format!(
            "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF\n",
            offsets.len(),
            xref_start
        )
        .as_bytes(),
    );

    Ok(out)
}
