#!/usr/bin/env python3
"""
SoundCloud Re-Listen Page Generator

This script fetches tracks from the radio-moafunk SoundCloud profile
and generates a static HTML page with embedded track data.

Usage:
    python generate_relisten.py --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET
"""

import argparse
import requests
import json
import base64
from datetime import datetime
import html

def get_access_token(client_id, client_secret):
    """Get OAuth access token using client credentials flow"""
    url = "https://api.soundcloud.com/oauth2/token"
    
    # Encode credentials
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Authorization': f'Basic {credentials}'
    }
    
    data = {
        'grant_type': 'client_credentials'
    }
    
    response = requests.post(url, headers=headers, data=data)
    
    if response.status_code != 200:
        print(f"Failed to get access token: {response.status_code}")
        print(f"Response: {response.text}")
        raise Exception(f"Failed to get access token: {response.status_code}")
    
    return response.json().get('access_token')

def fetch_soundcloud_tracks(client_id, client_secret, username='radio-moafunk'):
    """Fetch tracks from SoundCloud user profile"""
    
    # First, let's try to resolve the user to get their ID
    print(f"Resolving user: {username}")
    
    # Try to get access token first
    access_token = get_access_token(client_id, client_secret)
    print(f"Access token: {access_token}")
    
    # Try to resolve the user first
    if access_token:
        resolve_headers = {'Authorization': f'OAuth {access_token}'}
        resolve_url = f"https://api.soundcloud.com/resolve?url=https://soundcloud.com/{username}"
    else:
        resolve_headers = {}
        resolve_url = f"https://api.soundcloud.com/resolve?url=https://soundcloud.com/{username}&client_id={client_id}"
    
    print(f"Resolving URL: {resolve_url}")
    resolve_response = requests.get(resolve_url, headers=resolve_headers)
    print(f"Resolve response status: {resolve_response.status_code}")
    print(f"Resolve response: {resolve_response.text[:500]}...")
    
    if resolve_response.status_code == 200:
        user_data = resolve_response.json()
        user_id = user_data.get('id')
        print(f"Found user ID: {user_id}")
        
        # Now fetch tracks using user ID
        if access_token:
            headers = {'Authorization': f'OAuth {access_token}'}
            url = f"https://api.soundcloud.com/users/{user_id}/tracks?limit=50"
        else:
            headers = {}
            url = f"https://api.soundcloud.com/users/{user_id}/tracks?client_id={client_id}&limit=50"
    else:
        print("User resolve failed, trying direct username approach")
        if access_token:
            headers = {'Authorization': f'OAuth {access_token}'}
            url = f"https://api.soundcloud.com/users/{username}/tracks?limit=50"
        else:
            headers = {}
            url = f"https://api.soundcloud.com/users/{username}/tracks?client_id={client_id}&limit=50"
    
    print(f"Fetching tracks from: {url}")
    response = requests.get(url, headers=headers)
    print(f"Tracks response status: {response.status_code}")
    print(f"Tracks response: {response.text[:500]}...")
    
    if response.status_code != 200:
        print(f"Failed to fetch tracks: {response.status_code}")
        print(f"Response: {response.text}")
        raise Exception(f"Failed to fetch tracks: {response.status_code}")
    
    tracks = response.json()
    
    print(f"Total tracks returned: {len(tracks)}")
    
    # Debug: Print first track details
    if tracks:
        first_track = tracks[0]
        print(f"First track: {first_track.get('title', 'No title')}")
        print(f"Streamable: {first_track.get('streamable', 'Not set')}")
        print(f"Policy: {first_track.get('policy', 'Not set')}")
        print(f"Sharing: {first_track.get('sharing', 'Not set')}")
        print(f"State: {first_track.get('state', 'Not set')}")
    
    # Try different filtering approaches
    # First, just streamable tracks
    streamable_tracks = [track for track in tracks if track.get('streamable', False)]
    print(f"Streamable tracks: {len(streamable_tracks)}")
    
    # Then, try without policy filter
    if not streamable_tracks:
        streamable_tracks = [track for track in tracks if track.get('sharing') == 'public']
        print(f"Public tracks: {len(streamable_tracks)}")
    
    # If still none, return all tracks for debugging
    if not streamable_tracks:
        print("No tracks passed filters, returning all for debugging")
        return tracks[:10]  # Return first 10 for debugging
    
    return streamable_tracks

def format_track_data(tracks):
    """Format track data for JavaScript embedding"""
    formatted_tracks = []
    
    for track in tracks:
        # Get highest quality artwork URL
        artwork_url = track.get('artwork_url', './moafunk.png')
        if artwork_url and artwork_url != './moafunk.png':
            # Replace with highest quality version
            # SoundCloud sizes: t300x300, large (100x100), t500x500, crop (400x400), t67x67, badge, small, tiny
            artwork_url = artwork_url.replace('-large.jpg', '-t500x500.jpg')
            artwork_url = artwork_url.replace('-crop.jpg', '-t500x500.jpg')
            artwork_url = artwork_url.replace('-t300x300.jpg', '-t500x500.jpg')
        
        formatted_track = {
            'id': track['id'],
            'title': html.escape(track['title']),
            'artwork_url': artwork_url or './moafunk.png',
            'created_at': track['created_at'],
            'duration': track['duration'],
            'permalink_url': track['permalink_url'],
            'stream_url': track.get('stream_url', '#'),
            'description': html.escape(track.get('description', '')[:200] + '...' if track.get('description', '') else '')
        }
        formatted_tracks.append(formatted_track)
    
    return formatted_tracks

def generate_html(tracks):
    """Generate the complete HTML page with embedded track data"""
    
    tracks_json = json.dumps(tracks, indent=2)
    
    html_template = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Re-Listen - Moafunk Radio</title>

    <script async defer data-domain="live.moafunk.de" src="https://plausible.moafunk.de/js/plausible.js"></script>
    <link rel="stylesheet" type="text/css" href="main.css" media="screen" />

    <link rel="apple-touch-icon" sizes="180x180" href="./icons/apple-touch-icon.png">
    <link rel="icon" type="image/png" sizes="32x32" href="./icons/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="./icons/favicon-16x16.png">
    <link rel="manifest" href="./icons/site.webmanifest">
    <link rel="mask-icon" href="./icons/safari-pinned-tab.svg" color="#333333">
    <link rel="shortcut icon" href="./icons/favicon.ico">
    <meta name="msapplication-TileColor" content="#2b5797">
    <meta name="msapplication-config" content="./icons/browserconfig.xml">
    <meta name="theme-color" content="#ffffff">
    
    <style>
        .tracks-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 20px;
            margin: 20px;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }}
        
        /* Responsive design for smaller screens */
        @media (max-width: 768px) {{
            .tracks-grid {{
                grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
                gap: 15px;
                margin: 15px;
                padding: 15px;
            }}
        }}
        
        @media (max-width: 480px) {{
            .tracks-grid {{
                grid-template-columns: 1fr;
                gap: 0;
                margin: 0;
                padding: 0;
            }}
            
            .track-tile {{
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                text-align: center;
                padding: 40px 20px;
                margin: 0;
                border-radius: 0;
            }}
            
            .track-artwork {{
                width: 80%;
                max-width: 280px;
                aspect-ratio: 1;
                margin-bottom: 30px;
            }}
            
            .track-title {{
                font-size: 24px !important;
                margin-bottom: 15px;
            }}
            
            .track-date {{
                font-size: 18px !important;
                margin-bottom: 10px;
            }}
            
            .track-duration {{
                font-size: 16px !important;
                margin-bottom: 15px;
            }}
            
            .track-description {{
                font-size: 14px !important;
                max-width: 300px;
            }}
        }}
        
        .track-tile {{
            cursor: pointer;
            border: 2px solid #000;
            transition: all 0.3s ease;
            background: white;
            padding: 15px;
            text-align: left;
        }}
        
        .track-tile:hover {{
            transform: scale(1.02);
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
        }}
        
        .track-tile.playing {{
            border-color: #ff6600;
            background: #fff3e0;
        }}
        
        .track-artwork {{
            width: 100%;
            aspect-ratio: 1;
            object-fit: cover;
            background: #f0f0f0;
            display: block;
            margin-bottom: 10px;
        }}
        
        .track-title {{
            margin: 10px 0 5px 0;
            font-size: 14pt;
            font-weight: bold;
            line-height: 1.2;
        }}
        
        .track-date {{
            font-size: 12pt;
            color: #666;
            margin-bottom: 5px;
        }}
        
        .track-duration {{
            font-size: 11pt;
            color: #999;
        }}
        
        .track-description {{
            font-size: 10pt;
            color: #777;
            margin-top: 5px;
            line-height: 1.3;
        }}
        
        .player-footer {{
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            background: white;
            border-top: 3px solid #000;
            padding: 15px;
            display: none;
            z-index: 1000;
        }}
        
        .player-footer.active {{
            display: block;
        }}
        
        .close-player {{
            position: absolute;
            top: 10px;
            right: 15px;
            background: none;
            border: none;
            font-size: 20px;
            cursor: pointer;
            color: #666;
        }}
        
        .close-player:hover {{
            color: #000;
        }}
        
        .soundcloud-player {{
            width: 100%;
            height: 20px;
            border: none;
        }}
        
        .loading {{
            text-align: center;
            padding: 40px;
            font-size: 16pt;
        }}
        
        .error {{
            text-align: center;
            padding: 40px;
            color: #ff0000;
            font-size: 16pt;
        }}
        
        .external-link {{
            display: inline-block;
            margin-top: 10px;
            padding: 5px 10px;
            background: #ff6600;
            color: white;
            text-decoration: none;
            border: 2px solid #000;
            font-size: 11pt;
        }}
        
        .external-link:hover {{
            background: #e55a00;
            color: white;
        }}
        
        .last-updated {{
            text-align: center;
            font-size: 10pt;
            color: #999;
            margin-top: 20px;
        }}
        
        /* Mobile optimizations */
        @media (max-width: 768px) {{
            .track-title {{
                font-size: 16pt;
            }}
            
            .track-date {{
                font-size: 14pt;
            }}
            
            .track-duration {{
                font-size: 13pt;
            }}
            
            .track-description {{
                font-size: 12pt;
            }}
            
            .player-footer {{
                display: none !important; /* Hide footer on mobile since we open SoundCloud directly */
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <br/><br/>
        <img class="logo" src="./moafunk.png" alt="Moafunk Logo"/>
        <h2>Re-Listen</h2>
        <p>Listen to past Moafunk Radio recordings</p>
        
        <div id="tracks-container" class="tracks-grid"></div>
        
        <div class="last-updated">
            Last updated: {datetime.now().strftime("%Y-%m-%d %H:%M")}
        </div>
        
        <br/><br/><br/><br/><br/><br/>
        <div class="links">
            <a href="./index.html">← Back to Live Stream</a>
        </div>
        <br/><br/><br/><br/><br/><br/>
    </div>
    
    <div id="player-footer" class="player-footer">
        <button id="close-player" class="close-player">&times;</button>
        <iframe id="soundcloud-player" class="soundcloud-player" 
                scrolling="no" frameborder="no" allow="autoplay"
                src="">
        </iframe>
    </div>
    
    <script>
        // Embedded track data (generated by Python script)
        const tracks = {tracks_json};
        
        // Global variables
        let currentTrackIndex = -1;
        
        // DOM elements
        const tracksContainer = document.getElementById('tracks-container');
        const playerFooter = document.getElementById('player-footer');
        const closePlayerBtn = document.getElementById('close-player');
        const soundcloudPlayer = document.getElementById('soundcloud-player');
        
        // Initialize the app
        init();
        
        function init() {{
            renderTracks();
            setupEventListeners();
        }}
        
        function renderTracks() {{
            if (tracks.length === 0) {{
                tracksContainer.innerHTML = '<div class="error">No tracks found.</div>';
                return;
            }}
            
            tracksContainer.innerHTML = tracks.map((track, index) => `
                <div class="track-tile" data-index="${{index}}">
                    <img src="${{track.artwork_url}}" alt="${{track.title}}" class="track-artwork" 
                         onerror="this.src='./moafunk.png'" />
                    <div class="track-title">${{track.title}}</div>
                    <div class="track-date">${{formatDate(track.created_at)}}</div>
                    <div class="track-duration">${{formatDuration(track.duration)}}</div>
                    ${{track.description ? `<div class="track-description">${{track.description}}</div>` : ''}}
                </div>
            `).join('');
        }}
        
        function setupEventListeners() {{
            // Track tile clicks
            tracksContainer.addEventListener('click', (e) => {{
                const tile = e.target.closest('.track-tile');
                if (tile) {{
                    const index = parseInt(tile.dataset.index);
                    playTrack(index);
                }}
            }});
            
            // Close player button
            closePlayerBtn.addEventListener('click', closePlayer);
        }}
        
        function playTrack(index) {{
            const track = tracks[index];
            currentTrackIndex = index;
            
            // Check if on mobile device
            const isMobile = window.innerWidth <= 768 || /Android|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
            
            if (isMobile) {{
                // On mobile, directly open SoundCloud link
                window.open(track.permalink_url, '_blank');
                return;
            }}
            
            // Update UI - remove playing state from all tiles
            document.querySelectorAll('.track-tile').forEach(tile => {{
                tile.classList.remove('playing');
            }});
            
            // Add playing state to current tile
            document.querySelector(`[data-index="${{index}}"]`).classList.add('playing');
            
            // Create SoundCloud embed URL using exact format from example
            const embedUrl = `https://w.soundcloud.com/player/?url=https%3A//api.soundcloud.com/tracks/${{track.id}}&color=%23c4bc64&inverse=false&auto_play=true&show_user=false`;
            
            // Update iframe src
            soundcloudPlayer.src = embedUrl;
            
            // Show player footer
            playerFooter.classList.add('active');
        }}
        
        function closePlayer() {{
            // Hide player footer
            playerFooter.classList.remove('active');
            
            // Clear iframe src to stop playback
            soundcloudPlayer.src = '';
            
            // Remove playing state from all tiles
            document.querySelectorAll('.track-tile').forEach(tile => {{
                tile.classList.remove('playing');
            }});
            
            currentTrackIndex = -1;
        }}
        
        function formatDate(dateString) {{
            const date = new Date(dateString);
            return date.toLocaleDateString('en-US', {{ 
                year: 'numeric', 
                month: 'short', 
                day: 'numeric' 
            }});
        }}
        
        function formatDuration(ms) {{
            const minutes = Math.floor(ms / 60000);
            const seconds = Math.floor((ms % 60000) / 1000);
            return `${{minutes}}:${{seconds.toString().padStart(2, '0')}}`;
        }}
    </script>
</body>
</html>'''
    
    return html_template

def main():
    parser = argparse.ArgumentParser(description='Generate SoundCloud Re-Listen page')
    parser.add_argument('--client-id', required=True, help='SoundCloud Client ID')
    parser.add_argument('--client-secret', required=True, help='SoundCloud Client Secret')
    parser.add_argument('--username', default='radio-moafunk', help='SoundCloud username')
    parser.add_argument('--output', default='re-listen.html', help='Output HTML file')
    
    args = parser.parse_args()
    
    print(f"Fetching tracks from SoundCloud user: {{args.username}}")
    
    # Fetch tracks from SoundCloud
    tracks = fetch_soundcloud_tracks(args.client_id, args.client_secret, args.username)
    
    if not tracks:
        print("No tracks found or API error occurred")
        raise Exception("No tracks found or API error occurred")
    
    # Format track data
    formatted_tracks = format_track_data(tracks)
    
    # Generate HTML
    html_content = generate_html(formatted_tracks)
    
    # Write to file
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"Generated {{args.output}} with {{len(formatted_tracks)}} tracks")
    print(f"Tracks: {{[track['title'] for track in formatted_tracks[:5]]}}{'...' if len(formatted_tracks) > 5 else ''}")

if __name__ == '__main__':
    main()
