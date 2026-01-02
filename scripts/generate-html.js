#!/usr/bin/env node

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const TRACKS_JSON_PATH = path.join(__dirname, '../public/data/tracks.json');
const OUTPUT_HTML_PATH = path.join(__dirname, '../src/pages/re-listen.html');

function formatDuration(ms) {
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, '0')}`;
}

function formatDate(dateString) {
  const date = new Date(dateString);
  return date.toLocaleDateString('en-US', { 
    year: 'numeric', 
    month: 'long', 
    day: 'numeric' 
  });
}

function escapeHtml(text) {
  const map = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;'
  };
  return text.replace(/[&<>"']/g, m => map[m]);
}

function generateHTML(tracks) {
  const tracksHtml = tracks.map(track => `
    <div class="track-tile" onclick="window.open('${escapeHtml(track.permalink_url)}', '_blank')">
      <img 
        src="${escapeHtml(track.artwork_url)}" 
        alt="${escapeHtml(track.title)}"
        class="track-artwork"
        loading="lazy"
      />
      <div class="track-title">${escapeHtml(track.title)}</div>
      <div class="track-date">${formatDate(track.created_at)}</div>
      <div class="track-duration">${formatDuration(track.duration)}</div>
      ${track.description ? `<div class="track-description">${escapeHtml(track.description)}</div>` : ''}
    </div>
  `).join('\n');

  return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Re-Listen - Moafunk Radio</title>

    <script async defer data-domain="live.moafunk.de" src="https://plausible.moafunk.de/js/plausible.js"></script>
    <link rel="stylesheet" type="text/css" href="/main.css" media="screen" />

    <link rel="apple-touch-icon" sizes="180x180" href="/icons/apple-touch-icon.png">
    <link rel="icon" type="image/png" sizes="32x32" href="/icons/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/icons/favicon-16x16.png">
    <link rel="manifest" href="/icons/site.webmanifest">
    <link rel="mask-icon" href="/icons/safari-pinned-tab.svg" color="#333333">
    <link rel="shortcut icon" href="/icons/favicon.ico">
    <meta name="msapplication-TileColor" content="#2b5797">
    <meta name="msapplication-config" href="/icons/browserconfig.xml">
    <meta name="theme-color" content="#ffffff">
    
    <style>
        .tracks-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 20px;
            margin: 20px;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        
        @media (max-width: 768px) {
            .tracks-grid {
                grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
                gap: 15px;
                margin: 15px;
                padding: 15px;
            }
        }
        
        @media (max-width: 480px) {
            .tracks-grid {
                grid-template-columns: 1fr;
                gap: 0;
                margin: 0;
                padding: 0;
            }
            
            .track-tile {
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                text-align: center;
                padding: 40px 20px;
                margin: 0;
                border-radius: 0;
            }
            
            .track-artwork {
                width: 80%;
                max-width: 280px;
                aspect-ratio: 1;
                margin-bottom: 30px;
            }
            
            .track-title {
                font-size: 24px !important;
                margin-bottom: 15px;
            }
            
            .track-date {
                font-size: 18px !important;
                margin-bottom: 10px;
            }
            
            .track-duration {
                font-size: 16px !important;
                margin-bottom: 15px;
            }
            
            .track-description {
                font-size: 14px !important;
                max-width: 300px;
            }
        }
        
        .track-tile {
            cursor: pointer;
            border: 2px solid #000;
            transition: all 0.3s ease;
            background: white;
            padding: 15px;
            text-align: left;
        }
        
        .track-tile:hover {
            transform: scale(1.02);
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
        }
        
        .track-artwork {
            width: 100%;
            aspect-ratio: 1;
            object-fit: cover;
            background: #f0f0f0;
            display: block;
            margin-bottom: 10px;
        }
        
        .track-title {
            margin: 10px 0 5px 0;
            font-size: 14pt;
            font-weight: bold;
            line-height: 1.2;
        }
        
        .track-date {
            font-size: 12pt;
            color: #666;
            margin-bottom: 5px;
        }
        
        .track-duration {
            font-size: 11pt;
            color: #999;
        }
        
        .track-description {
            font-size: 10pt;
            color: #777;
            margin-top: 5px;
            line-height: 1.3;
        }
        
        .container {
            text-align: center;
            padding: 20px;
        }
        
        .logo {
            width: 150px;
            margin-bottom: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <br/><br/>
        <img class="logo" src="/moafunk.png" alt="Moafunk Logo"/>
        <h2>Re-Listen</h2>
        <p>Past recordings from Moafunk Radio</p>
        <br/>
        <div><a href="/">← Back to Live Stream</a></div>
        <br/><br/>
    </div>
    
    <div class="tracks-grid">
${tracksHtml}
    </div>
    
    <div class="container">
        <br/><br/>
        <p><a href="/">Back to Live Stream</a></p>
        <br/><br/>
    </div>
</body>
</html>`;
}

function main() {
  try {
    console.log(`Reading tracks from: ${TRACKS_JSON_PATH}`);
    
    if (!fs.existsSync(TRACKS_JSON_PATH)) {
      console.error(`Error: Tracks JSON file not found at ${TRACKS_JSON_PATH}`);
      console.error('');
      console.error('Please run the Python script first to generate the tracks data:');
      console.error('');
      console.error('  uv run scripts/generate_relisten.py \\');
      console.error('    --client-id "$SOUNDCLOUD_CLIENT_ID" \\');
      console.error('    --client-secret "$SOUNDCLOUD_CLIENT_SECRET"');
      console.error('');
      console.error('Or set environment variables in .env and run:');
      console.error('  uv run scripts/generate_relisten.py \\');
      console.error('    --client-id "$SOUNDCLOUD_CLIENT_ID" \\');
      console.error('    --client-secret "$SOUNDCLOUD_CLIENT_SECRET"');
      console.error('');
      process.exit(1);
    }
    
    const tracksData = fs.readFileSync(TRACKS_JSON_PATH, 'utf-8');
    const tracks = JSON.parse(tracksData);
    
    console.log(`Loaded ${tracks.length} tracks`);
    
    const html = generateHTML(tracks);
    
    // Ensure output directory exists
    const outputDir = path.dirname(OUTPUT_HTML_PATH);
    if (!fs.existsSync(outputDir)) {
      fs.mkdirSync(outputDir, { recursive: true });
    }
    
    fs.writeFileSync(OUTPUT_HTML_PATH, html, 'utf-8');
    
    console.log(`Generated ${OUTPUT_HTML_PATH} successfully`);
    console.log(`First 3 tracks: ${tracks.slice(0, 3).map(t => t.title).join(', ')}`);
  } catch (error) {
    console.error('Error generating HTML:', error.message);
    process.exit(1);
  }
}

main();
