#!/usr/bin/env python3
"""
SoundCloud Tracks Data Generator

This script fetches tracks from the radio-moafunk SoundCloud profile
and outputs them as JSON data for use in the re-listen page generation.

Usage:
    python generate_relisten.py --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET
"""

import argparse
import requests
import json
import base64
import os
from datetime import datetime
import html

def get_access_token(client_id, client_secret):
    """Get OAuth access token using client credentials flow"""
    # Updated endpoint as of Oct 2024 - old api.soundcloud.com endpoint is deprecated
    url = "https://secure.soundcloud.com/oauth/token"
    
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
    """Format track data for JSON output"""
    formatted_tracks = []
    
    for track in tracks:
        # Get highest quality artwork URL
        artwork_url = track.get('artwork_url', '/moafunk.png')
        if artwork_url and artwork_url != '/moafunk.png':
            # Replace with highest quality version
            # SoundCloud sizes: t300x300, large (100x100), t500x500, crop (400x400), t67x67, badge, small, tiny
            artwork_url = artwork_url.replace('-large.jpg', '-t500x500.jpg')
            artwork_url = artwork_url.replace('-crop.jpg', '-t500x500.jpg')
            artwork_url = artwork_url.replace('-t300x300.jpg', '-t500x500.jpg')
        
        formatted_track = {
            'id': track['id'],
            'title': track['title'],
            'artwork_url': artwork_url or '/moafunk.png',
            'created_at': track['created_at'],
            'duration': track['duration'],
            'permalink_url': track['permalink_url'],
            'stream_url': track.get('stream_url', '#'),
            'description': (track.get('description', '')[:200] + '...') if track.get('description', '') else ''
        }
        formatted_tracks.append(formatted_track)
    
    return formatted_tracks

def main():
    parser = argparse.ArgumentParser(description='Generate SoundCloud tracks JSON data')
    parser.add_argument('--client-id', required=True, help='SoundCloud Client ID')
    parser.add_argument('--client-secret', required=True, help='SoundCloud Client Secret')
    parser.add_argument('--username', default='radio-moafunk', help='SoundCloud username')
    parser.add_argument('--output', default='public/data/tracks.json', help='Output JSON file')
    
    args = parser.parse_args()
    
    print(f"Fetching tracks from SoundCloud user: {args.username}")
    
    # Fetch tracks from SoundCloud
    tracks = fetch_soundcloud_tracks(args.client_id, args.client_secret, args.username)
    
    if not tracks:
        print("No tracks found or API error occurred")
        raise Exception("No tracks found or API error occurred")
    
    # Format track data
    formatted_tracks = format_track_data(tracks)
    
    # Ensure output directory exists
    output_dir = os.path.dirname(args.output)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    # Write JSON to file
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(formatted_tracks, f, indent=2, ensure_ascii=False)
    
    print(f"Generated {args.output} with {len(formatted_tracks)} tracks")
    print(f"Tracks: {[track['title'] for track in formatted_tracks[:5]]}{'...' if len(formatted_tracks) > 5 else ''}")

if __name__ == '__main__':
    main()
