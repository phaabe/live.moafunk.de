#!/bin/bash

# Test the submit endpoint with curl
curl -X POST http://localhost:8000/api/submit \
  -F "artist-name=Test Artist" \
  -F "pronouns=they/them" \
  -F "track1-name=Test Track 1" \
  -F "track2-name=Test Track 2" \
  -F "artist-pic=@/tmp/test.jpg" \
  -F "track1-file=@/tmp/test.mp3" \
  -F "track2-file=@/tmp/test.mp3" \
  -v
