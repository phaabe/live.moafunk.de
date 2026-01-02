"""
Tests for SoundCloud tracks data generator
"""
import pytest
from scripts.generate_relisten import format_track_data


def test_format_track_data():
    """Test track data formatting"""
    mock_tracks = [
        {
            'id': 123,
            'title': 'Test Track',
            'artwork_url': 'https://example.com/artwork-large.jpg',
            'created_at': '2025-01-01T12:00:00Z',
            'duration': 180000,  # 3 minutes
            'permalink_url': 'https://soundcloud.com/test',
            'stream_url': 'https://soundcloud.com/stream',
            'description': 'A' * 250,  # Long description
        }
    ]
    
    formatted = format_track_data(mock_tracks)
    
    assert len(formatted) == 1
    assert formatted[0]['id'] == 123
    assert formatted[0]['title'] == 'Test Track'
    assert 't500x500' in formatted[0]['artwork_url']
    assert len(formatted[0]['description']) <= 203  # 200 + '...'


def test_format_track_data_with_missing_artwork():
    """Test handling of tracks without artwork"""
    mock_tracks = [
        {
            'id': 456,
            'title': 'No Artwork Track',
            'artwork_url': None,
            'created_at': '2025-01-01T12:00:00Z',
            'duration': 120000,
            'permalink_url': 'https://soundcloud.com/test2',
        }
    ]
    
    formatted = format_track_data(mock_tracks)
    
    assert formatted[0]['artwork_url'] == '/moafunk.png'


def test_format_track_data_with_no_description():
    """Test handling of tracks without description"""
    mock_tracks = [
        {
            'id': 789,
            'title': 'No Description Track',
            'artwork_url': 'https://example.com/art.jpg',
            'created_at': '2025-01-01T12:00:00Z',
            'duration': 60000,
            'permalink_url': 'https://soundcloud.com/test3',
            'description': '',
        }
    ]
    
    formatted = format_track_data(mock_tracks)
    
    assert formatted[0]['description'] == ''
